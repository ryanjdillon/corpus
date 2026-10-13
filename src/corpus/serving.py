"""The MCP surface of every storage tier, built by one function: :func:`build_mcp`.

Each tier serves the same generic tools, enforced by that tier's ``QueryPolicy``:

- ``query``: one page of the rows a :class:`~corpus.query.QuerySpec` matches;
- ``search``: the same, ranked by meaning (the text is embedded here);
- ``aggregate``: counts per group;
- ``get``: one row by id.

Next to them each tier keeps the fixed preset tools its consumers already call
(``index_*`` on the sanitized tier, ``corpus_*`` on the sensitive one), with
unchanged names, inputs and results.

Every tool's result is wrapped in the ``corpus.result/v1`` envelope and every
call is audited, both here at registration rather than in each tool, so a tool
cannot ship without either. A tool returns a :class:`Result`; its untrusted
fields are declared when it is registered, from the tier's projection.

Everything about a tier comes from its :class:`~corpus.tiers.Tier`: the server
name, the policy, the store and its DSN. No tool takes a policy from its caller.
What a tier serves follows from its kind: a source tier serves the documents
table and the ``corpus_*`` presets, a projected tier its ``messages`` table and
the ``index_*`` presets. The tier, the store factory, the query embedder and the
clock are the collaborators injected.
"""

import copy
import functools
import inspect
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import msgspec
from mcp.server.fastmcp import FastMCP
from pydantic import Field, WithJsonSchema

from . import search as semantic
from .config import settings
from .embeddings import embed_query
from .enricher import inline_refs
from .envelope import CURSOR_HELP, clamp, decode_cursor, encode_cursor, offset_of, wrap
from .query import (
    Filters,
    Page,
    Position,
    QueryPolicy,
    QuerySpec,
    Range,
    check,
    parse_spec,
    relative_cutoff,
)
from .sanitized_store import UNTRUSTED_COLS, SanitizedStore
from .store import DocumentStore
from .telemetry import audited
from .tiers import Tier
from .tiers import tier as configured_tier

Store = Any
OpenStore = Callable[[], AbstractContextManager[Store]]
Embed = Callable[[str], list[float]]
Clock = Callable[[], datetime]

#: The most rows one offset-paged call returns; a larger limit is clamped and the
#: caller pages on with ``next_cursor``.
MAX_LIMIT = 100
#: The deepest an offset-paged query pages. Paging stops (next_cursor is null)
#: here, which bounds the rows an offset scan skips.
MAX_OFFSET = 10 * MAX_LIMIT
#: Search re-embeds the text on every page, so its pages are smaller.
SEARCH_MAX_LIMIT = 50
#: The longest text a search embeds. It is a query, not a document, so this is
#: far below any embedding model's context, and refused before the embedder is called.
SEARCH_TEXT_MAX = 2000
#: A cursor carrying a relative bound resolved longer ago than this is refused;
#: the caller starts again.
CURSOR_MAX_AGE = timedelta(hours=1)

# The sensitive tier's raw mail: the subject, sender and body are third-party
# text; ids, dates, scores and the computed data-class label are not.
SEARCH_UNTRUSTED = ("from_addr", "snippet", "subject")
QUERY_UNTRUSTED = ("from_addr", "subject")
GET_UNTRUSTED = ("content", "meta")
#: Group keys read from third-party text (a sender's domain, an organization a
#: model extracted). Every other dimension is an enum, a month, a label, or the
#: owner's own account.
GROUP_UNTRUSTED = ("organization", "sender_domain")



def index_presets_policy(policy: QueryPolicy) -> QueryPolicy:
    """The index presets' policy: the tier's, plus the two dimensions only those
    fixed queries use (``due``, and grouping by disposition)."""
    return msgspec.structs.replace(
        policy,
        max_limit=MAX_LIMIT,
        filterable=policy.filterable | {"due"},
        aggregatable=policy.aggregatable | {"suggested_disposition"},
    )


