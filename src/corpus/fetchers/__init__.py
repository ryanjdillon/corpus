"""Source fetchers: each yields normalized Records for ingestion."""

from __future__ import annotations

from .base import Fetcher

__all__ = ["Fetcher", "build_fetcher"]


def build_fetcher(source: str) -> Fetcher:
    """Construct a fetcher from a source id like "imap:<name>"."""
    kind, _, name = source.partition(":")
    if kind == "imap":
        from .imap import ImapFetcher

        return ImapFetcher(name or "default")
    raise ValueError(f"unknown fetcher source: {source!r}")
