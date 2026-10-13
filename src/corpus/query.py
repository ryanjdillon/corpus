"""The query model: one structured query over a storage tier, within the tier's policy.

A :class:`QuerySpec` states what to read: filters, a keyword or a vector to match,
an order and a page size, or a ``group_by`` to count instead. Its JSON Schema is
the input of the query tools. A :class:`QueryPolicy` states what a tier allows,
and a spec that uses anything else is refused with a ``ValueError`` naming it,
never quietly ignored: a caller that believes it filtered must not get the
unfiltered result.

SQL is built only from a :class:`Layout`, which each store declares and which maps
every dimension to a fixed SQL expression. A spec chooses among those
expressions; it never supplies an identifier, and every value it carries is a
bound parameter.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, Literal

import msgspec

from .enrichment import (
    ActionType,
    Category,
    Disposition,
    Domain,
    Importance,
    SensitivityLevel,
    TransactionalType,
    WaitingOn,
)

Order = Literal["priority", "newest", "oldest", "deadline"]
GroupBy = Literal[
    "domain", "sender_domain", "organization", "month", "label", "category", "importance",
    "account", "suggested_disposition", "facts.travel.origin_country",
    "facts.travel.destination_country", "facts.travel.start_year",
]
#: Where a page resumes: an offset for the offset-paged orders, or the last row's
#: ``[sent_at key, id]`` for ``newest``/``oldest``. The caller encodes it.
Position = int | Sequence[str]

#: The deepest a vector-ordered query pages. An HNSW index scan ranks at most
#: ``hnsw.ef_search`` rows whatever the LIMIT, and the stores set it to this.
SEMANTIC_DEPTH = 200

_Short = Annotated[str, msgspec.Meta(min_length=1, max_length=200)]
#: The most values a list filter takes. Checked here rather than as the schema's
#: ``maxItems``, which the query model's tool schema does not otherwise use.
MAX_ANY_OF = 50
#: An ISO 3166-1 alpha-2 country code, in either case ("US", "no"). The code is
#: checked in ``Filters.__post_init__`` and the pattern only advertised in the
#: schema: msgspec (through 0.22.0) corrupts the heap when it frees a union that
#: holds both a ``pattern`` str and a list, as ``_Country | list[_Country]`` does.
#: No length keyword either, so the tool schema stays the pattern alone; ``type``
#: is repeated so the merged schema keeps its usual key order.
_COUNTRY_RE = re.compile(r"[A-Za-z]{2}")
_Country = Annotated[
    str, msgspec.Meta(extra_json_schema={"type": "string", "pattern": r"^[A-Za-z]{2}$"})
]
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

#: Sensitivity levels in increasing order, for ``max_sensitivity``.
SENSITIVITY_RANK = {level.value: rank for rank, level in enumerate(SensitivityLevel)}


def relative_cutoff(since: str, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> str:
    """Convert a relative window like '24h', '7d', '30m' into an ISO cutoff timestamp.

    Returns now - window, so callers need not know the current time. Units: s,
    m (minutes), h, d, w.
    """
    match = _DURATION_RE.match(since.lower())
    if not match:
        raise ValueError(f"invalid duration {since!r}; use e.g. '24h', '7d', '30m'")
    amount, unit = int(match.group(1)), match.group(2)
    cutoff = now() - timedelta(seconds=amount * _UNIT_SECONDS[unit])
    return cutoff.isoformat()


def resolve_bound(value: str, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> str:
    """Resolve a range bound to an absolute, zone-aware ISO timestamp.

    *value* is an ISO date (midnight UTC), an ISO datetime (UTC if it has no
    zone), or a duration before now ('14d'). A bound is made absolute here, not
    left to the database, so the session's time zone cannot move it.
    """
    if _DURATION_RE.match(value.lower()):
        return relative_cutoff(value, now=now)
    try:
        if len(value) == 10:
            day = date.fromisoformat(value)
            return datetime(day.year, day.month, day.day, tzinfo=UTC).isoformat()
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"invalid date {value!r}; use an ISO date or datetime, or a duration like '14d'"
        ) from None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).isoformat()


def resolve_day(
    value: str, *, end: bool, now: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> str:
    """Resolve a range bound on a date column to an ISO date, in UTC.

    A day matches when any part of it falls in the range. A start bound is
    therefore the day it falls on ('14d' is the date 14 days ago), and an end
    bound after midnight keeps its own day. Comparing a date to the timestamp
    instead would truncate the end bound and drop its day.
    """
    moment = datetime.fromisoformat(resolve_bound(value, now=now)).astimezone(UTC)
    day = moment.date()
    if end and moment.timetz() != time(tzinfo=UTC):
        day += timedelta(days=1)
    return day.isoformat()


def _timestamp_key(key: str) -> bool:
    """Whether *key* is a keyset key a ``timestamptz`` accepts: ISO, or a sentinel."""
    if key in ("infinity", "-infinity"):
        return True
    try:
        datetime.fromisoformat(key)
    except ValueError:
        return False
    return True


class Range(msgspec.Struct, forbid_unknown_fields=True, frozen=True, kw_only=True):
    """A half-open time range: ``since <= value < until``; either end may be open.

    Each end is an ISO date or datetime, or a duration before now ('14d').
    """

    since: str | None = None
    until: str | None = None

    def __post_init__(self) -> None:
        for bound in (self.since, self.until):
            if bound is not None:
                resolve_bound(bound)


class Filters(msgspec.Struct, forbid_unknown_fields=True, frozen=True, kw_only=True):
    """Every filter a query can apply; they combine with AND.

    An enum filter takes one value or a list (any of them). ``organization``
    matches any organization containing the text, case-insensitively. ``due`` is
    "time-sensitive or carrying a deadline".

    The ``facts.*`` filters read the enrichment's typed facts, named by their
    path: ``facts.travel`` (whether a row has travel facts), a country code or a
    list of them for ``facts.travel.origin_country`` and
    ``facts.travel.destination_country``, a carrier matched whole but ignoring
    case, and day ranges for ``facts.travel.start`` and ``facts.travel.end``.
    """

    domain: Domain | list[Domain] | None = None
    category: Category | list[Category] | None = None
    transactional_type: TransactionalType | list[TransactionalType] | None = None
    importance: Importance | list[Importance] | None = None
    waiting_on: WaitingOn | list[WaitingOn] | None = None
    action_type: ActionType | list[ActionType] | None = None
    suggested_disposition: Disposition | list[Disposition] | None = None
    requires_action: bool | None = None
    time_sensitive: bool | None = None
    due: bool | None = None
    sender_domain: _Short | None = None
    account: _Short | None = None
    source: _Short | None = None
    label: _Short | None = None
    organization: _Short | None = None
    sent_at: Range | None = None
    deadline: Range | None = None
    max_sensitivity: SensitivityLevel | None = None
    travel: bool | None = msgspec.field(default=None, name="facts.travel")
    travel_origin_country: _Country | list[_Country] | None = msgspec.field(
        default=None, name="facts.travel.origin_country"
    )
    travel_destination_country: _Country | list[_Country] | None = msgspec.field(
        default=None, name="facts.travel.destination_country"
    )
    travel_carrier: _Short | None = msgspec.field(default=None, name="facts.travel.carrier")
    travel_start: Range | None = msgspec.field(default=None, name="facts.travel.start")
    travel_end: Range | None = msgspec.field(default=None, name="facts.travel.end")

    def __post_init__(self) -> None:
        for name in (*ANY_OF, *_COUNTRIES):
            value = self.get(name)
            if value == []:
                raise ValueError(f"filter {name!r} is an empty list; omit it or name a value")
            if isinstance(value, list) and len(value) > MAX_ANY_OF:
                raise ValueError(
                    f"filter {name!r} lists {len(value)} values; at most {MAX_ANY_OF}"
                )
        for name in _COUNTRIES:
            value = self.get(name)
            codes = value if isinstance(value, list) else [] if value is None else [value]
            if bad := [c for c in codes if not _COUNTRY_RE.fullmatch(c)]:
                raise ValueError(f"filter {name!r}: {bad[0]!r} is not a two-letter country code")

    def get(self, name: str) -> Any:
        """The value of the filter named *name* (as a spec writes it)."""
        return getattr(self, FILTERS[name])

    def used(self) -> list[str]:
        """The names (as a spec writes them) of the filters this spec sets."""
        return [name for name in FILTERS if self.get(name) is not None]


#: Every filter: its name as a spec and a policy write it -> its attribute.
FILTERS = {f.encode_name: f.name for f in msgspec.structs.fields(Filters)}
#: The filters that take one value or a list of them.
ANY_OF = (
    "domain", "category", "transactional_type", "importance", "waiting_on", "action_type",
    "suggested_disposition",
)
#: The filters that take one country code or a list of them.
_COUNTRIES = ("facts.travel.origin_country", "facts.travel.destination_country")
#: The range filters.
RANGES = ("sent_at", "deadline", "facts.travel.start", "facts.travel.end")


class QuerySpec(msgspec.Struct, forbid_unknown_fields=True, frozen=True, kw_only=True):
    """One query over a tier.

    Attributes:
        filters: What to match.
        text: A keyword, matched case-insensitively in the tier's text fields.
        semantic: A query embedding; rows are ordered by distance to it. The
            caller embeds; the store does not.
        order: ``priority``, ``newest``, ``oldest`` or ``deadline``; ``newest``
            when unset, and must be unset with ``semantic``.
        limit: Rows per page, clamped to the policy's ``max_limit``.
        cursor: The opaque cursor of the previous page. The caller decodes it
            to a :data:`Position` and passes that to the store.
        group_by: Count matches per group instead of returning rows.
    """

    filters: Filters = msgspec.field(default_factory=Filters)
    text: _Short | None = None
    semantic: Annotated[list[float], msgspec.Meta(min_length=1)] | None = None
    order: Order | None = None
    limit: Annotated[int, msgspec.Meta(ge=1)] = 50
    cursor: str | None = None
    group_by: list[GroupBy] = msgspec.field(default_factory=list)

    def __post_init__(self) -> None:
        if self.semantic is not None and self.order is not None:
            raise ValueError("a semantic query is ordered by distance; omit order")
        if len(set(self.group_by)) != len(self.group_by):
            raise ValueError("group_by names a dimension twice")


def parse_spec(data: Mapping[str, Any]) -> QuerySpec:
    """Validate *data* (decoded JSON) into a spec; raises ``ValueError`` if invalid."""
    return msgspec.convert(data, type=QuerySpec)


class QueryPolicy(msgspec.Struct, forbid_unknown_fields=True, frozen=True, kw_only=True):
    """What one tier's query surface allows.

    A tier's ``query`` block in the config file decodes straight into this struct,
    so an unknown key, filter, dimension or order there is refused at startup.

    Attributes:
        max_limit: The most rows (or groups) one call returns.
        search_depth: The deepest a vector-ordered query pages. At most
            :data:`SEMANTIC_DEPTH`, the most one HNSW scan is set to rank.
        filterable: Filter names a spec may set.
        aggregatable: Dimensions a spec may group by.
        orders: Orders a spec may name.
        text: Whether keyword matching is allowed.
        semantic: Whether vector ordering is allowed.
    """

    max_limit: Annotated[int, msgspec.Meta(ge=1)]
    filterable: frozenset[str]
    aggregatable: frozenset[GroupBy]
    orders: frozenset[Order]
    text: bool
    semantic: bool
    search_depth: Annotated[int, msgspec.Meta(ge=1, le=SEMANTIC_DEPTH)] = SEMANTIC_DEPTH

    def __post_init__(self) -> None:
        unknown = sorted(self.filterable - set(FILTERS))
        if unknown:
            raise ValueError(f"unknown filter(s) {', '.join(map(repr, unknown))}")


SANITIZED_POLICY = QueryPolicy(
    max_limit=100,
    filterable=frozenset({
        "domain", "category", "transactional_type", "importance", "requires_action",
        "time_sensitive", "waiting_on", "action_type", "suggested_disposition",
        "sender_domain", "organization", "account", "source", "sent_at", "deadline",
        "max_sensitivity", "facts.travel", "facts.travel.origin_country",
        "facts.travel.destination_country", "facts.travel.carrier", "facts.travel.start",
        "facts.travel.end",
    }),
    aggregatable=frozenset({
        "domain", "sender_domain", "organization", "month", "category", "importance",
        "facts.travel.origin_country", "facts.travel.destination_country",
        "facts.travel.start_year",
    }),
    orders=frozenset({"priority", "newest", "oldest", "deadline"}),
    text=True,
    semantic=True,
)
SENSITIVE_POLICY = QueryPolicy(
    max_limit=100,
    filterable=frozenset({"label", "account", "source", "sent_at"}),
    aggregatable=frozenset({"label", "month", "account"}),
    orders=frozenset({"newest", "oldest"}),
    text=True,
    semantic=True,
)


def check(spec: QuerySpec, policy: QueryPolicy) -> None:
    """Raise ``ValueError`` naming the first thing in *spec* that *policy* refuses."""
    for name in spec.filters.used():
        if name not in policy.filterable:
            raise ValueError(f"filter {name!r} is not allowed on this tier")
    for name in spec.group_by:
        if name not in policy.aggregatable:
            raise ValueError(f"group_by {name!r} is not allowed on this tier")
    if spec.order is not None and spec.order not in policy.orders:
        raise ValueError(f"order {spec.order!r} is not allowed on this tier")
    if spec.text is not None and not policy.text:
        raise ValueError("text matching is not allowed on this tier")
    if spec.semantic is not None and not policy.semantic:
        raise ValueError("semantic ordering is not allowed on this tier")


@dataclass(frozen=True)
class Layout:
    """How one store's table answers the query model: each dimension's fixed SQL.

    Attributes:
        table: The qualified table.
        columns: The selected expressions, one per row key, ``id`` first.
        keys: The row keys.
        filters: Filter name -> a predicate with one ``%s`` for its value.
        ranges: Range filter name -> the expression it bounds.
        dates: The range filters whose expression is a date, not a timestamp;
            their bounds resolve to dates (see :func:`resolve_day`).
        groups: Group dimension -> its expression.
        joins: Group dimension -> a join that dimension needs.
        text: The expressions a keyword is matched in.
        sent_at: The send-time expression (``min_sent_at``/``max_sent_at``).
        newest, oldest: The keyset sort keys, NULL replaced by a value that
            sorts last in that direction.
        key_param: The placeholder a keyset key binds through.
        timestamp_key: Whether the keyset key is a timestamp, so a position's
            key must parse as one before it reaches the database.
        orders: Offset-paged order -> its ORDER BY, ending in ``id``.
        embedding: The vector expression, or ``None`` if the table has none.
        ranked: A predicate a row must meet to be ranked by ``embedding``, or
            ``None``: a semantic query matches no other row, which every other
            order and the aggregates still reach. A filter rather than an
            expression wrapped around ``embedding``, so an index on the vector
            column still serves the ordering.
    """

    table: str
    columns: tuple[str, ...]
    keys: tuple[str, ...]
    filters: Mapping[str, str]
    ranges: Mapping[str, str]
    groups: Mapping[str, str]
    text: tuple[str, ...]
    sent_at: str
    newest: str
    oldest: str
    key_param: str = "%s"
    timestamp_key: bool = False
    dates: frozenset[str] = frozenset()
    orders: Mapping[str, str] = field(default_factory=dict)
    joins: Mapping[str, str] = field(default_factory=dict)
    embedding: str | None = None
    ranked: str | None = None


def rank_case(column: str) -> str:
    """A CASE expression ranking *column*'s sensitivity level (unknown is NULL)."""
    whens = " ".join(f"WHEN '{level}' THEN {rank}" for level, rank in SENSITIVITY_RANK.items())
    return f"CASE {column} {whens} END"


