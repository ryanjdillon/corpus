"""Cover SanitizedStore table creation, projected-row upserts, and vector literals.

The psycopg boundary is spec-mocked off psycopg.Connection / psycopg.Cursor and
injected at ``store_base.psycopg.connect``; the store's own ``dsn`` argument carries
the connection string.
"""

from __future__ import annotations

from unittest.mock import create_autospec

import psycopg
import pytest

from corpus import sanitized_store, store_base

DSN = "postgresql://x@db/ai_sanitized"


@pytest.fixture
def cursor():
    cur = create_autospec(psycopg.Cursor, instance=True)
    cur.fetchall.return_value = []
    return cur


@pytest.fixture
def conn(cursor):
    c = create_autospec(psycopg.Connection, instance=True)
    # create_autospec does not spec method return values, so wire the
    # ``with conn.cursor() as cur`` context manager to the spec-bound cursor.
    c.cursor.return_value.__enter__.return_value = cursor
    return c


@pytest.fixture
def store(monkeypatch, conn):
    monkeypatch.setattr(store_base.psycopg, "connect", lambda *a, **k: conn)
    return sanitized_store.SanitizedStore(dsn=DSN, schema="corpus")


def test_creates_table_and_builds_upsert(store, cursor):
    assert any(
        "CREATE TABLE" in call.args[0] and "messages" in call.args[0]
        for call in cursor.execute.call_args_list
    )
    assert "ON CONFLICT (id)" in store._sql
    assert "summary_embedding = EXCLUDED" in store._sql


def test_save_message_formats_vector_literal(store, cursor):
    cursor.execute.reset_mock()
    store.save_message({"id": "d1", "one_line": "x"}, [0.1, 0.2])
    sql, params = cursor.execute.call_args.args
    assert "INSERT INTO" in sql and "%s::vector" in sql
    assert params[0] == "d1"          # first projection column
    assert params[-1] == "[0.100000,0.200000]"  # the vector literal, last param


def test_synced_versions_maps_id_to_enriched_at(store, cursor):
    cursor.fetchall.return_value = [("d1", "v1", "a"), ("d2", "v2", None)]
    assert store.synced_versions() == {"d1": "v1", "d2": "v2"}


def test_synced_state_carries_the_embedded_text(store, cursor):
    cursor.fetchall.return_value = [("d1", "v1", "Invoice due"), ("d2", "v2", None)]
    assert store.synced_state() == {"d1": ("v1", "Invoice due"), "d2": ("v2", None)}


def test_context_manager_closes(store, conn):
    with store as s:
        assert s is not None
    conn.close.assert_called_once()


def test_schema_parameter_names_the_table(monkeypatch, conn, cursor):
    # Several raw mailbox schemas can project into one shared sanitized schema.
    monkeypatch.setattr(store_base.psycopg, "connect", lambda *a, **k: conn)
    store = sanitized_store.SanitizedStore(dsn=DSN, schema="kbl")
    assert "INSERT INTO kbl.messages" in store._sql
    assert any("kbl.messages" in c.args[0] for c in cursor.execute.call_args_list)


def test_ddl_adds_the_uri_column_to_an_existing_table(store, cursor):
    # A table created before ``uri`` existed gains it; CREATE ... IF NOT EXISTS alone would not.
    ddl = " ".join(c.args[0] for c in cursor.execute.call_args_list)
    assert "ADD COLUMN IF NOT EXISTS uri text" in ddl
    assert "uri" in store._sql


def test_a_read_only_store_runs_no_ddl(monkeypatch, conn, cursor):
    monkeypatch.setattr(store_base.psycopg, "connect", lambda *a, **k: conn)
    sanitized_store.SanitizedStore(dsn=DSN, schema="corpus", read_only=True)
    assert not any("ALTER TABLE" in c.args[0] for c in cursor.execute.call_args_list)
