"""Purge stored IMAP mail from the folders a source's folder policy skips.

The IMAP fetcher skips junk, trash and drafts folders when it discovers an
account's folders (``fetchers.imap.FolderPolicy``). Mail ingested from them
before that, or before a folder was added to ``exclude_folders``, stays stored
and served until it is removed. This finds it by the folder in each document's
id (``corpus.id/v1``: ``imap:<mailbox>::<folder>:<uidvalidity>:<uid>``), judged by
the source's current policy against the server's current folder list, and
deletes it from every store that holds it.

The plan reports counts only (per source and per folder), never ids or content.
Deletion takes the derived copies first (each projected tier's rows, the vault
files) and the source documents last, so a run that fails part way finds the
same documents again on the next run, and a repeat deletes nothing.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import sources as configured
from . import tiers as configured_tiers
from . import vault
from .config import settings
from .fetchers.imap import ImapFetcher
from .models import parse_id
from .sanitized_store import SanitizedStore
from .sources import ImapSource
from .store import DocumentStore
from .tiers import Tier

log = logging.getLogger("corpus.purge")


@dataclass(frozen=True)
class Plan:
    """What purging one source deletes.

    Attributes:
        source: The source id, e.g. ``imap:example``.
        folders: Each excluded folder that has stored documents, mapped to
            their ids, in name order.
    """

    source: str
    folders: Mapping[str, tuple[str, ...]]

    @property
    def ids(self) -> list[str]:
        """Every document id the plan deletes."""
        return [doc_id for ids in self.folders.values() for doc_id in ids]


def imap_sources(source_id: str | None) -> list[ImapSource]:
    """The IMAP source *source_id*, or every declared IMAP source when it is ``None``.

    Raises:
        ValueError: *source_id* is not a configured IMAP source, or none is
            given and the config file declares no IMAP source.
    """
    if source_id is not None:
        entry = configured.source(source_id)
        if not isinstance(entry, ImapSource):
            raise ValueError(f"{source_id!r} is not an IMAP source")
        return [entry]
    found = [s for s in configured.sources() if isinstance(s, ImapSource)]
    if not found:
        raise ValueError(
            "the config file's sources: declares no IMAP source; name one, e.g. imap:example"
        )
    return found


def projected_tiers() -> list[Tier]:
    """Every projected tier, each with its store DSN set.

    Raises:
        RuntimeError: A projected tier's DSN variable is unset: its rows could
            not be purged, so nothing is.
    """
    found = [t for t in configured_tiers.tiers() if t.projection is not None]
    for t in found:
        t.store_dsn()
    return found


def plan(
    source: ImapSource,
    documents: DocumentStore,
    *,
    make_fetcher: Callable[[ImapSource], ImapFetcher] = ImapFetcher,
) -> Plan:
    """The stored documents of *source* whose folder its policy now skips.

    Connects to the server only when the source has stored documents, to read
    its folders' special-use attributes.
    """
    stored: dict[str, list[str]] = defaultdict(list)
    for doc_id in documents.existing_ids(source.id):
        try:
            folder = parse_id(doc_id).imap_parts()[0]
        except ValueError:  # outside corpus.id/v1: names no folder to judge
            continue
        stored[folder].append(doc_id)
    if not stored:
        return Plan(source.id, {})
    fetcher = make_fetcher(source)
    excluded = fetcher.policy.excluded(stored, fetcher.list_folders())
    return Plan(source.id, {f: tuple(sorted(stored[f])) for f in sorted(excluded)})


def vault_files(ids: Iterable[str], root: str | Path | None = None) -> list[Path]:
    """The vault files ``corpus export`` wrote for *ids* that exist."""
    return [p for p in (vault.vault_path(doc_id, root) for doc_id in ids) if p.is_file()]


def apply(
    plans: Sequence[Plan],
    documents: DocumentStore,
    tiers: Mapping[str, SanitizedStore],
    *,
    vault_root: str | Path | None = None,
) -> dict[str, int]:
    """Delete what *plans* name from every store; returns the count deleted per store.

    The keys are ``tier:<name>`` for each tier in *tiers* (its rows), ``vault``
    (files), and ``enrichments`` and ``documents`` (the sensitive store). Each store
    deletes in one transaction of its own. A vault file that resolves outside the
    vault root (through a symlink, say) is left alone, with a warning.
    """
    ids = [doc_id for p in plans for doc_id in p.ids]
    # The stores are separate databases, so no transaction spans them. Tiers go
    # first: ``project()`` does not remove orphans, so a tier row left behind by
    # a sensitive-first run that failed would never be planned again. Tiers-first
    # fails visibly, leaves the documents to plan from, and a re-run finishes.
    counts = {f"tier:{name}": store.delete(ids) for name, store in tiers.items()}
    root = Path(vault_root if vault_root is not None else settings.vault_path).resolve()
    removed = 0
    for path in vault_files(ids, vault_root):
        if not path.resolve().is_relative_to(root):
            log.warning("purge: not removing %s: it resolves outside the vault", path)
            continue
        path.unlink(missing_ok=True)
        removed += 1
    counts["vault"] = removed
    counts.update(documents.delete(ids))
    log.info("purged %d documents in excluded folders", counts["documents"])
    return counts
