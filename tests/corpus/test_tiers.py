"""The tier registry is the single source of truth for the storage pipeline."""

from __future__ import annotations

import pytest

from corpus import tiers


@pytest.fixture
def access():
    return {"sensitive": ["local-reader"], "sanitized": ["cloud-agent"]}


def test_registry_orders_most_sensitive_first(access):
    assert [t.name for t in tiers.tiers(access)] == ["sensitive", "sanitized"]


def test_sensitive_tier_is_a_source(access):
    sensitive = tiers.tier("sensitive", access)
    assert sensitive.projection is None
    assert sensitive.tool == "corpus-local"
    assert sensitive.access == ("local-reader",)


def test_sanitized_tier_is_projected(access):
    sanitized = tiers.tier("sanitized", access)
    assert sanitized.projection == "sanitize"
    assert sanitized.tool == "corpus-index"
    assert sanitized.access == ("cloud-agent",)


def test_a_tier_without_an_entry_grants_no_one():
    assert tiers.tier("sanitized", {}).access == ()


def test_access_is_parsed_from_json_in_the_environment(monkeypatch):
    from corpus.config import Settings

    monkeypatch.setenv("CORPUS_TIER_ACCESS", '{"sanitized": ["orchestrator", "pi"]}')
    access = Settings().tier_access
    assert tiers.tier("sanitized", access).access == ("orchestrator", "pi")
    assert tiers.tier("sensitive", access).access == ()


def test_unknown_tier_raises():
    with pytest.raises(KeyError, match="nope"):
        tiers.tier("nope")
