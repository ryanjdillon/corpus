# Detectors (corpus-detect)

The deterministic PII and credential detectors, and the redaction built on them,
are their own distribution: `corpus-detect`, in `packages/detect/` (import
package `corpus_detect`). corpus depends on it for the secret audit and tier
projection. [scan-gate](https://github.com/ryanjdillon/scan-gate), the egress gate in
front of model providers, is a consumer of it: it depends on corpus-detect
without the rest of corpus (haystack, psycopg, mcp). corpus-detect's only dependency is `presidio-analyzer`.

## Interface

```python
from corpus_detect import detect, detect_spans, redact

detect(text)        # {secret_type: count}; types and counts, never values
detect_spans(text)  # [Span(start, end, entity_type, category)], overlaps unresolved
redact(text)        # RedactResult(text, findings, counts): each match -> [REDACTED:<type>]
```

Also exported: `Span` and `resolve_overlaps` (leftmost-longest, a credential
before an identity number on a tie), `RedactResult`, `ENTITY_TYPES` (every fixed
type name a detector emits), `LeaksSettings`, and `fingerprint_parts()`. The
`pii` and `leaks` submodules stay importable for a caller that needs one
detector alone, as the tier projection does.

Importing the package does not load Presidio (over a second); the names that
need it load on first use.

## The two detectors

- `pii`: identity and financial numbers through Presidio's pattern recognizers
  (no spaCy model), plus Norwegian national ids and bank account numbers by
  their mod-11 checks; email addresses and phone numbers are found for
  redaction only. What each finds, and the context words it needs, is in
  [What is redacted](#what-is-redacted).
- `leaks`: credentials by local, high-precision rules (private-key blocks,
  provider-prefixed keys and tokens, JWTs), optionally unioned with
  [Betterleaks](https://github.com/betterleaks/betterleaks) for the counts.

## What is redacted

What `redact` replaces, each by a `[REDACTED:<type>]` placeholder:

- credentials (`leaks`): private keys, API keys and tokens (scan-gate, by
  default, refuses a body holding a private key rather than redacting it);
- identity and financial numbers (`pii`): IBANs and crypto wallets on format,
  and US SSNs, credit cards and US bank numbers only with a context word right
  beside them, so an order number that looks like a card passes. The context
  words are Presidio's English ones plus Norwegian ones for a card (`kort`,
  `kortnummer`…) and an account (`konto`, `kontonummer`…); see
  [Tiers](tiers.md#what-financial_id-does-not-find) for what still passes;
- Norwegian national id numbers (fødselsnummer, D-nummer, H-nummer, and the tax
  administration's Tenor synthetic test numbers), only when both mod-11 check
  digits and the birth date are valid;
- Norwegian bank account numbers, only when the mod-11 check digit is valid:
  dotted (`NNNN.NN.NNNNN`) or spaced (`NNNN NN NNNNN`, no-break spaces too) on
  format, and a bare 11-digit run only with an account word beside it (`konto`,
  `kontonummer`, `kontonr`, `account`, `bank`…). A bare run that is also a valid national id
  is reported as a national id;
- email addresses;
- phone numbers, only when they are clearly phones: an international prefix
  (`+47`, or `0047` followed by a separator), or a phone word within 32
  characters before the number (`tlf`, `telefon`, `mobil`, `mob`, `phone`, `tel`,
  `cell`, `direkte`, `fax`, including compounds such as `telefonnummer`, and
  allowing a bracket or one short word in between, as in `Tlf (privat)` or
  `Phone number:`). A bare digit run passes, because order numbers, references,
  dates and amounts look the same and redacting them would degrade what the
  model reads. The redaction ends once the number holds 8 digits after any
  country code, so a date or org number that follows stays readable.

Email addresses and phone numbers are found for redaction only. They are too
common in mail to count as findings in the archive audit; the national id
and bank account numbers are counted there too.

## Settings

`leaks` reads its own two environment variables, so the package installs
without corpus's configuration. They keep corpus's `CORPUS_` names, so a
deployment sets the same variables either way:

| Variable | Purpose |
|---|---|
| `CORPUS_LEAKS_BIN` | Betterleaks binary name or path; empty (default) runs the local rules only. The image sets `betterleaks`. |
| `CORPUS_LEAKS_TIMEOUT` | seconds one Betterleaks run may take (default `30`) |

They are read once at import. `leaks.scan(text, config=LeaksSettings(...))`
takes explicit settings instead, as the tests do.

## Versions

corpus stamps each secret audit with `scan.SCAN_VERSION`: a hash of
`fingerprint_parts()` (the recognizers, entity names, gates, patterns and rules)
plus corpus's own recovery-code hint. Changing any detection input changes the
version; the order and shape of `fingerprint_parts()` are part of that contract.
`tests/corpus/test_versions.py` pins `SCAN_VERSION` and the projection's
`REDACTION_VERSION`, so a change to either is deliberate.

## Installing

In this repository, `just setup` installs it editable as a uv workspace member.
The image builds both wheels and installs them in one `pip install`, so corpus's
`corpus-detect` requirement resolves to that wheel. Elsewhere, install it from a
tag:

```bash
pip install "corpus-detect @ git+https://github.com/ryanjdillon/corpus@<tag>#subdirectory=packages/detect"
```
