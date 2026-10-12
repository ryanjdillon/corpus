"""Source fetchers: each yields normalized Records for ingestion."""

from __future__ import annotations

from ..models import MAILBOX_NAME
from .base import Fetcher

__all__ = ["Fetcher", "build_fetcher"]


def build_fetcher(source: str) -> Fetcher:
    """Construct a fetcher from a source id like "imap:<name>".

    The name becomes the ``mailbox`` of every message reference the source
    produces (see docs/message-ids.md), so a name outside that grammar is refused
    here rather than producing ids no consumer can parse.
    """
    kind, _, name = source.partition(":")
    name = name or "default"
    if kind in ("imap", "gmail") and not MAILBOX_NAME.fullmatch(name):
        raise ValueError(
            f"invalid source name {name!r}: use 1-32 of a-z, 0-9, '_' and '-'"
        )
    if kind == "imap":
        from .imap import ImapFetcher

        return ImapFetcher(name)
    if kind == "gmail":
        from .gmail import GmailFetcher

        return GmailFetcher(name)
    raise ValueError(f"unknown fetcher source: {source!r}")
