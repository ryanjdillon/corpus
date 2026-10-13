"""The sanitized (trust-downgraded) storage tier.

One row per message, projected down to cloud-safe fields, in a *separate* Postgres
database (``ai_sanitized``). This is the write target of the one-way sync and the
read source of corpus-index. It never holds raw content, raw subject, full sender
or recipient addresses, or secret material, so a cloud-model consumer structurally
cannot retrieve them.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .config import settings
from .query import (
    ANY_OF,
    SEMANTIC_DEPTH,
    Aggregate,
    Layout,
    Page,
    Position,
    QueryPolicy,
    QuerySpec,
    build_aggregate,
    build_query,
    rank_case,
)
from .store_base import Store

# Ordered projection columns (row dict keys). ``summary_embedding`` and
# ``synced_at`` are handled separately (vector cast / server clock).
COLS = (
    "id", "source", "account", "thread_id", "sent_at", "sender_domain",
    "domain", "category", "transactional_type", "importance", "requires_action",
    "action_type", "action_summary", "deadline", "waiting_on", "time_sensitive",
    "suggested_disposition", "sensitivity_level", "one_line", "organizations",
    "enriched_at", "uri",
)

#: The projection's columns derived from third-party text, which a result
#: envelope marks untrusted (see ``envelope``): model summaries and entities of the
#: email, the sender's domain, and the thread id (from the Message-ID header).
#: Every other column is an id, a date, a schema-constrained enum, or ``uri``: a
#: provider web link the projection admits only as an https URL (see
#: ``sanitize.web_link``), which corpus derives from the provider, not the email.
UNTRUSTED_COLS = ("action_summary", "one_line", "organizations", "sender_domain", "thread_id")


# Priority ordering: importance, then urgency, then recency, then id. The id makes
# the order total, so offset pages neither skip nor repeat rows that tie on
# everything else.
PRIORITY = (
    "CASE importance WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
    "time_sensitive DESC NULLS LAST, sent_at DESC NULLS LAST, id"
)

def layout(schema: str) -> Layout:
    """The query model's view of *schema*'s ``messages`` table: each dimension's SQL."""
    return Layout(
        table=f"{schema}.messages",
        columns=COLS,
        keys=COLS,
        filters={
            **{name: f"{name} = ANY(%s)" for name in ANY_OF},
            **{name: f"{name} = %s" for name in (
                "requires_action", "time_sensitive", "sender_domain", "account", "source",
            )},
            "due": "(time_sensitive OR deadline IS NOT NULL) = %s",
            "organization": (
                "EXISTS (SELECT 1 FROM unnest(organizations) AS o(name) WHERE o.name ILIKE %s)"
            ),
            # An unknown level ranks NULL and so never passes a ceiling: a
            # sensitivity filter fails closed.
            "max_sensitivity": f"{rank_case('sensitivity_level')} <= %s",
        },
        ranges={"sent_at": "sent_at", "deadline": "deadline"},
        groups={
            **{name: name for name in (
                "domain", "sender_domain", "category", "importance", "suggested_disposition",
            )},
            "organization": "org.name",
            # In UTC, so a month's boundaries do not move with the session's zone.
            "month": "to_char(sent_at AT TIME ZONE 'UTC', 'YYYY-MM')",
        },
        joins={"organization": "LEFT JOIN LATERAL unnest(organizations) AS org(name) ON true"},
        text=("one_line", "action_summary"),
        sent_at="sent_at",
        newest="coalesce(sent_at, '-infinity'::timestamptz)",
        oldest="coalesce(sent_at, 'infinity'::timestamptz)",
        key_param="%s::timestamptz",
        timestamp_key=True,
        dates=frozenset({"deadline"}),
        orders={"priority": PRIORITY, "deadline": f"deadline NULLS LAST, {PRIORITY}"},
        embedding="summary_embedding",
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
        uri                   text,
        synced_at             timestamptz NOT NULL DEFAULT now()
    );
    -- Tables created before the column existed; their rows stay NULL until
    -- re-projected (corpus sync --reproject). A read-only reader opened before
    -- this runs reads the column as NULL (SanitizedStore._present).
    ALTER TABLE {schema}.messages ADD COLUMN IF NOT EXISTS uri text;
    """


class SanitizedStore(Store):
    """The sanitized tier's store: the ``messages`` table in ``ai_sanitized``.

    Lazily creates the table and upserts projected rows; ``synced_versions`` drives
    incremental resume. Raw email columns do not exist here by construction.
    :meth:`query`, :meth:`aggregate` and :meth:`get` are the reads of the index.
    """

    def __init__(self, dsn: str, schema: str, *, read_only: bool = False) -> None:
        """Open the sanitized DB at *dsn*, with its ``messages`` table in *schema*.

        Both come from the tier (``tiers.tier``). A *read_only* store runs no DDL
        and only reads: it is how the index connects, as a role that may not
        create the table, and whose writes should fail even if attempted.
        """
        self._schema = schema
        self._read_only = read_only
        super().__init__(dsn)
        if read_only:
            self._conn.read_only = True
        self._full_layout = layout(self._schema)
        # The writer's DDL guarantees every column; a reader checks (see _present).
        self._columns: frozenset[str] | None = None if read_only else frozenset(COLS)
        cols = ", ".join(COLS)
        placeholders = ", ".join(["%s"] * len(COLS))
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLS if c != "id")
        self._sql = (
            f"INSERT INTO {self._schema}.messages ({cols}, summary_embedding) "
            f"VALUES ({placeholders}, %s::vector) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}, "
            "summary_embedding = EXCLUDED.summary_embedding, synced_at = now()"
        )
        # Only rows whose projection differs are written, so a re-projection
        # interrupted and run again rewrites nothing it already finished.
        projected = [c for c in COLS if c != "id"]
        self._update_sql = (
            f"UPDATE {self._schema}.messages SET "
            + ", ".join(f"{c} = %s" for c in projected)
            + ", synced_at = now() WHERE id = %s AND ("
            + ", ".join(projected)
            + ") IS DISTINCT FROM ("
            + ", ".join(["%s"] * len(projected))
            + ")"
        )

    def schema_ddl(self) -> str:
        """Return the DDL for the sanitized ``messages`` table (none when read-only)."""
        # Even CREATE ... IF NOT EXISTS needs CREATE on the schema, which the
        # index's read-only role does not have.
        if self._read_only:
            return ""
        return _ddl(self._schema, settings.embedding_dimensions)

    def save_message(self, row: dict, embedding: list[float]) -> None:
        """Upsert one projected row and its summary embedding."""
        vec = "[" + ",".join(f"{x:.6f}" for x in embedding) + "]"
        self._write(self._sql, [row.get(c) for c in COLS] + [vec])

    def update_projections(self, rows: list[dict]) -> int:
        """Rewrite the projected columns of existing rows, keeping their embeddings.

        For a row whose embedded text (``one_line``) is unchanged, so its stored
        vector still holds. A row whose projection already matches is left
        untouched. Returns how many rows changed.
        """
        projected = [c for c in COLS if c != "id"]
        changed = 0
        with self._conn.transaction(), self._conn.cursor() as cur:
            for row in rows:
                values = [row.get(c) for c in projected]
                cur.execute(self._update_sql, [*values, row["id"], *values])
                changed += cur.rowcount
        return changed

    def synced_versions(self) -> dict[str, object]:
        """Map id -> ``enriched_at`` already synced, so the sync skips unchanged rows."""
        return {id_: version for id_, (version, _) in self.synced_state().items()}

    def synced_state(self) -> dict[str, tuple[object, str | None]]:
        """Map id -> (``enriched_at``, ``one_line``) of every synced row.

        ``one_line`` is the text the stored embedding was computed from, so the
        sync re-embeds a row only when it changes.
        """
        rows = self._read(f"SELECT id, enriched_at, one_line FROM {self._schema}.messages")
        return {r[0]: (r[1], r[2]) for r in rows}

    def _present(self) -> frozenset[str]:
        """The projection columns the table has.

        A read-only store runs no DDL, so after an upgrade that adds a column it
        can open before any writer has added it. It reads such a column as NULL
        instead of failing every query. The answer is cached once complete; while
        a column is missing it is checked again on each read, so the column
        appears as soon as a writer adds it, without a restart.
        """
        if self._columns is not None:
            return self._columns
        rows = self._read(
            "SELECT attname FROM pg_attribute WHERE attrelid = to_regclass(%s) "
            "AND attnum > 0 AND NOT attisdropped",
            (f"{self._schema}.messages",),
        )
        present = frozenset(COLS) & {r[0] for r in rows}
        # No row at all means no table: select every column and let the read
        # fail naming the table, rather than return rows of NULLs.
        if present == frozenset(COLS) or not present:
            self._columns = frozenset(COLS)
            return self._columns
        return present

    @property
    def _layout(self) -> Layout:
        """The query layout, selecting NULL for a column the table lacks."""
        present = self._present()
        if len(present) == len(COLS):
            return self._full_layout
        return replace(
            self._full_layout,
            columns=tuple(c if c in present else f"NULL AS {c}" for c in COLS),
        )

    def query(
        self, spec: QuerySpec, policy: QueryPolicy, position: Position | None = None
    ) -> Page:
        """One page of the rows (``COLS``) *spec* matches, within *policy*."""
        lay = self._layout
        built = build_query(spec, policy, lay, position)
        with self._conn.transaction():
            if spec.semantic is not None:
                # An HNSW index scan ranks only hnsw.ef_search rows (pgvector's
                # default is 40), so without this a vector page stops short of
                # SEMANTIC_DEPTH once the table is indexed.
                with self._conn.cursor() as cur:
                    cur.execute(f"SET LOCAL hnsw.ef_search = {SEMANTIC_DEPTH}")
            rows = self._read(built.sql, built.params)
        return built.page(rows, lay.keys)

    def aggregate(self, spec: QuerySpec, policy: QueryPolicy) -> Aggregate:
        """Count *spec*'s matches per ``group_by`` group, within *policy*."""
        built = build_aggregate(spec, policy, self._layout)
        with self._conn.transaction():
            groups = self._read(built.sql, built.params)
            total = self._read(built.count_sql, built.count_params)[0][0]
        return built.result(groups, total)

    def get(self, id: str) -> dict[str, Any] | None:
        """One row (``COLS``) by id, or None."""
        lay = self._layout
        rows = self._read(f"SELECT {', '.join(lay.columns)} FROM {lay.table} WHERE id = %s", (id,))
        return dict(zip(COLS, rows[0], strict=True)) if rows else None
