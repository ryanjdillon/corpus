"""Command-line entrypoints for the corpus service.

Subcommands: serve <tier>, pipeline, ingest, scan, scan-gate, enrich, sync,
audit-secrets, export. ``mcp`` and ``index`` are deprecated aliases of ``serve``.
"""

from __future__ import annotations

import logging

import click

from . import telemetry
from .pipeline import DEFAULT_STAGES, INGEST_BATCH_SIZE


@click.group()
def main() -> None:
    """Dispatch corpus subcommands."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


def _serve(tier: str) -> None:
    from . import serving
    from .tiers import tier as configured_tier

    try:
        t = configured_tier(tier)
        service = serving.tier_service(tier)
    except ValueError as exc:  # a bad config file, or a tier it does not define
        raise click.ClickException(str(exc)) from exc
    telemetry.configure(service)
    # The gauge counts the documents table, which only a source tier serves.
    if t.projection is None:
        telemetry.register_corpus_size_gauge()
    serving.serve(tier)


@main.command()
@click.argument("tier")
def serve(tier: str) -> None:
    """Serve a storage tier's MCP tools over streamable HTTP.

    TIER is any tier in the config file. By default: 'sensitive' (corpus), the
    full surface with raw mail, for trusted local callers; and 'sanitized'
    (corpus-index), what a trust-downgraded consumer (Hermes) queries: summaries
    and priority signal, never raw bodies.
    """
    _serve(tier)


@main.command(deprecated=True)
def mcp() -> None:
    """Alias of 'corpus serve sensitive', for one release."""
    _serve("sensitive")


@main.command(deprecated=True)
def index() -> None:
    """Alias of 'corpus serve sanitized', for one release."""
    _serve("sanitized")


@main.command()
@click.argument("source")
@click.option(
    "--batch-size", default=None, type=click.IntRange(min=1),
    help="records ingest embeds per request  "
    f"[default: the source's pipeline.batch_size, else {INGEST_BATCH_SIZE}]",
)
def ingest(source: str, batch_size: int | None) -> None:
    """Ingest from a configured source id, e.g. 'imap:example'."""
    telemetry.configure("corpus-ingest")
    from .ingest import ingest as run_ingest

    try:
        count = run_ingest(source, batch_size=batch_size)
        click.echo(f"ingested {count} documents from {source}")
    except ValueError as exc:  # an unconfigured source, or missing credentials
        raise click.ClickException(str(exc)) from exc
    finally:
        telemetry.shutdown()  # flush metrics/traces before the process exits


@main.command()
@click.option("--source", default=None, help="filter by source id, e.g. gmail:personal")
@click.option("--account", default=None, help="filter by account address")
@click.option("--limit", default=0, type=int, help="enrich at most N messages (0 = all)")
@click.option("--force", is_flag=True, help="re-enrich documents already stored")
@click.option(
    "--upgrade-stale", is_flag=True,
    help="also re-enrich documents enriched under an older schema version",
)
@click.option(
    "--retry-rejected", is_flag=True,
    help="also retry documents this model previously rejected",
)
@click.option(
    "--redo-recovered", is_flag=True,
    help="also re-enrich records recovered from a reply that stalled in whitespace",
)
def enrich(
    source: str | None, account: str | None, limit: int, force: bool, upgrade_stale: bool,
    retry_rejected: bool, redo_recovered: bool,
) -> None:
    """Batch-enrich stored documents with summary and classification.

    Also runs an LLM secret audit on any document with flagged candidates.
    """
    telemetry.configure("corpus-enrich")
    from .enrich_batch import run_enrich
    from .enrich_store import EnrichStore
    from .store import DocumentStore

    try:
        with EnrichStore() as store, DocumentStore() as documents:
            r = run_enrich(
                store, documents=documents, source=source, account=account, limit=limit, force=force,
                upgrade_stale=upgrade_stale, retry_rejected=retry_rejected,
                redo_recovered=redo_recovered,
            )
        msg = f"enriched {r['enriched']}, audited {r['audited']} of {r['scanned']} scanned"
        if r["ineligible"]:
            msg += (
                f" ({r['ineligible']} not enrichable by source policy: {r['declined']} declared "
                f"enrich: false, {r['undeclared']} from sources with no entry)"
            )
        click.echo(msg)
    except ValueError as exc:
        # Policy refusal: a clear message beats a traceback.
        raise click.ClickException(str(exc)) from exc
    finally:
        telemetry.shutdown()


@main.command()
@click.option("--source", default=None, help="filter by source id, e.g. gmail:personal")
@click.option("--limit", default=0, type=int, help="sync at most N documents (0 = all)")
@click.option(
    "--reproject", "--force", "reproject", is_flag=True,
    help="re-project every document, even those projected under the current policy; "
    "re-embeds only rows whose summary changed; --force is the old name",
)
@click.option(
    "--tier", "tier_name", default="sanitized", show_default=True,
    help="the projected tier to sync into (one with a projection in the config file)",
)
def sync(source: str | None, limit: int, reproject: bool, tier_name: str) -> None:
    """Project enriched documents into a projected tier (default: sanitized).

    A projected tier is a trust-downgraded store a cloud consumer may query.
    Its projection (source tier, columns, gate, redaction) comes from the tiers
    file. Drops raw content/subject/sender; redacts names, ids and contact
    details in the summaries. A changed projection re-projects on the next sync.
    """
    from .tiers import projected

    try:
        dst, src = projected(tier_name)
        dst_dsn, src_dsn = dst.store_dsn(), src.store_dsn()
    except (ValueError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    telemetry.configure("corpus-sync")
    from .config import settings
    from .sanitize import run_sync
    from .sanitized_store import SanitizedStore
    from .store import DocumentStore

    try:
        with SanitizedStore(dst_dsn, dst.schema) as store, \
                DocumentStore(src_dsn, src.schema) as documents:
            r = run_sync(
                store, documents=documents, source=source, limit=limit, force=reproject,
                policy=dst.projection.policy,
                reproject_limit=0 if reproject else settings.sync_reproject_limit,
            )
        click.echo(f"synced {r['synced']}, skipped {r['skipped']} of {r['scanned']} scanned")
        if r["deferred"]:
            click.echo(
                f"{r['deferred']} rows still projected under an older policy; the next syncs "
                "continue (or run with --reproject)"
            )
    finally:
        telemetry.shutdown()


def _stage_names(ctx: click.Context, param: click.Parameter, value: str) -> tuple[str, ...]:
    from .pipeline import select

    names = tuple(n.strip() for n in value.split(",") if n.strip())
    try:
        return tuple(stage.name for stage in select(names))
    except ValueError as exc:
        raise click.BadParameter(str(exc), ctx=ctx, param=param) from exc


@main.command()
@click.argument("source")
@click.option(
    "--stages", default=",".join(DEFAULT_STAGES), show_default=True, callback=_stage_names,
    help="comma-separated stages to run, in this order; a subset may skip stages",
)
@click.option(
    "--limit", default=None, type=click.IntRange(min=0),
    help="enrich at most N documents and sync at most N rows (0 = all); ingest is uncapped  "
    "[default: the source's pipeline.limit, else 0]",
)
@click.option(
    "--batch-size", default=None, type=click.IntRange(min=1),
    help="records ingest embeds per request  "
    f"[default: the source's pipeline.batch_size, else {INGEST_BATCH_SIZE}]",
)
@click.option(
    "--tier", "tier_name", default=None,
    help="the projected tier sync writes  [default: the source's pipeline.tier, else sanitized]",
)
def pipeline(
    source: str,
    stages: tuple[str, ...],
    limit: int | None,
    batch_size: int | None,
    tier_name: str | None,
) -> None:
    """Ingest, enrich and sync one configured SOURCE, e.g. 'imap:example', in that order.

    Each stage resumes where the last run stopped, so after a failure the same
    command picks up again. A run refused before any stage starts exits 2, as a
    usage error; a stage that fails stops the run and exits 1. Options the
    source entry sets in the config file are the defaults; a flag overrides its
    option.
    """
    from .pipeline import Options, StageFailedError, run

    telemetry.configure("corpus-pipeline")
    options = Options(limit=limit, batch_size=batch_size, tier=tier_name)
    try:
        counts = run(source, stages, options=options)
    except StageFailedError as exc:
        _echo_counts(exc.counts)
        raise click.ClickException(f"{exc.stage} failed: {exc.__cause__}") from exc
    except (ValueError, RuntimeError) as exc:
        # Refused before any stage ran: the invocation, or the configuration it
        # names, is wrong, so a scheduler can tell it apart from a failed stage.
        raise click.UsageError(str(exc)) from exc
    finally:
        telemetry.shutdown()
    _echo_counts(counts)


def _echo_counts(counts: dict[str, dict[str, int]]) -> None:
    for stage, c in counts.items():
        click.echo(f"{stage}: " + ", ".join(f"{k} {v}" for k, v in c.items()))


@main.command()
@click.option("--source", default=None, help="filter by source id, e.g. gmail:personal")
@click.option("--account", default=None, help="filter by account address")
@click.option("--limit", default=0, type=int, help="export at most N documents (0 = all)")
@click.option("--force", is_flag=True, help="rewrite vault files even if unchanged")
def export(source: str | None, account: str | None, limit: int, force: bool) -> None:
    """Materialize stored documents into the local markdown vault (one file each)."""
    telemetry.configure("corpus-export")
    from .export import export_archive
    from .store import DocumentStore

    try:
        with DocumentStore() as documents:
            r = export_archive(documents, source=source, account=account, limit=limit, force=force)
        click.echo(f"wrote {r['written']}, unchanged {r['unchanged']} of {r['scanned']} scanned")
    finally:
        telemetry.shutdown()


@main.command(name="audit-secrets")
@click.option("--source", default=None, help="filter by source id, e.g. gmail:personal")
@click.option("--account", default=None, help="filter by account address")
@click.option("--limit", default=0, type=int, help="scan at most N messages (0 = all)")
def audit_secrets_cmd(source: str | None, account: str | None, limit: int) -> None:
    """Re-run only the LLM secret confirmation over stored documents with candidates.

    No re-enrichment is performed.
    """
    telemetry.configure("corpus-audit")
    from .enrich_batch import run_audit
    from .enrich_store import EnrichStore
    from .store import DocumentStore

    try:
        with EnrichStore() as store, DocumentStore() as documents:
            r = run_audit(store, documents=documents, source=source, account=account, limit=limit)
        click.echo(f"audited {r['audited']} of {r['scanned']} scanned")
    finally:
        telemetry.shutdown()


@main.command()
@click.option("--source", default=None, help="filter by source id, e.g. gmail:personal")
@click.option("--account", default=None, help="filter by account address")
@click.option("--limit", default=0, type=int, help="scan at most N messages (0 = all)")
@click.option(
    "--json",
    "json_out",
    default=None,
    type=click.Path(dir_okay=False),
    help="also write the full report (ids + types, no values) as JSON",
)
def scan(source: str | None, account: str | None, limit: int, json_out: str | None) -> None:
    """Scan stored documents for secrets — reports types + counts, never values."""
    from .scan import scan_archive
    from .store import DocumentStore

    with DocumentStore() as documents:
        report = scan_archive(documents, source=source, account=account, limit=limit)
    click.echo(f"scanned {report['scanned']}; {report['with_secrets']} contain secrets")
    for secret_type, count in sorted(report["totals"].items(), key=lambda kv: -kv[1]):
        messages = sum(1 for h in report["hits"] if secret_type in h["secret_types"])
        click.echo(f"  {secret_type}: {count} ({messages} messages)")
    for hit in report["hits"]:
        types = ",".join(hit["secret_types"])
        click.echo(f"{hit['sent_at']}  [{types}]  {hit['from_addr']}  {hit['subject']}")
    if json_out:
        import json
        from pathlib import Path

        Path(json_out).write_text(json.dumps(report, indent=2))
        click.echo(f"wrote {json_out}")


@main.command(name="scan-gate")
def scan_gate_cmd() -> None:
    """Run the egress gate behind the configured gateway adapter.

    The gateway sends LLM request bodies here on the way to model providers. The
    gate redacts PII/secrets (redact-by-default), refuses the highest-confidence
    classes and oversized batch bodies, and passes local models unscanned.
    Reports types + counts, never values.
    """
    telemetry.configure("corpus-scan-gate")
    from .scan_gate import serve

    try:
        serve()
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    main()
