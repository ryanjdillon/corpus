# Enrichment policy

Enrichment is **opt-in per source kind**, and the default is deny. A source with
no declaration in `corpus.fetchers.policy.POLICIES` is never enriched — not by
`corpus enrich`, not with an explicit `--source`, not with `--force`.

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
`iter_documents` orders by `sent_at` descending, anything with a date sorts
straight into "recent".

The failure is silent and self-consistent, which is what makes it expensive. A
bad search result is visible; a poisoned priority queue is not. So adding a
source must be a deliberate act, and forgetting to think about it must be safe.

## Declaring a source

```python
SourcePolicy(
    kind="gmail",
    enrich=True,
    rationale="Personal correspondence -- the domain the enrichment schema was built for.",
)
```

`kind` is the prefix of a source id — `gmail:personal` and `gmail:work` share the
kind `gmail`, so accounts do not each need a declaration. It is the same taxonomy
`build_fetcher` dispatches on, which is why policy lives beside it: the set of
known kinds is declared once rather than duplicated into a list that drifts.

`rationale` is required in practice (a test asserts every declaration has one).
A policy decision without a recorded reason cannot be reviewed later.

## What enforcement looks like

| Situation | Result |
|---|---|
| `corpus enrich`, mixed archive | Eligible documents enriched; ineligible counted, never sent to the model |
| `corpus enrich --source youtube:@chan` (undeclared) | **Refused** with a message naming the enrichable kinds |
| `corpus enrich --force`, undeclared source | Still ineligible — `force` re-enriches, it does not grant permission |
| Document with no `source` in its metadata | Ineligible |

Ineligible documents are counted as `ineligible`, deliberately **separate from
`skipped`**, which means a record that failed to enrich. Conflating the two would
hide either one. A run that enriches nothing logs the excluded sources by name,
so "it did nothing" is never a mystery.

## What is not gated

`run_audit` — the LLM secret confirmation — deliberately ignores this policy. It
is a credential scan, and a secret pasted into a document is worth finding
whatever the document is; narrowing it to enrichable sources would create blind
spots exactly where nobody is looking. It already invokes the model only on
documents the deterministic detectors flagged, so the wider net costs little.
