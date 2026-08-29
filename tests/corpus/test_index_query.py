"""The corpus-index query layer builds sanitized, parameterized queries over the
sanitized_documents view, and the view never projects raw content or secrets."""

from __future__ import annotations

from unittest.mock import create_autospec

import psycopg
import pytest

from corpus import index_query


@pytest.fixture
def rows():
    """A spec-bound ``_rows`` double returning one canned sanitized row."""
    m = create_autospec(index_query._rows)
    m.return_value = [{"id": "gmail:personal::1", "one_line": "x"}]
    return m


@pytest.fixture
def cursor():
    """A spec-bound psycopg cursor that behaves as its own context manager."""
    cur = create_autospec(psycopg.Cursor, instance=True)
    cur.__enter__.return_value = cur
    cur.fetchall.return_value = []
    return cur


@pytest.fixture
def conn(cursor):
    """A spec-bound psycopg connection wrapping ``cursor``, as a context manager."""
    c = create_autospec(psycopg.Connection, instance=True)
    c.__enter__.return_value = c
    c.cursor.return_value = cursor
    return c


@pytest.fixture
def connect(conn):
    """A spec-bound ``psycopg.connect`` double handing back ``conn``."""
    f = create_autospec(psycopg.connect)
    f.return_value = conn
    return f


def test_action_items_filters_and_orders(rows):
    index_query.action_items(limit=10, domain="banking", importance="high", rows=rows)
    sql, params = rows.call_args.args

    assert "requires_action = true" in sql
    assert "domain = %s" in sql and "importance = %s" in sql
    assert "ORDER BY" in sql and "LIMIT %s" in sql
    assert params == ["banking", "high", 10]  # filters then limit, positionally bound


def test_action_items_no_filters(rows):
    index_query.action_items(rows=rows)
    sql, params = rows.call_args.args

    assert "requires_action = true" in sql
    assert "domain = %s" not in sql and "importance = %s" not in sql
    assert params == [50]  # just the default limit


def test_waiting_on_binds_who(rows):
    index_query.waiting_on(who="me", limit=5, rows=rows)
    sql, params = rows.call_args.args

    assert "waiting_on = %s" in sql
    assert params == ["me", 5]


def test_by_domain_binds_domain(rows):
    index_query.by_domain("health", rows=rows)
    _, params = rows.call_args.args
    assert params == ["health", 50]


def test_summary_returns_first_or_none(rows):
    rows.return_value = [{"id": "x"}]
    assert index_query.summary("x", rows=rows) == {"id": "x"}
    rows.return_value = []
    assert index_query.summary("x", rows=rows) is None


def test_view_ddl_exposes_signal_and_never_raw_content():
    ddl = index_query.view_ddl()

    # the priority signal the planner needs is present
    for col in ("one_line", "requires_action", "time_sensitive", "importance", "waiting_on", "sensitivity_level"):
        assert col in ddl
    # the trust boundary: the view must never select the raw body or secret material
    assert "content" not in ddl
    assert "secret_audit" not in ddl and "secret_candidates" not in ddl
    assert "embedding" not in ddl
    # richer detail is gated by the sensitivity floor
    assert "abstract" in ddl and "THEN NULL" in ddl


def test_due_soon_and_by_domain_shapes(rows):
    index_query.due_soon(limit=7, rows=rows)
    sql, params = rows.call_args.args
    assert "time_sensitive = true OR deadline IS NOT NULL" in sql
    assert params == [7]


def test_stats_aggregates(rows):
    def fake(sql, params):
        if "count(*) AS total" in sql:
            return [{"total": 3}]
        if "domain," in sql:
            return [{"domain": "banking", "n": 2}]
        return [{"d": "keep", "n": 3}]

    rows.side_effect = fake

    assert index_query.stats(rows=rows) == {
        "total": 3,
        "by_domain": {"banking": 2},
        "by_disposition": {"keep": 3},
    }


def test_query_layer_raises_without_index_url(monkeypatch):
    # a real query (default rows) refuses to run if the restricted DSN isn't set
    monkeypatch.setattr(index_query.settings, "index_database_url", "")
    with pytest.raises(RuntimeError, match="CORPUS_INDEX_DATABASE_URL"):
        index_query.action_items()


def test_rows_executes_against_the_index_dsn(monkeypatch, connect, conn, cursor):
    monkeypatch.setattr(index_query.settings, "index_database_url", "postgresql://ro@db/ai")
    cursor.fetchall.return_value = [{"id": "1"}]

    assert index_query._rows("SELECT 1", [], connect=connect) == [{"id": "1"}]

    connect.assert_called_once_with("postgresql://ro@db/ai", row_factory=index_query.dict_row)
    assert cursor.execute.call_args.args == ("SELECT 1", [])


def test_ensure_view_creates_view_and_grants(connect, conn, cursor):
    index_query.ensure_view("postgresql://owner@db/ai", connect=connect)

    executed = " ".join(call.args[0] for call in cursor.execute.call_args_list)
    assert "CREATE OR REPLACE VIEW" in executed
    assert "GRANT SELECT" in executed and "corpus_index_ro" in executed
    conn.commit.assert_called_once()
