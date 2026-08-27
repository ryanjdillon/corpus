"""Batch enrichment over the stored archive.

One pass per document: structured enrichment (LLM) always, plus an LLM secret
audit only where the deterministic detectors (or recovery wording) flagged
candidates -- so the local model does a single full pass, with the extra audit
falling on the small flagged subset rather than a second run over everything.

run_audit re-runs only the secret confirmation over the flagged documents,
without re-enriching -- for when the detectors or the model improve.
"""

from __future__ import annotations

import logging

import msgspec

from . import scan, store
from .config import settings
from .enrich_store import EnrichStore
from .enricher import Enricher
from .enrichment import SCHEMA_VERSION
from .secret_audit import audit_secrets

log = logging.getLogger("corpus.enrich")


def run_enrich(
    source: str | None = None,
    account: str | None = None,
    limit: int = 0,
    force: bool = False,
) -> dict[str, int]:
    """Enrich stored documents; audit only those with secret candidates. Resumable:
    already-enriched docs are skipped unless ``force``. ``limit`` of 0 does all."""
    enricher = Enricher()
    scanned = enriched = audited = 0
    with EnrichStore() as est:
        seen = set() if force else est.enriched_ids()
        for doc_id, content, _meta in store.iter_documents(source=source, account=account):
            if limit and scanned >= limit:
                break
            scanned += 1
            if doc_id in seen:
                continue
            enrichment = enricher.enrich(content or "")
            est.save_enrichment(
                doc_id, msgspec.to_builtins(enrichment), enricher.model, SCHEMA_VERSION
            )
            enriched += 1
            candidates = scan.audit_candidates(content)
            if candidates:
                audit = audit_secrets(content or "", candidates, model=enricher.model)
                est.save_audit(
                    doc_id, candidates, msgspec.to_builtins(audit), enricher.model, scan.SCAN_VERSION
                )
                audited += 1
    enricher.close()
    log.info("enriched %d, audited %d of %d scanned", enriched, audited, scanned)
    return {"scanned": scanned, "enriched": enriched, "audited": audited}


def run_audit(
    source: str | None = None, account: str | None = None, limit: int = 0
) -> dict[str, int]:
    """Re-run only the LLM secret confirmation over documents with candidates. Does
    not enrich; upserts the audit idempotently."""
    model = settings.enrich_model
    if not model:
        raise ValueError("no model configured (set CORPUS_ENRICH_MODEL)")
    scanned = audited = 0
    with EnrichStore() as est:
        for doc_id, content, _meta in store.iter_documents(source=source, account=account):
            if limit and scanned >= limit:
                break
            scanned += 1
            candidates = scan.audit_candidates(content)
            if not candidates:
                continue
            audit = audit_secrets(content or "", candidates, model=model)
            est.save_audit(
                doc_id, candidates, msgspec.to_builtins(audit), model, scan.SCAN_VERSION
            )
            audited += 1
    log.info("audited %d of %d scanned", audited, scanned)
    return {"scanned": scanned, "audited": audited}
