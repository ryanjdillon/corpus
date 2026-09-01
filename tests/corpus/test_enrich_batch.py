"""Exercise the batch runner with spec-bound, injected collaborators.

The runner enriches every document and audits only flagged ones, with the real
candidate gate. Collaborators are spec-bound mocks injected via fixtures, so the
doubles can't drift from the real interfaces and no I/O is touched.
"""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest

from corpus import secret_audit
from corpus.enrich_batch import run_audit, run_enrich
from corpus.enrich_store import EnrichStore
from corpus.enricher import Enricher, EnrichError
from corpus.enrichment import Category, Enrichment, SecretAudit

# Stored documents always carry meta["source"] (store.py sets it from
# Record.source), and enrichment is gated on it, so the fixtures carry one too.
MAIL = {"source": "gmail:personal"}


@pytest.fixture
def key_doc():
    """A document that trips the credential detector."""
    return ("d1", "deploy key AKIAIOSFODNN7EXAMPLE", MAIL)


@pytest.fixture
def clean_doc():
    """A document that trips no detector."""
    return ("d2", "are we still on for lunch tomorrow?", MAIL)


@pytest.fixture
def video_doc():
    """A document from a source with no enrichment policy."""
    return ("d3", "today I am going to teach you how to raise prices", {"source": "youtube:@chan"})


@pytest.fixture
def documents():
    """Wrap docs into the injected iter_documents callable (ignores its filters)."""
    return lambda *docs: (lambda **_: iter(docs))


@pytest.fixture
def store():
    m = create_autospec(EnrichStore, instance=True)
    m.enriched_ids.return_value = set()
    return m


@pytest.fixture
def enricher():
    m = create_autospec(Enricher, instance=True)
    m.model = "local"  # an __init__ attribute, so set explicitly on the spec mock
    m.enrich.return_value = Enrichment(one_line="x", abstract="y", category=Category.personal)
    return m


@pytest.fixture
def audit():
    m = create_autospec(secret_audit.audit_secrets)
    m.return_value = SecretAudit(contains_secret=True)
    return m


def test_enriches_all_audits_only_flagged(store, enricher, audit, documents, key_doc, clean_doc):
    r = run_enrich(store, documents=documents(key_doc, clean_doc), enricher=enricher, audit=audit)

    assert r == {"scanned": 2, "enriched": 2, "audited": 1, "skipped": 0, "ineligible": 0}
    assert store.save_enrichment.call_count == 2
    assert {c.args[0] for c in store.save_audit.call_args_list} == {"d1"}
    assert "aws_access_key" in store.save_audit.call_args.args[1]


def test_skips_already_enriched(store, enricher, documents, key_doc):
    store.enriched_ids.return_value = {"d1"}

    r = run_enrich(store, documents=documents(key_doc), enricher=enricher)

    assert r == {"scanned": 1, "enriched": 0, "audited": 0, "skipped": 0, "ineligible": 0}
    store.save_enrichment.assert_not_called()


def test_bad_record_is_skipped_not_fatal(store, enricher, documents, key_doc, clean_doc):
    # a per-record EnrichError must be skipped so it can't abort a long backfill
    enricher.enrich.side_effect = EnrichError("bad message")

    r = run_enrich(store, documents=documents(key_doc, clean_doc), enricher=enricher)

    assert r == {"scanned": 2, "enriched": 0, "audited": 0, "skipped": 2, "ineligible": 0}
    store.save_enrichment.assert_not_called()


def test_concurrency_one_enriches_all(store, enricher, audit, documents, key_doc, clean_doc):
    # concurrency=1 chunks one at a time; every document is still enriched
    r = run_enrich(
        store, documents=documents(key_doc, clean_doc), enricher=enricher, audit=audit, concurrency=1
    )
    assert r["enriched"] == 2


def test_streaming_refills_beyond_concurrency(store, enricher, audit, documents):
    # more documents than the pool width: every one is still enriched as slots refill
    docs = tuple((f"d{i}", "are we on for lunch?", MAIL) for i in range(7))
    r = run_enrich(store, documents=documents(*docs), enricher=enricher, audit=audit, concurrency=2)

    assert r["enriched"] == 7
    assert store.save_enrichment.call_count == 7


