"""The derived enrichments table: lazy creation, resume ids, idempotent upserts."""

from __future__ import annotations

import psycopg
import pytest

from corpus.config import settings
from corpus.enrich_store import EnrichStore

pytestmark = pytest.mark.integration


def _row(doc_id: str):
    with psycopg.connect(settings.database_url) as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT enrichment_model, schema_version, secret_candidates, audit_model "
            f"FROM {settings.db_schema}.enrichments WHERE doc_id = %s",
            (doc_id,),
        )
        return cur.fetchone()


def test_lazy_create_save_and_resume(pg):
    with EnrichStore() as est:
        assert est.enriched_ids() == set()  # table created empty
        est.save_enrichment("d1", {"one_line": "hello"}, "local", 1)
        est.save_audit("d1", ["us_ssn"], {"contains_secret": True}, "local", 1)
        assert est.enriched_ids() == {"d1"}

    # a fresh connection sees the persisted row and both stages' provenance
    assert _row("d1") == ("local", 1, ["us_ssn"], "local")


def test_upsert_replaces_enrichment_only(pg):
    with EnrichStore() as est:
        est.save_enrichment("d1", {"one_line": "a"}, "local", 1)
        est.save_audit("d1", ["credit_card"], {"contains_secret": False}, "local", 1)
        # re-enrich with a newer model/schema; the audit columns must be untouched
        est.save_enrichment("d1", {"one_line": "b"}, "local-v2", 2)

    model, schema_version, candidates, audit_model = _row("d1")
    assert (model, schema_version) == ("local-v2", 2)
    assert candidates == ["credit_card"]  # audit side preserved across re-enrichment
    assert audit_model == "local"
