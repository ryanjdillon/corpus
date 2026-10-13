"""One resumable run per source: ingest, then enrich, then sync.

The stages are the existing batch functions (``ingest.ingest``,
``enrich_batch.run_enrich``, ``sanitize.run_sync``), each wrapped as a
:class:`Stage` with one signature and run in the fixed order of :data:`STAGES`.
The pipeline adds no state of its own: a re-run after a failure resumes because
every stage already does: ingest from its persisted cursor (and skips ids it has
stored), enrich by passing over documents already enriched or rejected, and
sync by skipping rows whose enrichment is unchanged.

The secret audit is not a stage. ``run_enrich`` already audits each document it
enriches that the detectors flag, in the same pass; ``run_audit`` re-audits every
flagged document on each call, with no record of what it has done, so as a
scheduled stage it would re-send the same documents to the model on every run.
It stays a manual command (``corpus audit-secrets``) for when the detectors or
the audit model change.

A run is for one configured source (:mod:`corpus.sources`). Its entry's
``pipeline`` options are the defaults for the run, and the caller's
:class:`Options` override them one by one.

Each stage opens and closes its own stores. The stages use different stores
(ingest and enrich the source tier, sync a projected tier too), enrichment can
run for hours, and a stage that fails releases its connections on the way out;
opening everything once would hold idle connections across the long stage and
tie the pipeline to every stage's store wiring.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from .sources import (
    INGEST_BATCH_SIZE,  # noqa: F401 - re-exported: the shared ingest default
    PipelineOptions,
    Source,
)
from .sources import source as configured_source

if TYPE_CHECKING:  # the stages import their modules when they run, keeping the CLI light
    from .enricher import Enricher
    from .fetchers import Fetcher

log = logging.getLogger("corpus.pipeline")

#: A stage's result: the counts its batch function reports.
Counts = dict[str, int]


@dataclass(frozen=True)
class Options:
    """What the caller overrides of the source's ``pipeline`` options.

    The CLI builds this from its flags. ``None`` keeps the source entry's value
    (:class:`~corpus.sources.PipelineOptions`), so a stage reads its options from
    the resolved entry and never from the command line.

    Attributes:
        limit: Passed to the stages that take one, with each one's meaning:
            enrich sends at most this many documents to the model, sync embeds
            at most this many new or re-enriched rows (its re-projection for a
            changed policy is uncapped). Ingest has no cap; it resumes from its
            cursor. ``0`` means no limit.
        batch_size: The records ingest embeds in one request, and so how often
            it commits its cursor. Smaller batches keep each request inside a
            slow embedder's or gateway's timeout.
        tier: The projected tier sync writes (one with a projection in the
            config file).
    """

    limit: int | None = None
    batch_size: int | None = None
    tier: str | None = None

    def over(self, defaults: PipelineOptions) -> PipelineOptions:
        """*defaults* with every option set here in its place (:meth:`PipelineOptions.override`)."""
        return defaults.override(**asdict(self))


@dataclass(frozen=True)
class Context:
    """Everything a stage receives.

    Attributes:
        source: The configured source; every stage is scoped to its id.
        options: The run's per-stage options: the source's, overridden.
        fetcher: The fetcher ingest reads; ``None`` builds the one for ``source``.
        enricher: The enricher used; ``None`` builds one from the settings.
    """

    source: Source
    options: PipelineOptions
    fetcher: Fetcher | None = None
    enricher: Enricher | None = None


def _ready(ctx: Context) -> None:
    """A stage with nothing to check before the run starts."""


@dataclass(frozen=True)
class Stage:
    """One step of a run.

    Attributes:
        name: The name ``--stages`` selects it by.
        run: Runs the stage for ``ctx.source`` and returns its counts. Opens and
            closes whatever stores it needs.
        check: Raises ``ValueError`` or ``RuntimeError`` if the stage cannot run
            with this context. Every selected stage is checked before the first
            one starts, so a misconfigured sync fails before hours of enrichment.
    """

    name: str
    run: Callable[[Context], Counts]
    check: Callable[[Context], None] = _ready


def _ingest(ctx: Context) -> Counts:
    from .ingest import ingest
    from .sources import build_fetcher

    fetcher = build_fetcher(ctx.source) if ctx.fetcher is None else ctx.fetcher
    return {"ingested": ingest(ctx.source.id, ctx.options.batch_size, fetcher=fetcher)}


def _check_enrich(ctx: Context) -> None:
    from .sources import require_enrichable

    require_enrichable(ctx.source)


def _enrich(ctx: Context) -> Counts:
    from .enrich_batch import run_enrich
    from .enrich_store import EnrichStore
    from .store import DocumentStore

    with EnrichStore() as store, DocumentStore() as documents:
        return run_enrich(
            store, documents=documents, source=ctx.source.id, limit=ctx.options.limit,
            enricher=ctx.enricher,
        )


def _check_sync(ctx: Context) -> None:
    from .tiers import projected

    projected(ctx.options.tier)


def _sync(ctx: Context) -> Counts:
    from .config import settings
    from .sanitize import run_sync
    from .sanitized_store import SanitizedStore
    from .store import DocumentStore
    from .tiers import projected

    dst, src = projected(ctx.options.tier)
    assert dst.projection is not None  # projected() refuses a source tier
    with SanitizedStore(dst.store_dsn(), dst.schema) as store, \
            DocumentStore(src.store_dsn(), src.schema) as documents:
        # The embedding phase takes the tier's embed-stale rows whatever their
        # source: a policy change makes every source's rows stale at once, and
        # the cap bounds the embedder's load per run either way.
        return run_sync(
            store, documents=documents, source=ctx.source.id, limit=ctx.options.limit,
            policy=dst.projection.policy, reembed_limit=settings.sync_reembed_limit,
        )


#: Every stage, in the only order they run in.
STAGES: tuple[Stage, ...] = (
    Stage("ingest", _ingest),
    Stage("enrich", _enrich, _check_enrich),
    Stage("sync", _sync, _check_sync),
)

#: The stage names, in order; a run with no selection runs them all.
DEFAULT_STAGES: tuple[str, ...] = tuple(s.name for s in STAGES)


class StageFailedError(Exception):
    """A stage raised; the run stopped there.

    Attributes:
        stage: The stage that failed.
        counts: The counts of the stages that completed before it.
    """

    def __init__(self, stage: str, counts: dict[str, Counts]) -> None:
        super().__init__(f"stage {stage!r} failed")
        self.stage = stage
        self.counts = counts


def select(names: Iterable[str]) -> tuple[Stage, ...]:
    """The stages named by *names*, which must be in :data:`STAGES` order.

    An out-of-order list is refused rather than reordered: a list written as
    ``sync,ingest`` says the writer expects an order the run does not have.

    Raises:
        ValueError: *names* is empty, names an unknown stage, repeats one, or is
            out of order.
    """
    names = tuple(names)
    if not names:
        raise ValueError("no stages selected")
    if "audit" in names:
        raise ValueError(
            "'audit' is not a stage: enrich audits each document it enriches; "
            "re-audit stored documents with 'corpus audit-secrets'"
        )
    unknown = [n for n in names if n not in DEFAULT_STAGES]
    if unknown:
        raise ValueError(
            f"unknown stage(s) {', '.join(unknown)}; stages are {', '.join(DEFAULT_STAGES)}"
        )
    if len(set(names)) != len(names):
        raise ValueError(f"stage listed twice in {','.join(names)}")
    order = [DEFAULT_STAGES.index(n) for n in names]
    if order != sorted(order):
        raise ValueError(
            f"stages must be in the order {','.join(DEFAULT_STAGES)}, not {','.join(names)}"
        )
    return tuple(STAGES[i] for i in order)


def run(
    source: str | Source,
    stages: Iterable[str] = DEFAULT_STAGES,
    *,
    options: Options | None = None,
    fetcher: Fetcher | None = None,
    enricher: Enricher | None = None,
) -> dict[str, Counts]:
    """Run the selected *stages* for *source*, in order, and return each one's counts.

    *source* is a configured source's id, or an entry itself. Its ``pipeline``
    options, with *options* over them, are the run's. Every selected stage is
    checked before any runs. A stage that raises stops the run: the counts so
    far are logged and :class:`StageFailedError` is raised from the stage's
    exception. What the earlier stages committed stays committed, and running
    again resumes.

    Raises:
        ValueError: The selection is invalid, the source is not configured, or a
            stage's check refused.
        RuntimeError: A stage's check found its configuration missing.
        StageFailedError: A stage failed while running.
    """
    selected = select(stages)
    entry = configured_source(source) if isinstance(source, str) else source
    source_id = entry.id
    resolved = (options or Options()).over(entry.pipeline)
    ctx = Context(entry, resolved, fetcher=fetcher, enricher=enricher)
    for stage in selected:
        stage.check(ctx)
    counts: dict[str, Counts] = {}
    for stage in selected:
        log.info("pipeline %s: %s", source_id, stage.name)
        try:
            counts[stage.name] = stage.run(ctx)
        except Exception as exc:
            log.error(
                "pipeline %s: %s failed (%s: %s); completed: %s",
                source_id, stage.name, type(exc).__name__, exc, counts or "none",
            )
            raise StageFailedError(stage.name, counts) from exc
        log.info("pipeline %s: %s done: %s", source_id, stage.name, counts[stage.name])
    log.info("pipeline %s complete: %s", source_id, counts)
    return counts
