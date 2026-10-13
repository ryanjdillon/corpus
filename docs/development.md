# Development

## Setup

The dev shell (Nix flake) provides Python, `uv`, `just`, `ruff`, and
`commitlint`:

```bash
nix develop
just setup      # create the uv venv and install test + dev deps
```

The repository is a uv workspace: `packages/detect` (the `corpus-detect`
distribution) installs editable alongside corpus, and its tests
(`packages/detect/tests`) run and count toward coverage with corpus's.

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

At exit the test session prints any thread a test left running, with where it
is, as a lead if the interpreter crashes after the tests pass. The CI test step
sets `PYTHONFAULTHANDLER`, so such a crash prints the Python stack it hit.

Locally, `just cov` (part of `just check`) does not enforce the baseline when the
Docker-backed tests were skipped, because the unit tier alone covers less than the
baseline; it prints an advisory line instead. CI always has Docker and always
enforces it, so a green `just check` without Docker is not proof of green CI.

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/). This is
enforced against `@commitlint/config-conventional` both in CI and locally by
`just commitlint` (part of `just check`).

## Build

`deploy/Dockerfile` builds two wheels, corpus and
[corpus-detect](detect.md) (`packages/detect`), and installs them together
against `deploy/constraints.txt`, which pins every runtime dependency. `pyproject.toml`
states only ranges, so without the pins a rebuild of the same release could pull
a different presidio, spaCy or grpcio. CI installs with the same constraints, so
the tests run against what ships. Run `just lock` after changing dependencies, or
to take upstream updates, and commit the result. Pushing a `v*` tag builds and
publishes `ghcr.io/<owner>/corpus` (see `.github/workflows/docker.yml`).

The base images (`python:3.12-slim`, `debian:12-slim`) are pinned by digest
next to their tag, so a rebuild gets the same layers; Dependabot's `docker`
ecosystem proposes digest bumps.

### The Betterleaks pin

The `leaks` stage downloads the [Betterleaks](detect.md#settings) release
tarball and checks it against `BETTERLEAKS_SHA256` with `sha256sum -c` before
unpacking it, so a tampered or swapped release fails the build. Betterleaks
reads the whole archive in the sensitive tier, which is why it is not taken on
trust from the download. Building it from source would add a Go toolchain stage
for no extra assurance over a pinned digest, so the image downloads and verifies.

The release publishes `checksums.txt` with a cosign keyless signature
(`checksums.txt.sigstore.json`) from its release workflow. To bump
`BETTERLEAKS_VERSION`, verify that signature, take the `linux_x64` line, and
check it against the tarball:

```bash
v=1.8.1  # the new version
gh release download "v$v" -R betterleaks/betterleaks \
  -p checksums.txt -p checksums.txt.sigstore.json -p "betterleaks_${v}_linux_x64.tar.gz"
cosign verify-blob --bundle checksums.txt.sigstore.json \
  --certificate-identity "https://github.com/betterleaks/betterleaks/.github/workflows/release.yml@refs/tags/v$v" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com checksums.txt
grep "_linux_x64.tar.gz" checksums.txt   # the new BETTERLEAKS_SHA256
sha256sum "betterleaks_${v}_linux_x64.tar.gz"   # must match
```

Set both `ARG`s in `deploy/Dockerfile` together. `just verify-dockerfile-checksum`
builds only the `leaks` stage with a wrong checksum and passes only if
`sha256sum` refused it. It needs Docker and the network, so it is a local
check, not part of `just check` or CI. Whether the binary ran or the audit fell
back to the local rules is counted per scan; see
[Observability](observability.md#credential-scanner-fallback).
