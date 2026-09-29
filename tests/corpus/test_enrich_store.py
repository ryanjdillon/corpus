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
        est.save_enrichment("d1", {"one_line": "hello"}, "local", "sv1")
        est.save_audit("d1", ["us_ssn"], {"contains_secret": True}, "local", "scan1")
        assert est.enriched_ids() == {"d1"}

    # a fresh connection sees the persisted row and both stages' provenance
    assert _row("d1") == ("local", "sv1", ["us_ssn"], "local")


def test_resume_ids_can_exclude_stale_schema_versions(pg):
    with EnrichStore() as est:
        est.save_enrichment("old", {"one_line": "a"}, "local", "sv1")
        est.save_enrichment("new", {"one_line": "b"}, "local", "sv2")

        assert est.enriched_ids() == {"old", "new"}
        assert est.enriched_ids("sv2") == {"new"}


def test_rejection_is_per_model_and_cleared_by_a_later_enrichment(pg):
    with EnrichStore() as est:
        est.save_rejection("d1", "400: too long", "remote")
        assert est.rejected_ids("remote") == {"d1"}
        assert est.rejected_ids("local") == set()
        assert est.enriched_ids() == set()  # a rejection is not an enrichment

        est.save_enrichment("d1", {"one_line": "ok"}, "local", "sv1")
        assert est.rejected_ids("remote") == set()
        assert est.enriched_ids() == {"d1"}


def test_upsert_replaces_enrichment_only(pg):
    with EnrichStore() as est:
        est.save_enrichment("d1", {"one_line": "a"}, "local", "sv1")
        est.save_audit("d1", ["credit_card"], {"contains_secret": False}, "local", "scan1")
        # re-enrich with a newer model/schema; the audit columns must be untouched
        est.save_enrichment("d1", {"one_line": "b"}, "local-v2", "sv2")

    model, schema_version, candidates, audit_model = _row("d1")
    assert (model, schema_version) == ("local-v2", "sv2")
    assert candidates == ["credit_card"]  # audit side preserved across re-enrichment
    assert audit_model == "local"
