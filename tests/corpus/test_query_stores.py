"""The query model against real Postgres: each dimension, each order's paging, vector
ordering and group counts, on both tiers. Every row here is synthetic."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import psycopg
import pytest

from corpus import index_query
from corpus.config import settings
from corpus.models import Classification, Record
from corpus.query import (
    SANITIZED_POLICY,
    SENSITIVE_POLICY,
    Page,
    QueryPolicy,
    QuerySpec,
    parse_spec,
)
from corpus.sanitized_store import COLS, SanitizedStore
from corpus.store import DocumentStore, to_document

pytestmark = pytest.mark.integration

SCHEMA = "corpus"
NOW = datetime.now(UTC)
DIM = 16  # the pg fixture's embedding dimension (conftest.TEST_DIM)


def _vec(*head: float) -> list[float]:
    """A DIM-wide vector starting with *head*, zero-padded."""
    return [*head, *([0.0] * (DIM - len(head)))]


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _row(id_: str, vec: list[float], **overrides) -> tuple[str, dict, list[float]]:
    return id_, overrides, vec


#: Synthetic sanitized rows: (id, overrides, embedding). Ties in sent_at (t2/t3,
#: w1/w2), undated rows (n1/n2) and rows without deadlines exercise the paging.
ROWS = [
    _row("t1", _vec(1.0), domain="travel", sent_at=_at("2025-01-15T09:00"), importance="high",
         organizations=["ACME Travel AS"], one_line="Flight to Tromsø booked",
         deadline=date(2025, 1, 20), sensitivity_level="none"),
    _row("t2", _vec(0.9, 0.1), domain="travel", sent_at=_at("2025-03-02T10:00"),
         importance="low", organizations=["Fjordline"], sensitivity_level="low"),
    _row("t3", _vec(0.8, 0.2), domain="travel", sent_at=_at("2025-03-02T10:00"),
         importance="medium", sensitivity_level="medium",
         action_summary="Pay the ferry deposit"),
    _row("t4", _vec(0.7, 0.3), domain="travel", sent_at=_at("2025-12-31T23:30"),
         importance="high", time_sensitive=True, sensitivity_level="high"),
    _row("t5", _vec(0.0, 1.0), domain="travel", sent_at=_at("2024-12-31T23:59"),
         importance="low"),
    _row("t6", _vec(0.1, 0.9), domain="travel", sent_at=_at("2026-01-01T00:00"),
         importance="low"),
    _row("w1", _vec(0.0, 0.0, 1.0), domain="work", sent_at=_at("2025-03-02T10:00"),
         importance="high", requires_action=True, organizations=["acme corp"],
         deadline=date(2025, 3, 10), waiting_on="me"),
    _row("w2", _vec(0.0, 0.0, 0.9), domain="work", sent_at=_at("2025-03-02T10:00"),
         importance="high", requires_action=True, deadline=date(2025, 3, 10)),
    _row("b1", _vec(0.0, 0.0, 0.0, 1.0), domain="banking", sent_at=NOW - timedelta(days=3),
         importance="medium", deadline=(NOW + timedelta(days=5)).date(),
         sender_domain="bank.example.com", sensitivity_level=None),
    _row("b2", _vec(0.0, 0.0, 0.0, 0.9), domain="banking", sent_at=NOW - timedelta(days=30),
         importance="medium", deadline=(NOW - timedelta(days=20)).date()),
    _row("n1", _vec(0.0, 0.0, 0.0, 0.0, 1.0), domain="other", sent_at=None, importance="low"),
    _row("n2", _vec(0.0, 0.0, 0.0, 0.0, 0.9), domain="other", sent_at=None, importance="low"),
]
DEFAULTS = {
    "source": "imap:test", "account": "me@example.com", "requires_action": False,
    "time_sensitive": False, "waiting_on": "none", "suggested_disposition": "keep",
    "sensitivity_level": "none", "category": "personal", "action_type": "none",
}


@pytest.fixture
def sanitized(pg):
    """A SanitizedStore on the test schema, holding ``ROWS``."""
    with SanitizedStore(dsn=pg, schema=SCHEMA) as store:
        for id_, overrides, vec in ROWS:
            row = {c: None for c in COLS} | DEFAULTS | {"id": id_} | overrides
            store.save_message(row, vec)
        yield store


@pytest.fixture
def reader_dsn(pg, sanitized):
    """A DSN for a role that may only read the sanitized table, like the index's."""
    with psycopg.connect(pg, autocommit=True) as conn:
        conn.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'index_ro') "
            "THEN CREATE ROLE index_ro LOGIN PASSWORD 'ro'; END IF; END $$"
        )
        conn.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO index_ro")
        conn.execute(f"GRANT SELECT ON {SCHEMA}.messages TO index_ro")
    return pg.replace("postgres:test@", "index_ro:ro@")


