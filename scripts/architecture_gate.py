#!/usr/bin/env python3
"""Architecture drift gate.

`docs/architecture.md` describes the pipeline as a set of deep modules. Nothing
forces it to change when the module set does, so it silently goes stale: the
enrichment and secret-scanning stacks both landed without it being touched.

This gate fails a change that adds or removes a top-level `corpus` module
without naming it in (or removing it from) the architecture artifacts. It
checks content, not merely that the file was edited, and it only judges modules
this change touches — pre-existing gaps are reported as notices so fixing them
stays a deliberate act rather than a blocked merge.

Editing a module's body is not architecture: only added, removed, and renamed
modules are gated. Modules inside subpackages (`fetchers/`) are exempt — a new
fetcher behind `build_fetcher` is the design working, not a topology change.

Usage:

    architecture_gate.py [base-ref]     # default: origin/main
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_DIR = "src/corpus"
# Every artifact that claims to describe the module set. The JSON is optional so
# an Archify specification can be added later without touching this gate.
ARTIFACTS = ("docs/architecture.md", "docs/architecture.json")
EXEMPT_STEMS = frozenset({"__init__", "__main__"})


def top_level_module(path: str) -> str | None:
    """Return the module stem for a gated path, or None if it is not gated."""
    prefix = f"{MODULE_DIR}/"
    if not path.startswith(prefix) or not path.endswith(".py"):
        return None
    relative = path[len(prefix) :]
    if "/" in relative:  # a subpackage module, e.g. fetchers/gmail.py
        return None
    stem = relative[: -len(".py")]
    return None if stem in EXEMPT_STEMS else stem


def changed_modules(base_ref: str) -> tuple[set[str], set[str]]:
    """Modules added and removed between the merge base and HEAD."""
    result = subprocess.run(
        ["git", "diff", "--name-status", "-M", f"{base_ref}...HEAD", "--", MODULE_DIR],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"::error::Could not diff against {base_ref}: {result.stderr.strip()}")

    added: set[str] = set()
    removed: set[str] = set()
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        status = fields[0]
        # A rename is a delete of the old path plus an add of the new one.
        if status.startswith("R") and len(fields) == 3:
            pairs = ((fields[1], removed), (fields[2], added))
        elif status.startswith("A") and len(fields) == 2:
            pairs = ((fields[1], added),)
        elif status.startswith("D") and len(fields) == 2:
            pairs = ((fields[1], removed),)
        else:  # M, C, T: the module set is unchanged
            continue
        for path, bucket in pairs:
            module = top_level_module(path)
            if module is not None:
                bucket.add(module)

    both = added & removed
    return added - both, removed - both


def mentions(text: str, module: str) -> bool:
    return re.search(rf"(?<![\w.]){re.escape(module)}(?![\w])", text) is not None


def existing_artifacts() -> list[tuple[Path, str]]:
    found = []
    for name in ARTIFACTS:
        path = REPO_ROOT / name
        if path.exists():
            found.append((path, path.read_text(encoding="utf-8")))
    return found


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print("usage: architecture_gate.py [base-ref]", file=sys.stderr)
        return 2
    base_ref = argv[1] if len(argv) == 2 else "origin/main"

    added, removed = changed_modules(base_ref)
    if not added and not removed:
        print("No top-level corpus modules added or removed; architecture unchanged.")
        return 0

    artifacts = existing_artifacts()
    if not artifacts:
        print(f"::error::None of {', '.join(ARTIFACTS)} exist; cannot check architecture drift.")
        return 1

    failures = []
    for path, text in artifacts:
        name = path.relative_to(REPO_ROOT)
        for module in sorted(added):
            if not mentions(text, module):
                failures.append(f"{name} does not describe the new module `{module}`")
        for module in sorted(removed):
            if mentions(text, module):
                failures.append(f"{name} still describes the removed module `{module}`")

    if failures:
        for failure in failures:
            print(f"::error::{failure}")
        print(
            "::error::This change alters the module set. Update the architecture "
            "artifacts in the same change so they cannot drift.",
        )
        return 1

    # Pre-existing drift is worth seeing, but this change is not the place to
    # block on it.
    for path, text in artifacts:
        stale = sorted(
            module
            for source in (REPO_ROOT / MODULE_DIR).glob("*.py")
            if (module := top_level_module(f"{MODULE_DIR}/{source.name}")) is not None
            and not mentions(text, module)
        )
        if stale:
            print(
                f"::notice::{path.relative_to(REPO_ROOT)} does not mention: {', '.join(stale)}",
            )

    described = ", ".join(f"+{m}" for m in sorted(added))
    dropped = ", ".join(f"-{m}" for m in sorted(removed))
    print(f"Architecture artifacts cover the module changes ({described or dropped}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
