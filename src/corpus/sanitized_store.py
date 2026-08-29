"""The sanitized (trust-downgraded) store: one row per message projected down to
cloud-safe fields, in a *separate* Postgres database (`ai_sanitized`). This is the
write target of the one-way sync and the read source of corpus-index. It never
holds raw content, raw subject, full sender addresses, or secret material — so a
cloud-model consumer (Hermes) structurally cannot retrieve them.
"""

from __future__ import annotations

from typing import Self

import psycopg

from .config import settings

# Ordered projection columns (row dict keys). summary_embedding + synced_at are
# handled separately (vector cast / server clock).
COLS = (
    "id", "source", "account", "thread_id", "sent_at", "sender_domain",
    "domain", "category", "transactional_type", "importance", "requires_action",
    "action_type", "action_summary", "deadline", "waiting_on", "time_sensitive",
    "suggested_disposition", "sensitivity_level", "one_line", "organizations",
    "enriched_at",
)


def _ddl(schema: str, dim: int) -> str:
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


class SanitizedStore:
    """Owns the sanitized DB connection + the `messages` table (lazy DDL, upserts).
    The caller owns the lifecycle (context manager)."""

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or settings.sanitized_database_url
        if not self._dsn:
            raise RuntimeError("CORPUS_SANITIZED_DATABASE_URL not set (sync disabled)")
        self._schema = settings.db_schema
        self._conn = psycopg.connect(self._dsn)
        with self._conn.cursor() as cur:
            cur.execute(_ddl(self._schema, settings.embedding_dimensions))
        self._conn.commit()
        # Prebuilt upsert: every column, plus the vector cast; refresh on conflict.
        cols = ", ".join(COLS)
        ph = ", ".join(["%s"] * len(COLS))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLS if c != "id")
        self._sql = (
            f"INSERT INTO {self._schema}.messages ({cols}, summary_embedding) "
            f"VALUES ({ph}, %s::vector) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}, "
            "summary_embedding = EXCLUDED.summary_embedding, synced_at = now()"
        )

    def save_message(self, row: dict, embedding: list[float]) -> None:
        vec = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"
        params = [row.get(c) for c in COLS] + [vec]
        with self._conn.cursor() as cur:
            cur.execute(self._sql, params)
        self._conn.commit()

    def synced_versions(self) -> dict[str, object]:
        """id -> enriched_at already synced, so the sync skips unchanged rows and
        re-projects a document only after it is re-enriched."""
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT id, enriched_at FROM {self._schema}.messages")
            return {r[0]: r[1] for r in cur.fetchall()}

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
