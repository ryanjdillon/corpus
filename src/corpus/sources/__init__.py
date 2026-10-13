"""Sources: each mailbox corpus ingests is one entry in the config file.

A source is declared under ``sources:`` in the config file (``CORPUS_CONFIG_FILE``,
the file that also holds ``tiers:``) and validated strictly: an unknown key, a
kind outside the closed set, a name outside the corpus.id/v1 mailbox grammar or
a source declared twice is an error. Its id is ``<kind>:<name>``, the prefix of
every message reference it produces (docs/message-ids.md).

Secrets never go in the file. A source names the prefix of its credential
variables (``credentials_env_prefix``); the default, ``CORPUS_<KIND>_<NAME>``, is
the naming the fetchers used before the file had sources, so their variables
keep working unchanged.

The entry also carries the enrichment decision. Enrichment is default-deny: the
``Enrichment`` schema asks the questions of personal correspondence and drives
guided decoding, so pointed at anything else the model returns well-formed,
fabricated metadata rather than "none of this applies". ``enrich`` is therefore
required on every entry, and a document whose source is not declared enrichable
is never sent to the model.

A file without ``sources:`` keeps the behaviour from before it had any: every
well-formed ``gmail:<name>`` and ``imap:<name>`` id resolves to a source built
from the defaults, and enrichment is allowed by kind (:data:`LEGACY_ENRICH`).
That mode is deprecated and logs so once.
"""

from __future__ import annotations

import functools
import logging
import os
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, ClassVar

import msgspec

if TYPE_CHECKING:  # the adapters import their HTTP and IMAP clients only when built
    from ..fetchers.base import Fetcher

log = logging.getLogger("corpus.sources")

_Name = Annotated[str, msgspec.Meta(pattern=r"^[a-z0-9_-]{1,32}$")]
_EnvPrefix = Annotated[str, msgspec.Meta(pattern=r"^[A-Z_][A-Z0-9_]*$")]
_TierName = Annotated[str, msgspec.Meta(pattern=r"^[a-z][a-z0-9_-]*$", max_length=63)]
_Selection = Annotated[list[Annotated[str, msgspec.Meta(min_length=1)]], msgspec.Meta(min_length=1)]


class _Strict(
    msgspec.Struct, forbid_unknown_fields=True, frozen=True, kw_only=True, omit_defaults=True
):
    pass


#: Records ingest embeds per request when neither a flag nor the source entry
#: sets one; ``corpus pipeline`` and ``corpus ingest`` share it.
INGEST_BATCH_SIZE = 50


class PipelineOptions(_Strict):
    """How ``corpus pipeline`` (and ``corpus ingest``) process a source.

    Each is the default for its command-line flag, which overrides it.

    Attributes:
        batch_size: Records ingest embeds in one request and writes per batch,
            and so how often it commits its cursor. Smaller batches keep each
            request inside a slow embedder's or gateway's timeout.
        limit: The most documents enrich sends to the model and sync projects;
            ``0`` is no limit.
        tier: The projected tier sync writes.
    """

    batch_size: Annotated[int, msgspec.Meta(ge=1)] = INGEST_BATCH_SIZE
    limit: Annotated[int, msgspec.Meta(ge=0)] = 0
    tier: _TierName = "sanitized"

    def override(
        self, *, batch_size: int | None = None, limit: int | None = None, tier: str | None = None
    ) -> PipelineOptions:
        """These options with every one given in its place; ``None`` keeps this one.

        The one place an option is resolved, for ``corpus pipeline`` and
        ``corpus ingest`` alike: the flag, else the source entry's value, else
        the entry's default (:data:`INGEST_BATCH_SIZE` for the batch size).
        """
        given = {"batch_size": batch_size, "limit": limit, "tier": tier}
        return msgspec.structs.replace(self, **{k: v for k, v in given.items() if v is not None})