@dataclass(frozen=True)
class Result:
    """What a tool returns, before the envelope: rows and how they page."""

    rows: list[dict[str, Any]]
    total: int | None = None
    next_cursor: str | None = None
    truncated: bool | None = None


def open_documents(t: Tier) -> DocumentStore:
    """Open a source tier's documents table."""
    return DocumentStore(t.serving_dsn(), t.schema)


def open_index_store(t: Tier) -> SanitizedStore:
    """Open a projected tier's store read-only, as its reader role if it has one."""
    return SanitizedStore(t.serving_dsn(), t.schema, read_only=True)


def now_utc() -> datetime:
    """The current time, in UTC: the clock relative bounds resolve against."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Surface:
    """How one kind of tier is served: its store, row classification and presets."""

    service: str
    open_store: Callable[[Tier], Store]
    page: Callable[[Store, QuerySpec, QueryPolicy, Position | None], Page]
    rows_untrusted: tuple[str, ...]
    get_untrusted: tuple[str, ...]
    group_untrusted: tuple[str, ...]
    instructions: str
    presets: Callable[..., list[tuple[Callable[..., Result], tuple[str, ...]]]] = field(
        repr=False
    )


# --- relative bounds, frozen in the cursor -----------------------------------


def _relative(value: str) -> bool:
    """Whether a range bound is a duration before now ('14d'), not a fixed time."""
    try:
        relative_cutoff(value)
    except ValueError:
        return False
    return True


def _within(frozen: Any, relative: str, now: Clock) -> bool:
    """Whether *frozen* is *relative*'s cutoff as resolved up to ``CURSOR_MAX_AGE`` ago.

    A cursor carries the first page's resolved bound so a window does not slide
    between pages. Unsigned, it must still not widen the call, so it must lie
    between the window's current start and ``CURSOR_MAX_AGE`` before it.
    """
    if not isinstance(frozen, str):
        return False
    try:
        moment = datetime.fromisoformat(frozen)
    except ValueError:
        return False
    if moment.tzinfo is None:  # relative_cutoff is always zone-aware
        return False
    current = datetime.fromisoformat(relative_cutoff(relative, now=now))
    return current - CURSOR_MAX_AGE <= moment <= current


def _ends(spec: QuerySpec) -> dict[str, str]:
    """Every relative range end *spec* sets, keyed ``<filter>.<since|until>``."""
    ends = {}
    for name in ("sent_at", "deadline"):
        bounds = getattr(spec.filters, name)
        for end in ("since", "until"):
            value = getattr(bounds, end) if bounds is not None else None
            if value is not None and _relative(value):
                ends[f"{name}.{end}"] = value
    return ends


def _freeze(spec: QuerySpec, frozen: Mapping[str, str]) -> QuerySpec:
    """*spec* with each relative range end replaced by its frozen absolute value."""
    changes = {}
    for name in ("sent_at", "deadline"):
        bounds = getattr(spec.filters, name)
        if bounds is None:
            continue
        ends = {end: frozen.get(f"{name}.{end}", getattr(bounds, end)) for end in ("since", "until")}
        changes[name] = Range(**ends)
    return msgspec.structs.replace(
        spec, filters=msgspec.structs.replace(spec.filters, **changes)
    )


def _resume(
    spec: QuerySpec, position: Any, now: Clock, max_offset: int, keyset: bool
) -> tuple[QuerySpec, dict[str, str], Position | None]:
    """The spec with frozen bounds, the bounds, and where the store resumes.

    The first page resolves each relative bound once. A later page takes them
    from its cursor, after checking each against this call's own arguments, so a
    forged cursor can neither widen a window nor add one the call did not ask for.
    """
    relative = _ends(spec)
    if position is None:
        frozen = {key: relative_cutoff(value, now=now) for key, value in relative.items()}
        return _freeze(spec, frozen), frozen, None
    if not isinstance(position, dict) or set(position) != {"at", "bounds"}:
        raise ValueError("invalid cursor")
    frozen, at = position["bounds"], position["at"]
    if not isinstance(frozen, dict) or set(frozen) != set(relative) or not all(
        _within(frozen[key], value, now) for key, value in relative.items()
    ):
        raise ValueError("invalid cursor")
    if isinstance(at, list):
        if not keyset or len(at) != 2 or not all(isinstance(k, str) for k in at):
            raise ValueError("invalid cursor")
    else:
        at = offset_of(at, max_offset)
    return _freeze(spec, frozen), frozen, at


# --- tool inputs ---------------------------------------------------------------

_SPEC_SCHEMA = inline_refs(msgspec.json.schema(QuerySpec))
_SPEC_HELP = {
    "filters": "What to match; every filter set applies (AND). Ranges are half-open "
    "{since, until}: an ISO date or datetime (UTC if no zone), or a duration before now "
    "such as '14d'.",
    "text": "A keyword, matched case-insensitively in the tier's text fields.",
    "order": "Row order; offset-paged except newest/oldest, which page by keyset.",
    "limit": f"Rows (or groups) per call; a larger value is clamped (at most {MAX_LIMIT}).",
    "cursor": CURSOR_HELP,
}


def _spec_schema(policy: QueryPolicy, fields: tuple[str, ...]) -> dict[str, Any]:
    """The JSON Schema of a spec argument: *fields* of ``QuerySpec``, within *policy*.

    It lists only what the tier allows, so a caller sees its affordances. The
    policy check still refuses anything else at call time.
    """
    properties = {}
    for name in fields:
        prop = copy.deepcopy(_SPEC_SCHEMA["properties"][name])
        if name == "filters":
            prop["properties"] = {
                k: v for k, v in prop["properties"].items() if k in policy.filterable
            }
            prop["additionalProperties"] = False
            prop.pop("description", None)
        if name == "order":
            prop["anyOf"][0]["enum"] = sorted(policy.orders)
        properties[name] = {**prop, "description": _SPEC_HELP[name]}
    return {"type": "object", "properties": properties, "additionalProperties": False}


def _spec(
    data: Mapping[str, Any] | None, policy: QueryPolicy, *, refuse: tuple[str, ...]
) -> QuerySpec:
    """Parse a spec argument within *policy*, refusing the fields this tool does not take.

    The policy is checked here, before a store is opened, as well as by the store.
    """
    spec = parse_spec(data or {})
    for name in refuse:
        if getattr(spec, name):
            raise ValueError(f"{name!r} does not apply to this tool")
    check(spec, policy)
    return spec


def _bound(spec: QuerySpec) -> dict[str, Any]:
    """The spec as a cursor binds it: everything but the paging fields."""
    return {k: v for k, v in msgspec.to_builtins(spec).items() if k not in ("cursor", "limit")}


def _resumable(nxt: Position | None, offset_ceiling: int) -> Position | None:
    """*nxt*, or ``None`` once an offset reaches the ceiling paging stops at."""
    if isinstance(nxt, int) and nxt >= offset_ceiling:
        return None
    return nxt


# --- the generic tools -----------------------------------------------------------


def _generic(
    t: _Surface, tier: str, policy: QueryPolicy, open_store: OpenStore, embed: Embed, now: Clock
) -> list:
    """The four tools every tier serves, within *policy*.

    Cursors are bound to *tier*'s name, so one cannot be replayed on another tier.
    """
    query_input = Annotated[
        dict[str, Any] | None,
        WithJsonSchema(_spec_schema(policy, ("filters", "text", "order", "limit", "cursor"))),
    ]
    search_input = Annotated[
        dict[str, Any] | None,
        WithJsonSchema(_spec_schema(policy, ("filters", "text", "limit", "cursor"))),
    ]
    count_input = Annotated[
        dict[str, Any] | None, WithJsonSchema(_spec_schema(policy, ("filters", "text", "limit")))
    ]
    dimensions = Annotated[
        list[str],
        WithJsonSchema({
            "type": "array", "items": {"enum": sorted(policy.aggregatable)},
            "minItems": 1, "uniqueItems": True,
            "description": "Dimensions to count by, in order.",
        }),
    ]

    def query(spec: query_input = None) -> Result:
        """Rows matching a structured query on this tier, one page per call.

        Pass the next page's ``cursor`` inside ``spec`` with the same other fields.
        A filter, order or field this tier does not allow is refused, never ignored.
        """
        parsed = _spec(spec, policy, refuse=("semantic", "group_by"))
        args = {"tier": tier, "spec": _bound(parsed)}
        position = decode_cursor(parsed.cursor, "query", args)
        frozen_spec, bounds, at = _resume(parsed, position, now, MAX_OFFSET, keyset=True)
        n = clamp(parsed.limit, policy.max_limit)
        with open_store() as store:
            page = t.page(
                store, msgspec.structs.replace(frozen_spec, limit=n, cursor=None), policy, at
            )
        nxt = _resumable(page.next_cursor, MAX_OFFSET)
        cursor = None if nxt is None else encode_cursor("query", args, {"at": nxt, "bounds": bounds})
        return Result(page.rows, next_cursor=cursor)

    def search(
        text: Annotated[
            str,
            Field(description="Natural-language text, matched by meaning.", max_length=SEARCH_TEXT_MAX),
        ],
        spec: search_input = None,
    ) -> Result:
        """Rows ranked by meaning: *text* is embedded and compared with the tier's vectors.

        ``spec`` narrows the candidates with filters and pages with ``cursor``.
        Ranking stops at the tier's search depth (at most 200 rows).
        """
        parsed = _spec(spec, policy, refuse=("semantic", "order", "group_by"))
        args = {"tier": tier, "text": text, "spec": _bound(parsed)}
        position = decode_cursor(parsed.cursor, "search", args)
        frozen_spec, bounds, at = _resume(
            parsed, position, now, policy.search_depth, keyset=False
        )
        n = clamp(parsed.limit, SEARCH_MAX_LIMIT)
        ranked = msgspec.structs.replace(frozen_spec, semantic=embed(text), limit=n, cursor=None)
        with open_store() as store:
            page = t.page(store, ranked, policy, at)
        nxt = page.next_cursor
        cursor = None if nxt is None else encode_cursor("search", args, {"at": nxt, "bounds": bounds})
        return Result(page.rows, next_cursor=cursor)

    def aggregate(spec: count_input, group_by: dimensions) -> Result:
        """Counts of the items a query matches, per group of ``group_by``.

        One row per group: its dimensions, ``count``, ``min_sent_at`` and
        ``max_sent_at``. ``total`` counts each matching item once; ``truncated``
        says more groups exist than ``limit``.
        """
        parsed = _spec(spec, policy, refuse=("semantic", "order", "group_by", "cursor"))
        frozen_spec, _, _ = _resume(parsed, None, now, 0, keyset=False)
        counted = msgspec.structs.replace(frozen_spec, group_by=list(group_by))
        check(counted, policy)
        with open_store() as store:
            result = store.aggregate(counted, policy)
        return Result(result.groups, total=result.total, truncated=result.truncated)

    def get(id: Annotated[str, Field(description="Row id from a query or search result.")]) -> Result:
        """One row by id: 0 or 1 rows."""
        with open_store() as store:
            row = store.get(id)
        return Result([row] if row else [])

    return [
        (query, t.rows_untrusted),
        (search, t.rows_untrusted),
        (aggregate, t.group_untrusted),
        (get, t.get_untrusted),
    ]


# --- the sanitized tier's presets -----------------------------------------------


def _index_presets(policy: QueryPolicy, open_store: OpenStore, embed: Embed, now: Clock) -> list:
    """The corpus-index presets: fixed queries the briefing and skills call by name."""
    presets = index_presets_policy(policy)

    def page(tool: str, spec: QuerySpec, limit: int, cursor: str | None, **args: Any) -> Result:
        offset = offset_of(decode_cursor(cursor, tool, args), MAX_OFFSET)
        n = clamp(limit, MAX_LIMIT)
        with open_store() as store:
            found = store.query(msgspec.structs.replace(spec, limit=n), presets, offset)
        more = found.next_cursor is not None and offset + n < MAX_OFFSET
        return Result(found.rows, next_cursor=encode_cursor(tool, args, offset + n) if more else None)

    def index_action_items(
        limit: Annotated[int, Field(description="max items")] = 50,
        domain: Annotated[str | None, Field(description="filter by life-area domain (banking, health, work…)")] = None,
        importance: Annotated[str | None, Field(description="filter: high | medium | low")] = None,
        cursor: Annotated[str | None, Field(description=CURSOR_HELP)] = None,
    ) -> Result:
        """Messages that need an action from you, ranked by importance then urgency."""
        filters = Filters(
            requires_action=True, domain=domain or None, importance=importance or None
        )
        spec = QuerySpec(filters=filters, order="priority")
        return page(
            "index_action_items", spec, limit, cursor, domain=domain, importance=importance
        )

    def index_due_soon(
        limit: Annotated[int, Field(description="max items")] = 50,
        cursor: Annotated[str | None, Field(description=CURSOR_HELP)] = None,
    ) -> Result:
        """Time-sensitive items, or items carrying a deadline."""
        spec = QuerySpec(filters=Filters(due=True), order="deadline")
        return page("index_due_soon", spec, limit, cursor)

    def index_waiting_on(
        who: Annotated[str, Field(description="'them' = you're waiting on someone; 'me' = they're waiting on you")] = "them",
        limit: Annotated[int, Field(description="max items")] = 50,
        cursor: Annotated[str | None, Field(description=CURSOR_HELP)] = None,
    ) -> Result:
        """Threads awaiting a reply."""
        spec = QuerySpec(filters=Filters(waiting_on=who), order="priority")
        return page("index_waiting_on", spec, limit, cursor, who=who)

    def index_by_domain(
        domain: str,
        limit: Annotated[int, Field(description="max items")] = 50,
        cursor: Annotated[str | None, Field(description=CURSOR_HELP)] = None,
    ) -> Result:
        """Recent sanitized items in a life-area domain."""
        spec = QuerySpec(filters=Filters(domain=domain), order="priority")
        return page("index_by_domain", spec, limit, cursor, domain=domain)

    def index_summary(id: str) -> Result:
        """The sanitized summary + priority signal for one message id (0 or 1 rows)."""
        with open_store() as store:
            row = store.get(id)
        return Result([row] if row else [])

    def index_stats() -> Result:
        """Counts of sanitized items by domain and suggested disposition.

        One row per group: ``dimension`` (``domain`` or ``disposition``), ``key`` and
        ``count``; ``total`` is the item count. Both keys are schema enums.
        """
        counts = []
        with open_store() as store:
            for dim in ("domain", "suggested_disposition"):
                spec = QuerySpec(group_by=[dim], limit=presets.max_limit)
                counts.append(store.aggregate(spec, presets))
        rows = [
            {"dimension": dimension, "key": g[dim], "count": g["count"]}
            for (dimension, dim), agg in zip(
                (("domain", "domain"), ("disposition", "suggested_disposition")), counts,
                strict=True,
            )
            for g in agg.groups
        ]
        return Result(rows, total=counts[0].total)

    return [
        (index_action_items, UNTRUSTED_COLS),
        (index_due_soon, UNTRUSTED_COLS),
        (index_waiting_on, UNTRUSTED_COLS),
        (index_by_domain, UNTRUSTED_COLS),
        (index_summary, UNTRUSTED_COLS),
        (index_stats, ()),
    ]


# --- the sensitive tier's presets -------------------------------------------------


def _keyset(position: Any) -> bool:
    """Whether *position* is a ``corpus_query`` keyset: two strings, and its bounds."""
    if not isinstance(position, dict):
        return False
    key = position.get("key")
    return (
        isinstance(key, list) and len(key) == 2 and all(isinstance(k, str) for k in key)
        and all(position.get(end) is None or isinstance(position.get(end), str)
                for end in ("after", "before"))
    )


def _first(value: str | None, now: Clock) -> str | None:
    """A ``corpus_query`` bound as the first page reads it: a duration resolved now."""
    return relative_cutoff(value, now=now) if value and _relative(value) else value or None


def _same_bound(frozen: Any, value: str | None, now: Clock) -> bool:
    """Whether a cursor's frozen bound is the one this call's argument *value* gives.

    A fixed time must equal it, a duration must be that bound as resolved within
    ``CURSOR_MAX_AGE`` (see :func:`_within`), and no argument means no bound. The
    audit log then states the window actually read.
    """
    if not value:
        return frozen is None
    if _relative(value):
        return _within(frozen, value, now)
    return frozen == value


def _same_window(
    position: Mapping[str, Any], after: str | None, before: str | None, since: str | None,
    now: Clock,
) -> bool:
    """Whether a ``corpus_query`` cursor's bounds are the ones this call's arguments give.

    The lower bound is ``after``'s, or else ``since``'s (always a duration); the
    upper is ``before``'s.
    """
    lower = position.get("after")
    same_lower = (
        _same_bound(lower, after, now) if after
        else _within(lower, since, now) if since
        else lower is None
    )
    return same_lower and _same_bound(position.get("before"), before, now)


def _corpus_presets(policy: QueryPolicy, open_store: OpenStore, embed: Embed, now: Clock) -> list:
    """The corpus presets: the sensitive tier's tools as consumers know them."""

    def corpus_search(
        query: str = Field(
            description="Natural-language text, matched semantically against content.",
            max_length=SEARCH_TEXT_MAX,
        ),
        top_k: int = Field(default=10, description="Maximum number of ranked matches to return."),
        cursor: Annotated[str | None, Field(description=CURSOR_HELP)] = None,
    ) -> Result:
        """Semantic (vector) search over the indexed mail/documents, by MEANING.

        Use ONLY for topic/similarity lookups ("find emails about the invoice").
        Do NOT use for time-based questions — "today", "recent", "this week",
        "latest" — those MUST use ``corpus_query`` with ``since`` (semantic search
        ignores dates and will return old, irrelevant matches).

        Returns:
            A ``corpus.result/v1`` envelope whose ``rows`` are ranked matches, each
            with subject, from_addr, sent_at, label, and a 300-character snippet.
        """
        depth = policy.search_depth
        offset = offset_of(decode_cursor(cursor, "corpus_search", {"query": query}), depth)
        n = clamp(top_k, SEARCH_MAX_LIMIT)
        # One beyond the page shows whether more exist, up to the depth search ranks.
        with open_store() as documents:
            ranked = semantic.semantic_search(
                documents, query, min(offset + n + 1, depth), embed=embed
            )
        ranked = ranked[offset:]
        next_cursor = (
            encode_cursor("corpus_search", {"query": query}, offset + n) if len(ranked) > n else None
        )
        return Result(ranked[:n], next_cursor=next_cursor)

    def corpus_query(
        label: str | None = Field(
            default=None,
            description="Exact data-class label, e.g. 'personal', 'promotional', 'newsletter'.",
        ),
        account: str | None = Field(default=None, description="Exact account address to filter by."),
        before: str | None = Field(
            default=None,
            description="Upper bound on sent time, an ISO-8601 timestamp (or a duration before "
            "now, like `since`).",
        ),
        after: str | None = Field(
            default=None,
            description="Lower bound on sent time, an ISO-8601 timestamp (or a duration before "
            "now, like `since`).",
        ),
        since: str | None = Field(
            default=None,
            description="Relative lower bound (now - window): '30m', '24h', '7d', '2w'. Preferred "
            "for 'recent'/'last day' queries; ignored when `after` is given.",
        ),
        limit: int = Field(
            default=100,
            description=f"Matches per page, newest first (at most {MAX_LIMIT}); page on with "
            "next_cursor.",
        ),
        cursor: Annotated[str | None, Field(description=CURSOR_HELP)] = None,
    ) -> Result:
        """Structured, non-semantic metadata query over the indexed mail/documents.

        Use for time-ranged or "recent" questions (the last day is ``since='1d'``) and
        for filtering by label or account.

        Returns:
            A ``corpus.result/v1`` envelope whose ``rows`` are metadata (id, subject,
            from_addr, sent_at, label), newest first.
        """
        args = {"label": label, "account": account, "before": before, "after": after, "since": since}
        position = decode_cursor(cursor, "corpus_query", args)
        if position is None:
            # Resolve a relative bound once, so every page shares the first page's
            # window instead of sliding forward with the clock.
            lower = _first(after, now) if after else (relative_cutoff(since, now=now) if since else None)
            upper, key = _first(before, now), None
        elif _keyset(position) and _same_window(position, after, before, since, now):
            lower, upper, key = position.get("after"), position.get("before"), position["key"]
        else:
            raise ValueError("invalid cursor")
        window = Range(since=lower, until=upper) if lower or upper else None
        spec = QuerySpec(
            filters=Filters(label=label or None, account=account or None, sent_at=window),
            order="newest",
            limit=clamp(limit, MAX_LIMIT),
        )
        with open_store() as documents:
            page = documents.query_spec(spec, policy, key)
        next_cursor = None
        if page.next_cursor is not None:
            position = {"key": list(page.next_cursor), "after": lower, "before": upper}
            next_cursor = encode_cursor("corpus_query", args, position)
        return Result(page.rows, next_cursor=next_cursor)

    def corpus_get(
        id: str = Field(description="Document id from a corpus_search / corpus_query result."),
    ) -> Result:
        """Fetch one indexed document by id: full body content and all metadata.

        Use to escalate from a ``corpus_search`` snippet to the complete record.

        Returns:
            A ``corpus.result/v1`` envelope with 0 or 1 ``rows``, each with ``id``,
            ``content`` (full body), and ``meta``.
        """
        with open_store() as documents:
            doc = documents.get(id)
        return Result([doc] if doc else [])

    def corpus_stats() -> Result:
        """Index totals and per-label document counts.

        Returns:
            A ``corpus.result/v1`` envelope with one row per label (``label`` and
            ``count``) and the document count as ``total``.
        """
        with open_store() as documents:
            s = documents.stats()
        rows = [{"label": label, "count": n} for label, n in s["by_label"].items()]
        return Result(rows, total=s["total"])

    return [
        (corpus_search, SEARCH_UNTRUSTED),
        (corpus_query, QUERY_UNTRUSTED),
        (corpus_get, GET_UNTRUSTED),
        (corpus_stats, ()),
    ]


