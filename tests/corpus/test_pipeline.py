"""The per-source pipeline: stage selection, the checks before a run, and resumption.

The stages run for real against the Docker Postgres tier; only the edges are
doubles: a spec-bound fetcher yielding synthetic records, a spec-bound enricher,
and the in-process embedding endpoint.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from unittest.mock import call, create_autospec

import pytest

from corpus import sources, tiers
from corpus.config import settings
from corpus.enricher import Enricher, EnrichUnavailableError
from corpus.enrichment import Category, Enrichment
from corpus.fetchers import Fetcher
from corpus.models import Record
from corpus.pipeline import (
    DEFAULT_STAGES,
    INGEST_BATCH_SIZE,
    Options,
    StageFailedError,
    run,
    select,
)
from corpus.sanitized_store import SanitizedStore
from corpus.sources import ImapSource, PipelineOptions
from corpus.store import get_cursor
from corpus.telemetry import EnrichMetrics, SyncMetrics

SOURCE = "imap:test"
CURSOR = "1:3"


def _records() -> list[Record]:
    return [
        Record(
            source=SOURCE,
            source_uid=str(i),
            kind="email",
            from_addr=f"sender{i}@example.com",
            subject=f"invented subject {i}",
            sent_at=datetime(2026, 1, i + 1, tzinfo=UTC),
            body_text=f"an invented note about the boat club rota, week {i}",
        )
        for i in range(3)
    ]


@pytest.fixture
def fetcher() -> Fetcher:
    """A spec-bound fetcher yielding the same synthetic records on every fetch."""
    f = create_autospec(Fetcher, instance=True)
    f.source = SOURCE
    f.fetch.side_effect = lambda _cursor, known=frozenset(): iter(_records())
    f.next_cursor.return_value = CURSOR
    return f


@pytest.fixture
def enricher() -> Enricher:
    m = create_autospec(Enricher, instance=True)
    m.model = "test-model"  # an __init__ attribute, so set explicitly on the spec mock
    m.enrich_reporting.return_value = (
        Enrichment(one_line="a rota note", abstract="invented", category=Category.personal),
        False,
    )
    return m


@pytest.fixture
def default_tiers(monkeypatch):
    """The built-in tiers and the pre-file sources, whatever the environment's config file."""
    monkeypatch.setattr(settings, "config_file", "")
    monkeypatch.setattr(settings, "tiers_file", "")
    tiers.registry.cache_clear()
    sources.registry.cache_clear()
    yield
    tiers.registry.cache_clear()
    sources.registry.cache_clear()


@pytest.fixture
def stores(pg, fake_embeddings, default_tiers, monkeypatch) -> str:
    """The test database as both the source and the sanitized tier's store."""
    monkeypatch.setenv("CORPUS_SANITIZED_DATABASE_URL", pg)
    return pg


def _synced(dsn: str) -> set[str]:
    with SanitizedStore(dsn, "corpus") as store:
        return set(store.synced_state())


# --------------------------------------------------------------------------- #
# stage selection and checks (no I/O)
# --------------------------------------------------------------------------- #
def test_the_default_runs_every_stage_in_order():
    assert DEFAULT_STAGES == ("ingest", "enrich", "sync")
    assert [s.name for s in select(DEFAULT_STAGES)] == ["ingest", "enrich", "sync"]


def test_a_subset_keeps_its_order():
    assert [s.name for s in select(["ingest", "sync"])] == ["ingest", "sync"]


