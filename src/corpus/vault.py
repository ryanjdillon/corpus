"""The canonical raw vault: one markdown file per document — source-fact YAML
frontmatter + the body — on a local-only volume.

The vault is (becoming) the source of truth; the DB is a derived index rebuildable
from it. So the frontmatter carries only **source facts** (from/to/subject/date/
labels/ids), never corpus-derived classification (label/confidence/signals), which
is re-computed into the DB.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

from .config import settings

# Corpus-derived fields — excluded from the canonical vault (re-derived into the DB).
_DERIVED = frozenset({"label", "label_confidence", "signals"})
_SAFE_UID = re.compile(r"[A-Za-z0-9._-]{1,120}")


def vault_path(doc_id: str, root: str | Path | None = None) -> Path:
    """Deterministic file path for a document id, e.g.
    ``gmail:personal::15ab…`` -> ``<root>/gmail/personal/15/15ab….md``. A uid with
    filesystem-unsafe characters falls back to a stable hash of the full id."""
    base = Path(root if root is not None else settings.vault_path)
    source, _, uid = doc_id.partition("::")
    stem = uid if _SAFE_UID.fullmatch(uid or "") else hashlib.sha1(doc_id.encode()).hexdigest()[:24]
    return base / source.replace(":", "/") / stem[:2] / f"{stem}.md"


def render(doc_id: str, content: str | None, meta: dict | None) -> str:
    """The markdown file text: source-fact frontmatter (+ the doc id) then the body."""
    front = {"id": doc_id, **{k: v for k, v in (meta or {}).items() if k not in _DERIVED}}
    fm = yaml.safe_dump(front, sort_keys=True, allow_unicode=True, default_flow_style=False).rstrip()
    return f"---\n{fm}\n---\n\n{content or ''}\n"


def write(doc_id: str, content: str | None, meta: dict | None, root=None, force: bool = False) -> bool:
    """Write the vault file. Idempotent: returns False (no write) when the file is
    already byte-identical, unless ``force``. Returns True when created or changed."""
    path = vault_path(doc_id, root)
    text = render(doc_id, content, meta)
    if not force and path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def read(doc_id: str, root=None) -> str | None:
    """The stored vault file text, or None if absent."""
    path = vault_path(doc_id, root)
    return path.read_text(encoding="utf-8") if path.exists() else None
