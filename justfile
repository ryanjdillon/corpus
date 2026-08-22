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

# Lint.
lint:
    ruff check .

# Full coverage (unit + integration) with the ratchet gate.
cov:
    uv run coverage run --source=corpus -m pytest -o addopts= tests
    uv run coverage report --precision=2
    uv run python scripts/coverage_gate.py "$(uv run coverage report --format=total --precision=2)" .github/coverage-baseline.txt

# One-time Gmail OAuth to mint a refresh token.
# Usage: just gmail-auth path/to/client_secret.json
gmail-auth CLIENT_SECRET:
    uv run python scripts/gmail_oauth.py {{CLIENT_SECRET}}
