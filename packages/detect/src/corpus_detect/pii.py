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
from datetime import date

from presidio_analyzer.predefined_recognizers import (
    CreditCardRecognizer,
    CryptoRecognizer,
    EmailRecognizer,
    IbanRecognizer,
    UsBankRecognizer,
    UsSsnRecognizer,
)
from tldextract import TLDExtract

from .span import Span

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

# Presidio's context words are English only. Norwegian mail names a card or an
# account in Norwegian, so these count as context too. A US SSN has no
# Norwegian name; it is found only beside "ssn" or "social security".
_ACCOUNT_WORDS = (
    "konto", "kontoen", "kontonummer", "kontonummeret", "kontonr",
    "bankkonto", "bankkontoen", "bankkontoer",
)
_EXTRA_CONTEXT = {
    "credit_card": ("kort", "kortet", "kortnummer", "bankkort", "kredittkort", "betalingskort"),
    "us_bank_number": _ACCOUNT_WORDS,
}

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
# _RECOGNIZERS/_ENTITY_NAMES so the audit counts (and the detector fingerprint) are unchanged.
# Presidio validates an email match with module-level ``tldextract.extract``, whose
# default extractor fetches the Public Suffix List over HTTPS on first use and
# falls back to the bundled snapshot only after that fetch fails. Behind a
# deny-all egress policy the fetch stalls the first email-bearing text, so the
# suffix list here is the bundled snapshot only: no fetch, no disk cache.
class _OfflineEmailRecognizer(EmailRecognizer):
    """Presidio's email recognizer, validated against the bundled suffix list."""

    def __init__(self, extractor: TLDExtract | None = None) -> None:
        super().__init__()
        self._tld_extractor = (
            extractor if extractor is not None else TLDExtract(suffix_list_urls=(), cache_dir=None)
        )

    def validate_result(self, pattern_text: str) -> bool:
        """Accept a match whose domain ends in a known public suffix."""
        return self._tld_extractor(pattern_text).fqdn != ""


_EMAIL_RECOGNIZER = _OfflineEmailRecognizer()
_EMAIL_ENTITY = "email"

# Norwegian national identity number (fødselsnummer; D-nummer for non-residents,
# H-nummer for health-service help numbers, and Tenor synthetic test numbers): 11 digits, a DDMMYY birth date, and two mod-11 check digits.
# The checksum makes it as specific as an IBAN, so it counts on format alone, in
# the audit and for redaction. Presidio has no recognizer for it.
_NO_NATIONAL_ID = re.compile(r"(?<!\d)(\d{6})[ ]?(\d{5})(?!\d)")
_NO_NATIONAL_ID_ENTITY = "no_national_id"
_K1_WEIGHTS = (3, 7, 6, 1, 8, 9, 4, 5, 2)
_K2_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)

# Norwegian bank account number (kontonummer): 11 digits, written "NNNN.NN.NNNNN",
# "NNNN NN NNNNN" or bare, the last a mod-11 check digit over the first ten
# (_K2_WEIGHTS: the same check a national id ends with). The grouped forms are
# specific to account numbers and count on format alone; a bare 11-digit run is
# any reference number with a 1-in-11 chance of passing, so it needs an account
# word beside it, like the US bank recognizer. A bare run that is a valid national
# id is labelled a national id: its check is the account's plus a birth date and
# a second check digit, so it is the more specific reading.
#
# Each form is refused only where the digits continue its own grouping, so an
# amount after the number ("0100.12.00018 500 kr") does not hide it: the dotted
# form by another ".digit", the spaced form by two more spaced groups, the bare
# form by a decimal part. Mail spaces the groups with a no-break space (U+00A0)
# or a narrow one (U+202F) as often as with a plain space.
_NO_BANK_ACCOUNT = re.compile(
    r"(?<![\w+])(?:"
    r"(?<!\d\.)(?P<dotted>\d{4}\.\d{2}\.\d{5})(?!\.\d)"
    r"|(?<!\d[., \u00a0\u202f])(?P<spaced>\d{4}[ \u00a0\u202f]\d{2}[ \u00a0\u202f]\d{5})"
    r"(?![.,]\d)(?![ \u00a0\u202f]\d+[ \u00a0\u202f]\d)"
    r"|(?<!\d[.,])(?P<bare>\d{11})(?![.,]\d)"
    r")(?!\w)"
)
_NO_BANK_ACCOUNT_ENTITY = "no_bank_account"
_NO_BANK_ACCOUNT_CONTEXT = (*_ACCOUNT_WORDS, "account", "acct", "bank")

