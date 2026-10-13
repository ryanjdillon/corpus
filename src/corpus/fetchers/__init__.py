"""Source fetchers: each yields normalized Records for ingestion.

Which fetcher a source gets, and with what configuration, is decided by its
entry in the config file: see :mod:`corpus.sources`.
"""

from __future__ import annotations

import warnings

from .base import Fetcher

__all__ = ["Fetcher", "build_fetcher"]


def build_fetcher(source: str) -> Fetcher:
    """Deprecated: ``sources.build_fetcher(sources.source(source))``. Kept for one release.

    Construct the fetcher for a source id like ``"imap:<name>"``, resolved through
    the configured sources.
    """
    from .. import sources

    warnings.warn(
        "corpus.fetchers.build_fetcher(id) is deprecated; use "
        "corpus.sources.build_fetcher(corpus.sources.source(id))",
        DeprecationWarning,
        stacklevel=2,
    )
    kind, _, name = source.partition(":")
    return sources.build_fetcher(sources.source(f"{kind}:{name or 'default'}"))
