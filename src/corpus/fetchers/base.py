"""Fetcher protocol."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from ..models import Record


def as_text(value: object) -> str | None:
    """Normalize a parsed header to str|None. mailparser may return a header
    (e.g. a duplicated Subject) as a list rather than a string."""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class Fetcher(Protocol):
    """A source of Records supporting incremental sync via an opaque cursor."""

    source: str

    def fetch(self, cursor: str | None) -> Iterator[Record]:
        """Yield records newer than `cursor` (all records if cursor is None)."""
        ...

    def next_cursor(self) -> str | None:
        """Cursor to persist after a successful fetch pass."""
        ...