# Redaction-only, like email: phone numbers are everywhere in signatures, so they
# are not secrets for the audit, but they must not egress to an untrusted model.
# Digit runs are the noisiest pattern there is (order numbers, references, dates,
# amounts), so a number counts as a phone only with an international prefix
# ("+47", or "0047 " with a separator) or a phone word shortly before it ("Tlf:",
# "telefonnummer", "Mobil (privat)"); a bare 8-digit run passes. A run of digit
# groups is cut as soon as it holds a full national number (8 digits after any
# country code), so a date or org number that follows is neither swallowed nor
# lets the phone escape.
_PHONE = re.compile(
    r"(?<![\w+])(?P<intl>\+\d{1,3}[ .-]?|00\d{1,3}[ .-])?\(?\d{1,4}\)?(?:[ .-]?\d{1,4}){0,7}(?![\w])"
)
_PHONE_CONTEXT = re.compile(
    r"\b(?:tlf|telefon|mobil|mobile|mob|phone|tel|cell|ph|direkte|fax|faks)\w*"
    r"(?:\s*\([^)\n]{0,20}\)|\s+[^\W\d]{1,12})?\.?:?\s*$",
    re.IGNORECASE,
)
_PHONE_CONTEXT_WINDOW = 32
_NATIONAL_DIGITS = 8
_MAX_PHONE_DIGITS = 15
_PHONE_ENTITY = "phone"

#: Every type name a span or count from this module can carry.
ENTITY_TYPES = frozenset(
    {
        *_ENTITY_NAMES.values(),
        _NO_NATIONAL_ID_ENTITY,
        _NO_BANK_ACCOUNT_ENTITY,
        _EMAIL_ENTITY,
        _PHONE_ENTITY,
    }
)


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


def _check_digit(digits: str, weights: tuple[int, ...]) -> int | None:
    """The mod-11 check digit over *digits*, or None where none is valid (10)."""
    k = 11 - sum(int(d) * w for d, w in zip(digits, weights, strict=True)) % 11
    return 0 if k == 11 else None if k == 10 else k


def _valid_birth_date(ddmmyy: str) -> bool:
    """Whether DDMMYY is a calendar date in some century.

    Offsets mark the number's kind: day + 40 for a D-nummer, month + 40 for an
    H-nummer, and month + 80 for the synthetic test numbers the Norwegian tax
    administration issues (Tenor), which are never assigned to a person.
    """
    day, month, year = int(ddmmyy[:2]), int(ddmmyy[2:4]), int(ddmmyy[4:])
    if day > 40:
        day -= 40
    if month > 80:
        month -= 80
    elif month > 40:
        month -= 40
    for century in (1900, 2000):
        try:
            date(century + year, month, day)
        except ValueError:
            continue
        return True
    return False


def _is_no_national_id(digits: str) -> bool:
    """Whether 11 digits form a valid Norwegian national id: a fødselsnummer,
    D-nummer, H-nummer, or a Tenor synthetic test number."""
    if len(digits) != 11 or not digits.isdigit() or not _valid_birth_date(digits[:6]):
        return False
    k1 = _check_digit(digits[:9], _K1_WEIGHTS)
    if k1 is None or k1 != int(digits[9]):
        return False
    k2 = _check_digit(digits[:10], _K2_WEIGHTS)
    return k2 is not None and k2 == int(digits[10])


def _no_national_id_spans(text: str) -> Iterator[Span]:
    for m in _NO_NATIONAL_ID.finditer(text):
        if _is_no_national_id(m.group(1) + m.group(2)):
            yield Span(m.start(), m.end(), _NO_NATIONAL_ID_ENTITY, "pii")


