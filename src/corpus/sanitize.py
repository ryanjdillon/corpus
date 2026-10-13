"""Project the sensitive corpus (documents ⨝ enrichments) into the sanitized DB.

Applies the per-field exposure policy on the way down.

**Included** (cloud-safe): opaque ids, timestamps, the enrichment classification
signal, coarse sender-domain (org, never the person), LLM `one_line`,
`action_summary`, `organizations`, and the provider's web link (`uri`) when it is
an https URL. **Excluded** (never leaves local): raw
`content`, raw `subject`, full sender/recipient addresses, secret material,
raw-body embeddings, any other `uri` (an IMAP URI names the mail host and the
private folder). `one_line`/`action_summary`/`organizations` are additionally
withheld at/above the sensitivity gate — a high-sensitivity item degrades to a
content-free stub (domain + action only).

One-directional (sensitive → sanitized), incremental (re-projects a document only
after it is re-enriched, or every document on request), resumable, and re-embeds
a row only when its embedded text changed. The gate and
the projected columns are the sanitized tier's ``projection`` (see ``tiers``).
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from contextlib import closing
from urllib.parse import urlsplit

from .embeddings import Embedder
from .sanitized_store import COLS
from .store import DocumentStore

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


#: Columns every projected row keeps, whatever the tier's ``fields``: the key,
#: and the version the incremental sync compares.
BOOKKEEPING = ("id", "enriched_at")


#: The longest uri projected. A provider web link is far shorter; a longer one is
#: not a link a consumer should be handed.
MAX_URI = 2048


def web_link(uri: object) -> str | None:
    """Return *uri* if it is an https URL without credentials, else ``None``.

    A Gmail message's uri is a web link a consumer can hand the owner. An IMAP
    uri (``imap://host/<folder>;UIDVALIDITY=…``) would expose the mail host and
    private folder names to the cloud-facing tier, so only https passes.

    The column is trusted, so the check is on the whole string, not on what
    ``urlsplit`` parses: it silently drops tab, CR and LF, which would let text
    after a line break ride into the column. Any whitespace or unprintable
    character, or more than :data:`MAX_URI` characters, is refused.
    """
    if not isinstance(uri, str) or len(uri) > MAX_URI:
        return None
    if any(c.isspace() or not c.isprintable() for c in uri):
        return None
    try:
        parts = urlsplit(uri)
        if parts.scheme != "https" or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
    except ValueError:  # a malformed netloc (e.g. a bad port or IPv6 literal)
        return None
    return uri


def project(
    doc_id: str,
    meta: dict | None,
    enr: dict | None,
    gate: str = "high",
    fields: Collection[str] | None = None,
) -> dict:
    """Build the sanitized row for one enriched document.

    Free-text (one_line, action_summary, organizations) is withheld at/above
    ``gate``; a stub keeps the item plannable. A column outside *fields*
    (default: every column) is stored as NULL; the bookkeeping columns are kept.
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
    row = {
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
        "uri": web_link(meta.get("uri")),
    }
    if fields is None:
        return row
    keep = set(fields) | set(BOOKKEEPING)
    return {col: (value if col in keep else None) for col, value in row.items()}


def run_sync(
    store,
    *,
    documents: DocumentStore,
    source: str | None = None,
    limit: int = 0,
    force: bool = False,
    gate: str = "high",
    fields: Collection[str] = COLS,
    embedder: Embedder | None = None,
    batch_size: int = 64,
) -> dict[str, int]:
    """Project enriched documents into the sanitized store.

    Skips rows whose source enrichment is unchanged since the last sync unless
    ``force``, which re-projects every row (after the projection itself
    changed). ``limit`` caps the rows projected (0 does all). *gate* and
    *fields* are the tier's projection (see :func:`project`). ``store`` is an
    open SanitizedStore and ``documents`` an open DocumentStore on the source
    tier, whose lifecycles the caller owns.

    A row is embedded only when its ``one_line`` (the embedded text) differs
    from the stored one; otherwise only its projected columns are rewritten, and
    only if they differ. A re-projection is therefore resumable: run again after
    an interruption, it embeds and writes nothing for the rows already done, and
    counts them as skipped.
    """
    embedder = embedder or Embedder()
    state = store.synced_state()
    counts = {"scanned": 0, "synced": 0, "skipped": 0}
    to_embed: list[dict] = []
    to_update: list[dict] = []
    queued = 0

    def flush() -> None:
        if to_update:
            changed = store.update_projections(to_update)
            counts["synced"] += changed
            counts["skipped"] += len(to_update) - changed
            to_update.clear()
        if to_embed:
            vectors = embedder.embed([r["one_line"] or "" for r in to_embed])
            for row, vec in zip(to_embed, vectors):
                store.save_message(row, vec)
                counts["synced"] += 1
            to_embed.clear()

    with closing(documents.iter_enriched(source=source)) as enriched:
        for doc_id, meta, enr, enriched_at in enriched:
            counts["scanned"] += 1
            stored = state.get(doc_id)
            if not force and stored is not None and stored[0] == enriched_at:
                counts["skipped"] += 1
                continue
            row = project(doc_id, meta, enr, gate, fields)
            row["enriched_at"] = enriched_at
            pending = to_update if stored is not None and stored[1] == row["one_line"] else to_embed
            pending.append(row)
            queued += 1
            if len(pending) >= batch_size:
                flush()
            # The limit caps rows projected, not rows scanned: the reader's order
            # is stable, so a scan cap would re-scan the same already-synced
            # prefix on every run and never reach older unsynced rows.
            if limit and queued >= limit:
                break
    flush()
    log.info(
        "synced %d, skipped %d of %d scanned",
        counts["synced"],
        counts["skipped"],
        counts["scanned"],
    )
    return counts