def _value(v: Any) -> Any:
    """An enum member's value, else *v*: presets pass plain strings."""
    return getattr(v, "value", v)


def _like(text: str) -> str:
    """A contains-pattern matching *text* literally (``%`` and ``_`` escaped)."""
    return "%" + re.sub(r"([\\%_])", r"\\\1", text) + "%"


def _where(spec: QuerySpec, layout: Layout, now: Callable[[], datetime]) -> tuple[str, list]:
    """The WHERE clause (with its leading keyword, or empty) and its parameters."""
    clauses: list[str] = []
    params: list[Any] = []
    f = spec.filters
    for name in f.used():
        value = f.get(name)
        if name in RANGES:
            if name not in layout.ranges:
                raise ValueError(f"filter {name!r} is not available on this tier")
            expr = layout.ranges[name]
            for bound, op in ((value.since, ">="), (value.until, "<")):
                if bound is not None:
                    clauses.append(f"{expr} {op} %s")
                    params.append(
                        resolve_day(bound, end=op == "<", now=now)
                        if name in layout.dates
                        else resolve_bound(bound, now=now)
                    )
            continue
        if name not in layout.filters:
            raise ValueError(f"filter {name!r} is not available on this tier")
        clauses.append(layout.filters[name])
        if name in ANY_OF:
            values = value if isinstance(value, list) else [value]
            params.append([_value(v) for v in values])
        elif name == "organization":
            params.append(_like(value))
        elif name in _COUNTRIES:
            params.append([c.upper() for c in (value if isinstance(value, list) else [value])])
        elif name == "max_sensitivity":
            params.append(SENSITIVITY_RANK[_value(value)])
        else:
            params.append(_value(value))
    if spec.text is not None:
        if not layout.text:
            raise ValueError("text matching is not available on this tier")
        clauses.append("(" + " OR ".join(f"{e} ILIKE %s" for e in layout.text) + ")")
        params += [_like(spec.text)] * len(layout.text)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _and(where: str, clause: str) -> str:
    return f"{where} AND {clause}" if where else f" WHERE {clause}"