def _no_bank_account_spans(text: str) -> Iterator[Span]:
    for m in _NO_BANK_ACCOUNT.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if _check_digit(digits[:10], _K2_WEIGHTS) != int(digits[10]):
            continue
        if m.group("bare") and (
            _is_no_national_id(digits)
            or not _context_near(text, m.start(), m.end(), list(_NO_BANK_ACCOUNT_CONTEXT))
        ):
            continue
        yield Span(m.start(), m.end(), _NO_BANK_ACCOUNT_ENTITY, "pii")


def _phone_end(m: re.Match[str]) -> int | None:
    """Where the phone number in a digit-group run ends, or None if it holds none.

    The run ends at the first group that completes a national number, so trailing
    groups belong to whatever comes next (a year, an org number).
    """
    intl = m.group("intl") or ""
    if intl[-1:].isdigit():
        # "+4741234567": without a separator the country code's length is
        # unknown, so every digit after the "+" counts toward the number.
        intl = intl[0]
    national, total = 0, sum(c.isdigit() for c in intl)
    for group in re.finditer(r"\d+", m.group()[len(intl):]):
        national += len(group.group())
        if total + national > _MAX_PHONE_DIGITS:
            return None
        if national >= _NATIONAL_DIGITS:
            end = m.start() + len(intl) + group.end()
            # Keep a closing parenthesis that belongs to the number.
            return end + 1 if m.string[end : end + 1] == ")" else end
    return None


def _phone_spans(text: str) -> Iterator[Span]:
    for m in _PHONE.finditer(text):
        end = _phone_end(m)
        if end is None:
            continue
        before = text[max(0, m.start() - _PHONE_CONTEXT_WINDOW) : m.start()]
        if m.group("intl") or _PHONE_CONTEXT.search(before):
            yield Span(m.start(), end, _PHONE_ENTITY, "pii")


def _iter_spans(text: str, *, for_egress: bool) -> Iterator[Span]:
    """Yield gated PII spans; the single detection pass behind both public views.

    Applies the same precision gates as before: ``_CONTEXT_REQUIRED`` types need a
    context word beside the match, the rest need ``_MIN_SCORE``. Email and phone
    numbers are yielded only ``for_egress`` (the redaction path); the
    national id and bank account numbers are yielded on both paths. The US bank
    recognizer matches any digit run beside an account word, so it yields nothing
    over a Norwegian number: the Norwegian checksum names it more precisely.
    """
    norwegian = [*_no_national_id_spans(text), *_no_bank_account_spans(text)]
    yield from norwegian
    for rec in _RECOGNIZERS:
        ctx_words = getattr(rec, "context", None) or []
        for res in rec.analyze(text=text, entities=rec.supported_entities, nlp_artifacts=None):
            name = _ENTITY_NAMES.get(res.entity_type)
            if not name:
                continue
            if name == "us_bank_number" and any(
                n.start < res.end and res.start < n.end for n in norwegian
            ):
                continue
            if name in _CONTEXT_REQUIRED:
                # A plausible match AND a context word right beside it — this is what
                # separates a real disclosure from an incidental number.
                words = [*ctx_words, *_EXTRA_CONTEXT.get(name, ())]
                if res.score >= 0.05 and _context_near(text, res.start, res.end, words):
                    yield Span(res.start, res.end, name, "pii")
            elif res.score >= _MIN_SCORE:
                yield Span(res.start, res.end, name, "pii")
    if for_egress:
        yield from _phone_spans(text)
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
    for span in _iter_spans(text, for_egress=False):
        counts[span.entity_type] = counts.get(span.entity_type, 0) + 1
    return ScanResult(secret_types=tuple(sorted(counts)), secret_counts=counts)


def scan_spans(text: str | None) -> list[Span]:
    """Detect identity/financial numbers and emails, returning match offsets.

    The redaction view: same identity/financial gates as :func:`scan`, plus email
    (egress-only). Offsets, not counts — the values are never returned.
    """
    if not text:
        return []
    return list(_iter_spans(text, for_egress=True))
