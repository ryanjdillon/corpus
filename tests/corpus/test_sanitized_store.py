"""SanitizedStore lazily creates the messages table and upserts projected rows,
formatting the summary embedding as a pgvector literal. psycopg is mocked."""

from __future__ import annotations

import pytest

from corpus import sanitized_store, store_base


class _Cur:
    def __init__(self, result=None):
        self.result = result or []
        self.executed: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.result


class _Conn:
    def __init__(self, result=None):
        self._cur = _Cur(result)
        self.commits = 0

    def cursor(self, name=None):
        return self._cur

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def _store(monkeypatch, result=None):
    monkeypatch.setattr(sanitized_store.settings, "sanitized_database_url", "postgresql://x@db/ai_sanitized")
    conn = _Conn(result)
    monkeypatch.setattr(store_base.psycopg, "connect", lambda *a, **k: conn)
    return sanitized_store.SanitizedStore(), conn


def test_creates_table_and_builds_upsert(monkeypatch):
    store, conn = _store(monkeypatch)
    assert any("CREATE TABLE" in sql and "messages" in sql for sql, _ in conn._cur.executed)
    assert "ON CONFLICT (id)" in store._sql and "summary_embedding = EXCLUDED" in store._sql


def test_save_message_formats_vector_literal(monkeypatch):
    store, conn = _store(monkeypatch)
    conn._cur.executed.clear()
    store.save_message({"id": "d1", "one_line": "x"}, [0.1, 0.2])
    sql, params = conn._cur.executed[0]
    assert "INSERT INTO" in sql and "%s::vector" in sql
    assert params[0] == "d1"          # first projection column
    assert params[-1] == "[0.100000,0.200000]"  # the vector literal, last param


def test_synced_versions_maps_id_to_enriched_at(monkeypatch):
    store, _ = _store(monkeypatch, result=[("d1", "v1"), ("d2", "v2")])
    assert store.synced_versions() == {"d1": "v1", "d2": "v2"}


def test_requires_dsn(monkeypatch):
    monkeypatch.setattr(sanitized_store.settings, "sanitized_database_url", "")
    with pytest.raises(RuntimeError, match="CORPUS_SANITIZED_DATABASE_URL"):
        sanitized_store.SanitizedStore()


def test_context_manager_closes(monkeypatch):
    monkeypatch.setattr(sanitized_store.settings, "sanitized_database_url", "postgresql://x@db/ai_sanitized")
    conn = _Conn()
    closed = {"v": False}
    conn.close = lambda: closed.__setitem__("v", True)
    monkeypatch.setattr(store_base.psycopg, "connect", lambda *a, **k: conn)

    with sanitized_store.SanitizedStore() as s:
        assert s is not None
    assert closed["v"] is True
