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
from corpus.enrichment import (
    RECOVERED_SCHEMA_VERSION,
    SCHEMA_VERSION,
    Category,
    Enrichment,
    SecretAudit,
)

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
    m.rejected_ids.return_value = set()
    return m


@pytest.fixture
def enricher():
    m = create_autospec(Enricher, instance=True)
    m.model = "local"  # an __init__ attribute, so set explicitly on the spec mock
    m.enrich_reporting.return_value = (
        Enrichment(one_line="x", abstract="y", category=Category.personal),
        False,
    )
    return m


@pytest.fixture
def audit():
    m = create_autospec(secret_audit.audit_secrets)
    m.return_value = SecretAudit(contains_secret=True)
    return m


def test_recovered_enrichment_is_stored_as_stale(store, enricher, audit, documents, clean_doc):
    # Kept, but under a version --upgrade-stale treats as not yet done.
    enricher.enrich_reporting.return_value = (
        Enrichment(one_line="x", abstract="y", category=Category.personal),
        True,
    )

    r = run_enrich(store, documents=documents(clean_doc), enricher=enricher, audit=audit)

    assert (r["enriched"], r["recovered"]) == (1, 1)
    assert store.save_enrichment.call_args.args[3] == RECOVERED_SCHEMA_VERSION
    assert RECOVERED_SCHEMA_VERSION != SCHEMA_VERSION


def test_enriches_all_audits_only_flagged(store, enricher, audit, documents, key_doc, clean_doc):
    r = run_enrich(store, documents=documents(key_doc, clean_doc), enricher=enricher, audit=audit)

    assert r == {"scanned": 2, "enriched": 2, "recovered": 0, "audited": 1, "audit_failed": 0, "skipped": 0, "ineligible": 0}
    assert store.save_enrichment.call_count == 2
    assert {c.args[0] for c in store.save_audit.call_args_list} == {"d1"}
    assert "aws_access_key" in store.save_audit.call_args.args[1]


def test_audit_uses_its_own_model_when_configured(
    store, enricher, audit, documents, key_doc, monkeypatch
):
    # Enrichment may run remotely; the audit reads the secrets the egress gate
    # redacts, so a configured audit model must be used and recorded instead.
    from corpus.config import settings

    monkeypatch.setattr(settings, "audit_model", "local-auditor")

    run_enrich(store, documents=documents(key_doc), enricher=enricher, audit=audit)

    assert audit.call_args.kwargs["model"] == "local-auditor"
    assert store.save_audit.call_args.args[3] == "local-auditor"
    assert store.save_enrichment.call_args.args[2] == "local"


def test_audit_defaults_to_the_enrichment_model(store, enricher, audit, documents, key_doc):
    run_enrich(store, documents=documents(key_doc), enricher=enricher, audit=audit)

    assert audit.call_args.kwargs["model"] == "local"


def test_run_audit_prefers_the_audit_model(store, audit, documents, key_doc, monkeypatch):
    from corpus.config import settings

    monkeypatch.setattr(settings, "audit_model", "local-auditor")
    monkeypatch.setattr(settings, "enrich_model", "remote")

    run_audit(store, documents=documents(key_doc), audit=audit)

    assert audit.call_args.kwargs["model"] == "local-auditor"


def test_audit_failure_keeps_the_enrichment_and_continues(
    store, enricher, audit, documents, key_doc, clean_doc
):
    # e.g. a document longer than the audit model's context window: the enrichment
    # is saved (so the document is not retried forever) and the run carries on.
    audit.side_effect = EnrichError("400: maximum context length exceeded")

    r = run_enrich(store, documents=documents(key_doc, clean_doc), enricher=enricher, audit=audit)

    assert r["enriched"] == 2
    assert r["audit_failed"] == 1
    assert r["audited"] == 0
    store.save_audit.assert_not_called()


def test_resume_ignores_schema_version_by_default(store, enricher, audit, documents, key_doc):
    run_enrich(store, documents=documents(key_doc), enricher=enricher, audit=audit)

    store.enriched_ids.assert_called_once_with(None)


def test_upgrade_stale_only_counts_current_schema_as_done(
    store, enricher, audit, documents, key_doc
):
    from corpus.enrichment import SCHEMA_VERSION

    run_enrich(
        store, documents=documents(key_doc), enricher=enricher, audit=audit, upgrade_stale=True
    )

    store.enriched_ids.assert_called_once_with(SCHEMA_VERSION)


def test_rejection_is_recorded_with_reason_and_model(store, enricher, documents, clean_doc):
    enricher.enrich_reporting.side_effect = EnrichError("400: context length exceeded")

    run_enrich(store, documents=documents(clean_doc), enricher=enricher)

    store.save_rejection.assert_called_once_with("d2", "400: context length exceeded", "local")


def test_rejected_documents_are_passed_over_for_the_same_model(
    store, enricher, audit, documents, key_doc, clean_doc
):
    store.rejected_ids.return_value = {"d2"}

    r = run_enrich(store, documents=documents(key_doc, clean_doc), enricher=enricher, audit=audit)

    store.rejected_ids.assert_called_once_with("local")
    assert r["enriched"] == 1
    assert {c.args[0] for c in store.save_enrichment.call_args_list} == {"d1"}


