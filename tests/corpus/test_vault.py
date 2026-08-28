"""The vault renders source-fact frontmatter + body to a deterministic path,
idempotently."""

from __future__ import annotations

import yaml

from corpus import vault


def test_vault_path_scheme(tmp_path):
    p = vault.vault_path("gmail:personal::15abCDef0123", root=tmp_path)
    assert p == tmp_path / "gmail" / "personal" / "15" / "15abCDef0123.md"


def test_vault_path_unsafe_uid_falls_back_to_hash(tmp_path):
    p = vault.vault_path("imap:x::weird/uid with spaces", root=tmp_path)
    assert p.parent.parent == tmp_path / "imap" / "x"
    assert p.suffix == ".md"
    assert "/" not in p.stem and " " not in p.stem


def test_render_source_facts_only_and_body():
    meta = {
        "source": "gmail:personal",
        "from_addr": "a@b.com",
        "subject": "Hi",
        "label": "personal",
        "label_confidence": 0.5,
        "signals": {},
    }
    text = vault.render("gmail:personal::1", "the body", meta)
    front, _, body = text.partition("\n---\n\n")
    data = yaml.safe_load(front.removeprefix("---\n"))
    assert data["id"] == "gmail:personal::1"
    assert data["from_addr"] == "a@b.com"
    # corpus-derived fields are excluded from the canonical vault
    assert "label" not in data
    assert "label_confidence" not in data
    assert "signals" not in data
    assert body.rstrip("\n") == "the body"


def test_write_idempotent_then_force_then_changed(tmp_path):
    args = ("gmail:personal::1", "body", {"subject": "s"})
    assert vault.write(*args, root=tmp_path) is True  # created
    assert vault.write(*args, root=tmp_path) is False  # byte-identical -> skipped
    assert vault.write(*args, root=tmp_path, force=True) is True  # forced rewrite
    assert vault.write("gmail:personal::1", "changed", {"subject": "s"}, root=tmp_path) is True
    assert vault.read("gmail:personal::1", root=tmp_path).endswith("changed\n")


def test_read_missing_returns_none(tmp_path):
    assert vault.read("gmail:personal::nope", root=tmp_path) is None
