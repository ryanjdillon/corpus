# Enrichment

`corpus enrich` asks a model for structured metadata about each stored document:
summaries, classification axes, the action it asks for, entities, and sensitivity.
It also runs a secret audit on documents the deterministic detectors flagged. The
schema is `corpus.enrichment.Enrichment`, and guided decoding constrains the
model's output to it, so every response parses. Only sources the policy declares
enrichable are processed (see [Enrichment policy](fetchers/policy.md)).

## Running it

```sh
corpus enrich                # every eligible document not yet enriched
corpus enrich --limit 400    # at most 400 documents sent to the model
corpus enrich --force        # re-enrich documents that already have a record
corpus enrich --upgrade-stale    # also redo records from an older schema version
corpus enrich --retry-rejected   # also retry documents this model rejected
corpus enrich --redo-recovered   # also redo records recovered from a stalled reply
```

A run is resumable: documents that already have an enrichment are skipped, so a
re-run continues rather than restarts. `--limit` counts documents sent to the
model, not documents scanned, so a capped scheduled run keeps making progress past
the ones already done.

Requests run `CORPUS_ENRICH_CONCURRENCY` at a time. A transient endpoint failure
is retried in-process (see [Configuration](configuration.md)), and an endpoint
that stays down aborts the run so it can resume later. By default that fails
the job too, so an outage is loud; `corpus pipeline --defer-when-model-unavailable`
instead defers enrichment and goes on to sync when the cause is an outage (a
5xx, a timeout, a rate limit, a spent budget). A 401, 403 or 404 is a bad key
or route that waiting will not fix, so it fails the job even then (see
[An unavailable model](pipeline.md#an-unavailable-model)). Either way the
pipeline records the source's backlog, the time enrichment last succeeded and
why the model was unavailable (see
[Enrichment backlog](observability.md#enrichment-backlog)).

A per-document rejection (a 4xx such as an input over the model's context, or
unparseable output) is counted as skipped and recorded against the model that
rejected it, with the reason. Later runs on that model pass the document over
instead of re-sending it every time; `--retry-rejected` sends them again, and a
different model is always given a try. A later successful enrichment clears the
record. A 429, or a key or route error (401, 403, 404), is not a rejection: it
is the endpoint's state, so it is retried or aborts the run like an outage (see
[Configuration](configuration.md)), and the document is sent again on the next
run. Rejections recorded for a 429 by earlier versions are ignored. If the secret audit fails for a document, the enrichment is kept and the
audit is counted as `audit_failed`; configuring the audit model's
`context_tokens` avoids the overflow case (see below).

The schema also bounds what the model can write: each entity list holds at most
12 items (`enrichment.MAX_ITEMS`), names and labels at most 120 characters, and
free text 400 (one_line 160). Guided decoding enforces the schema, so a model
that starts repeating itself inside a list, as newsletters tend to provoke, must
close it instead of looping. Changing these bounds changes `SCHEMA_VERSION`, so
existing records count as stale; `--upgrade-stale` re-enriches them when you
want.

Every request caps the model's output at 4096 tokens (`max_tokens`; a model's
`extra_body` can override it). Guided decoding guarantees the shape, not the
length: a document can send a model into a loop that keeps generating, and an
uncapped request then runs until the gateway's request timeout, is retried as an
outage, and can stall the run. Capped, the cut-off output fails to parse and the
document is rejected like any other. The same 4096 is the output reserve that
`context_tokens` budgeting subtracts when sizing the input, so raising one model's
`max_tokens` above it can make prompt plus output exceed that model's context;
lower its `context_tokens` by the difference when you do.

The schema bounds cannot stop one kind of loop: guided decoding allows
whitespace between any two JSON tokens, and a model can stall there, padding
with spaces and newlines until it reaches the cap. Everything it wrote before
the padding is intact, so an enrichment reply ending in at least 256 characters
of whitespace (`STALL_PADDING_CHARS`) is recovered instead of rejected. The
padding is dropped, as is a dangling `,`, `"key":` or unfilled `{`, and the
open brackets are closed. A reply cut off inside a string, or one that is not a
valid record once closed, is rejected as before.

Fields the model never reached fail closed where it matters:
`sensitivity_level` becomes `high` (`UNREACHED_ENRICHMENT`), so the sanitized
tier treats the record as high-sensitivity rather than as non-sensitive: it
stubs the free text, or, under `gate_action: redact`, redacts it, and stubs it
anyway when the model never reached `people` or `references` (see
[Tiers](tiers.md#at-the-gate-and-failing-closed)). Other fields take their schema defaults. A recovered record is
stored under `RECOVERED_SCHEMA_VERSION` (`<SCHEMA_VERSION>+recovered`), which
counts as stale, and the run log reports how many were recovered.
`--redo-recovered` enriches only those records again, in full, whichever
schema they were recovered under. `--upgrade-stale` does too, but it also
redoes every record from an older schema. A recovered record the model has
since rejected stays passed over unless `--retry-rejected` is also given. A run does not
retry recovered records on its own: at temperature 0 the same document usually
stalls the same way, so redo them after changing what caused the stall, such as
enabling `disable_any_whitespace` (below).

The secret audit is never recovered. A stall would leave later candidates
without a verdict, and reading that as "no secret" would silently downgrade a
real one, so a stalled audit is rejected. (vLLM can forbid the padding outright
with the server-wide `disable_any_whitespace` structured-outputs option; the
per-request field is ignored.)

## Instruction-like output

Anyone can send the mailbox an email, and a model summarising an email that
addresses an AI ("ignore previous instructions and …") can repeat the request in
its summary. After each enrichment, its free-text fields
(`enrichment.FREE_TEXT_FIELDS`: the summaries, key points, action summary,
entity names, and reference values) are checked with the same detector the MCP result envelope uses
(see [MCP results](mcp-results.md#flags)). A match is kept, not rejected, since
the owner should see that the email arrived; it is counted as `instruction_like`
in the run log and in the `corpus.enrich.instruction_like` metric (by model), and
logged as a warning with the document id. Envelopes flag the row again each time
it is read, so a pattern added later covers records enriched before it.
How a model handles injected email (described, not repeated or obeyed) is
measured on the evaluation's [red-team records](evaluation.md#red-team-records).

## People, references, and role-based summaries

The summaries name people by role and quote no identifiers, so a tier that must
not show names or reference numbers can drop two fields instead of guessing
names out of prose:

- `people` is a list of `{role, name}`. `role` is required: a short noun for
  how the person relates to the owner or the message ("friend", "mechanic",
  "landlord", "colleague"). `name` is the name as written, or null when the
  message gives none.
- `references` is a list of `{kind, value}`: the identifiers the message
  quotes. `kind` is one of `booking`, `order`, `case`, `invoice`, `ticket`,
  `parking`, `membership`, `tracking`, `account` (a customer number with a
  company), or `other`; `value` is the identifier as written. It has the same
  bounds as the other entity lists (12 items, 120 characters each). A
  reference is never a secret: card and bank account numbers, passwords, and
  one-time codes stay with the secret audit.
- `one_line`, `abstract`, and `action_summary` refer to people by role and
  quote no reference values ("a friend proposes a weekend trip", not "Zoe
  proposes a weekend trip"). Organizations may still be named. Of these,
  `one_line` and `action_summary` reach the sanitized tier.

A `role` and a reference `value` must not be blank, and a person or reference
with a key outside the struct is refused. Both are checked when the reply is
decoded (the structs' `__post_init__` and `forbid_unknown_fields`), not in the
guided schema: `json_schema()` uses no `minLength` and drops
`additionalProperties`, so a provider constraining generation never sees a
keyword the schema did not already use, and one that rejects an unfamiliar
keyword cannot fail every request. A reply that breaks either rule is
unparseable, whether complete or recovered from a stall.

The prompt asks for this; nothing enforces it, since guided decoding constrains
the shape of the output, not what its strings say. The eval measures how often
a model gets it wrong (see the name/reference leak rate in
[Enrichment evaluation](evaluation.md#metrics)). A reference value is copied
from untrusted mail, so it is free text like the summaries: it is in
`FREE_TEXT_FIELDS` and is checked for instruction-like text.

Which tier sees `people` names and `references` values is decided by the
tier's projection, not here (see [Tiers](tiers.md#redaction)). The lists are
also what the projection redacts by: it finds each person's name in the
summaries and replaces it with the role, initials or nothing, and masks each
reference value, whether or not the model kept to the prompt. On the sanitized
tier by default, a name becomes the role and a reference `[booking ••••42]`.

Dropping `people` and `references` does not remove every name. The prompt asks
only the three summaries to speak in roles, so a name can still appear in
`key_points`, `appointments[].who`, `topics`, and `locations`. None of these is
a column of the sanitized tier, so no projection can expose them; a tier that
ever projects one has to redact it too (`sanitize.REDACTED_COLS`).

**Older records.** The change to `people` and the new `references` field change
`SCHEMA_VERSION`, so only newly enriched mail gets the new shape. A stored
record keeps the shape it was written in. `enrichment.upgrade` gives a person
without a role the role `unknown` and a record without `references` an empty
list. It is applied where stored enrichments are read, so every reader sees the
current shape: `DocumentStore.iter_enriched` upgrades each row before the
sanitized projection receives it, and `enrichment.decode` upgrades before it
decodes. Applying it in the store rather than in `sanitize.project` keeps the
fallback in one place for any reader of stored rows, and keeps the projection a
policy over one shape. Bringing the archive to the new shape is
a separate backfill with `--upgrade-stale`, run as a local GPU job. Do not run
it in bulk against a shared remote endpoint.

## Typed facts

A few domains have facts worth querying by value, not by words: when a trip
starts, where it goes. `facts` holds them, typed, beside the core fields. It
starts with travel and has one key per domain; each is null unless the message
states it.

`facts.travel` is filled only for a travel booking, e-ticket or itinerary: a
flight, train, bus or ferry ticket, or a stay. A hotel booking counts as
travel, since a stay is where a trip goes, even when the journey was booked
elsewhere. A fare sale, a newsletter, or a dentist's reminder with a date and
an address is not, and leaves it null.

| Field | Value |
| --- | --- |
| `start`, `end` | ISO dates: the first and last day (departure and return, or check-in and check-out). |
| `origin`, `destination` | `{name, country}`: the place as written and its ISO 3166-1 alpha-2 code (`US`, `NO`), when the model can tell. For a stay, `destination` is where it is and `origin` is null. The sanitized tier keeps only the country by default (see [Tiers](tiers.md#typed-facts)). |
| `carrier` | The airline, rail, bus or ferry operator; null for a stay. |

Every field may be null. Booking codes, ticket and seat numbers never go here:
they are `references`, which the projection masks. A place's name is not
safe to project: for a stay it can be someone's home, address and all, so the
sanitized tier keeps only its country unless its policy says otherwise.

A country that is not an officially assigned ISO 3166-1 alpha-2 code
(`enrichment.ISO_COUNTRIES`, a static set) is dropped to null when the reply
is decoded, rather than refused, and a lower-case code is upper-cased: a place
without a country is still a fact, and refusing it would lose the record. As
with roles, that is checked after decoding, not with a `pattern` or `enum`:
the guided schema adds no JSON-schema keyword (a test pins the set). The
schema bounds `country` at eight characters, not two: a value past the bound
refuses the whole record, so a tight bound would lose a trip for writing
`usa`, and guided decoding would cut `Sweden` to `SW` and `Germany` to `GE`.
The looser bound lets such a value through to be dropped. Validation cannot
catch every wrong code: one that lands on an assigned code is valid and wrong
(`GE` is Georgia), whether guided decoding truncated it (a provider that
enforces a bound by cutting the value) or the model guessed it, and no
check of the value alone can tell. The structs' docstrings reach the
model as schema descriptions, so they are written for it. Place and carrier
names are written by a model from untrusted mail, so `facts` is in
`FREE_TEXT_FIELDS`.

`domain` is unchanged: a travel booking is still `domain: travel`, and the
facts are an addition to it. The projection decides per tier whether `facts`
is projected (see [Tiers](tiers.md#typed-facts)), and the query model filters
and counts by them (see [Query model](query.md#typed-facts)).

**Older records.** `facts` changes `SCHEMA_VERSION`. `enrichment.upgrade` gives a
record written before it `{"travel": null}`, where stored rows are read, as for
`references`. Filling the facts in for the archive is a re-enrichment
(`--upgrade-stale`), on the same terms as above.

## Models

`CORPUS_ENRICH_MODEL` does the enrichment, and `CORPUS_AUDIT_MODEL` does the
secret audit. The audit model defaults to the enrichment model. Set it apart when
enrichment runs on a remote endpoint. The audit reads the secrets that an egress
gate redacts, so on a redacting route it would judge text with the secrets
already removed, and on a non-redacting one it would send them off-prem. Both
calls use the same enrichment endpoint (`CORPUS_ENRICH_API_BASE`, else
`CORPUS_OPENAI_API_BASE`; see [Configuration](configuration.md#endpoints-and-keys)),
so the audit is local only if the gateway serves the named model on-prem.

`CORPUS_MODEL_OPTIONS` tunes how each model is called, as JSON keyed by model
name. A model with no entry is called exactly as before.

```sh
CORPUS_MODEL_OPTIONS='{"bonsai-2-27b": {"inline_schema_refs": true,
  "extra_body": {"reasoning_effort": "none"}, "context_tokens": 32768}}'
```

- `inline_schema_refs`: inline the response schema's `$ref`s. llama.cpp can't
  resolve the nested references msgspec emits and silently drops the grammar, so
  the model answers unconstrained. `SCHEMA_VERSION` is unaffected.
- `extra_body`: fields merged into every request, e.g. `reasoning_effort`.
- `context_tokens`: the model's per-request context. Enrichment input is capped
  to what fits (after `CORPUS_ENRICH_MAX_INPUT_CHARS`, whichever is tighter), and
  a secret audit that would overflow is run on windows of text around each
  detected candidate, chunked to fit, with the chunk verdicts merged (worst
  severity per type). A secret deep in a long message is still audited rather
  than cut off or skipped.

## Schema and `SCHEMA_VERSION`

Every field of every struct is required in the schema sent to the model, even
though the structs carry defaults. Under guided decoding, an optional field is one
the grammar lets the model skip, and which fields get skipped depends on the
server, not the document. The classification axes collapsed to their defaults
until they were forced required. Later, vLLM dropped every entity list and
llama.cpp every deadline. Required means the model must decide. An empty list or a
null deadline is still a valid answer.

`SCHEMA_VERSION` is a hash of that schema, stored with each enrichment, so it
changes whenever the schema does. Records written under an older version stay
readable, because decoding fills defaults, and a normal run still counts them as
enriched. `--upgrade-stale` treats them as not yet done, so they are re-enriched.
It is opt-in: a scheduled run on a remote model would otherwise re-send the whole
archive after every schema change. Run it where the model is local.
