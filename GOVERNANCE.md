# Governance & Access Control

This document defines the target access-control model for corpus and the roadmap
toward it. It is a **design roadmap**, not a description of the current
implementation — today corpus authenticates callers only at its network edge.
New design decisions should be checked against this roadmap so the system does
not drift into a state that is hard to migrate (see `AGENTS.md`).

## Principles

1. **Principals are first-class — humans *and* agents.** Every caller is a named
   principal with its own least-privilege grants. No shared API keys.
2. **Isolate physically at the boundary that must never leak; differentiate
   logically within it.** Hard security domains get physical separation; users
   and agents inside a domain are separated by policy.
3. **Identity is IdP-agnostic.** Authentication is OIDC against any standard
   provider (e.g. Microsoft Entra ID, Amazon Cognito, Authentik). Authorization
   derives from token claims (subject, groups/roles, scopes, delegation).
4. **Delegation is explicit and bounded.** An agent acting for a user is limited
   to the intersection of the agent's and the user's rights, and every action is
   attributable to both.
5. **Everything is auditable and revocable.**

## Isolation model

corpus supports three isolation mechanisms, applied in layers:

| Mechanism | Strength | ANN index | Use for |
|---|---|---|---|
| Row-Level Security (one table, policy-filtered) | Logical | Shared | Many users/agents sharing data within one trust domain |
| Table/schema-per-tenant | Object-level | Per-tenant | A bounded number of tenants needing independent indexes |
| Separate instance/database per domain | Physical | Per-domain | Security domains that must never mix |

**Guidance:**

- **Security domains that must never mix → separate instances (or databases).**
  A shared approximate-nearest-neighbour index is a shared physical surface: a
  policy bug or misconfiguration can leak across it, and strict per-row filtering
  degrades ANN recall. For hard boundaries, remove the shared surface entirely.
  Each domain gets its own database + index, credentials, ingestion config, and
  network policy, which also contains credential blast radius.
- **User/agent differentiation within a domain → Row-Level Security or
  table-per-tenant.** Use RLS when principals need overlapping views of shared
  data and one index is desired; use table-per-tenant when each sub-tenant wants
  an independent index and clean revoke-by-drop.
- **Defense in depth:** where a trusted layer injects query-time filters, keep
  database RLS as a backstop so an application bug cannot return rows the database
  would refuse.

### Vector-search caveats to respect

- Filtered ANN queries can under-return: the index selects nearest neighbours
  *before* the filter applies. Enable iterative index scans
  (`SET LOCAL hnsw.iterative_scan`) to preserve recall under selective policies,
  bounded by `hnsw.max_scan_tuples`.
- The document store connects as a single role and does not set a per-request
  principal. DB-native RLS keyed on session context therefore requires either
  issuing retrieval SQL with per-request `SET LOCAL`, a connection proxy that
  injects context, or query-time filter injection from a trusted policy layer.
  Decide this explicitly before relying on RLS.

## Identity (authentication)

- Humans and agents authenticate via **OIDC**; the gateway validates JWTs
  (JWKS) for the REST API and MCP-spec OAuth for the MCP endpoint, then forwards
  validated claims to corpus.
- Agents authenticate with their **own workload credentials** (client-credentials
  grant) — a named principal, never a shared key.

## Authorization

Two tiers:

- **Coarse** (which corpus/tools a principal may use) — enforced at the gateway
  from claims, including per-tool restrictions (e.g. read-only search vs. bulk
  export).
- **Fine** (which documents) — enforced in/near the data layer from a policy
  decision point (PDP) that yields a per-request predicate over document
  governance attributes.

**Policy engine:** a relationship-based engine (ReBAC, Zanzibar-style — e.g.
OpenFGA) is the recommended default, because "agent acts-for user; user
member-of group; group can-read corpus" is naturally a relationship graph.
Attribute-based engines (e.g. Cedar) are an alternative where rules are purely
attribute-driven.

### Document governance attributes

Each document carries: `security_domain` (the hard boundary), `owner`,
`classification`/`visibility`, and `acl` (explicit principal/group grants or PDP
relation tuples). A read is allowed iff domain matches **and** the PDP allows the
principal for that document.

## Agents as first-class principals

- **Identity:** stable, unique per agent.
- **Least privilege:** explicit grants, default deny, tool-level scoping.
- **Delegation:** OAuth token exchange (RFC 8693); the `act` claim carries the
  on-behalf-of chain. Effective permission = intersection of agent and user
  scope.
- **Attribution:** audit records both the agent and the user it acted for.
- **Revocation:** revoking a credential or a delegation cuts access immediately.

## Audit

Access decisions are logged at the gateway (principal, route, tool, decision),
at the PDP (allow/deny, reason, policy version), and at the database (queries per
principal), with retention appropriate to each domain.

## Roadmap

1. **Edge auth** — callers authenticated at the network edge. *(current)*
2. **Domain isolation** — per-domain instances/databases: separate index,
   credentials, ingestion config, and network policy.
3. **OIDC identity** — gateway JWT / MCP OAuth; claims forwarded; agents as named
   principals.
4. **Fine-grained authorization** — PDP, document governance attributes, and
   RLS/filter injection within a domain, with iterative scan for recall.
5. **Delegation** — RFC 8693 token exchange for agent-acts-for-user with
   intersected scope and full attribution.
6. **Audit hardening** — centralised decision and access logs.

Design changes should state which roadmap stage they target and must not
foreclose later stages (e.g. schema and identity choices should not make
per-domain isolation or a PDP hard to introduce).
