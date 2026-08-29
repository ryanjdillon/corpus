"""corpus-index: the sanitized query surface.

The trust gate for a cloud-model consumer (Hermes). It reads only the
``sanitized_documents`` view — documents ⨝ enrichments projected down to safe
metadata + the enrichment priority signal + secret-free ``one_line`` — as the
restricted ``corpus_index_ro`` role, which has no privilege on ``content`` or any
raw table. Summaries are secret-free by construction (see ``enrichment.py``); this
adds a topic-sensitivity floor by withholding richer detail at high sensitivity.

The view is created by the schema owner via ``ensure_view`` (``corpus index-init``,
connecting as ``corpus_app``); queries connect as the restricted role via
``CORPUS_INDEX_DATABASE_URL``.
"""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from psycopg.rows import dict_row

from .config import settings

_INDEX_ROLE = "corpus_index_ro"
_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _view() -> str:
    return f"{settings.db_schema}.sanitized_documents"


def view_ddl() -> str:
    """The sanitized view: safe metadata + the enrichment priority signal + one_line,
    with abstract/key_points withheld at/above the sensitivity gate. Never exposes
    ``content``, ``embedding``, ``secret_audit`` or ``secret_candidates``."""
    docs = f"{settings.db_schema}.{settings.documents_table}"
    enr = f"{settings.db_schema}.enrichments"
    gate = _RANK.get(settings.index_sensitivity_gate, 3)
    sens = (
        "(CASE e.enrichment->>'sensitivity_level' "
        "WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END)"
    )
    return f"""
    CREATE OR REPLACE VIEW {_view()} AS
    SELECT
        d.id,
        d.meta->>'source'    AS source,
        d.meta->>'account'   AS account,
        d.meta->>'from_addr' AS from_addr,
        d.meta->>'subject'   AS subject,
        d.meta->>'sent_at'   AS sent_at,
        d.meta->'labels'     AS labels,
        d.meta->>'thread_id' AS thread_id,
        e.enrichment->>'one_line'                   AS one_line,
        e.enrichment->>'category'                   AS category,
        e.enrichment->>'domain'                     AS domain,
        e.enrichment->>'transactional_type'         AS transactional_type,
        (e.enrichment->>'requires_action')::boolean AS requires_action,
        e.enrichment->>'action_type'                AS action_type,
        e.enrichment->>'action_summary'             AS action_summary,
        e.enrichment->>'deadline'                   AS deadline,
        e.enrichment->>'waiting_on'                 AS waiting_on,
        e.enrichment->>'importance'                 AS importance,
        (e.enrichment->>'time_sensitive')::boolean  AS time_sensitive,
        e.enrichment->>'sensitivity_level'          AS sensitivity_level,
        e.enrichment->>'suggested_disposition'      AS suggested_disposition,
        CASE WHEN {sens} >= {gate} THEN NULL ELSE e.enrichment->>'abstract' END AS abstract,
        CASE WHEN {sens} >= {gate} THEN NULL ELSE e.enrichment->'key_points' END AS key_points
    FROM {docs} d
    JOIN {enr} e ON e.doc_id = d.id
    """


def ensure_view(admin_url: str | None = None) -> None:
    """Create/refresh the sanitized view and grant SELECT to ``corpus_index_ro``.
    Run as the schema owner (``corpus_app`` via ``CORPUS_DATABASE_URL``) — the app
    role can create the view and grant, but not create the role (a DB bootstrap).
    The grant is skipped if the role does not yet exist, so this is safe to run
    before the deploy provisions it."""
    grants = f"""
    DO $do$ BEGIN
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_INDEX_ROLE}') THEN
        GRANT USAGE ON SCHEMA {settings.db_schema} TO {_INDEX_ROLE};
        GRANT SELECT ON {_view()} TO {_INDEX_ROLE};
      END IF;
    END $do$;
    """
    with psycopg.connect(admin_url or settings.database_url) as conn, conn.cursor() as cur:
        cur.execute(view_ddl())
        cur.execute(grants)
        conn.commit()


def _rows(sql: str, params: list) -> list[dict]:
    """Run a query as the restricted index role; rows come back as dicts."""
    if not settings.index_database_url:
        raise RuntimeError("CORPUS_INDEX_DATABASE_URL not set (corpus-index disabled)")
    with (
        psycopg.connect(settings.index_database_url, row_factory=dict_row) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql, params)
        return cur.fetchall()


# Priority ordering: importance, then urgency, then recency. Constant SQL (no input).
_ORDER = (
    "CASE importance WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
    "time_sensitive DESC NULLS LAST, sent_at DESC NULLS LAST"
)

Rows = Callable[[str, list], list[dict]]


def action_items(
    limit: int = 50, domain: str | None = None, importance: str | None = None, *, rows: Rows = _rows
) -> list[dict]:
    """Messages that need an action from the owner, ranked by importance + urgency."""
    clauses, params = ["requires_action = true"], []
    if domain:
        clauses.append("domain = %s")
        params.append(domain)
    if importance:
        clauses.append("importance = %s")
        params.append(importance)
    params.append(limit)
    where = " AND ".join(clauses)
    return rows(f"SELECT * FROM {_view()} WHERE {where} ORDER BY {_ORDER} LIMIT %s", params)


def due_soon(limit: int = 50, *, rows: Rows = _rows) -> list[dict]:
    """Time-sensitive items, or items carrying a deadline."""
    return rows(
        f"SELECT * FROM {_view()} WHERE time_sensitive = true OR deadline IS NOT NULL "
        f"ORDER BY deadline NULLS LAST, {_ORDER} LIMIT %s",
        [limit],
    )


def waiting_on(who: str = "them", limit: int = 50, *, rows: Rows = _rows) -> list[dict]:
    """Threads flagged as awaiting a reply — 'them' = owner is waiting on someone,
    'me' = someone is waiting on the owner."""
    return rows(f"SELECT * FROM {_view()} WHERE waiting_on = %s ORDER BY {_ORDER} LIMIT %s", [who, limit])


def by_domain(domain: str, limit: int = 50, *, rows: Rows = _rows) -> list[dict]:
    """Recent sanitized items in a life-area domain (banking, health, work…)."""
    return rows(f"SELECT * FROM {_view()} WHERE domain = %s ORDER BY {_ORDER} LIMIT %s", [domain, limit])


def summary(doc_id: str, *, rows: Rows = _rows) -> dict | None:
    """The sanitized summary + priority signal for one message id."""
    got = rows(f"SELECT * FROM {_view()} WHERE id = %s", [doc_id])
    return got[0] if got else None


def stats(*, rows: Rows = _rows) -> dict:
    """Counts of sanitized items by domain and suggested disposition."""
    total = rows(f"SELECT count(*) AS total FROM {_view()}", [])[0]["total"]
    by_dom = {r["domain"]: r["n"] for r in rows(f"SELECT domain, count(*) AS n FROM {_view()} GROUP BY 1", [])}
    by_disp = {
        r["d"]: r["n"]
        for r in rows(f"SELECT suggested_disposition AS d, count(*) AS n FROM {_view()} GROUP BY 1", [])
    }
    return {"total": total, "by_domain": by_dom, "by_disposition": by_disp}
