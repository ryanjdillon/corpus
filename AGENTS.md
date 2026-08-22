# Agent guidance for this repository

## Documentation & comments

Docs and code comments describe the **current state of the code** and its
rationale — nothing else.

- Never reference a chat/agent dialogue, a prior conversation, or how the code
  "used to" work / "was just changed". A comment explains the code as it stands.
- Keep comments general to the code, not to any one deployment. Describe what the
  code does and why (e.g. "an OpenAI-compatible embedding endpoint"), not the
  author's cluster, hosts, or environment.
- When you change code, update the comments and docs it touches so they stay
  true; delete comments describing things that no longer exist.

## Commits

Use Conventional Commit messages (`feat:`, `fix:`, `docs:`, `refactor:`,
`test:`, `build:`, `ci:`, `chore:`, `revert:`); the `main` branch enforces this.

## Tests

`pytest` runs the unit tests; `pytest -m integration` runs the Docker-backed
tier. Keep both green. Coverage is ratcheted in CI (see
`.github/coverage-baseline.txt`): it may not drop, and any increase must raise
the baseline in the same change.

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
