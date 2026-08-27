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
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from presidio_analyzer.predefined_recognizers import (
    CreditCardRecognizer,
    CryptoRecognizer,
    IbanRecognizer,
    UsBankRecognizer,
    UsSsnRecognizer,
)

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


@dataclass(frozen=True)
class ScanResult:
    """Secret-detection result — types and per-type counts only, never values."""

    secret_types: tuple[str, ...] = ()
    secret_counts: dict[str, int] = field(default_factory=dict)

    @property
    def has_secrets(self) -> bool:
        return bool(self.secret_types)


def _bump(counts: dict[str, int], name: str, n: int = 1) -> None:
    counts[name] = counts.get(name, 0) + n


def _context_near(text: str, start: int, end: int, words: list[str]) -> bool:
    """True if any of ``words`` appears within _CONTEXT_WINDOW chars of the match."""
    if not words:
        return False
    window = text[max(0, start - _CONTEXT_WINDOW) : min(len(text), end + _CONTEXT_WINDOW)]
    return any(re.search(rf"\b{re.escape(w)}\b", window, re.IGNORECASE) for w in words)


def scan(text: str | None) -> ScanResult:
    """Detect identity/financial numbers in ``text``, returning types + per-type
    counts. The matched values are never retained."""
    if not text:
        return ScanResult()
    counts: dict[str, int] = {}

    for rec in _RECOGNIZERS:
        ctx_words = getattr(rec, "context", None) or []
        for res in rec.analyze(
            text=text, entities=rec.supported_entities, nlp_artifacts=None
        ):
            name = _ENTITY_NAMES.get(res.entity_type)
            if not name:
                continue
            if name in _CONTEXT_REQUIRED:
                # A plausible match AND a context word right beside it — this is what
                # separates a real disclosure from an incidental number.
                if res.score >= 0.05 and _context_near(text, res.start, res.end, ctx_words):
                    _bump(counts, name)
            elif res.score >= _MIN_SCORE:
                _bump(counts, name)

    return ScanResult(secret_types=tuple(sorted(counts)), secret_counts=counts)