def _vector(embedding: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


@dataclass(frozen=True)
class Page:
    """One page of rows.

    ``next_cursor`` is the :data:`Position` the next page resumes at (``None`` on
    the last page), for the caller to encode. ``total`` is not counted for row
    queries (``None``); an aggregate counts.
    """

    rows: list[dict[str, Any]]
    next_cursor: Position | None
    total: int | None = None


@dataclass(frozen=True)
class Aggregate:
    """Per-group counts: each group has its dimensions, ``count``, ``min_sent_at``
    and ``max_sent_at``. ``total`` counts the matching items once each (a message
    naming two organizations is in two groups); ``truncated`` says more groups
    exist than the page holds."""

    groups: list[dict[str, Any]]
    total: int
    truncated: bool


@dataclass(frozen=True)
class RowQuery:
    """A built row query, and how to turn its result into a :class:`Page`."""

    sql: str
    params: list[Any]
    size: int
    offset: int | None

    def page(self, rows: list[tuple], keys: tuple[str, ...]) -> Page:
        """The page from *rows*; a keyset query selects its key last."""
        more = len(rows) > self.size
        rows = rows[: self.size]
        if self.offset is None:
            nxt: Position | None = [rows[-1][-1], rows[-1][0]] if more else None
            rows = [r[:-1] for r in rows]
        else:
            nxt = self.offset + self.size if more else None
        return Page([dict(zip(keys, r, strict=True)) for r in rows], nxt)


def build_query(
    spec: QuerySpec,
    policy: QueryPolicy,
    layout: Layout,
    position: Position | None = None,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RowQuery:
    """Build the SQL for one page of *spec*'s rows, resuming at *position*.

    ``newest``/``oldest`` page by keyset on ``(sent_at, id)``, so new mail cannot
    shift a page; the others page by offset with ``id`` as the last sort key, and
    a semantic query no deeper than the policy's ``search_depth``. One row beyond the
    page is fetched to learn whether another page exists.
    """
    check(spec, policy)
    if spec.group_by:
        raise ValueError("group_by returns counts, not rows; use aggregate")
    where, params = _where(spec, layout, now)
    size = min(spec.limit, policy.max_limit)
    select = ", ".join(layout.columns)
    order = spec.order or ("newest" if spec.semantic is None else None)
    if order is not None and order not in policy.orders:
        raise ValueError(f"order {order!r} is not allowed on this tier")
    if order in ("newest", "oldest"):
        key, op, way = (
            (layout.newest, "<", "DESC") if order == "newest" else (layout.oldest, ">", "ASC")
        )
        if position is not None:
            if not (
                isinstance(position, Sequence) and not isinstance(position, str)
                and len(position) == 2 and all(isinstance(p, str) for p in position)
                and (not layout.timestamp_key or _timestamp_key(position[0]))
            ):
                raise ValueError("invalid cursor position")
            where = _and(where, f"({key}, id) {op} ({layout.key_param}, %s)")
            params += list(position)
        sql = (
            f"SELECT {select}, ({key})::text FROM {layout.table}{where} "
            f"ORDER BY {key} {way}, id {way} LIMIT %s"
        )
        return RowQuery(sql, params + [size + 1], size, None)
    depth = policy.search_depth if order is None else None
    if position is None:
        offset = 0
    elif isinstance(position, bool) or not isinstance(position, int) or position < 0 or (
        depth is not None and position >= depth
    ):
        raise ValueError("invalid cursor position")
    else:
        offset = position
    if order is None:
        if layout.embedding is None:
            raise ValueError("semantic ordering is not available on this tier")
        if layout.ranked is not None:
            where = _and(where, layout.ranked)
        by = f"{layout.embedding} <=> %s::vector, id"
        params.append(_vector(spec.semantic or ()))
        size = min(size, depth - offset)
    elif order in layout.orders:
        by = layout.orders[order]
    else:
        raise ValueError(f"order {order!r} is not available on this tier")
    fetch = size + 1 if depth is None or offset + size < depth else size
    sql = f"SELECT {select} FROM {layout.table}{where} ORDER BY {by} LIMIT %s OFFSET %s"
    return RowQuery(sql, params + [fetch, offset], size, offset)


@dataclass(frozen=True)
class AggregateQuery:
    """A built aggregate: the grouped query and the count of matching items."""

    sql: str
    params: list[Any]
    count_sql: str
    count_params: list[Any]
    size: int
    dims: tuple[str, ...]

    def result(self, groups: list[tuple], total: int) -> Aggregate:
        """The aggregate from the grouped rows and the item count."""
        keys = (*self.dims, "count", "min_sent_at", "max_sent_at")
        return Aggregate(
            [dict(zip(keys, g, strict=True)) for g in groups[: self.size]],
            total,
            len(groups) > self.size,
        )


def build_aggregate(
    spec: QuerySpec,
    policy: QueryPolicy,
    layout: Layout,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AggregateQuery:
    """Build the SQL counting *spec*'s matches per ``group_by`` group, in group order."""
    check(spec, policy)
    if not spec.group_by:
        raise ValueError("aggregate needs group_by")
    for name, what in (("order", spec.order), ("semantic", spec.semantic)):
        if what is not None:
            raise ValueError(f"{name} does not apply to group_by")
    for name in spec.group_by:
        if name not in layout.groups:
            raise ValueError(f"group_by {name!r} is not available on this tier")
    where, params = _where(spec, layout, now)
    size = min(spec.limit, policy.max_limit)
    dims = tuple(spec.group_by)
    joins = "".join(f" {layout.joins[d]}" for d in dims if d in layout.joins)
    # Each group is aliased by its position: a dimension's name (``facts.travel.…``)
    # is not an identifier, and never reaches the SQL.
    select = ", ".join(f"{layout.groups[d]} AS g{i}" for i, d in enumerate(dims, 1))
    positions = range(1, len(dims) + 1)
    sql = (
        f"SELECT {select}, count(*), min({layout.sent_at}), max({layout.sent_at}) "
        f"FROM {layout.table}{joins}{where} GROUP BY {', '.join(map(str, positions))} "
        f"ORDER BY {', '.join(f'{i} NULLS LAST' for i in positions)} LIMIT %s"
    )
    count_sql = f"SELECT count(*) FROM {layout.table}{where}"
    return AggregateQuery(sql, params + [size + 1], count_sql, list(params), size, dims)