# --- the tiers ----------------------------------------------------------------------

_SOURCE = _Surface(
    service="corpus-mcp",
    open_store=open_documents,
    page=lambda store, spec, policy, at: store.query_spec(spec, policy, at),
    rows_untrusted=QUERY_UNTRUSTED,
    get_untrusted=GET_UNTRUSTED,
    group_untrusted=(),
    instructions=(
        "corpus indexes the user's own email and documents. For ANY question about "
        "their mail or documents — recent messages, who wrote, what arrived, finding "
        "or summarizing a message — use these tools instead of saying you lack "
        "access: corpus_query (structured / time-ranged; the last day is since='1d'), "
        "corpus_search (semantic 'find X'), corpus_get (full body by id), "
        "corpus_stats (totals). query, search, aggregate and get take a structured "
        "spec, for filters and counts the presets do not cover."
    ),
    presets=_corpus_presets,
)
_PROJECTED = _Surface(
    service="corpus-index",
    open_store=open_index_store,
    page=lambda store, spec, policy, at: store.query(spec, policy, at),
    rows_untrusted=UNTRUSTED_COLS,
    get_untrusted=UNTRUSTED_COLS,
    group_untrusted=GROUP_UNTRUSTED,
    instructions=(
        "Sanitized priority signal over the owner's private corpus. You receive "
        "secret-free summaries and classification only — never raw message bodies. "
        "Use it to build a prioritized plan: what needs action, what's due, what's "
        "awaiting a reply. Rows carry a uri, when one exists, that opens the message "
        "for the owner. query, search, aggregate and get take a structured spec "
        "for anything the index_* presets do not cover, such as counts per month."
    ),
    presets=_index_presets,
)


