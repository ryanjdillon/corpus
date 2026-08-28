"""export_archive materializes stored documents into the vault, idempotently."""

from __future__ import annotations

from corpus import export as export_mod
from corpus import vault
from corpus.config import settings

_DOCS = [
    ("gmail:personal::1", "body one", {"subject": "s1", "from_addr": "a@b.com"}),
    ("gmail:personal::2", "body two", {"subject": "s2", "from_addr": "c@d.com"}),
]


def _wire(monkeypatch, tmp_path, docs=_DOCS):
    monkeypatch.setattr(
        export_mod.store, "iter_documents", lambda source=None, account=None: iter(docs)
    )
    monkeypatch.setattr(settings, "vault_path", str(tmp_path))


def test_export_writes_then_unchanged(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)

    assert export_mod.export_archive() == {"scanned": 2, "written": 2, "unchanged": 0}
    assert vault.vault_path("gmail:personal::1", root=tmp_path).exists()
    assert "body one" in vault.read("gmail:personal::1", root=tmp_path)

    # a second pass rewrites nothing
    assert export_mod.export_archive() == {"scanned": 2, "written": 0, "unchanged": 2}


def test_export_limit(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path)
    assert export_mod.export_archive(limit=1)["scanned"] == 1
