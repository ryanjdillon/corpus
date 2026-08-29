"""The corpus-index MCP tools are thin, sanitized delegations to the query layer —
they pass through arguments and never reach past index_query."""

from __future__ import annotations

import pytest

from corpus import index_server


@pytest.fixture
def spy(monkeypatch):
    """Replace each index_query function with a recorder returning a canned result."""
    seen: dict[str, dict] = {}

    def make(name):
        def fn(*args, **kwargs):
            seen[name] = {"args": args, "kwargs": kwargs}
            return [{"tool": name}]

        return fn

    for name in ("action_items", "due_soon", "waiting_on", "by_domain", "summary", "stats"):
        monkeypatch.setattr(index_server.index_query, name, make(name))
    return seen


def test_action_items_passes_filters(spy):
    out = index_server.index_action_items(limit=5, domain="banking", importance="high")
    assert out == [{"tool": "action_items"}]
    assert spy["action_items"]["kwargs"] == {"limit": 5, "domain": "banking", "importance": "high"}


def test_due_soon_delegates(spy):
    assert index_server.index_due_soon(limit=9) == [{"tool": "due_soon"}]
    assert spy["due_soon"]["kwargs"] == {"limit": 9}


def test_waiting_on_delegates(spy):
    index_server.index_waiting_on(who="me", limit=3)
    assert spy["waiting_on"]["kwargs"] == {"who": "me", "limit": 3}


def test_by_domain_delegates(spy):
    index_server.index_by_domain("health", limit=4)
    assert spy["by_domain"]["args"] == ("health",)
    assert spy["by_domain"]["kwargs"] == {"limit": 4}


def test_summary_and_stats_delegate(spy):
    index_server.index_summary("gmail:personal::1")
    assert spy["summary"]["args"] == ("gmail:personal::1",)
    index_server.index_stats()
    assert "stats" in spy