@pytest.fixture
def reader(reader_dsn):
    with SanitizedStore(dsn=reader_dsn, schema=SCHEMA, read_only=True) as store:
        yield store


def _ids(page_or_rows) -> list[str]:
    rows = page_or_rows.rows if isinstance(page_or_rows, Page) else page_or_rows
    return [r["id"] for r in rows]


def _query(store, policy=SANITIZED_POLICY, **spec) -> list[str]:
    return _ids(store.query(parse_spec(spec), policy))


def _drain(query, spec: QuerySpec, policy: QueryPolicy) -> list[str]:
    """Every id over every page of *spec*, in order."""
    ids: list[str] = []
    position = None
    while True:
        page = query(spec, policy, position)
        ids += _ids(page)
        if page.next_cursor is None:
            return ids
        position = page.next_cursor


def test_the_index_reads_as_a_read_only_role_without_ddl(reader):
    # Construction ran no DDL: CREATE ... IF NOT EXISTS would need CREATE on the schema.
    assert _query(reader, filters={"domain": "work"}, order="priority") == ["w1", "w2"]
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        reader.save_message({"id": "x"}, _vec(1.0))


def test_any_of_lists_and_single_values(reader):
    assert sorted(_query(reader, filters={"domain": ["work", "banking"]})) == [
        "b1", "b2", "w1", "w2",
    ]
    assert _query(reader, filters={"importance": "high", "domain": "travel"}) == ["t4", "t1"]
    assert _query(reader, filters={"requires_action": True, "waiting_on": ["me", "them"]}) == [
        "w1"
    ]
    assert _query(reader, filters={"sender_domain": "bank.example.com"}) == ["b1"]


def test_organization_contains_case_insensitively(reader):
    assert sorted(_query(reader, filters={"organization": "acme"})) == ["t1", "w1"]
    assert _query(reader, filters={"organization": "FJORD"}) == ["t2"]
    assert _query(reader, filters={"organization": "%"}) == []  # matched literally


def test_text_matches_the_sanitized_free_text(reader):
    assert _query(reader, text="tromsø") == ["t1"]
    assert _query(reader, text="FERRY DEPOSIT") == ["t3"]


def test_sent_at_absolute_and_relative(reader):
    in_2025 = {"since": "2025-01-01", "until": "2026-01-01"}
    assert sorted(_query(reader, filters={"sent_at": in_2025})) == [
        "t1", "t2", "t3", "t4", "w1", "w2",
    ]
    assert _query(reader, filters={"sent_at": {"since": "7d"}}) == ["b1"]
    assert sorted(_query(reader, filters={"sent_at": {"since": "60d", "until": "7d"}})) == ["b2"]


def test_deadline_absolute_and_relative(reader):
    march = {"since": "2025-03-01", "until": "2025-04-01"}
    assert sorted(_query(reader, filters={"deadline": march})) == ["w1", "w2"]
    assert _query(reader, filters={"deadline": {"since": "1d"}}) == ["b1"]  # still ahead


def test_a_deadline_on_the_end_day_of_a_datetime_range_matches(reader):
    # deadline is a date: an end after midnight keeps its own day, a date end does not
    by_noon = {"until": "2025-03-10T12:00:00"}
    assert sorted(_query(reader, filters={"deadline": by_noon})) == ["t1", "w1", "w2"]
    assert _query(reader, filters={"deadline": {"until": "2025-03-10"}}) == ["t1"]
    from_noon = {"since": "2025-03-10T12:00:00", "until": "2025-03-11"}
    assert sorted(_query(reader, filters={"deadline": from_noon})) == ["w1", "w2"]


