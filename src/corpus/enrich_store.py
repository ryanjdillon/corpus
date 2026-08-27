"""Storage for per-message enrichment + secret-audit records.

The documents table (raw content + embedding) is the source of truth; this table
is a *derived* index keyed by document id, rebuildable by re-running enrichment.
Enrichment and secret audit carry independent provenance (model, schema/scan
version, timestamp) so either can be regenerated — a better model, tightened rules
— without touching the other. Values are never stored: the audit holds the LLM's
verdict (types, severity, worded notes), never the secret itself.

The table is created lazily (the DB role owns the schema), so no out-of-band
migration is needed for it to appear.
"""

from __future__ import annotations

from typing import Self

import psycopg
from psycopg.types.json import Json

from .config import settings

_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    doc_id            text PRIMARY KEY,
    enrichment        jsonb,
    enrichment_model  text,
    schema_version    int,
    enriched_at       timestamptz,
    secret_candidates text[],
    secret_audit      jsonb,
    audit_model       text,
    scan_version      int,
    audited_at        timestamptz
)
"""


class EnrichStore:
    """A single connection to the derived enrichments table. Open once per batch;
    use as a context manager."""

    def __init__(self) -> None:
        self._conn = psycopg.connect(settings.database_url)
        self._table = f"{settings.db_schema}.enrichments"
        with self._conn.cursor() as cur:
            cur.execute(_DDL.format(table=self._table))
        self._conn.commit()

    def enriched_ids(self) -> set[str]:
        """Doc ids that already have an enrichment, so a batch can resume."""
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT doc_id FROM {self._table} WHERE enrichment IS NOT NULL")
            return {row[0] for row in cur.fetchall()}

    def save_enrichment(
        self, doc_id: str, enrichment: dict, model: str, schema_version: int
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {self._table}
                    (doc_id, enrichment, enrichment_model, schema_version, enriched_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (doc_id) DO UPDATE SET
                    enrichment       = EXCLUDED.enrichment,
                    enrichment_model = EXCLUDED.enrichment_model,
                    schema_version   = EXCLUDED.schema_version,
                    enriched_at      = now()
                """,
                (doc_id, Json(enrichment), model, schema_version),
            )
        self._conn.commit()

    def save_audit(
        self, doc_id: str, candidates: list[str], audit: dict, model: str, scan_version: int
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {self._table}
                    (doc_id, secret_candidates, secret_audit, audit_model, scan_version, audited_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (doc_id) DO UPDATE SET
                    secret_candidates = EXCLUDED.secret_candidates,
                    secret_audit      = EXCLUDED.secret_audit,
                    audit_model       = EXCLUDED.audit_model,
                    scan_version      = EXCLUDED.scan_version,
                    audited_at        = now()
                """,
                (doc_id, list(candidates), Json(audit), model, scan_version),
            )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
