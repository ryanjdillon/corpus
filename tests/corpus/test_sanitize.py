"""Cover the projection (raw-email drop, sensitivity gating) and the sync loop.

The sync loop projects, embeds, and upserts, skipping rows unchanged since last
sync. It writes a real sanitized store, so those tests run in the Docker tier.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import create_autospec

import pytest

from corpus import sanitize
from corpus.embeddings import Embedder
from corpus.sanitized_store import SanitizedStore
from corpus.store import DocumentStore

DIM = 16  # the pg fixture's embedding dimension (conftest.TEST_DIM)
V1 = datetime(2026, 1, 1, tzinfo=UTC)
V2 = datetime(2026, 2, 1, tzinfo=UTC)


@pytest.fixture
def store(pg):
    """A real SanitizedStore on the test database (the Docker tier)."""
    with SanitizedStore(dsn=pg, schema="corpus") as sanitized:
        yield sanitized


@pytest.fixture
def embedder():
    m = create_autospec(Embedder, instance=True)
    m.embed.side_effect = lambda texts: [[0.1] * DIM for _ in texts]
    return m


@pytest.fixture
def documents():
    """Build a spec-bound DocumentStore yielding canned (id, meta, enrichment, enriched_at)."""

    def _make(*docs):
        store = create_autospec(DocumentStore, instance=True)
        store.iter_enriched.side_effect = lambda source=None: (d for d in docs)  # a generator
        return store

    return _make


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


def _doc(doc_id, meta, enr, enriched_at=V1):
    return (doc_id, meta, enr, enriched_at)


@pytest.mark.integration
def test_run_sync_projects_embeds_upserts(store, embedder, documents):
    docs = documents(
        _doc("d1", {"from_addr": "a@example.com"}, {"one_line": "hi", "sensitivity_level": "low"}),
        _doc("d2", {"from_addr": "c@example.org"}, {"one_line": "yo", "sensitivity_level": "low"}),
    )
    r = sanitize.run_sync(store, documents=docs, embedder=embedder)

    assert r == {"scanned": 2, "synced": 2, "skipped": 0}
    assert store.synced_versions() == {"d1": V1, "d2": V1}
    assert store.get("d2")["sender_domain"] == "example.org"
    embedder.embed.assert_called_once_with(["hi", "yo"])  # batched


@pytest.mark.integration
def test_run_sync_embeds_in_batches(store, embedder, documents):
    docs = documents(*(_doc(f"d{i}", {}, {"one_line": f"m{i}"}) for i in range(3)))

    r = sanitize.run_sync(store, documents=docs, embedder=embedder, batch_size=2)

    assert r["synced"] == 3
    assert [c.args[0] for c in embedder.embed.call_args_list] == [["m0", "m1"], ["m2"]]


@pytest.mark.integration
def test_run_sync_skips_unchanged(store, embedder, documents):
    doc = _doc("d1", {}, {"one_line": "x", "sensitivity_level": "low"})
    sanitize.run_sync(store, documents=documents(doc), embedder=embedder)

    r = sanitize.run_sync(store, documents=documents(doc), embedder=embedder)

    assert r == {"scanned": 1, "synced": 0, "skipped": 1}


@pytest.mark.integration
def test_run_sync_reprojects_a_re_enriched_row(store, embedder, documents):
    sanitize.run_sync(store, documents=documents(_doc("d1", {}, {"one_line": "x"})),
                      embedder=embedder)

    r = sanitize.run_sync(store, documents=documents(_doc("d1", {}, {"one_line": "y"}, V2)),
                          embedder=embedder)

    assert r["synced"] == 1
    assert (store.synced_versions(), store.get("d1")["one_line"]) == ({"d1": V2}, "y")


@pytest.mark.integration
def test_run_sync_limit_reaches_past_already_synced_rows(store, embedder, documents):
    # A capped sync must project unsynced rows even when the head of the stable
    # read order is already synced.
    rows = [_doc(f"d{i}", {}, {"one_line": f"m{i}", "sensitivity_level": "low"}) for i in range(5)]
    sanitize.run_sync(store, documents=documents(*rows[:2]), embedder=embedder)

    r = sanitize.run_sync(store, documents=documents(*rows), embedder=embedder, limit=2)

    assert r == {"scanned": 4, "synced": 2, "skipped": 2}
    assert set(store.synced_versions()) == {"d0", "d1", "d2", "d3"}


@pytest.mark.integration
def test_run_sync_force_reprojects(store, embedder, documents):
    doc = _doc("d1", {}, {"one_line": "x", "sensitivity_level": "low"})
    sanitize.run_sync(store, documents=documents(doc), embedder=embedder)

    r = sanitize.run_sync(store, documents=documents(doc), embedder=embedder, force=True)

    assert r["synced"] == 1


@pytest.mark.integration
def test_run_sync_default_embedder(monkeypatch, store, documents):
    # exercises the `embedder or Embedder()` default without a real gateway call
    default = create_autospec(Embedder, instance=True)
    default.embed.side_effect = lambda texts: [[0.1] * DIM for _ in texts]
    monkeypatch.setattr(sanitize, "Embedder", lambda: default)
    r = sanitize.run_sync(store, documents=documents())
    assert r == {"scanned": 0, "synced": 0, "skipped": 0}


@pytest.mark.integration
def test_run_sync_reads_only_the_requested_source(store, embedder, documents):
    docs = documents()

    sanitize.run_sync(store, documents=docs, embedder=embedder, source="gmail:personal")

    docs.iter_enriched.assert_called_once_with(source="gmail:personal")
