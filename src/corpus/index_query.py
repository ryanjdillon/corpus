"""corpus-index: the sanitized query surface.

The trust gate for a cloud-model consumer (Hermes). It reads only the sanitized
``messages`` table in the separate ``ai_sanitized`` database — cloud-safe fields
plus the enrichment priority signal, never raw content, subject, sender, or secret
material — as the restricted ``corpus_index_ro`` role. That table is written by the
one-way sync (``corpus sync`` → ``SanitizedStore``); nothing here writes.

Queries connect via ``CORPUS_INDEX_DATABASE_URL`` (the sanitized DB, read-only
role). Each function here is a fixed preset over :meth:`SanitizedStore.query` or
:meth:`SanitizedStore.aggregate`, so the tier's SQL lives in one place. ``COLS``
from ``sanitized_store`` is the single projection contract, so the returned
columns track the tier's schema and never include the embedding.
"""

from __future__ import annotations

from collections.abc import Callable

import msgspec

from .config import settings
from .query import SANITIZED_POLICY, Filters, QuerySpec
from .sanitized_store import SanitizedStore

#: The most rows one index tool call returns. The MCP surface clamps a larger
#: ``limit`` to it, and the caller pages on with ``next_cursor``.
MAX_LIMIT = 100

#: The presets' policy: the sanitized tier's, plus the two dimensions only these
#: fixed queries use (``due``, and grouping by disposition). Its row cap is one
#: above ``MAX_LIMIT`` because the MCP surface fetches one row beyond a full page
#: to learn whether another exists.
PRESETS = msgspec.structs.replace(
    SANITIZED_POLICY,
    max_limit=MAX_LIMIT + 1,
    filterable=SANITIZED_POLICY.filterable | {"due"},
    aggregatable=SANITIZED_POLICY.aggregatable | {"suggested_disposition"},
)

Reader = Callable[[], SanitizedStore]


def reader() -> SanitizedStore:
    """Open the sanitized store as the restricted index role, read-only."""
    if not settings.index_database_url:
        raise RuntimeError("CORPUS_INDEX_DATABASE_URL not set (corpus-index disabled)")
    return SanitizedStore(settings.index_database_url, read_only=True)


def _page(spec: QuerySpec, offset: int, open_store: Reader) -> list[dict]:
    with open_store() as store:
        return store.query(spec, PRESETS, offset).rows


def action_items(
    limit: int = 50,
    domain: str | None = None,
    importance: str | None = None,
    offset: int = 0,
    *,
    open_store: Reader = reader,
) -> list[dict]:
    """Items that need an action from the owner, ranked by importance + urgency."""
    filters = Filters(requires_action=True, domain=domain or None, importance=importance or None)
    return _page(QuerySpec(filters=filters, order="priority", limit=limit), offset, open_store)


def due_soon(limit: int = 50, offset: int = 0, *, open_store: Reader = reader) -> list[dict]:
    """Time-sensitive items, or items carrying a deadline."""
    spec = QuerySpec(filters=Filters(due=True), order="deadline", limit=limit)
    return _page(spec, offset, open_store)


def waiting_on(
    who: str = "them", limit: int = 50, offset: int = 0, *, open_store: Reader = reader
) -> list[dict]:
    """Items flagged as awaiting a reply.

    'them' = owner is waiting on someone, 'me' = someone is waiting on the owner.
    """
    spec = QuerySpec(filters=Filters(waiting_on=who), order="priority", limit=limit)
    return _page(spec, offset, open_store)


def by_domain(
    domain: str, limit: int = 50, offset: int = 0, *, open_store: Reader = reader
) -> list[dict]:
    """Recent sanitized items in a life-area domain (banking, health, work…)."""
    spec = QuerySpec(filters=Filters(domain=domain), order="priority", limit=limit)
    return _page(spec, offset, open_store)


def summary(doc_id: str, *, open_store: Reader = reader) -> dict | None:
    """The sanitized summary + priority signal for one item id."""
    with open_store() as store:
        return store.get(doc_id)


def stats(*, open_store: Reader = reader) -> dict:
    """Counts of sanitized items by domain and suggested disposition."""
    with open_store() as store:
        by_dom = store.aggregate(QuerySpec(group_by=["domain"], limit=PRESETS.max_limit), PRESETS)
        by_disp = store.aggregate(
            QuerySpec(group_by=["suggested_disposition"], limit=PRESETS.max_limit), PRESETS
        )
    return {
        "total": by_dom.total,
        "by_domain": {g["domain"]: g["count"] for g in by_dom.groups},
        "by_disposition": {g["suggested_disposition"]: g["count"] for g in by_disp.groups},
    }