class Source(_Strict, tag_field="kind"):
    """One configured source. Concrete entries are one subclass per kind.

    Attributes:
        name: The mailbox name: the part of the id after the kind.
        enrich: Whether its documents may be enriched. Required: enrichment is
            a deliberate decision per source.
        credentials_env_prefix: The prefix of its credential variables; unset
            means ``CORPUS_<KIND>_<NAME>``.
        pipeline: Defaults for the pipeline's per-stage options.
    """

    kind: ClassVar[str]

    name: _Name
    enrich: bool
    credentials_env_prefix: _EnvPrefix | None = None
    pipeline: PipelineOptions = PipelineOptions()

    @property
    def id(self) -> str:
        """The source id, ``<kind>:<name>``: the prefix of every document id it stores."""
        return f"{self.kind}:{self.name}"

    @property
    def env_prefix(self) -> str:
        """The prefix its credential variables use."""
        # Upper-cased as the fetchers always have, so a name with a '-' keeps
        # the '-' in its default prefix.
        return self.credentials_env_prefix or f"CORPUS_{self.kind.upper()}_{self.name.upper()}"

    def env_name(self, key: str) -> str:
        """The variable that holds *key* (e.g. ``PASSWORD``) for this source."""
        return f"{self.env_prefix}_{key}"


class GmailOptions(_Strict):
    """What a Gmail source fetches.

    Attributes:
        labels: Label names to fetch; unset falls back to ``<prefix>_LABELS``,
            and all mail when that is unset too.
    """

    labels: _Selection | None = None


class GmailSource(Source, tag="gmail"):
    """A Gmail account over the Gmail API (``fetchers.gmail``)."""

    kind: ClassVar[str] = "gmail"
    options: GmailOptions = GmailOptions()


class ImapOptions(_Strict):
    """What an IMAP source fetches.

    Attributes:
        folders: Folders to fetch; unset falls back to ``<prefix>_FOLDERS``, and
            every selectable folder when that is unset too.
    """

    folders: _Selection | None = None


class ImapSource(Source, tag="imap"):
    """An IMAP mailbox (``fetchers.imap``)."""

    kind: ClassVar[str] = "imap"
    options: ImapOptions = ImapOptions()


#: A source entry as written in the config file: the closed set of kinds.
SourceEntry = GmailSource | ImapSource

_BY_KIND: dict[str, type[Source]] = {cls.kind: cls for cls in (GmailSource, ImapSource)}

#: The kinds there are, for messages.
KINDS: tuple[str, ...] = tuple(_BY_KIND)

#: Without ``sources:``, whether each kind may be enriched: the per-kind policy
#: from before sources were configured. Both kinds are personal correspondence,
#: the domain the enrichment schema was built for.
LEGACY_ENRICH: Mapping[str, bool] = {"gmail": True, "imap": True}


class SourceConfigError(ValueError):
    """The ``sources:`` of the config file do not fit together."""


