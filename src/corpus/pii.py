"""Deterministic secret scan over document content.

Model-independent: Presidio's tested pattern recognizers for structured secrets
(SSN, credit card, IBAN, bank number, crypto wallet, passport, driver license),
plus a couple of local regex checks (private-key headers, recovery/backup codes).

Returns the *types* and *counts* of secrets found — never the matched values — so a
message can be flagged as containing secrets without ever copying the secret into
the index. This is the authoritative, model-independent half of enrichment; the LLM
never makes the security-critical call.

Only structured *secrets* live here. Ordinary context PII (names, addresses,
phones) is retained as normal enrichment elsewhere, not treated as a secret.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from presidio_analyzer import RecognizerResult
from presidio_analyzer.predefined_recognizers import (
    CreditCardRecognizer,
    CryptoRecognizer,
    IbanRecognizer,
    UsBankRecognizer,
    UsLicenseRecognizer,
    UsPassportRecognizer,
    UsSsnRecognizer,
)

# Presidio entity type -> our short, stable secret name.
_ENTITY_NAMES = {
    "US_SSN": "us_ssn",
    "CREDIT_CARD": "credit_card",
    "IBAN_CODE": "iban",
    "US_BANK_NUMBER": "us_bank_number",
    "CRYPTO": "crypto_wallet",
    "US_PASSPORT": "us_passport",
    "US_DRIVER_LICENSE": "us_driver_license",
}

# Only count a Presidio hit at/above this confidence (Luhn/checksum-validated
# recognizers score high; weak partial matches fall below this).
_MIN_SCORE = 0.4

# Pattern recognizers used standalone (no AnalyzerEngine / spaCy model).
_RECOGNIZERS = [
    UsSsnRecognizer(),
    CreditCardRecognizer(),
    IbanRecognizer(),
    UsBankRecognizer(),
    UsPassportRecognizer(),
    UsLicenseRecognizer(),
    CryptoRecognizer(),
]

# --- local regex for the high-signal / email-specific cases ---
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")
# A grouped code token (ABCD-1234-EF56); only treated as a recovery/backup code
# when recovery context words are present in the same message.
_CODE_TOKEN = re.compile(r"\b(?:[A-Za-z0-9]{4,6}[- ]){1,}[A-Za-z0-9]{4,6}\b")
_RECOVERY_CTX = re.compile(
    r"\b(recovery|backup|two[- ]?factor|2fa|verification|one[- ]?time|otp|passcode)\b",
    re.IGNORECASE,
)


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


def scan(text: str | None) -> ScanResult:
    """Detect secrets in ``text``, returning types + per-type counts. The matched
    values are never retained."""
    if not text:
        return ScanResult()
    counts: dict[str, int] = {}

    for rec in _RECOGNIZERS:
        # Presidio boosts a match when nearby context words appear, but that needs
        # the AnalyzerEngine's NLP artifacts (spaCy). Replicate it cheaply: if any
        # of the recognizer's own context words are present, boost its matches.
        ctx_words = getattr(rec, "context", None) or []
        boost = 0.35 if any(
            re.search(rf"\b{re.escape(w)}\b", text, re.IGNORECASE) for w in ctx_words
        ) else 0.0
        results: list[RecognizerResult] = rec.analyze(
            text=text, entities=rec.supported_entities, nlp_artifacts=None
        )
        for res in results:
            name = _ENTITY_NAMES.get(res.entity_type)
            if name and res.score + boost >= _MIN_SCORE:
                _bump(counts, name)

    private_keys = _PRIVATE_KEY.findall(text)
    if private_keys:
        _bump(counts, "private_key", len(private_keys))

    if _RECOVERY_CTX.search(text):
        code_tokens = _CODE_TOKEN.findall(text)
        if code_tokens:
            _bump(counts, "recovery_code", len(code_tokens))

    return ScanResult(secret_types=tuple(sorted(counts)), secret_counts=counts)
