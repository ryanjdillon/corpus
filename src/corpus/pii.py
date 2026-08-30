"""Deterministic PII/identity scan over document content.

Model-independent: Presidio's tested pattern recognizers for structured
identity/financial numbers. Machine credentials (keys, tokens, private keys) are a
separate, higher-precision concern handled in ``leaks``; recovery/backup codes have
no reliable deterministic signature and are left to the LLM confirmation stage.

Returns the *types* and *counts* of matches — never the matched values — so a
message can be flagged without ever copying the value into the index.

Precision matters more than recall here: pattern-only matching is inherently noisy
(a 9-digit datalogger reading looks like an SSN; a Luhn-valid order id looks like a
card). So the weak, format-only recognizers (US_DRIVER_LICENSE, US_PASSPORT) are
omitted entirely, and the ambiguous numeric types (SSN, credit card, bank routing)
are only counted when a matching context word sits *next to* the match, not merely
somewhere in the message. IBAN and crypto addresses carry their own checksum and
are specific enough to count on format alone.

Two views share one detection pass. :func:`scan` returns counts for the archive
audit; :func:`scan_spans` returns match offsets for redaction. Email addresses are
surfaced by :func:`scan_spans` only — they are ubiquitous in mail (every sender,
every signature) so flagging them in the archive audit is noise, but they must not
egress to an untrusted model, so the egress redactor strips them.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from presidio_analyzer.predefined_recognizers import (
    CreditCardRecognizer,
    CryptoRecognizer,
    EmailRecognizer,
    IbanRecognizer,
    UsBankRecognizer,
    UsSsnRecognizer,
)

from .redact import Span

# Presidio entity type -> our short, stable secret name.
_ENTITY_NAMES = {
    "US_SSN": "us_ssn",
    "CREDIT_CARD": "credit_card",
    "IBAN_CODE": "iban",
    "US_BANK_NUMBER": "us_bank_number",
    "CRYPTO": "crypto_wallet",
}

# Format-specific, checksum-backed types count on their own score.
_MIN_SCORE = 0.4

# Types whose format alone is too weak to trust: require a context word adjacent to
# the match (within _CONTEXT_WINDOW chars), not merely present in the message.
_CONTEXT_REQUIRED = {"us_ssn", "credit_card", "us_bank_number"}
_CONTEXT_WINDOW = 48

# Pattern recognizers used standalone (no AnalyzerEngine / spaCy model). Driver's
# license and passport are deliberately excluded — near-zero precision on prose.
_RECOGNIZERS = [
    UsSsnRecognizer(),
    CreditCardRecognizer(),
    IbanRecognizer(),
    UsBankRecognizer(),
    CryptoRecognizer(),
]

# Redaction-only: email is high-precision on format but far too common to flag in
# the archive audit, so it is scanned solely for the egress redactor. Kept out of
# _RECOGNIZERS/_ENTITY_NAMES so the audit counts (and SCAN_VERSION) are unchanged.
_EMAIL_RECOGNIZER = EmailRecognizer()
_EMAIL_ENTITY = "email"


@dataclass(frozen=True)
class ScanResult:
    """Secret-detection result — types and per-type counts only, never values."""

    secret_types: tuple[str, ...] = ()
    secret_counts: dict[str, int] = field(default_factory=dict)

    @property
    def has_secrets(self) -> bool:
        """Return True when any secret type was detected."""
        return bool(self.secret_types)


def _context_near(text: str, start: int, end: int, words: list[str]) -> bool:
    """True if any of ``words`` appears within _CONTEXT_WINDOW chars of the match."""
    if not words:
        return False
    window = text[max(0, start - _CONTEXT_WINDOW) : min(len(text), end + _CONTEXT_WINDOW)]
    return any(re.search(rf"\b{re.escape(w)}\b", window, re.IGNORECASE) for w in words)


def _iter_spans(text: str, *, include_email: bool) -> Iterator[Span]:
    """Yield gated PII spans; the single detection pass behind both public views.

    Applies the same precision gates as before: ``_CONTEXT_REQUIRED`` types need a
    context word beside the match, the rest need ``_MIN_SCORE``. Email is yielded
    only when ``include_email`` (the redaction path).
    """
    for rec in _RECOGNIZERS:
        ctx_words = getattr(rec, "context", None) or []
        for res in rec.analyze(text=text, entities=rec.supported_entities, nlp_artifacts=None):
            name = _ENTITY_NAMES.get(res.entity_type)
            if not name:
                continue
            if name in _CONTEXT_REQUIRED:
                # A plausible match AND a context word right beside it — this is what
                # separates a real disclosure from an incidental number.
                if res.score >= 0.05 and _context_near(text, res.start, res.end, ctx_words):
                    yield Span(res.start, res.end, name, "pii")
            elif res.score >= _MIN_SCORE:
                yield Span(res.start, res.end, name, "pii")
    if include_email:
        for res in _EMAIL_RECOGNIZER.analyze(
            text=text, entities=["EMAIL_ADDRESS"], nlp_artifacts=None
        ):
            if res.score >= _MIN_SCORE:
                yield Span(res.start, res.end, _EMAIL_ENTITY, "pii")


def scan(text: str | None) -> ScanResult:
    """Detect identity/financial numbers in ``text``.

    Returns the matched types and per-type counts; the matched values are never
    retained.
    """
    if not text:
        return ScanResult()
    counts: dict[str, int] = {}
    for span in _iter_spans(text, include_email=False):
        counts[span.entity_type] = counts.get(span.entity_type, 0) + 1
    return ScanResult(secret_types=tuple(sorted(counts)), secret_counts=counts)


def scan_spans(text: str | None) -> list[Span]:
    """Detect identity/financial numbers and emails, returning match offsets.

    The redaction view: same identity/financial gates as :func:`scan`, plus email
    (egress-only). Offsets, not counts — the values are never returned.
    """
    if not text:
        return []
    return list(_iter_spans(text, include_email=True))