class Registry:
    """The sources: the ones the file declares, or, without ``sources:``, the defaults.

    Args:
        declared: The file's ``sources:``; ``None`` when it has none, which
            resolves ids by the pre-file convention.
    """

    def __init__(self, declared: Sequence[Source] | None) -> None:
        self._legacy = declared is None
        self._by_id: dict[str, Source] = {}
        for s in declared or ():
            if s.id in self._by_id:
                raise SourceConfigError(f"source {s.id!r} is declared twice")
            self._by_id[s.id] = s

    @property
    def declared(self) -> bool:
        """Whether the file declares its sources (``sources:`` present, even empty)."""
        return not self._legacy

    def sources(self) -> tuple[Source, ...]:
        """The declared sources, in file order; empty without ``sources:``."""
        return tuple(self._by_id.values())

    def source(self, source_id: str) -> Source:
        """The source *source_id* (``<kind>:<name>``); raises ``ValueError`` if there is none."""
        if not self._legacy:
            try:
                return self._by_id[source_id]
            except KeyError:
                raise ValueError(
                    f"no source {source_id!r} in the config file's sources: "
                    f"(declared: {', '.join(self._by_id) or 'none'})"
                ) from None
        kind, _, name = source_id.partition(":")
        cls = _BY_KIND.get(kind)
        if cls is None:
            raise ValueError(f"unknown fetcher source: {source_id!r} (kinds: {', '.join(KINDS)})")
        try:
            return msgspec.convert({"name": name, "enrich": LEGACY_ENRICH[kind]}, cls)
        except msgspec.ValidationError:
            raise ValueError(
                f"invalid source name {name!r}: use 1-32 of a-z, 0-9, '_' and '-'"
            ) from None

    def may_enrich(self, source: Source | str | None) -> bool:
        """Whether documents from *source* may be enriched; an unknown source may not."""
        if isinstance(source, Source):
            return source.enrich
        if not source:
            return False
        if self._legacy:
            # By kind alone, as before: stored ids from before the name grammar
            # existed keep their eligibility.
            return LEGACY_ENRICH.get(source.partition(":")[0], False)
        entry = self._by_id.get(source)
        return entry is not None and entry.enrich

    def declares(self, source: str | None) -> bool:
        """Whether *source* has an entry deciding its enrichment.

        Tells a source declared ``enrich: false`` apart from one missing from
        ``sources:``: both are refused, but only the second is a likely mistake.
        Without ``sources:``, a kind with a policy (:data:`LEGACY_ENRICH`) counts
        as declared.
        """
        if not source:
            return False
        if self._legacy:
            return source.partition(":")[0] in LEGACY_ENRICH
        return source in self._by_id

    def require_enrichable(self, source: Source | str) -> None:
        """Raise ``ValueError`` saying how to allow it if *source* may not be enriched."""
        if self.may_enrich(source):
            return
        source_id = source.id if isinstance(source, Source) else source
        hint = "declare it under sources: in the config file with `enrich: true`"
        if self._legacy:
            kinds = ", ".join(k for k, v in LEGACY_ENRICH.items() if v)
            hint = f"without sources: in the config file the enrichable kinds are {kinds}; {hint}"
        raise ValueError(f"source {source_id!r} is not declared enrichable; {hint}")


@functools.cache
def registry() -> Registry:
    """The process's sources, from the config file, loaded once."""
    from ..tiers import config_path, read

    path = config_path()
    declared = read(path).sources if path else None
    if declared is None:  # logged once: the registry is loaded once
        log.warning(
            "no sources: in the config file (CORPUS_CONFIG_FILE); resolving gmail:<name> and "
            "imap:<name> from the CORPUS_<KIND>_<NAME>_* convention, enrichable by kind. "
            "Declare each source; this fallback is deprecated"
        )
    return Registry(declared)


def sources() -> tuple[Source, ...]:
    """The sources the config file declares."""
    return registry().sources()


def source(source_id: str) -> Source:
    """The configured source *source_id*; raises ``ValueError`` if there is none."""
    return registry().source(source_id)


def may_enrich(source: Source | str | None) -> bool:
    """Whether documents from *source* (an entry or an id) may be enriched."""
    return registry().may_enrich(source)


def require_enrichable(source: Source | str) -> None:
    """Raise ``ValueError`` if documents from *source* may not be enriched."""
    registry().require_enrichable(source)


def build_fetcher(source: Source, *, environ: Mapping[str, str] = os.environ) -> Fetcher:
    """The fetcher for *source*, reading its credentials from *environ*.

    Raises:
        ValueError: A credential variable the kind needs is unset.
    """
    match source:
        case GmailSource():
            from ..fetchers.gmail import GmailFetcher

            return GmailFetcher(source, environ=environ)
        case ImapSource():
            from ..fetchers.imap import ImapFetcher

            return ImapFetcher(source, environ=environ)
    raise ValueError(f"no fetcher for source kind {source.kind!r}")
