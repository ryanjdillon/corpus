"""Adapter selection for ``corpus scan-gate``."""

from __future__ import annotations

import pytest

from corpus import scan_gate
from corpus.egress import envoy


def test_envoy_adapter_is_selectable():
    assert scan_gate.adapter("envoy-ext-proc") is envoy.serve


def test_configured_adapter_defaults_to_envoy():
    assert scan_gate.adapter() is envoy.serve


def test_unknown_adapter_names_the_known_ones():
    with pytest.raises(ValueError, match="envoy-ext-proc"):
        scan_gate.adapter("kong")
