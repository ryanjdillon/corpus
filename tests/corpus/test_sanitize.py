"""Cover the projection (raw-email drop, sensitivity gating) and the sync loop.

The sync loop projects, embeds, and upserts, skipping rows unchanged since last sync.
"""

from __future__ import annotations

from unittest.mock import create_autospec

import psycopg
import pytest

from corpus import sanitize
from corpus.embeddings import Embedder
from corpus.sanitized_store import SanitizedStore


@pytest.fixture
def store():
    m = create_autospec(SanitizedStore, instance=True)
    m.synced_versions.return_value = {}
    return m


@pytest.fixture
def embedder():
    m = create_autospec(Embedder, instance=True)
    m.embed.side_effect = lambda texts: [[0.1, 0.2] for _ in texts]
    return m


@pytest.fixture
def documents():
    """Wrap canned (id, meta, enrichment, enriched_at) tuples into the injected reader."""
    return lambda *docs: (lambda read_dsn, source=None: iter(docs))


def test_sender_domain_is_org_not_person():
    assert sanitize._sender_domain("bob@gmail.com") == "gmail.com"
    assert sanitize._sender_domain("Bob <b@Chase.COM>") == "chase.com"
    assert sanitize._sender_domain(None) is None
    assert sanitize._sender_domain("no-at-sign") is None


def test_project_low_sensitivity_keeps_signal_drops_raw():
    row = sanitize.project(
        "gmail:personal::1",
        {"source": "gmail:personal", "account": "me", "from_addr": "bob@gmail.com", "subject": "raw subj"},
        {"one_line": "Boat-club faktura question", "domain": "bills", "requires_action": True,
         "action_type": "reply", "organizations": ["boat club"], "action_summary": "reply re faktura",
         "sensitivity_level": "low", "waiting_on": "them"},
    )
    assert row["one_line"] == "Boat-club faktura question"
    assert row["organizations"] == ["boat club"]
    assert row["action_summary"] == "reply re faktura"
    assert row["sender_domain"] == "gmail.com"
    assert row["domain"] == "bills" and row["waiting_on"] == "them"
    # trust boundary: raw email fields are absent entirely
    for raw in ("content", "subject", "from_addr", "body"):
        assert raw not in row


def test_project_high_sensitivity_gates_free_text():
    row = sanitize.project(
        "d1",
        {"from_addr": "a@clinic.com"},
        {"one_line": "Your test results are in", "domain": "health", "requires_action": True,
         "action_type": "reply", "organizations": ["clinic"], "action_summary": "call the clinic",
         "sensitivity_level": "high"},
    )
    assert row["one_line"].startswith("[sensitive]") and "health" in row["one_line"]
    assert "test results" not in row["one_line"]  # substance withheld
    assert row["action_summary"] is None
    assert row["organizations"] is None
    # sender_domain + classification stay (the user's choice — still connect the dots)
    assert row["sender_domain"] == "clinic.com"
    assert row["domain"] == "health" and row["requires_action"] is True


def _doc(doc_id, meta, enr, enriched_at="v1"):
    return (doc_id, meta, enr, enriched_at)


def test_run_sync_projects_embeds_upserts(store, embedder, documents):
    docs = documents(
        _doc("d1", {"from_addr": "a@b.com"}, {"one_line": "hi", "sensitivity_level": "low"}),
        _doc("d2", {"from_addr": "c@d.com"}, {"one_line": "yo", "sensitivity_level": "low"}),
    )
    r = sanitize.run_sync(store, documents=docs, embedder=embedder, read_dsn="x")

    assert r == {"scanned": 2, "synced": 2, "skipped": 0}
    assert store.save_message.call_count == 2
    embedder.embed.assert_called_once_with(["hi", "yo"])  # batched


def test_run_sync_skips_unchanged(store, embedder, documents):
    store.synced_versions.return_value = {"d1": "v1"}
    docs = documents(_doc("d1", {}, {"one_line": "x", "sensitivity_level": "low"}, "v1"))

    r = sanitize.run_sync(store, documents=docs, embedder=embedder, read_dsn="x")

    assert r == {"scanned": 1, "synced": 0, "skipped": 1}
    store.save_message.assert_not_called()


def test_run_sync_force_reprojects(store, embedder, documents):
    store.synced_versions.return_value = {"d1": "v1"}
    docs = documents(_doc("d1", {}, {"one_line": "x", "sensitivity_level": "low"}, "v1"))

    r = sanitize.run_sync(store, documents=docs, embedder=embedder, force=True, read_dsn="x")

    assert r["synced"] == 1


def test_run_sync_default_embedder(monkeypatch, store, documents):
    # exercises the `embedder or Embedder()` default without a real gateway call
    default = create_autospec(Embedder, instance=True)
    default.embed.side_effect = lambda texts: [[0.1] for _ in texts]
    monkeypatch.setattr(sanitize, "Embedder", lambda: default)
    r = sanitize.run_sync(store, documents=documents(), read_dsn="x")
    assert r == {"scanned": 0, "synced": 0, "skipped": 0}


def test_iter_enriched_joins_documents_and_enrichments(monkeypatch):
    rows = [("d1", {"source": "gmail:personal"}, {"one_line": "x"}, "v1")]
    cur = create_autospec(psycopg.Cursor, instance=True)
    cur.__iter__.return_value = iter(rows)
    conn = create_autospec(psycopg.Connection, instance=True)
    # create_autospec does not spec method return values, so wire the two
    # ``with`` context managers (connect() -> conn, conn.cursor(name=...) -> cur).
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    monkeypatch.setattr(sanitize.psycopg, "connect", lambda dsn: conn)

    out = list(sanitize.iter_enriched("dsn", source="gmail:personal"))

    assert out == rows
    sql, params = cur.execute.call_args.args
    assert "JOIN" in sql and "enrichments" in sql and "e.enrichment IS NOT NULL" in sql
    assert params == ["gmail:personal"]
