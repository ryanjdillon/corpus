import 'agents.just'

# corpus dev tasks. Enter the dev shell first: `nix develop`.

# Create the uv venv (from the dev shell's Python) and install test + dev deps.
# corpus-detect (packages/detect) installs editable with it, as a uv workspace member.
setup:
    uv venv --python python3.12
    uv pip install -e '.[test,dev]'

# Re-pin the runtime and test dependencies the image and CI install
# (deploy/constraints.txt; the image ignores the test pins).
# Run after changing dependencies in pyproject.toml, or to take upstream updates.
# corpus-detect is left out: it is built from this repo, not installed from an index.
lock:
    uv pip compile pyproject.toml --extra test --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --upgrade --no-emit-package corpus-detect --custom-compile-command "just lock" -o deploy/constraints.txt

# Fast unit tests.
test:
    uv run pytest

# Docker-backed integration tests (needs a running Docker daemon).
test-int:
    uv run pytest -m integration

# Lint (uses the pinned ruff from the venv, matching CI).
lint:
    uv run ruff check .

# Full coverage (unit + integration) with the ratchet gate. Measures corpus and
# corpus_detect ([tool.coverage.run] source).
cov:
    uv run coverage run -m pytest -o addopts= tests packages/detect/tests
    uv run coverage report --precision=2
    uv run python scripts/coverage_gate.py "$(uv run coverage report --format=total --precision=2)" .github/coverage-baseline.txt .docker-skip-count

# Fail if this branch changes the module set without updating docs/architecture.*.
arch BASE="origin/main":
    python3 scripts/architecture_gate.py {{BASE}}

# Lint this branch's commit messages against Conventional Commits, mirroring the
# wagoid/commitlint CI. Checks commits not yet on origin/main (auto-discovers
# commitlint.config.mjs).
commitlint BASE="origin/main":
    commitlint --from {{BASE}} --to HEAD

# Lint the Helm chart, render it with each example and test values file, check
# that rendered numbers are exact, and check that the invalid values under
# deploy/helm/tests/invalid are refused (deploy/helm/scripts/check.sh, as CI).
helm-check:
    bash deploy/helm/scripts/check.sh

# All pre-PR checks, mirroring CI. Run this (and fix any failures) before pushing.
check: lint cov arch commitlint helm-check

# Evaluate an enrichment model (docs/evaluation.md). Run, then score one or more outputs:
#   just eval-enrich run [--allow-remote] [--limit N] [--only hard_case=TAG]
#   just eval-enrich score outputs/<run>.jsonl [...] [--price-per-mtok P]
# `run` refuses a non-loopback endpoint (the gateway included) without --allow-remote.
eval-enrich *ARGS:
    uv run python scripts/eval_enrich.py {{ARGS}}

# One-time Gmail OAuth to mint a refresh token.
# Usage: just gmail-auth path/to/client_secret.json
gmail-auth CLIENT_SECRET:
    uv run python scripts/gmail_oauth.py {{CLIENT_SECRET}}
