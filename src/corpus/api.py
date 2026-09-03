"""FastAPI REST surface."""

from __future__ import annotations

from typing import Any

import msgspec
from fastapi import FastAPI
from pydantic import BaseModel

from . import __version__, scan, search
from .config import settings
from .enrich_store import EnrichStore
from .secret_audit import audit_secrets

app = FastAPI(title="corpus", version=__version__)


class SearchRequest(BaseModel):
    """Request body for the semantic ``/search`` endpoint."""

    query: str
    top_k: int = 10
    filters: dict[str, Any] | None = None


class QueryRequest(BaseModel):
    """Request body for the structured ``/query`` endpoint."""

    label: str | None = None
    account: str | None = None
    before: str | None = None
    after: str | None = None
    limit: int = 500


class AuditRequest(BaseModel):
    """Request body for the secret audit ``/audit`` endpoint."""

    id: str


@app.get("/health")
def health() -> dict[str, str]:
    """Report liveness of the service."""
    return {"status": "ok"}


@app.post("/search")
def search_endpoint(req: SearchRequest) -> dict[str, Any]:
    """Return semantic matches for the request query."""
    return {"results": search.semantic_search(req.query, req.top_k, req.filters)}


@app.post("/query")
def query_endpoint(req: QueryRequest) -> dict[str, Any]:
    """Return structured metadata matches for the request filters."""
    results = search.structured_query(
        label=req.label,
        account=req.account,
        before=req.before,
        after=req.after,
        limit=req.limit,
    )
    return {"count": len(results), "results": results}


@app.get("/stats")
def stats_endpoint() -> dict[str, Any]:
    """Return index totals and per-label document counts."""
    return search.stats()


def _model_text(meta: dict | None, content: str | None) -> str:
    """Build model input: subject prepended to body."""
    subject = (meta or {}).get("subject") or ""
    return f"Subject: {subject}\n\n{content or ''}"


@app.post("/audit")
def audit_endpoint(req: AuditRequest) -> dict[str, Any] | None:
    """Run LLM secret audit on one document, confirming deterministic candidates.

    Re-scans with the current detectors and model, saves the result to the
    enrichments table, and returns the verdict. Use when the detectors or
    model have improved and you want to re-confirm a specific document.
    """
    doc = search.get_document(req.id)
    if doc is None:
        return None

    content = doc.get("content")
    meta = doc.get("meta")
    candidates = scan.audit_candidates(content)

    model = settings.enrich_model
    if not model:
        raise ValueError("no model configured (set CORPUS_ENRICH_MODEL)")

    audit_result = None
    if candidates:
        text = _model_text(meta, content)
        audit_result = audit_secrets(text, candidates, model=model)

    with EnrichStore() as store:
        store.save_audit(
            req.id,
            candidates,
            msgspec.to_builtins(audit_result) if audit_result else {"contains_secret": False, "findings": []},
            model,
            scan.SCAN_VERSION,
        )

    return {
        "id": req.id,
        "candidates": candidates,
        "audit": msgspec.to_builtins(audit_result) if audit_result else {"contains_secret": False, "findings": []},
        "model": model,
        "scan_version": scan.SCAN_VERSION,
    }
