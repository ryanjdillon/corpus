"""The corpus-index MCP tools are thin, sanitized delegations to the query layer —
they pass through arguments and never reach past index_query."""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest

from corpus import index_query, index_server


def _patch(monkeypatch, name):
    """Autospec ``index_query.<name>`` and splice it into the server's reference."""
    m = create_autospec(getattr(index_query, name))
    m.return_value = [{"tool": name}]
    monkeypatch.setattr(index_server.index_query, name, m)
    return m


@pytest.fixture
def action_items(monkeypatch):
    return _patch(monkeypatch, "action_items")


@pytest.fixture
def due_soon(monkeypatch):
    return _patch(monkeypatch, "due_soon")


@pytest.fixture
def waiting_on(monkeypatch):
    return _patch(monkeypatch, "waiting_on")


@pytest.fixture
def by_domain(monkeypatch):
    return _patch(monkeypatch, "by_domain")


@pytest.fixture
def summary(monkeypatch):
    return _patch(monkeypatch, "summary")


@pytest.fixture
def stats(monkeypatch):
    return _patch(monkeypatch, "stats")


def test_action_items_passes_filters(action_items):
    out = index_server.index_action_items(limit=5, domain="banking", importance="high")
    assert out == [{"tool": "action_items"}]
    assert action_items.call_args.kwargs == {"limit": 5, "domain": "banking", "importance": "high"}


def test_due_soon_delegates(due_soon):
    assert index_server.index_due_soon(limit=9) == [{"tool": "due_soon"}]
    assert due_soon.call_args.kwargs == {"limit": 9}


def test_waiting_on_delegates(waiting_on):
    index_server.index_waiting_on(who="me", limit=3)
    assert waiting_on.call_args.kwargs == {"who": "me", "limit": 3}


def test_by_domain_delegates(by_domain):
    index_server.index_by_domain("health", limit=4)
    assert by_domain.call_args.args == ("health",)
    assert by_domain.call_args.kwargs == {"limit": 4}


def test_summary_and_stats_delegate(summary, stats):
    index_server.index_summary("gmail:personal::1")
    assert summary.call_args.args == ("gmail:personal::1",)
    index_server.index_stats()
    stats.assert_called_once_with()
