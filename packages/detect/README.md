# corpus-detect

Deterministic PII and credential detection, and redaction built on it. The
detectors behind corpus's secret audit and tier projection, and behind the
scan-gate egress redactor, packaged alone so a consumer gets them without the
rest of corpus.

```python
from corpus_detect import detect, detect_spans, redact

detect("My SSN is 900-12-3456")        # {"us_ssn": 1}: types and counts, never values
redact("key AKIAIOSFODNN7EXAMPLE").text  # "key [REDACTED:aws_access_key]"
```

Install from a tag of the corpus repository:

```bash
pip install "corpus-detect @ git+https://github.com/ryanjdillon/corpus@<tag>#subdirectory=packages/detect"
```

`CORPUS_LEAKS_BIN` names a [Betterleaks](https://github.com/betterleaks/betterleaks)
binary to union its ruleset into the credential counts; `CORPUS_LEAKS_TIMEOUT`
bounds one run (seconds, default 30). Unset, the local rules run alone.

See [Detectors](https://ryanjdillon.github.io/corpus/detect/) for the interface,
what each detector finds, and the version fingerprint.