def test_retry_rejected_sends_them_again(store, enricher, audit, documents, clean_doc):
    store.rejected_ids.return_value = {"d2"}

    r = run_enrich(
        store, documents=documents(clean_doc), enricher=enricher, audit=audit, retry_rejected=True
    )

    store.rejected_ids.assert_not_called()
    assert r["enriched"] == 1


def test_long_document_audit_is_windowed_and_merged(
    store, enricher, audit, documents, monkeypatch
):
    # An audit model with a small context sees candidate-centred chunks of a long
    # document, not the whole of it, and the chunk verdicts are merged.
    from corpus.config import settings

    monkeypatch.setattr(settings, "model_options", {"local": {"context_tokens": 8192}})
    body = ("filler text " * 2000) + " key AKIAQZX3PL7RKEXAMPLE " + ("filler text " * 2000)
    body += " and another AKIAQZX3PL7RKEXAMPLE far away " + ("more text " * 2000)
    doc = ("d9", body, MAIL)

    run_enrich(store, documents=documents(doc), enricher=enricher, audit=audit)

    sent = [c.args[0] for c in audit.call_args_list]
    assert sent and all(len(t) < len(body) for t in sent)
    assert any("AKIAQZX3PL7RKEXAMPLE" in t for t in sent)
    store.save_audit.assert_called_once()


def test_an_enricher_built_by_the_run_is_closed_by_it(store, documents, monkeypatch):
    from corpus import enrich_batch

    built = create_autospec(Enricher, instance=True)
    built.model = "local"
    monkeypatch.setattr(enrich_batch, "Enricher", lambda: built)

    run_enrich(store, documents=documents())

    built.close.assert_called_once()


def test_run_audit_limit_stops_scanning(store, audit, documents, key_doc, clean_doc):
    r = run_audit(store, documents=documents(key_doc, clean_doc), audit=audit, model="local",
                  limit=1)

    assert r["scanned"] == 1


def test_skips_already_enriched(store, enricher, documents, key_doc):
    store.enriched_ids.return_value = {"d1"}

    r = run_enrich(store, documents=documents(key_doc), enricher=enricher)

    assert r == {"scanned": 1, "enriched": 0, "recovered": 0, "audited": 0, "audit_failed": 0, "skipped": 0, "ineligible": 0}
    store.save_enrichment.assert_not_called()


def test_bad_record_is_skipped_not_fatal(store, enricher, documents, key_doc, clean_doc):
    # a per-record EnrichError must be skipped so it can't abort a long backfill
    enricher.enrich_reporting.side_effect = EnrichError("bad message")

    r = run_enrich(store, documents=documents(key_doc, clean_doc), enricher=enricher)

    assert r == {"scanned": 2, "enriched": 0, "recovered": 0, "audited": 0, "audit_failed": 0, "skipped": 2, "ineligible": 0}
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


def test_limit_caps_documents_sent_to_the_model(store, enricher, documents, key_doc, clean_doc):
    r = run_enrich(store, documents=documents(clean_doc, key_doc), enricher=enricher, limit=1)

    assert r["scanned"] == 1
    store.save_enrichment.assert_called_once()


def test_limit_reaches_past_already_enriched_documents(store, enricher, audit, documents):
    # A capped daily run must enrich new mail even when the head of the stable
    # document order is already enriched.
    docs = tuple((f"d{i}", "are we on for lunch?", MAIL) for i in range(5))
    store.enriched_ids.return_value = {"d0", "d1", "d2"}

    r = run_enrich(store, documents=documents(*docs), enricher=enricher, audit=audit, limit=2)

    assert r["enriched"] == 2
    assert {c.args[0] for c in store.save_enrichment.call_args_list} == {"d3", "d4"}


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

    assert r == {"scanned": 1, "enriched": 0, "recovered": 0, "audited": 0, "audit_failed": 0, "skipped": 0, "ineligible": 1}
    enricher.enrich_reporting.assert_not_called()
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
    enricher.enrich_reporting.assert_not_called()


def test_ineligible_is_counted_apart_from_skipped(store, enricher, documents, key_doc, video_doc):
    # "skipped" means a record that failed; conflating the two would hide either.
    enricher.enrich_reporting.side_effect = EnrichError("bad message")

    r = run_enrich(store, documents=documents(key_doc, video_doc), enricher=enricher)

    assert r["skipped"] == 1
    assert r["ineligible"] == 1


def test_explicit_ineligible_source_refuses(store, enricher, documents, video_doc):
    # An explicit --source is not a policy decision; it must not override the registry.
    with pytest.raises(ValueError, match="not declared enrichable"):
        run_enrich(
            store, source="youtube:@chan", documents=documents(video_doc), enricher=enricher
        )
    enricher.enrich_reporting.assert_not_called()


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
    enricher.enrich_reporting.assert_not_called()


def test_run_audit_is_not_gated_by_enrichment_policy(store, audit, documents, video_doc):
    # A credential scan must not have blind spots at sources nobody enriches.
    r = run_audit(store, documents=documents(video_doc), audit=audit, model="local")

    assert r["scanned"] == 1
