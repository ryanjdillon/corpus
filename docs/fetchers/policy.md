# Enrichment policy

Enrichment is **opt-in per source**, and the default is deny. Each entry under
`sources:` in the config file must say `enrich: true` or `enrich: false` (see
[Source configuration](config.md)); a document whose source is not declared
enrichable is never enriched — not by `corpus enrich`, not with an explicit
`--source`, not with `--force`.

## Why it is default-deny

Enrichment is not neutral summarisation. The `Enrichment` schema encodes the
questions you ask of *personal correspondence*: what action does this ask of me,
who am I waiting on, how important is it, when is it due. That schema also drives
guided decoding, so the model's output is always a structurally valid
`Enrichment` — which means it **cannot answer "none of this applies"**.
`Importance` has no `none` member at all.

Point that at a document which is not a message — an article, a transcript, a
file — and it does not fail. It returns well-formed, confidently-typed,
fabricated metadata: an action type, a due signal, an importance. Those then rank
alongside real obligations wherever enrichment is read, and because
`DocumentStore.iter` orders by `sent_at` descending, anything with a date sorts
straight into "recent".

The failure is silent and self-consistent, which is what makes it expensive. A
bad search result is visible; a poisoned priority queue is not. So adding a
source must be a deliberate act, and forgetting to think about it must be safe.

## Declaring a source

```yaml
sources:
  - name: personal
    kind: gmail
    enrich: true    # personal correspondence: the domain the schema was built for
```

`enrich` has no default: every entry records the decision, and a review of the
file sees it. Record why next to it, as a comment, when the answer is not
obvious from the kind. The check is `sources.may_enrich`, which reads the
entry's `enrich`; an id with no entry may not be enriched.

A config file without `sources:` keeps the decision from before sources were
configured, made per kind: `gmail` and `imap` documents are enrichable and every
other kind is not, whatever the name (see
[Without `sources:`](config.md#without-sources-deprecated)).

## What enforcement looks like

| Situation | Result |
|---|---|
| `corpus enrich`, mixed archive | Eligible documents enriched; ineligible counted, never sent to the model |
| `corpus enrich --source youtube:@chan` (undeclared) | **Refused** with a message saying how to declare it |
| `corpus pipeline imap:notices`, declared `enrich: false` | **Refused** before ingest; `--stages ingest,sync` runs it without enrichment |
| `corpus enrich --force`, undeclared source | Still ineligible — `force` re-enriches, it does not grant permission |
| Document with no `source` in its metadata | Ineligible |

Ineligible documents are counted as `ineligible`, deliberately **separate from
`skipped`**, which means a record that failed to enrich. Conflating the two would
hide either one. `ineligible` is split in two, and each part logs its sources
by name, so "it did nothing" is never a mystery:

- `declined`: the source is declared with `enrich: false`. A deliberate
  decision, logged at INFO.
- `undeclared`: the source has no entry under `sources:` (or no `source` at
  all). Usually a missing entry rather than a decision, so it is logged as a
  WARNING. Source ids are config names, not message content.

## What is not gated

`run_audit` — the LLM secret confirmation — deliberately ignores this policy. It
is a credential scan, and a secret pasted into a document is worth finding
whatever the document is; narrowing it to enrichable sources would create blind
spots exactly where nobody is looking. It already invokes the model only on
documents the deterministic detectors flagged, so the wider net costs little.
