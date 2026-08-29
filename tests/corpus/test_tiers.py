"""The tier registry is the single source of truth for the storage pipeline."""

from __future__ import annotations

import pytest

from corpus import tiers


def test_registry_orders_most_sensitive_first():
    assert [t.name for t in tiers.tiers()] == ["sensitive", "sanitized"]


def test_sensitive_tier_is_a_source():
    sensitive = tiers.tier("sensitive")
    assert sensitive.projection is None
    assert sensitive.tool == "corpus-local"
    assert "orchestrator" not in sensitive.access  # cloud agent denied the raw tier


def test_sanitized_tier_is_projected_and_cloud_reachable():
    sanitized = tiers.tier("sanitized")
    assert sanitized.projection == "sanitize"
    assert sanitized.tool == "corpus-index"
    assert "orchestrator" in sanitized.access


def test_unknown_tier_raises():
    with pytest.raises(KeyError, match="nope"):
        tiers.tier("nope")