def test_a_garbage_keyset_position_is_refused_not_sent(reader):
    with pytest.raises(ValueError, match="invalid cursor position"):
        reader.query(QuerySpec(order="newest"), SANITIZED_POLICY, ["not a date", "t1"])


def test_max_sensitivity_is_an_ordered_ceiling_that_fails_closed(reader):
    low = _query(reader, filters={"max_sensitivity": "low", "domain": "travel"})
    assert sorted(low) == ["t1", "t2", "t5", "t6"]
    # b1 has no level: it never passes a ceiling, not even the highest.
    assert "b1" not in _query(reader, filters={"max_sensitivity": "high"}, limit=100)


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        ("newest", ["b1", "b2", "t6", "t4", "w2", "w1", "t3", "t2", "t1", "t5", "n2", "n1"]),
        ("oldest", ["t5", "t1", "t2", "t3", "w1", "w2", "t4", "t6", "b2", "b1", "n1", "n2"]),
        ("priority", ["t4", "w1", "w2", "t1", "b1", "b2", "t3", "t6", "t2", "t5", "n1", "n2"]),
        ("deadline", ["t1", "w1", "w2", "b2", "b1", "t4", "t3", "t6", "t2", "t5", "n1", "n2"]),
    ],
)
@pytest.mark.parametrize("limit", [1, 2, 5, 100])
def test_each_order_pages_every_row_exactly_once(reader, order, expected, limit):
    spec = QuerySpec(order=order, limit=limit)
    assert _drain(reader.query, spec, SANITIZED_POLICY) == expected


def test_keyset_pages_are_stable_when_mail_arrives(sanitized, reader):
    first = reader.query(QuerySpec(order="newest", limit=3), SANITIZED_POLICY)
    sanitized.save_message({c: None for c in COLS} | {"id": "z", "sent_at": NOW}, _vec(1.0))
    second = reader.query(QuerySpec(order="newest", limit=3), SANITIZED_POLICY, first.next_cursor)
    assert _ids(first) == ["b1", "b2", "t6"] and _ids(second) == ["t4", "w2", "w1"]


def test_semantic_orders_by_distance_and_pages(reader):
    spec = QuerySpec(semantic=_vec(1.0), filters=parse_spec(
        {"filters": {"domain": "travel"}}).filters, limit=2)
    assert _drain(reader.query, spec, SANITIZED_POLICY) == ["t1", "t2", "t3", "t4", "t6", "t5"]


def test_semantic_ranks_past_the_pgvector_default(pg, monkeypatch):
    # With an HNSW index forced, pgvector's default hnsw.ef_search (40) would end
    # the page at 40 rows; the store's own setting is what lets it fill.
    with SanitizedStore(dsn=pg, schema=SCHEMA) as store:
        for i in range(60):
            row = {c: None for c in COLS} | DEFAULTS | {"id": f"s{i:02}"}
            store.save_message(row, _vec(1.0, i / 60))
    with psycopg.connect(pg, autocommit=True) as conn:
        conn.execute(
            f"CREATE INDEX ON {SCHEMA}.messages USING hnsw (summary_embedding vector_cosine_ops)"
        )
        conn.execute(f"ANALYZE {SCHEMA}.messages")
    sep = "&" if "?" in pg else "?"
    dsn = f"{pg}{sep}options=-c%20enable_seqscan%3Doff"

    with SanitizedStore(dsn=dsn, schema=SCHEMA, read_only=True) as store:
        page = store.query(QuerySpec(semantic=_vec(1.0), limit=55), SANITIZED_POLICY)

    assert len(page.rows) == 55 and page.next_cursor == 55


def test_group_by_month_and_domain(reader):
    spec = parse_spec({
        "group_by": ["month", "domain"],
        "filters": {"sent_at": {"since": "2025-01-01", "until": "2026-01-01"}},
    })
    agg = reader.aggregate(spec, SANITIZED_POLICY)
    assert [(g["month"], g["domain"], g["count"]) for g in agg.groups] == [
        ("2025-01", "travel", 1), ("2025-03", "travel", 2), ("2025-03", "work", 2),
        ("2025-12", "travel", 1),
    ]
    assert (agg.total, agg.truncated) == (6, False)
    march = agg.groups[1]
    assert march["min_sent_at"] == march["max_sent_at"] == _at("2025-03-02T10:00")


