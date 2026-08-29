"""The sanitized (trust-downgraded) storage tier.

One row per message, projected down to cloud-safe fields, in a *separate* Postgres
database (``ai_sanitized``). This is the write target of the one-way sync and the
read source of corpus-index. It never holds raw content, raw subject, full sender
or recipient addresses, or secret material, so a cloud-model consumer structurally
cannot retrieve them.
"""

from __future__ import annotations

from .config import settings
from .store_base import Store

# Ordered projection columns (row dict keys). ``summary_embedding`` and
# ``synced_at`` are handled separately (vector cast / server clock).
COLS = (
    "id", "source", "account", "thread_id", "sent_at", "sender_domain",
    "domain", "category", "transactional_type", "importance", "requires_action",
    "action_type", "action_summary", "deadline", "waiting_on", "time_sensitive",
    "suggested_disposition", "sensitivity_level", "one_line", "organizations",
    "enriched_at",
)


def _ddl(schema: str, dim: int) -> str:
    """Return the DDL for the sanitized ``messages`` table (with a *dim*-wide vector)."""
    return f"""
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE TABLE IF NOT EXISTS {schema}.messages (
        id                    text PRIMARY KEY,
        source                text,
        account               text,
        thread_id             text,
        sent_at               timestamptz,
        sender_domain         text,
        domain                text,
        category              text,
        transactional_type    text,
        importance            text,
        requires_action       boolean,
        action_type           text,
        action_summary        text,
        deadline              date,
        waiting_on            text,
        time_sensitive        boolean,
        suggested_disposition text,
        sensitivity_level     text,
        one_line              text,
        organizations         text[],
        summary_embedding     vector({dim}),
        enriched_at           timestamptz,
        synced_at             timestamptz NOT NULL DEFAULT now()
    );
    """


class SanitizedStore(Store):
    """The sanitized tier's store: the ``messages`` table in ``ai_sanitized``.

    Lazily creates the table and upserts projected rows; ``synced_versions`` drives
    incremental resume. Raw email columns do not exist here by construction.
    """

    def __init__(self, dsn: str | None = None) -> None:
        """Open the sanitized DB (from *dsn* or ``CORPUS_SANITIZED_DATABASE_URL``)."""
        dsn = dsn or settings.sanitized_database_url
        if not dsn:
            raise RuntimeError("CORPUS_SANITIZED_DATABASE_URL not set (sync disabled)")
        self._schema = settings.db_schema
        super().__init__(dsn)
        cols = ", ".join(COLS)
        placeholders = ", ".join(["%s"] * len(COLS))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLS if c != "id")
        self._sql = (
            f"INSERT INTO {self._schema}.messages ({cols}, summary_embedding) "
            f"VALUES ({placeholders}, %s::vector) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}, "
            "summary_embedding = EXCLUDED.summary_embedding, synced_at = now()"
        )

    def schema_ddl(self) -> str:
        return _ddl(self._schema, settings.embedding_dimensions)

    def save_message(self, row: dict, embedding: list[float]) -> None:
        """Upsert one projected row and its summary embedding."""
        vec = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"
        self._write(self._sql, [row.get(c) for c in COLS] + [vec])

    def synced_versions(self) -> dict[str, object]:
        """Map id -> ``enriched_at`` already synced, so the sync skips unchanged rows."""
        return {r[0]: r[1] for r in self._read(f"SELECT id, enriched_at FROM {self._schema}.messages")}
