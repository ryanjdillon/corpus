"""The batch runner enriches every document and audits only flagged ones, with the
real candidate gate. The LLM and store are faked via a configurable fixture, so no
network or DB is touched."""

from __future__ import annotations

import pytest

from corpus import enrich_batch
from corpus.enrichment import Category, Enrichment, SecretAudit

# A doc with an AWS key trips the credential detector; a clean doc trips nothing.
_KEY_DOC = ("d1", "deploy key AKIAIOSFODNN7EXAMPLE", {})
_CLEAN_DOC = ("d2", "are we still on for lunch tomorrow?", {})


class FakeStore:
    def __init__(self, seen: set[str] | None = None) -> None:
        self.enrichments: dict = {}
        self.audits: dict = {}
        self._seen = seen or set()

    def enriched_ids(self) -> set[str]:
        return set(self._seen)

    def save_enrichment(self, doc_id, enrichment, model, schema_version) -> None:
        self.enrichments[doc_id] = (enrichment, model, schema_version)

    def save_audit(self, doc_id, candidates, audit, model, scan_version) -> None:
        self.audits[doc_id] = (candidates, audit, model, scan_version)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass


def _default_enrich(text: str) -> Enrichment:
    return Enrichment(one_line="x", abstract="y", category=Category.personal)


def _default_audit(text, candidates, model=None) -> SecretAudit:
    return SecretAudit(contains_secret=bool(candidates))


@pytest.fixture
def enrich_env(monkeypatch):
    """Wire the batch runner's dependencies to configurable fakes; returns a factory
    so each test can vary docs, resume state, and enrich/audit behavior."""

    def setup(docs, *, seen=None, enrich=_default_enrich, audit=_default_audit, model="local"):
        store = FakeStore(seen=seen)

        class _Enricher:
            def __init__(self):
                self.model = model

            def enrich(self, text):
                return enrich(text)

            def close(self):
                pass

        monkeypatch.setattr(
            enrich_batch.store, "iter_documents", lambda source=None, account=None: iter(docs)
        )
        monkeypatch.setattr(enrich_batch, "Enricher", lambda *a, **k: _Enricher())
        monkeypatch.setattr(enrich_batch, "EnrichStore", lambda: store)
        monkeypatch.setattr(enrich_batch, "audit_secrets", audit)
        return store

    return setup


def test_run_enrich_enriches_all_audits_only_flagged(enrich_env):
    store = enrich_env([_KEY_DOC, _CLEAN_DOC])

    r = enrich_batch.run_enrich()

    assert r == {"scanned": 2, "enriched": 2, "audited": 1}
    assert set(store.enrichments) == {"d1", "d2"}
    assert set(store.audits) == {"d1"}  # only the credential doc got an audit
    assert "aws_access_key" in store.audits["d1"][0]


def test_run_enrich_skips_already_enriched(enrich_env):
    store = enrich_env([_KEY_DOC], seen={"d1"})

    r = enrich_batch.run_enrich()

    assert r == {"scanned": 1, "enriched": 0, "audited": 0}
    assert store.enrichments == {}


def test_force_reenriches_seen(enrich_env):
    enrich_env([_KEY_DOC], seen={"d1"})
    assert enrich_batch.run_enrich(force=True)["enriched"] == 1


def test_limit_caps_scan(enrich_env):
    enrich_env([_CLEAN_DOC, _KEY_DOC])
    assert enrich_batch.run_enrich(limit=1)["scanned"] == 1


def test_run_audit_only_audits_candidates_without_enriching(enrich_env, monkeypatch):
    store = enrich_env([_KEY_DOC, _CLEAN_DOC])
    monkeypatch.setattr(enrich_batch.settings, "enrich_model", "local")

    r = enrich_batch.run_audit()

    assert r == {"scanned": 2, "audited": 1}
    assert set(store.audits) == {"d1"}
    assert store.enrichments == {}  # re-audit never enriches


def test_run_audit_requires_model(monkeypatch):
    monkeypatch.setattr(enrich_batch.settings, "enrich_model", "")
    with pytest.raises(ValueError):
        enrich_batch.run_audit()
