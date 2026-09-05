"""FastAPI REST surface."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from . import __version__, search
from .enrich_batch import audit_one
from .enrich_store import EnrichStore

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


@app.post("/audit")
def audit_endpoint(req: AuditRequest) -> dict[str, Any] | None:
    """Re-run the LLM secret audit on one document, confirming its candidates.

    Re-scans with the current detectors and model and upserts the verdict, so a
    single message can be re-confirmed once either has improved without
    replaying the whole ``corpus audit-secrets`` job.

    Returns ``None`` if no document has that id, and an ``audit`` of ``null``
    when the detectors flag nothing (the model is not consulted, and no verdict
    is stored).
    """
    doc = search.get_document(req.id)
    if doc is None:
        return None
    with EnrichStore() as store:
        return audit_one(store, req.id, doc.get("content"), doc.get("meta"))
