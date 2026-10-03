import 'agents.just'

# corpus dev tasks. Enter the dev shell first: `nix develop`.

# Create the uv venv (from the dev shell's Python) and install test + dev deps.
setup:
    uv venv --python python3.12
    uv pip install -e '.[test,dev]'

# Fast unit tests.
test:
    uv run pytest

# Docker-backed integration tests (needs a running Docker daemon).
test-int:
    uv run pytest -m integration

# Lint (uses the pinned ruff from the venv, matching CI).
lint:
    uv run ruff check .

# Full coverage (unit + integration) with the ratchet gate.
cov:
    uv run coverage run --source=corpus -m pytest -o addopts= tests
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

# All pre-PR checks, mirroring CI. Run this (and fix any failures) before pushing.
check: lint cov arch commitlint

# One-time Gmail OAuth to mint a refresh token.
# Usage: just gmail-auth path/to/client_secret.json
gmail-auth CLIENT_SECRET:
    uv run python scripts/gmail_oauth.py {{CLIENT_SECRET}}