def test_force_reenriches_seen(store, enricher, audit, documents, key_doc):
    store.enriched_ids.return_value = {"d1"}

    run_enrich(store, documents=documents(key_doc), enricher=enricher, audit=audit, force=True)

    store.save_enrichment.assert_called_once()


def test_limit_caps_scan(store, enricher, documents, key_doc, clean_doc):
    r = run_enrich(store, documents=documents(clean_doc, key_doc), enricher=enricher, limit=1)

    assert r["scanned"] == 1
    store.save_enrichment.assert_called_once()


def test_run_audit_only_audits_candidates_without_enriching(
    store, audit, documents, key_doc, clean_doc
):
    r = run_audit(store, documents=documents(key_doc, clean_doc), audit=audit, model="local")

    assert r == {"scanned": 2, "audited": 1}
    assert {c.args[0] for c in store.save_audit.call_args_list} == {"d1"}
    store.save_enrichment.assert_not_called()


def test_run_audit_requires_model(store, documents):
    # no model given and none configured -> refuse rather than call the LLM
    with pytest.raises(ValueError):
        run_audit(store, documents=documents(), model="")


def test_undeclared_source_is_never_enriched(store, enricher, audit, documents, video_doc):
    # The whole point: a source nobody declared must not reach the model, even
    # though no filter was passed.
    r = run_enrich(store, documents=documents(video_doc), enricher=enricher, audit=audit)

    assert r == {"scanned": 1, "enriched": 0, "audited": 0, "skipped": 0, "ineligible": 1}
    enricher.enrich.assert_not_called()
    store.save_enrichment.assert_not_called()


def test_mixed_archive_enriches_only_eligible(
    store, enricher, audit, documents, key_doc, video_doc
):
    r = run_enrich(store, documents=documents(key_doc, video_doc), enricher=enricher, audit=audit)

    assert r["enriched"] == 1
    assert r["ineligible"] == 1
    assert {c.args[0] for c in store.save_enrichment.call_args_list} == {"d1"}


def test_document_without_a_source_is_ineligible(store, enricher, documents):
    # Absence of a source is absence of a declaration: deny.
    r = run_enrich(store, documents=documents(("d9", "text", {})), enricher=enricher)

    assert r["ineligible"] == 1
    enricher.enrich.assert_not_called()


def test_ineligible_is_counted_apart_from_skipped(store, enricher, documents, key_doc, video_doc):
    # "skipped" means a record that failed; conflating the two would hide either.
    enricher.enrich.side_effect = EnrichError("bad message")

    r = run_enrich(store, documents=documents(key_doc, video_doc), enricher=enricher)

    assert r["skipped"] == 1
    assert r["ineligible"] == 1


def test_explicit_ineligible_source_refuses(store, enricher, documents, video_doc):
    # An explicit --source is not a policy decision; it must not override the registry.
    with pytest.raises(ValueError, match="not declared enrichable"):
        run_enrich(
            store, source="youtube:@chan", documents=documents(video_doc), enricher=enricher
        )
    enricher.enrich.assert_not_called()


def test_explicit_eligible_source_is_allowed(store, enricher, audit, documents, key_doc):
    r = run_enrich(
        store, source="gmail:personal", documents=documents(key_doc), enricher=enricher, audit=audit
    )
    assert r["enriched"] == 1


def test_force_does_not_override_policy(store, enricher, documents, video_doc):
    # force re-enriches already-seen documents; it does not grant eligibility.
    store.enriched_ids.return_value = set()

    r = run_enrich(store, documents=documents(video_doc), enricher=enricher, force=True)

    assert r["ineligible"] == 1
    enricher.enrich.assert_not_called()


def test_run_audit_is_not_gated_by_enrichment_policy(store, audit, documents, video_doc):
    # A credential scan must not have blind spots at sources nobody enriches.
    r = run_audit(store, documents=documents(video_doc), audit=audit, model="local")

    assert r["scanned"] == 1