def _surface(t: Tier) -> _Surface:
    return _SOURCE if t.projection is None else _PROJECTED


def tier_service(tier: str) -> str:
    """The telemetry service name a tier's server reports as (by default)."""
    return _surface(configured_tier(tier)).service


def _registrar(mcp: FastMCP, tier: str) -> Callable[[Callable[..., Result], tuple[str, ...]], None]:
    """Register tools on *mcp*: each result enveloped, each call audited."""
    audit = audited(server=mcp.name, tier=tier)

    def register(fn: Callable[..., Result], untrusted: tuple[str, ...]) -> None:
        @functools.wraps(fn)
        def tool(**kwargs: Any) -> dict[str, Any]:
            r = fn(**kwargs)
            return wrap(
                r.rows, server=mcp.name, tool=fn.__name__, tier=tier, untrusted_fields=untrusted,
                total=r.total, next_cursor=r.next_cursor, truncated=r.truncated,
            )

        # The description is the docstring, which nesting indents.
        tool.__doc__ = inspect.cleandoc(fn.__doc__ or "")
        # FastMCP derives the output schema from the return annotation; a
        # ``dict[str, Any]`` one keeps the envelope as structured content.
        tool.__signature__ = inspect.signature(fn).replace(return_annotation=dict[str, Any])
        mcp.tool()(audit(tool))

    return register


