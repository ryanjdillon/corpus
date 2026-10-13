# corpus-detect

Deterministic PII and credential detection, and redaction built on it. The
detectors behind corpus's secret audit and tier projection, and behind the
[scan-gate](https://github.com/ryanjdillon/scan-gate) egress redactor, packaged alone
so a consumer gets them without the rest of corpus.

```python
from corpus_detect import detect, detect_spans, redact

detect("My SSN is 900-12-3456")        # {"us_ssn": 1}: types and counts, never values
redact("key AKIAIOSFODNN7EXAMPLE").text  # "key [REDACTED:aws_access_key]"
```

`redact(text, extra_spans=[Span(start, end, type, category)])` also masks
matches from a detector of your own. Offsets are code points (Python `str`
indices) with `0 <= start < end <= len(text)`. `category` is `secret` or `pii`,
and `type` matches `^[a-z0-9:_-]{1,64}$`. Anything else raises `ValueError`
without echoing the text; `validate_spans(spans, len(text))` runs the same
check up front. A secret always wins over an overlapping pii span, and every
character any span covers is masked. `counts` counts each span once, even when
it is masked as several pieces.
Every mask is `[REDACTED:<type>]`, which never holds any of the value, even for
a type corpus-detect does not know.

Install from a tag of the corpus repository:

```bash
pip install "corpus-detect @ git+https://github.com/ryanjdillon/corpus@<tag>#subdirectory=packages/detect"
```

`CORPUS_LEAKS_BIN` names a [Betterleaks](https://github.com/betterleaks/betterleaks)
binary to union its ruleset into the credential counts; `CORPUS_LEAKS_TIMEOUT`
bounds one run (seconds, default 30). Unset, the local rules run alone. If the
binary is missing, fails or times out, the scan falls back to the local rules;
pass `on_scanner` to `detect` or `leaks.scan` to learn which scanner ran.

See [Detectors](https://ryanjdillon.github.io/corpus/detect/) for the interface,
what each detector finds, and the version fingerprint.
