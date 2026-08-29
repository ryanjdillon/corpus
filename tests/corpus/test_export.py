"""export_archive materializes stored documents into the vault, idempotently."""

from __future__ import annotations

import pytest

from corpus import export as export_mod
from corpus import vault

_DOCS = [
    ("gmail:personal::1", "body one", {"subject": "s1", "from_addr": "a@b.com"}),
    ("gmail:personal::2", "body two", {"subject": "s2", "from_addr": "c@d.com"}),
]


@pytest.fixture
def document_source():
    """Factory yielding a fresh iterator of stored (id, content, meta) tuples.

    A plain factory rather than an autospec'd mock: generators do not autospec
    cleanly, and export_archive calls the source once per run.
    """

    def _source(source=None, account=None):
        return iter(_DOCS)

    return _source


@pytest.fixture
def writer(tmp_path):
    """Real vault writer bound to a tmp_path (honest filesystem double)."""

    def _writer(doc_id, content, meta, force=False):
        return vault.write(doc_id, content, meta, root=tmp_path, force=force)

    return _writer


def test_export_writes_then_unchanged(document_source, writer, tmp_path):
    assert export_mod.export_archive(document_source=document_source, writer=writer) == {
        "scanned": 2,
        "written": 2,
        "unchanged": 0,
    }
    assert vault.vault_path("gmail:personal::1", root=tmp_path).exists()
    assert "body one" in vault.read("gmail:personal::1", root=tmp_path)

    # a second pass rewrites nothing
    assert export_mod.export_archive(document_source=document_source, writer=writer) == {
        "scanned": 2,
        "written": 0,
        "unchanged": 2,
    }


def test_export_limit(document_source, writer):
    result = export_mod.export_archive(limit=1, document_source=document_source, writer=writer)
    assert result["scanned"] == 1
