"""The corpus-index query layer builds sanitized, parameterized queries over the
sanitized_documents view, and the view never projects raw content or secrets."""

from __future__ import annotations

import pytest

from corpus import index_query


@pytest.fixture
def capture():
    """An injectable `rows` that records (sql, params) and returns a canned row."""
    calls: list[tuple[str, list]] = []

    def rows(sql, params):
        calls.append((sql, params))
        return [{"id": "gmail:personal::1", "one_line": "x"}]

    rows.calls = calls
    return rows


def test_action_items_filters_and_orders(capture):
    index_query.action_items(limit=10, domain="banking", importance="high", rows=capture)
    sql, params = capture.calls[0]

    assert "requires_action = true" in sql
    assert "domain = %s" in sql and "importance = %s" in sql
    assert "ORDER BY" in sql and "LIMIT %s" in sql
    assert params == ["banking", "high", 10]  # filters then limit, positionally bound


def test_action_items_no_filters(capture):
    index_query.action_items(rows=capture)
    sql, params = capture.calls[0]

    assert "requires_action = true" in sql
    assert "domain = %s" not in sql and "importance = %s" not in sql
    assert params == [50]  # just the default limit


def test_waiting_on_binds_who(capture):
    index_query.waiting_on(who="me", limit=5, rows=capture)
    sql, params = capture.calls[0]

    assert "waiting_on = %s" in sql
    assert params == ["me", 5]


def test_by_domain_binds_domain(capture):
    index_query.by_domain("health", rows=capture)
    _, params = capture.calls[0]
    assert params == ["health", 50]


def test_summary_returns_first_or_none():
    assert index_query.summary("x", rows=lambda s, p: [{"id": "x"}]) == {"id": "x"}
    assert index_query.summary("x", rows=lambda s, p: []) is None


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


def test_due_soon_and_by_domain_shapes(capture):
    index_query.due_soon(limit=7, rows=capture)
    sql, params = capture.calls[0]
    assert "time_sensitive = true OR deadline IS NOT NULL" in sql
    assert params == [7]


def test_stats_aggregates():
    def rows(sql, params):
        if "count(*) AS total" in sql:
            return [{"total": 3}]
        if "domain," in sql:
            return [{"domain": "banking", "n": 2}]
        return [{"d": "keep", "n": 3}]

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


class _FakeCursor:
    def __init__(self, result):
        self._result = result
        self.executed: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._result


class _FakeConn:
    def __init__(self, result):
        self.cur = _FakeCursor(result)
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True


def test_rows_executes_against_the_index_dsn(monkeypatch):
    monkeypatch.setattr(index_query.settings, "index_database_url", "postgresql://ro@db/ai")
    conn = _FakeConn([{"id": "1"}])
    monkeypatch.setattr(index_query.psycopg, "connect", lambda *a, **k: conn)

    assert index_query._rows("SELECT 1", []) == [{"id": "1"}]
    assert conn.cur.executed[0][0] == "SELECT 1"


def test_ensure_view_creates_view_and_grants(monkeypatch):
    conn = _FakeConn([])
    monkeypatch.setattr(index_query.psycopg, "connect", lambda *a, **k: conn)

    index_query.ensure_view("postgresql://owner@db/ai")

    executed = " ".join(sql for sql, _ in conn.cur.executed)
    assert "CREATE OR REPLACE VIEW" in executed
    assert "GRANT SELECT" in executed and "corpus_index_ro" in executed
    assert conn.committed
