"""Fetcher protocol."""

from __future__ import annotations

from typing import Iterator, Protocol

from ..models import Record


class Fetcher(Protocol):
    """A source of Records supporting incremental sync via an opaque cursor."""

    source: str

    def fetch(self, cursor: str | None) -> Iterator[Record]:
        """Yield records newer than `cursor` (all records if cursor is None)."""
        ...

    def next_cursor(self) -> str | None:
        """Cursor to persist after a successful fetch pass."""
        ...
