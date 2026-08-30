# Corpus

## Docs & publishing

`docs/` is published to GitHub Pages by `.github/workflows/docs.yml` (MkDocs
Material). The `README.md` is the landing page only; deep detail lives in
`docs/`.

One page per module or concern. A topic that has parts becomes a directory of
focused pages: `docs/fetchers/gmail.md`, not a `Gmail` section buried in
`docs/fetchers.md`. When you add a page, add it to the `nav` in `mkdocs.yml`.

## Commits

In addition to the base commit rules:

Use Conventional Commit messages (`feat:`, `fix:`, `docs:`, `refactor:`,
`test:`, `build:`, `ci:`, `chore:`, `revert:`). This is enforced against
`@commitlint/config-conventional` (`commitlint.config.mjs`) both in CI (the
`wagoid/commitlint-github-action` on every PR) and locally by `just commitlint`
— part of `just check` — which lints the commits on your branch not yet on
`origin/main`. Keep each commit atomic and its subject in the imperative mood.

## Tests

`pytest` runs the unit tests; `pytest -m integration` runs the Docker-backed
tier. Keep both green. Coverage is ratcheted in CI (see
`.github/coverage-baseline.txt`): it may not drop, and any increase must raise
the baseline in the same change.

## Before opening a PR

Run **`just check`** and fix everything it reports **before** pushing a branch or
opening a PR. It runs the same lint (pinned ruff), tests, coverage ratchet, and
commit-message lint (commitlint) as CI, so a green `just check` means green CI.
Do not rely on CI to surface lint, coverage-regression, or commit-message
failures — catch them locally first. (ruff is pinned in the `dev` extra and in
the CI workflow to the same version so results match.)

**Architecture-gate gotcha (adding/removing a top-level `src/corpus/*.py` module).**
The gate (`just arch`) diffs the **committed** module set against `origin/main`, so
it sees a new/removed module only **after you commit** — run `just check` (or
`just arch`) once **more after committing**, or it passes on an uncommitted new
module and then fails in CI. When the module set changes, describe it in
`docs/architecture.json` — the single maintained artifact (a node, or a `sources`
entry citing `src/corpus/<module>.py` on the nearest node); the gate checks that
file. After editing the JSON, bump
`meta.repository.revision` to a commit that contains the file and re-run
`archify deliver … docs/architecture.html` so the rendered diagram — the
published Architecture page — stays in sync (the archify skill lives in the
fornybar agent-skills bundle).

## Governance roadmap (do not drift)

`GOVERNANCE.md` defines the target access-control model and a staged roadmap
(edge auth → domain isolation → OIDC identity → fine-grained authorization →
delegation → audit). **Weigh every design decision against that roadmap and do
not introduce drift that is hard to migrate.** In particular:

- Data-model and schema choices must not foreclose per-domain isolation
  (separate instance/database) or the addition of document governance attributes
  (`security_domain`, `owner`, `classification`, `acl`).
- Identity/authorization choices must not assume shared keys or a single
  principal; they should compose with OIDC claims, per-agent principals, and an
  external policy decision point.
- When a change relates to access control, state which roadmap stage it targets,
  and prefer designs that keep later stages cheap to adopt.
