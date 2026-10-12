"""export_archive materializes stored documents into the vault, idempotently."""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest

from corpus import export as export_mod
from corpus import vault
from corpus.store import DocumentStore

_DOCS = [
    ("gmail:personal::1", "body one", {"subject": "s1", "from_addr": "a@b.com"}),
    ("gmail:personal::2", "body two", {"subject": "s2", "from_addr": "c@d.com"}),
]


@pytest.fixture
def documents():
    """A spec-bound DocumentStore whose ``iter`` yields a fresh pass over ``_DOCS``."""
    store = create_autospec(DocumentStore, instance=True)
    store.iter.side_effect = lambda source=None, account=None: (d for d in _DOCS)  # a generator
    return store


@pytest.fixture
def writer(tmp_path):
    """Real vault writer bound to a tmp_path (honest filesystem double)."""

    def _writer(doc_id, content, meta, force=False):
        return vault.write(doc_id, content, meta, root=tmp_path, force=force)

    return _writer


def test_export_writes_then_unchanged(documents, writer, tmp_path):
    assert export_mod.export_archive(documents, writer=writer) == {
        "scanned": 2,
        "written": 2,
        "unchanged": 0,
    }
    assert vault.vault_path("gmail:personal::1", root=tmp_path).exists()
    assert "body one" in vault.read("gmail:personal::1", root=tmp_path)

    # a second pass rewrites nothing
    assert export_mod.export_archive(documents, writer=writer) == {
        "scanned": 2,
        "written": 0,
        "unchanged": 2,
    }


def test_export_limit(documents, writer):
    result = export_mod.export_archive(documents, limit=1, writer=writer)
    assert result["scanned"] == 1


def test_export_closes_a_stream_it_stops_early(documents, writer):
    # The store streams through a server-side cursor in a transaction, which
    # only closing the generator ends; garbage collection is not prompt.
    stream = (d for d in _DOCS)
    documents.iter.side_effect = None
    documents.iter.return_value = stream

    export_mod.export_archive(documents, limit=1, writer=writer)

    assert stream.gi_frame is None  # closed, though not exhausted


def test_export_passes_the_filters_to_the_store(documents, writer):
    export_mod.export_archive(documents, source="gmail:personal", account="me@x.org", writer=writer)
    documents.iter.assert_called_once_with(source="gmail:personal", account="me@x.org")
