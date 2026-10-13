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
from corpus.sanitize import DEFAULT_POLICY, Policy

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
    cursor.fetchall.return_value = [("d1", "v1", "a", "p1", "p1"), ("d2", "v2", None, None, None)]
    assert store.synced_versions() == {"d1": "v1", "d2": "v2"}


def test_synced_state_carries_the_text_and_both_policies(store, cursor):
    cursor.fetchall.return_value = [
        ("d1", "v1", "Invoice due", "p2", "p1"), ("d2", "v2", None, None, None),
    ]
    assert store.synced_state() == {
        "d1": ("v1", "Invoice due", "p2", "p1"), "d2": ("v2", None, None, None),
    }


def test_the_policy_hash_is_written_but_never_served(store, cursor):
    # The sync writes it with every row; the query layout returns only COLS, and
    # a store without a projection policy does not read it at all.
    cursor.execute.reset_mock()
    store.save_message({"id": "d1", "policy_hash": "p1", "embedded_policy_hash": "p0"}, [0.1])
    sql, params = cursor.execute.call_args.args
    assert "policy_hash" in sql and params[-3:-1] == ["p1", "p0"]
    for col in ("policy_hash", "embedded_policy_hash"):
        assert col in store._update_sql
        assert col not in sanitized_store.COLS
        assert col not in store._layout.columns


def test_stale_embeddings_reads_the_newest_rows_under_the_policy(store, cursor):
    cursor.fetchall.return_value = [("d1", "Invoice due"), ("d2", None)]
    cursor.execute.reset_mock()

    assert store.stale_embeddings("p1", 2) == [("d1", "Invoice due"), ("d2", None)]
    assert store.stale_embeddings("p1") == [("d1", "Invoice due"), ("d2", None)]

    (capped_sql, capped), (_, uncapped) = (c.args for c in cursor.execute.call_args_list)
    assert "embedded_policy_hash IS DISTINCT FROM policy_hash" in capped_sql
    assert "ORDER BY sent_at DESC" in capped_sql
    assert (capped, uncapped) == (("p1", 2), ("p1", None))  # LIMIT NULL is no limit


def test_save_embeddings_writes_only_rows_whose_text_is_unchanged(store, cursor):
    cursor.rowcount = 1
    cursor.execute.reset_mock()

    assert store.save_embeddings("p1", [("d1", "x", [0.5]), ("d2", None, [0.25])]) == 2

    sql, params = cursor.execute.call_args_list[0].args
    assert "embedded_policy_hash = policy_hash" in sql
    assert "one_line IS NOT DISTINCT FROM %s" in sql
    assert params == ("[0.500000]", "d1", "p1", "x")


def test_backlog_counts_pending_and_embed_stale_rows(store, cursor):
    cursor.fetchall.return_value = [(3, 7)]
    assert store.backlog("p1") == sanitized_store.Backlog(pending=3, embed_stale=7)


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


def test_ddl_adds_new_columns_to_an_existing_table(store, cursor):
    # A table created before ``uri`` existed gains it; CREATE ... IF NOT EXISTS alone would not.
    ddl = " ".join(c.args[0] for c in cursor.execute.call_args_list)
    assert "ADD COLUMN IF NOT EXISTS uri text" in ddl
    assert "ADD COLUMN IF NOT EXISTS policy_hash text" in ddl
    # Added once, starting as a copy of policy_hash: those vectors are current.
    assert "ADD COLUMN embedded_policy_hash text" in ddl
    # Only rows with a hash are written: the rest already read NULL.
    assert "SET embedded_policy_hash = policy_hash\n            WHERE policy_hash IS NOT NULL" in ddl
    assert "uri" in store._sql


def test_a_read_only_store_runs_no_ddl(monkeypatch, conn, cursor):
    monkeypatch.setattr(store_base.psycopg, "connect", lambda *a, **k: conn)
    sanitized_store.SanitizedStore(dsn=DSN, schema="corpus", read_only=True)
    assert not any("ALTER TABLE" in c.args[0] for c in cursor.execute.call_args_list)


def test_a_store_with_a_projection_gates_each_free_text_column_on_its_digest(monkeypatch, conn):
    monkeypatch.setattr(store_base.psycopg, "connect", lambda *a, **k: conn)
    store = sanitized_store.SanitizedStore(
        dsn=DSN, schema="corpus", read_only=True, projection=DEFAULT_POLICY
    )
    table = store._full_layout.table
    current = f"policy_hash = '{DEFAULT_POLICY.digest()}'"
    for col in ("one_line", "action_summary", "organizations"):
        assert f"CASE WHEN {current} THEN {col}" in table
    assert table.endswith("FROM corpus.messages) AS messages")


def test_a_served_row_ranks_only_when_its_text_and_vector_are_current(monkeypatch, conn):
    # A filter, not a CASE around summary_embedding, so an index still orders it.
    monkeypatch.setattr(store_base.psycopg, "connect", lambda *a, **k: conn)
    store = sanitized_store.SanitizedStore(
        dsn=DSN, schema="corpus", read_only=True, projection=DEFAULT_POLICY
    )
    current = (
        f"policy_hash = '{DEFAULT_POLICY.digest()}' "
        "AND embedded_policy_hash IS NOT DISTINCT FROM policy_hash"
    )
    assert f", summary_embedding, {current} AS vector_current FROM" in store._full_layout.table
    assert store._full_layout.ranked == "vector_current"


@pytest.mark.parametrize(
    ("present", "ranked"),
    [
        ({"policy_hash", "embedded_policy_hash"},
         "embedded_policy_hash IS NOT DISTINCT FROM policy_hash"),
        ({"policy_hash"}, "true"),  # every vector predates two-phase re-projection
        ({"embedded_policy_hash"}, "embedded_policy_hash IS NOT DISTINCT FROM NULL"),
    ],
)
def test_the_ranking_predicate_reads_a_missing_hash_column_safely(present, ranked):
    assert sanitized_store._vector_current(frozenset(present)) == ranked


def test_a_store_without_a_projection_reads_the_table_itself(store):
    assert store._full_layout.table == "corpus.messages"
    assert store._full_layout.ranked == "embedded_policy_hash IS NOT DISTINCT FROM policy_hash"


def test_a_digest_that_is_not_hex_is_refused(monkeypatch, conn):
    # The digest is inlined into the served relation, so only hex may pass.
    monkeypatch.setattr(store_base.psycopg, "connect", lambda *a, **k: conn)
    policy = create_autospec(Policy, instance=True, fields=DEFAULT_POLICY.fields)
    policy.digest.return_value = "x' OR true --"
    with pytest.raises(ValueError, match="not a policy digest"):
        sanitized_store.SanitizedStore(dsn=DSN, schema="corpus", projection=policy)
