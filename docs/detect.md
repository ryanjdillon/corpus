# Detectors (corpus-detect)

The deterministic PII and credential detectors, and the redaction built on them,
are their own distribution: `corpus-detect`, in `packages/detect/` (import
package `corpus_detect`). corpus depends on it for the secret audit and tier
projection; the scan-gate egress redactor depends on it without the rest of
corpus (haystack, psycopg, mcp). Its only dependency is `presidio-analyzer`.

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
  [Configuration](configuration.md#scan-gate).
- `leaks`: credentials by local, high-precision rules (private-key blocks,
  provider-prefixed keys and tokens, JWTs), optionally unioned with
  [Betterleaks](https://github.com/betterleaks/betterleaks) for the counts.

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
