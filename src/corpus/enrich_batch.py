"""Batch enrichment over the stored archive.

One pass per document: structured enrichment (LLM) always, plus an LLM secret
audit only where the deterministic detectors (or recovery wording) flagged
candidates -- so the local model does a single full pass, with the extra audit
falling on the small flagged subset rather than a second run over everything.

run_audit re-runs only the secret confirmation over the flagged documents,
without re-enriching -- for when the detectors or the model improve.

Enrichment is gated on per-source policy (``fetchers.policy``), which is
default-deny: a document whose source is not declared enrichable is passed over
and counted, never sent to the model. The gate is applied here rather than in
``iter_documents`` because that reader is shared with export and audit, and
enrichment policy has no business narrowing what they see.

The caller owns the store (opens and closes it); the LLM, the document source, and
the audit call are injectable, so the orchestration can be exercised without I/O.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from itertools import islice

import msgspec

from . import scan
from .config import settings
from .enricher import Enricher, EnrichError
from .enrichment import RECOVERED_SCHEMA_VERSION, SCHEMA_VERSION
from .fetchers.policy import enrichable_kinds, may_enrich
from .secret_audit import audit_secrets, audit_texts, merge_audits
from .store import iter_documents

log = logging.getLogger("corpus.enrich")


def _model_text(meta, content) -> str:
    """Return the model's input: the subject prepended to the body.

    The subject is highly informative for classification (domain/category), and
    the deterministic detectors already run on the body separately.
    """
    subject = (meta or {}).get("subject") or ""
    return f"Subject: {subject}\n\n{content or ''}"


def _audit_windowed(audit, text: str, content: str, candidates, model: str):
    """Audit ``text``, split into candidate-centred chunks if it overflows ``model``.

    A document that fits is audited whole, exactly as before; a longer one is
    audited on the windows around its candidates, and the chunk verdicts merged.
    """
    texts = audit_texts(text, content, scan.candidate_spans(content), model)
    results = [audit(t, candidates, model=model) for t in texts]
    return results[0] if len(results) == 1 else merge_audits(results)


def run_enrich(
    store,
    *,
    source: str | None = None,
    account: str | None = None,
    limit: int = 0,
    force: bool = False,
    upgrade_stale: bool = False,
    retry_rejected: bool = False,
    enricher: Enricher | None = None,
    documents=iter_documents,
    audit=audit_secrets,
    concurrency: int | None = None,
) -> dict[str, int]:
    """Enrich stored documents; audit only those with secret candidates.

    Resumable: already-enriched docs are skipped unless ``force``. With
    ``upgrade_stale``, docs enriched under an older ``SCHEMA_VERSION`` are treated
    as not yet done and re-enriched; it is opt-in so that a scheduled run on a
    remote model does not re-send the whole archive after a schema change.

    A document the model rejects (4xx, or unparseable output) is recorded as
    rejected by that model and passed over by later runs on it; ``retry_rejected``
    sends those again. A different model is always given a try. ``limit`` caps
    the documents sent to the model (0 does all), so a capped scheduled run keeps
    making progress past the already-enriched ones. ``store`` is an open EnrichStore whose lifecycle the caller owns.

    The audit uses ``CORPUS_AUDIT_MODEL`` when set, else the enrichment model.

    Enrichment/audit LLM calls run ``concurrency`` at a time (the local server
    batches them); the store writes stay single-threaded on the caller's one
    connection. A per-record ``EnrichError`` (a bad message) is skipped so it can't
    abort a long backfill; an ``EnrichUnavailableError`` still propagates.
    """
    if source is not None and not may_enrich(source):
        raise ValueError(
            f"source {source!r} is not declared enrichable; "
            f"enrichable kinds are {', '.join(enrichable_kinds()) or '(none)'}. "
            "Declare it in corpus.fetchers.policy.POLICIES to enable enrichment."
        )

    concurrency = concurrency or settings.enrich_concurrency
    own = enricher is None
    enricher = enricher or Enricher()
    audit_model = settings.audit_model or enricher.model
    counts = {
        "scanned": 0, "enriched": 0, "recovered": 0, "audited": 0, "audit_failed": 0,
        "skipped": 0, "ineligible": 0,
    }
    excluded: set[str] = set()

    def selected() -> Iterator[tuple]:
        if force:
            seen: set[str] = set()
        else:
            seen = store.enriched_ids(SCHEMA_VERSION if upgrade_stale else None)
            if not retry_rejected:
                seen |= store.rejected_ids(enricher.model)
        queued = 0
        for doc_id, content, meta in documents(source=source, account=account):
            # The limit caps documents sent to the model, not documents scanned:
            # documents arrive in a stable order, so a scan cap would re-scan the
            # same already-enriched prefix on every run and never reach new mail.
            counts["scanned"] += 1
            doc_source = (meta or {}).get("source")
            if not may_enrich(doc_source):
                # Counted apart from ``skipped``, which means a record that
                # failed: a backfill that enriches nothing must say why.
                counts["ineligible"] += 1
                excluded.add(doc_source or "<unset>")
                continue
            if doc_id not in seen:
                queued += 1
                yield doc_id, content, meta
                if limit and queued >= limit:
                    return

    def work(item: tuple) -> tuple:
        doc_id, content, meta = item
        text = _model_text(meta, content)
        try:
            enrichment, recovered = enricher.enrich_reporting(text)
        except EnrichError as exc:
            log.warning("skipping %s: %s", doc_id, exc)
            return doc_id, None, False, None, str(exc)
        candidates = scan.audit_candidates(content)
        # The audit gets the full text even when the enricher caps its own input: a
        # secret can sit past the cap, and the candidates came from a full-body scan.
        result = None
        if candidates:
            # The audit may run on a different model than the enrichment (see
            # CORPUS_AUDIT_MODEL), e.g. one with a smaller context window. Its
            # per-record failure must not discard the enrichment or abort the run:
            # the document would then never be marked done and every later run
            # would fail on it again.
            try:
                result = _audit_windowed(audit, text, content, candidates, audit_model)
            except EnrichError as exc:
                log.warning("audit skipped for %s: %s", doc_id, exc)
        return doc_id, enrichment, recovered, candidates, result

    def persist(res: tuple) -> None:
        doc_id, enrichment, recovered, candidates, result = res
        if enrichment is None:  # a per-record EnrichError; ``result`` holds the reason
            counts["skipped"] += 1
            store.save_rejection(doc_id, result, enricher.model)
            return
        # A recovered record is kept but stored under a marked version, so it
        # counts as stale: --upgrade-stale enriches it again in full.
        version = RECOVERED_SCHEMA_VERSION if recovered else SCHEMA_VERSION
        store.save_enrichment(doc_id, msgspec.to_builtins(enrichment), enricher.model, version)
        counts["enriched"] += 1
        if recovered:
            counts["recovered"] += 1
            log.warning("recovered %s from a reply stalled in whitespace padding", doc_id)
        if candidates and result is None:
            counts["audit_failed"] += 1
        elif candidates:
            store.save_audit(
                doc_id, candidates, msgspec.to_builtins(result), audit_model, scan.SCAN_VERSION
            )
            counts["audited"] += 1

    items = selected()
    try:
        # Keep ``concurrency`` LLM calls in flight at all times: refill a slot the
        # moment one finishes and persist its result on this thread (the store stays
        # single-connection). Continuous streaming keeps the GPU saturated, unlike a
        # per-chunk barrier that stalls on the slowest record and the serial writes
        # between chunks.
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            inflight = {pool.submit(work, item) for item in islice(items, concurrency)}
            while inflight:
                done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in done:
                    persist(fut.result())
                inflight.update(pool.submit(work, item) for item in islice(items, len(done)))
    finally:
        if own:
            enricher.close()
    log.info(
        "enriched %d (%d recovered), audited %d (%d failed), skipped %d, ineligible %d of %d "
        "scanned",
        counts["enriched"], counts["recovered"], counts["audited"], counts["audit_failed"], counts["skipped"],
        counts["ineligible"], counts["scanned"],
    )
    if excluded:
        log.info(
            "not enrichable (no policy declaring enrich=True): %s",
            ", ".join(sorted(excluded)),
        )
    return counts


def run_audit(
    store,
    *,
    source: str | None = None,
    account: str | None = None,
    limit: int = 0,
    documents=iter_documents,
    audit=audit_secrets,
    model: str | None = None,
) -> dict[str, int]:
    """Re-run only the LLM secret confirmation over documents with candidates.

    Does not enrich; upserts the audit idempotently.

    Deliberately *not* gated on enrichment policy. This is a credential scan, and
    a secret pasted into a document is worth finding whatever the document is;
    narrowing it to enrichable sources would create blind spots exactly where
    nobody is looking. It already runs the model only on documents the
    deterministic detectors flagged, so the cost of the wider net is small.
    """
    model = model or settings.audit_model or settings.enrich_model
    if not model:
        raise ValueError("no model configured (set CORPUS_AUDIT_MODEL or CORPUS_ENRICH_MODEL)")
    scanned = audited = 0
    for doc_id, content, meta in documents(source=source, account=account):
        if limit and scanned >= limit:
            break
        scanned += 1
        candidates = scan.audit_candidates(content)
        if not candidates:
            continue
        result = _audit_windowed(audit, _model_text(meta, content), content, candidates, model)
        store.save_audit(doc_id, candidates, msgspec.to_builtins(result), model, scan.SCAN_VERSION)
        audited += 1
    log.info("audited %d of %d scanned", audited, scanned)
    return {"scanned": scanned, "audited": audited}