def build_mcp(
    tier: str,
    *,
    config: Tier | None = None,
    open_store: OpenStore | None = None,
    embed: Embed = embed_query,
    now: Clock = now_utc,
) -> FastMCP:
    """Build the MCP server for *tier*, a name in the tiers file.

    Args:
        tier: The storage tier to serve; it fixes the server name, the policy and
            the store.
        config: The tier itself (default: the configured tier named *tier*).
        open_store: Opens the tier's store as a context manager (default: the
            tier's real store; read-only, as its reader, on a projected tier).
        embed: Embeds search text, with the model the tier's vectors came from.
        now: The clock relative bounds resolve against.
    """
    t = config or configured_tier(tier)
    surface = _surface(t)
    mcp = FastMCP(
        t.server_name,
        host=settings.host,
        port=settings.mcp_port,
        # Every tool is a self-contained query, so no session state is needed. A
        # stateful server expires idle sessions, and a long-lived client (a gateway
        # MCP proxy, an agent polling once a day) then gets 404 on its next call.
        stateless_http=True,
        instructions=surface.instructions,
    )
    register = _registrar(mcp, t.name)
    opener = open_store or functools.partial(surface.open_store, t)
    tools = (
        *_generic(surface, t.name, t.policy, opener, embed, now),
        *surface.presets(t.policy, opener, embed, now),
    )
    for fn, untrusted in tools:
        register(fn, untrusted)
    return mcp


def serve(tier: str, *, build: Callable[[str], FastMCP] = build_mcp) -> None:
    """Serve *tier*'s MCP tools over streamable HTTP."""
    build(tier).run(transport="streamable-http")
