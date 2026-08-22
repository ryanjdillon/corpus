"""Shared fetcher helpers."""

from __future__ import annotations

from corpus.fetchers.base import as_text


def test_as_text_passthrough_and_strip():
    assert as_text("hello") == "hello"
    assert as_text("  hi  ") == "hi"


def test_as_text_joins_list():
    # mailparser can return a duplicated header (e.g. Subject) as a list.
    assert as_text(["a", "b"]) == "a b"


def test_as_text_empty_is_none():
    assert as_text(None) is None
    assert as_text("") is None
    assert as_text("   ") is None
    assert as_text([]) is None
