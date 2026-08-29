# Development

## Setup

The dev shell (Nix flake) provides Python, `uv`, `just`, `ruff`, and
`commitlint`:

```bash
nix develop
just setup      # create the uv venv and install test + dev deps
```

## The pre-PR gate

Run `just check` before pushing a branch or opening a PR. It runs the same lint
(pinned ruff), tests, coverage ratchet, and commit-message lint as CI, so a green
`just check` means green CI:

```bash
just check      # lint + coverage ratchet + architecture gate + commitlint
```

## Tests

```bash
just test                       # fast unit tests
just test-int                   # Docker-backed: pgvector + GreenMail
```

Integration tests spin up pinned containers via the Docker SDK
(`pgvector/pgvector:pg17`, `greenmail/standalone:2.1.3`) and auto-skip when Docker
is unavailable. The embedding endpoint is faked in-process, so no model is
downloaded. Unit tests are the default; the integration tier is opt-in.

Coverage is ratcheted against `.github/coverage-baseline.txt`: it may not drop,
and any increase must raise the baseline in the same change.

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/). This is
enforced against `@commitlint/config-conventional` both in CI and locally by
`just commitlint` (part of `just check`).

## Build

`deploy/Dockerfile` builds a wheel and installs it. Pushing a `v*` tag builds and
publishes `ghcr.io/<owner>/corpus` (see `.github/workflows/docker.yml`).
