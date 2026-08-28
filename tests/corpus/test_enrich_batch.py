"""The batch runner enriches every document and audits only flagged ones, with the
real candidate gate. Collaborators are spec-bound mocks injected via fixtures, so
the doubles can't drift from the real interfaces and no I/O is touched."""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest

from corpus import secret_audit
from corpus.enrich_batch import run_audit, run_enrich
from corpus.enrich_store import EnrichStore
from corpus.enricher import Enricher, EnrichError
from corpus.enrichment import Category, Enrichment, SecretAudit


@pytest.fixture
def key_doc():
    """A document that trips the credential detector."""
    return ("d1", "deploy key AKIAIOSFODNN7EXAMPLE", {})


@pytest.fixture
def clean_doc():
    """A document that trips no detector."""
    return ("d2", "are we still on for lunch tomorrow?", {})


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

    assert r == {"scanned": 2, "enriched": 2, "audited": 1, "skipped": 0}
    assert store.save_enrichment.call_count == 2
    assert {c.args[0] for c in store.save_audit.call_args_list} == {"d1"}
    assert "aws_access_key" in store.save_audit.call_args.args[1]


def test_skips_already_enriched(store, enricher, documents, key_doc):
    store.enriched_ids.return_value = {"d1"}

    r = run_enrich(store, documents=documents(key_doc), enricher=enricher)

    assert r == {"scanned": 1, "enriched": 0, "audited": 0, "skipped": 0}
    store.save_enrichment.assert_not_called()


def test_bad_record_is_skipped_not_fatal(store, enricher, documents, key_doc, clean_doc):
    # a per-record EnrichError must be skipped so it can't abort a long backfill
    enricher.enrich.side_effect = EnrichError("bad message")

    r = run_enrich(store, documents=documents(key_doc, clean_doc), enricher=enricher)

    assert r == {"scanned": 2, "enriched": 0, "audited": 0, "skipped": 2}
    store.save_enrichment.assert_not_called()


def test_concurrency_one_enriches_all(store, enricher, audit, documents, key_doc, clean_doc):
    # concurrency=1 chunks one at a time; every document is still enriched
    r = run_enrich(
        store, documents=documents(key_doc, clean_doc), enricher=enricher, audit=audit, concurrency=1
    )
    assert r["enriched"] == 2


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