def test_group_by_organization_counts_each_named_organization(reader):
    agg = reader.aggregate(
        parse_spec({"group_by": ["organization"], "filters": {"domain": "travel"}}),
        SANITIZED_POLICY,
    )
    assert [(g["organization"], g["count"]) for g in agg.groups] == [
        ("ACME Travel AS", 1), ("Fjordline", 1), (None, 4),
    ]
    assert agg.total == 6


def test_aggregate_truncates_to_the_limit(reader):
    agg = reader.aggregate(parse_spec({"group_by": ["domain"], "limit": 2}), SANITIZED_POLICY)
    assert ([g["domain"] for g in agg.groups], agg.truncated) == (["banking", "other"], True)


def test_acceptance_travel_in_2025_newest_first(reader):
    spec = parse_spec({
        "filters": {"domain": "travel", "sent_at": {"since": "2025-01-01", "until": "2026-01-01"}},
        "order": "newest",
    })
    page = reader.query(spec, SANITIZED_POLICY)
    assert _ids(page) == ["t4", "t3", "t2", "t1"]
    assert page.next_cursor is None


def test_acceptance_travel_items_per_month_in_2025(reader):
    spec = parse_spec({
        "filters": {"domain": "travel", "sent_at": {"since": "2025-01-01", "until": "2026-01-01"}},
        "group_by": ["month"],
    })
    agg = reader.aggregate(spec, SANITIZED_POLICY)
    assert {g["month"]: g["count"] for g in agg.groups} == {
        "2025-01": 1, "2025-03": 2, "2025-12": 1,
    }


def test_get_returns_the_projection_or_none(reader):
    row = reader.get("t1")
    assert set(row) == set(COLS) and row["one_line"] == "Flight to Tromsø booked"
    assert reader.get("missing") is None


def test_the_presets_run_on_the_reader(reader_dsn, monkeypatch):
    monkeypatch.setattr(settings, "index_database_url", reader_dsn)
    monkeypatch.setattr(settings, "sanitized_db_schema", SCHEMA)

    assert _ids(index_query.action_items()) == ["w1", "w2"]
    assert _ids(index_query.action_items(limit=1, offset=1)) == ["w2"]
    assert _ids(index_query.due_soon()) == ["t1", "w1", "w2", "b2", "b1", "t4"]
    assert _ids(index_query.waiting_on(who="me")) == ["w1"]
    assert _ids(index_query.by_domain("banking")) == ["b1", "b2"]
    assert index_query.summary("t3")["action_summary"] == "Pay the ferry deposit"
    assert index_query.stats() == {
        "total": 12,
        "by_domain": {"banking": 2, "other": 2, "travel": 6, "work": 2},
        "by_disposition": {"keep": 12},
    }


# --- the sensitive tier: the documents table's JSONB meta ---


def _doc(uid: str, sent_at: datetime | None, label: str, vec: list[float], subject: str = ""):
    rec = Record(
        source="imap:test", source_uid=uid, kind="email", account="me@example.com",
        from_addr="sender@example.com", subject=subject or f"note {uid}", sent_at=sent_at,
        body_text=f"synthetic body {uid}",
    )
    return to_document(rec, Classification(label=label, confidence=0.9), vec)


@pytest.fixture
def documents(pg):
    with DocumentStore() as store:
        yield store


def test_a_missing_documents_table_reads_as_empty(documents):
    assert documents.query_spec(QuerySpec(), SENSITIVE_POLICY) == Page([], None)
    agg = documents.aggregate(parse_spec({"group_by": ["label"]}), SENSITIVE_POLICY)
    assert (agg.groups, agg.total) == ([], 0)


@pytest.fixture
def stored(documents):
    documents.write([
        _doc("1", _at("2025-01-10T08:00"), "personal", _vec(1.0), "Kayak rental"),
        _doc("2", _at("2025-03-05T08:00"), "promotional", _vec(0.9, 0.1)),
        _doc("3", _at("2025-03-05T08:00"), "personal", _vec(0.5, 0.5)),
        _doc("4", None, "personal", _vec(0.0, 1.0)),
        _doc("5", None, "newsletter", _vec(0.1, 0.9)),
    ])
    return documents