@pytest.mark.parametrize(
    ("names", "message"),
    [
        ([], "no stages selected"),
        (["sync", "ingest"], "must be in the order"),
        (["ingest", "ingest"], "listed twice"),
        (["ingest", "export"], "unknown stage(s) export"),
        (["ingest", "audit"], "enrich audits each document"),
    ],
)
def test_a_bad_selection_is_refused(names, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        select(names)


def test_a_source_that_may_not_be_enriched_is_refused_before_ingest(fetcher, enricher):
    with pytest.raises(ValueError, match="'imap:test' is not declared enrichable"):
        run(ImapSource(name="test", enrich=False), fetcher=fetcher, enricher=enricher)
    fetcher.fetch.assert_not_called()


def test_an_unconfigured_source_is_refused_before_ingest(fetcher, default_tiers):
    with pytest.raises(ValueError, match="unknown fetcher source: 'youtube:@chan'"):
        run("youtube:@chan", fetcher=fetcher)
    fetcher.fetch.assert_not_called()


@pytest.mark.parametrize(
    ("overrides", "resolved"),
    [
        (Options(), PipelineOptions(batch_size=20, limit=400, tier="partner")),
        (Options(limit=0), PipelineOptions(batch_size=20, limit=0, tier="partner")),
        (Options(tier="sanitized", batch_size=5), PipelineOptions(batch_size=5, limit=400)),
        (
            Options(defer_when_model_unavailable=True),
            PipelineOptions(batch_size=20, limit=400, tier="partner",
                            defer_when_model_unavailable=True),
        ),
    ],
)
def test_options_override_the_sources_one_by_one(overrides, resolved):
    entry = PipelineOptions(batch_size=20, limit=400, tier="partner")
    assert overrides.over(entry) == resolved


def test_with_neither_a_flag_nor_an_entry_value_ingest_batches_by_the_shared_default():
    assert Options().over(PipelineOptions()).batch_size == INGEST_BATCH_SIZE


def test_an_unavailable_model_fails_the_run_unless_told_otherwise():
    assert Options().over(PipelineOptions()).defer_when_model_unavailable is False


def test_a_false_flag_overrides_an_entry_that_defers():
    # False is an override, not an unset option: --fail-when-model-unavailable.
    entry = PipelineOptions(defer_when_model_unavailable=True)

    assert Options(defer_when_model_unavailable=False).over(entry).defer_when_model_unavailable \
        is False


def test_the_sources_tier_is_checked_before_ingest(fetcher, default_tiers):
    source = ImapSource(name="test", enrich=True, pipeline=PipelineOptions(tier="nope"))
    with pytest.raises(ValueError, match="no tier named 'nope'"):
        run(source, ("ingest", "sync"), fetcher=fetcher)
    fetcher.fetch.assert_not_called()


def test_an_unknown_sync_tier_is_refused_before_ingest(fetcher, default_tiers):
    with pytest.raises(ValueError, match="no tier named 'nope'"):
        run(SOURCE, ("ingest", "sync"), options=Options(tier="nope"), fetcher=fetcher)
    fetcher.fetch.assert_not_called()


def test_an_unset_sync_store_is_refused_before_ingest(fetcher, default_tiers, monkeypatch):
    monkeypatch.delenv("CORPUS_SANITIZED_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="CORPUS_SANITIZED_DATABASE_URL not set"):
        run(SOURCE, ("ingest", "sync"), fetcher=fetcher)
    fetcher.fetch.assert_not_called()


# --------------------------------------------------------------------------- #
# integration
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_every_stage_runs_in_order_with_counts(stores, fetcher, enricher):
    counts = run(SOURCE, fetcher=fetcher, enricher=enricher)

    assert list(counts) == ["ingest", "enrich", "sync"]
    assert counts["ingest"] == {"ingested": 3}
    assert (counts["enrich"]["scanned"], counts["enrich"]["enriched"]) == (3, 3)
    assert counts["sync"] == {
        "scanned": 3, "synced": 3, "skipped": 0, "reprojected": 0, "embedded": 3, "capped": 0,
        "pending": 0, "embed_stale": 0,
    }
    assert len(_synced(stores)) == 3
    assert get_cursor(SOURCE) == CURSOR


@pytest.mark.integration
def test_the_sync_stage_records_its_tiers_backlog(stores, fetcher, enricher):
    metrics = create_autospec(SyncMetrics, instance=True)

    counts = run(SOURCE, fetcher=fetcher, enricher=enricher, sync_metrics=metrics)

    # Labelled by the tier the source syncs into, which other sources share.
    metrics.record.assert_called_once_with(counts["sync"], tiers.tier("sanitized"))


@pytest.mark.integration
def test_the_limit_caps_enrich_and_sync(stores, fetcher, enricher):
    counts = run(SOURCE, options=Options(limit=2), fetcher=fetcher, enricher=enricher)

    assert counts["ingest"] == {"ingested": 3}  # ingest is uncapped
    assert counts["enrich"]["enriched"] == 2
    assert counts["sync"]["synced"] == 2


@pytest.mark.integration
def test_ingest_embeds_and_commits_in_batches_of_the_batch_size(stores, fetcher):
    counts = run(SOURCE, ("ingest",), options=Options(batch_size=1), fetcher=fetcher)

    assert counts["ingest"] == {"ingested": 3}
    # The cursor is read after each batch and once at the end: one batch per record.
    assert fetcher.next_cursor.call_count == 3 + 1


@pytest.mark.integration
def test_ingest_takes_the_whole_fetch_in_one_default_batch(stores, fetcher):
    run(SOURCE, ("ingest",), fetcher=fetcher)

    assert fetcher.next_cursor.call_count == 1 + 1


@pytest.mark.integration
def test_the_source_entry_sets_the_runs_options(stores, fetcher, enricher):
    source = ImapSource(name="test", enrich=True, pipeline=PipelineOptions(limit=1, batch_size=1))

    counts = run(source, fetcher=fetcher, enricher=enricher)

    assert counts["ingest"] == {"ingested": 3}
    assert fetcher.next_cursor.call_count == 3 + 1  # one batch per record
    assert (counts["enrich"]["enriched"], counts["sync"]["synced"]) == (1, 1)


@pytest.mark.integration
def test_a_source_that_may_not_be_enriched_still_ingests_and_syncs(stores, fetcher):
    # Only the enrich stage asks whether the source may be enriched.
    counts = run(ImapSource(name="test", enrich=False), ("ingest", "sync"), fetcher=fetcher)

    assert counts["ingest"] == {"ingested": 3}
    assert counts["sync"]["synced"] == 0  # nothing enriched, so nothing to project


@pytest.mark.integration
def test_an_option_overrides_the_source_entry(stores, fetcher, enricher):
    source = ImapSource(name="test", enrich=True, pipeline=PipelineOptions(limit=1))

    counts = run(source, options=Options(limit=2), fetcher=fetcher, enricher=enricher)

    assert (counts["enrich"]["enriched"], counts["sync"]["synced"]) == (2, 2)


@pytest.mark.integration
def test_a_failing_enrich_stops_the_run_and_keeps_the_cursor(stores, fetcher, enricher):
    enricher.enrich_reporting.side_effect = EnrichUnavailableError("endpoint unavailable")

    with pytest.raises(StageFailedError) as failed:
        run(SOURCE, fetcher=fetcher, enricher=enricher)

    assert failed.value.stage == "enrich"
    assert failed.value.counts == {"ingest": {"ingested": 3}}
    assert isinstance(failed.value.__cause__, EnrichUnavailableError)
    assert get_cursor(SOURCE) == CURSOR  # ingest's progress is committed
    assert _synced(stores) == set()  # sync never ran


@pytest.mark.integration
def test_a_re_run_after_a_failure_resumes_without_re_ingesting(stores, fetcher, enricher):
    enricher.enrich_reporting.side_effect = EnrichUnavailableError("endpoint unavailable")
    with pytest.raises(StageFailedError):
        run(SOURCE, fetcher=fetcher, enricher=enricher)
    enricher.enrich_reporting.side_effect = None

    counts = run(SOURCE, fetcher=fetcher, enricher=enricher)

    assert fetcher.fetch.call_args.args == (CURSOR,)  # resumed from the committed cursor
    assert counts["ingest"] == {"ingested": 0}
    assert counts["enrich"]["enriched"] == 3
    assert counts["sync"]["synced"] == 3


@pytest.mark.integration
def test_a_second_clean_run_does_no_work(stores, fetcher, enricher):
    run(SOURCE, fetcher=fetcher, enricher=enricher)
    enricher.enrich_reporting.reset_mock()

    counts = run(SOURCE, fetcher=fetcher, enricher=enricher)

    assert counts["ingest"] == {"ingested": 0}
    assert counts["enrich"]["enriched"] == 0
    enricher.enrich_reporting.assert_not_called()
    assert (counts["sync"]["synced"], counts["sync"]["skipped"]) == (0, 3)


@pytest.mark.integration
def test_the_sync_stage_re_embeds_stale_rows_up_to_the_setting(
    stores, fetcher, enricher, monkeypatch
):
    run(SOURCE, fetcher=fetcher, enricher=enricher)
    with SanitizedStore(stores, "corpus") as store:  # as a re-projection leaves them
        store._write("UPDATE corpus.messages SET embedded_policy_hash = NULL")
    monkeypatch.setattr(settings, "sync_reembed_limit", 2)

    counts = run(SOURCE, ("sync",))

    assert (counts["sync"]["embedded"], counts["sync"]["embed_stale"]) == (2, 1)


@pytest.fixture
def enrich_metrics() -> EnrichMetrics:
    return create_autospec(EnrichMetrics, instance=True)


@pytest.mark.integration
def test_the_enrich_stage_records_its_success_and_what_the_next_run_would_send(
    stores, fetcher, enricher, enrich_metrics
):
    # Capped by the limit is still a success; the one left is the backlog.
    run(SOURCE, options=Options(limit=2), fetcher=fetcher, enricher=enricher,
        enrich_metrics=enrich_metrics)

    enrich_metrics.record_outcome.assert_called_once_with(SOURCE, None)
    enrich_metrics.record_backlog.assert_called_once_with(SOURCE, 1)
    enricher.enrich_reporting.reset_mock()
    enrich_metrics.reset_mock()

    counts = run(SOURCE, fetcher=fetcher, enricher=enricher, enrich_metrics=enrich_metrics)

    # The backlog was exactly what this run sent.
    assert enricher.enrich_reporting.call_count == counts["enrich"]["enriched"] == 1
    enrich_metrics.record_outcome.assert_called_once_with(SOURCE, None)
    enrich_metrics.record_backlog.assert_called_once_with(SOURCE, 0)


@pytest.mark.integration
def test_an_unavailable_model_is_recorded_before_the_run_fails(
    stores, fetcher, enricher, enrich_metrics, caplog
):
    enricher.enrich_reporting.side_effect = EnrichUnavailableError("down", "http_503")

    with pytest.raises(StageFailedError) as failed, caplog.at_level("WARNING"):
        run(SOURCE, fetcher=fetcher, enricher=enricher, enrich_metrics=enrich_metrics)

    assert failed.value.stage == "enrich"
    # The outcome before the backlog: it needs no database, so a failing
    # backlog query still leaves the reason recorded.
    assert enrich_metrics.mock_calls == [
        call.record_outcome(SOURCE, "http_503"),
        call.record_backlog(SOURCE, 3),
    ]
    assert "enrichment unavailable: http_503 (backlog 3)" in caplog.text


@pytest.mark.integration
def test_a_bad_key_fails_the_run_even_when_it_defers(
    stores, fetcher, enricher, enrich_metrics, caplog
):
    # A 401 never heals by waiting: deferring it would hide a run that cannot succeed.
    enricher.enrich_reporting.side_effect = EnrichUnavailableError("no", "http_401")

    with pytest.raises(StageFailedError) as failed, caplog.at_level("WARNING"):
        run(SOURCE, options=Options(defer_when_model_unavailable=True), fetcher=fetcher,
            enricher=enricher, enrich_metrics=enrich_metrics)

    assert failed.value.stage == "enrich"
    assert isinstance(failed.value.__cause__, EnrichUnavailableError)
    assert enrich_metrics.mock_calls == [
        call.record_outcome(SOURCE, "http_401"),
        call.record_backlog(SOURCE, 3),
    ]
    assert "enrichment unavailable: http_401 (backlog 3)" in caplog.text
    assert _synced(stores) == set()  # sync never ran


@pytest.mark.integration
def test_a_deferred_enrichment_goes_on_to_sync(
    stores, fetcher, enricher, enrich_metrics, caplog, monkeypatch
):
    # One at a time, so the first document is persisted before the model goes away.
    monkeypatch.setattr(settings, "enrich_concurrency", 1)
    enricher.enrich_reporting.side_effect = [
        (Enrichment(one_line="a rota note", abstract="invented", category=Category.personal),
         False),
        EnrichUnavailableError("down", "http_429_budget"),
    ]
    source = ImapSource(
        name="test", enrich=True, pipeline=PipelineOptions(defer_when_model_unavailable=True)
    )

    with caplog.at_level("WARNING"):
        counts = run(source, fetcher=fetcher, enricher=enricher, enrich_metrics=enrich_metrics)

    assert list(counts) == ["ingest", "enrich", "sync"]
    assert (counts["enrich"]["enriched"], counts["enrich"]["deferred"]) == (1, 1)
    enrich_metrics.record_outcome.assert_called_once_with(SOURCE, "http_429_budget")
    enrich_metrics.record_backlog.assert_called_once_with(SOURCE, 2)
    # Sync projected what was enriched before the model went away.
    assert (counts["sync"]["synced"], counts["sync"]["embedded"]) == (1, 1)
    assert len(_synced(stores)) == 1
    assert "enrichment deferred: http_429_budget (backlog 2)" in caplog.text


@pytest.mark.integration
def test_deferring_does_not_hide_any_other_enrich_failure(
    stores, fetcher, enricher, enrich_metrics
):
    enricher.enrich_reporting.side_effect = RuntimeError("a bug, not an outage")

    with pytest.raises(StageFailedError) as failed:
        run(SOURCE, options=Options(defer_when_model_unavailable=True), fetcher=fetcher,
            enricher=enricher, enrich_metrics=enrich_metrics)

    assert failed.value.stage == "enrich"
    assert isinstance(failed.value.__cause__, RuntimeError)
    assert _synced(stores) == set()
    enrich_metrics.record_outcome.assert_not_called()
