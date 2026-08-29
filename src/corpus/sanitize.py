"""Project the sensitive corpus (documents ⨝ enrichments) into the sanitized DB.

Applies the per-field exposure policy on the way down.

**Included** (cloud-safe): opaque ids, timestamps, the enrichment classification
signal, coarse sender-domain (org, never the person), LLM `one_line`,
`action_summary`, `organizations`. **Excluded** (never leaves local): raw
`content`, raw `subject`, full sender/recipient addresses, secret material,
raw-body embeddings. `one_line`/`action_summary`/`organizations` are additionally
withheld at/above the sensitivity gate — a high-sensitivity item degrades to a
content-free stub (domain + action only).

One-directional (sensitive → sanitized), incremental (re-projects a document only
after it is re-enriched), resumable.
"""

from __future__ import annotations

import logging

import psycopg

from .config import settings
from .embeddings import Embedder

log = logging.getLogger("corpus.sanitize")

_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _sender_domain(from_addr: str | None) -> str | None:
    """Return the sending organization's domain (e.g. 'chase.com'), never the person.

    Personal-provider senders (gmail.com) yield little; the descriptive signal for
    those lives in one_line / organizations instead.
    """
    if not from_addr or "@" not in from_addr:
        return None
    dom = from_addr.rsplit("@", 1)[-1].strip().strip(">").lower()
    return dom or None


def project(doc_id: str, meta: dict | None, enr: dict | None, gate: str = "high") -> dict:
    """Build the sanitized row for one enriched document.

    Free-text (one_line, action_summary, organizations) is withheld at/above
    ``gate``; a stub keeps the item plannable.
    """
    meta = meta or {}
    enr = enr or {}
    sens = enr.get("sensitivity_level") or "none"
    domain = enr.get("domain") or "other"
    action_type = enr.get("action_type") or "none"
    gated = _RANK.get(sens, 0) >= _RANK.get(gate, 3)
    if gated:
        stub = f"[sensitive] {domain} item"
        if enr.get("requires_action"):
            stub += f" — action: {action_type}"
        one_line, action_summary, organizations = stub, None, None
    else:
        one_line = enr.get("one_line")
        action_summary = enr.get("action_summary")
        organizations = enr.get("organizations") or None
    return {
        "id": doc_id,
        "source": meta.get("source"),
        "account": meta.get("account"),
        "thread_id": meta.get("thread_id"),
        "sent_at": meta.get("sent_at") or None,
        "sender_domain": _sender_domain(meta.get("from_addr")),
        "domain": domain,
        "category": enr.get("category"),
        "transactional_type": enr.get("transactional_type"),
        "importance": enr.get("importance") or "low",
        "requires_action": bool(enr.get("requires_action")),
        "action_type": action_type,
        "action_summary": action_summary,
        "deadline": enr.get("deadline") or None,
        "waiting_on": enr.get("waiting_on") or "none",
        "time_sensitive": bool(enr.get("time_sensitive")),
        "suggested_disposition": enr.get("suggested_disposition") or "keep",
        "sensitivity_level": sens,
        "one_line": one_line,
        "organizations": organizations,
        "enriched_at": None,  # set by run_sync from the source enriched_at
    }


def iter_enriched(read_dsn: str, source: str | None = None):
    """Stream (id, meta, enrichment, enriched_at) for enriched documents, newest first.

    Reads from the sensitive DB via a server-side cursor.
    """
    schema = settings.db_schema
    docs = f"{schema}.{settings.documents_table}"
    enr = f"{schema}.enrichments"
    where = "WHERE e.enrichment IS NOT NULL"
    params: list = []
    if source:
        where += " AND d.meta->>'source' = %s"
        params.append(source)
    sql = (
        f"SELECT d.id, d.meta, e.enrichment, e.enriched_at FROM {docs} d "
        f"JOIN {enr} e ON e.doc_id = d.id {where} "
        "ORDER BY d.meta->>'sent_at' DESC NULLS LAST, d.id"
    )
    with psycopg.connect(read_dsn) as conn, conn.cursor(name="corpus_sync_scan") as cur:
        cur.itersize = 500
        cur.execute(sql, params)
        yield from cur


def run_sync(
    store,
    *,
    source: str | None = None,
    limit: int = 0,
    force: bool = False,
    documents=iter_enriched,
    embedder: Embedder | None = None,
    read_dsn: str | None = None,
    batch_size: int = 64,
) -> dict[str, int]:
    """Project enriched documents into the sanitized store.

    Skips rows whose source enrichment is unchanged since the last sync unless
    ``force``. ``store`` is an open SanitizedStore whose lifecycle the caller owns.
    """
    read_dsn = read_dsn or settings.database_url
    embedder = embedder or Embedder()
    seen = {} if force else store.synced_versions()
    counts = {"scanned": 0, "synced": 0, "skipped": 0}
    batch: list[dict] = []

    def flush() -> None:
        if not batch:
            return
        vectors = embedder.embed([r["one_line"] or "" for r in batch])
        for row, vec in zip(batch, vectors):
            store.save_message(row, vec)
            counts["synced"] += 1
        batch.clear()

    for doc_id, meta, enr, enriched_at in documents(read_dsn, source=source):
        if limit and counts["scanned"] >= limit:
            break
        counts["scanned"] += 1
        if not force and seen.get(doc_id) == enriched_at:
            counts["skipped"] += 1
            continue
        row = project(doc_id, meta, enr, settings.index_sensitivity_gate)
        row["enriched_at"] = enriched_at
        batch.append(row)
        if len(batch) >= batch_size:
            flush()
    flush()
    log.info(
        "synced %d, skipped %d of %d scanned",
        counts["synced"],
        counts["skipped"],
        counts["scanned"],
    )
    return counts