@pytest.mark.parametrize(
    ("order", "expected"),
    [("newest", ["3", "2", "1", "5", "4"]), ("oldest", ["1", "2", "3", "4", "5"])],
)
@pytest.mark.parametrize("limit", [1, 2, 100])
def test_documents_keyset_pages_every_row_once(stored, order, expected, limit):
    ids = _drain(stored.query_spec, QuerySpec(order=order, limit=limit), SENSITIVE_POLICY)
    assert ids == [f"imap:test::{i}" for i in expected]


def test_documents_filters_text_and_semantic(stored):
    def ids(**spec):
        return [i.removeprefix("imap:test::") for i in _ids(
            stored.query_spec(parse_spec(spec), SENSITIVE_POLICY))]

    assert ids(filters={"label": "personal"}) == ["3", "1", "4"]
    assert ids(filters={"sent_at": {"since": "2025-03-01"}}) == ["3", "2"]
    assert ids(filters={"account": "me@example.com", "source": "imap:test"}, limit=1) == ["3"]
    assert ids(text="kayak") == ["1"]  # the subject
    assert ids(text="SYNTHETIC BODY 4") == ["4"]  # the content
    assert ids(semantic=_vec(1.0)) == ["1", "2", "3", "5", "4"]
    with pytest.raises(ValueError, match="filter 'domain' is not allowed"):
        ids(filters={"domain": "travel"})


def test_documents_group_by_month_and_label(stored):
    agg = stored.aggregate(parse_spec({"group_by": ["month", "label"]}), SENSITIVE_POLICY)
    assert [(g["month"], g["label"], g["count"]) for g in agg.groups] == [
        ("2025-01", "personal", 1), ("2025-03", "personal", 1), ("2025-03", "promotional", 1),
        (None, "newsletter", 1), (None, "personal", 1),
    ]
    assert agg.total == 5


def test_documents_dates_compare_in_utc(documents):
    # 23:30 at -05:00 on 31 March is 04:30 UTC on 1 April: as text it sorts in March
    late = datetime(2025, 3, 31, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    documents.write([
        _doc("late", late, "personal", _vec(1.0)),
        _doc("mid", _at("2025-03-15T12:00"), "personal", _vec(0.5)),
        _doc("undated", None, "personal", _vec(0.1)),
    ])

    def ids(**range_):
        spec = parse_spec({"filters": {"sent_at": range_}})
        return sorted(i.removeprefix("imap:test::") for i in _ids(
            documents.query_spec(spec, SENSITIVE_POLICY)))

    agg = documents.aggregate(parse_spec({"group_by": ["month"]}), SENSITIVE_POLICY)
    assert [(g["month"], g["count"]) for g in agg.groups] == [
        ("2025-03", 1), ("2025-04", 1), (None, 1),
    ]
    assert agg.groups[1]["min_sent_at"] == late
    assert ids(since="2025-04-01") == ["late"]
    assert ids(until="2025-04-01") == ["mid"]
    assert ids(since="2025-04-01T04:00:00Z", until="2025-04-01T05:00:00Z") == ["late"]


def test_documents_semantic_ranks_past_the_pgvector_default(pg, monkeypatch):
    # pgvector's default hnsw.ef_search (40) caps an index scan at 40 rows; force
    # the index path so the store's own setting is what lets the page fill.
    with DocumentStore() as documents:
        documents.write([_doc(str(i), None, "personal", _vec(1.0, i / 60)) for i in range(60)])
    with psycopg.connect(pg, autocommit=True) as conn:
        conn.execute(f"ANALYZE {SCHEMA}.documents")
    sep = "&" if "?" in pg else "?"
    monkeypatch.setattr(settings, "database_url", f"{pg}{sep}options=-c%20enable_seqscan%3Doff")

    with DocumentStore() as documents:
        page = documents.query_spec(QuerySpec(semantic=_vec(1.0), limit=55), SENSITIVE_POLICY)

    assert len(page.rows) == 55 and page.next_cursor == 55
