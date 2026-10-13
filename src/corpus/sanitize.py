"""Project the sensitive corpus (documents ⨝ enrichments) into the sanitized DB.

Applies the tier's projection :class:`Policy` on the way down.

**Included** (cloud-safe): opaque ids, timestamps, the enrichment classification
signal, coarse sender-domain (org, never the person), LLM `one_line`,
`action_summary`, `organizations`, typed travel facts (`facts`), and the
provider's web link (`uri`) when it is an https URL. **Excluded** (never leaves
local): raw `content`, raw `subject`, full sender/recipient addresses, secret material,
raw-body embeddings, any other `uri` (an IMAP URI names the mail host and the
private folder).

The free-text columns (:data:`REDACTED_COLS`) lose any credential (``leaks``)
under every policy, and are redacted per entity class (:class:`Redaction`): people's names, reference ids, financial and identity
numbers, email addresses, phone numbers and organizations are each kept,
masked, replaced or dropped. At/above the sensitivity gate a row is either
redacted the same way or degraded to a content-free stub (domain + action only),
as the policy's ``gate_action`` says.

One-directional (sensitive → sanitized), incremental (re-projects a document
after it is re-enriched, after the policy changes, or every document on
request), resumable, and re-embeds a row only when its embedded text changed.
A re-projection rewrites the text at once and leaves the re-embedding to a
capped phase of this and later syncs (see :func:`run_sync`). The policy is the
tier's ``projection`` (see ``tiers``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import msgspec
from corpus_detect import Span, leaks, resolve_overlaps

from . import telemetry
from .embeddings import Embedder
from .enrichment import (
    UNKNOWN_ROLE,
    Place,
    ReferenceKind,
    SensitivityLevel,
    Travel,
    country_code,
)
from .sanitized_store import COLS

if TYPE_CHECKING:
    from .store import DocumentStore
    from .tiers import Tier

log = logging.getLogger("corpus.sanitize")

_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}

KeepOrDrop = Literal["keep", "drop"]
NameAction = Literal["keep", "initial", "role", "drop"]
IdAction = Literal["keep", "mask", "drop"]
GateAction = Literal["redact", "stub"]


class _Config(
    msgspec.Struct, forbid_unknown_fields=True, frozen=True, kw_only=True, omit_defaults=True
):
    pass


class Redaction(_Config):
    """What the projection does with each entity class in every free-text column.

    The defaults keep what carries context and hide what is only an identifier:
    an organization says what an item is about, a person's role says who it
    involves, but a booking code, a card number, an address or a phone number
    adds nothing a reader of the summary needs.

    Attributes:
        organization: ``keep`` or ``drop`` the organizations, in the text and as
            the ``organizations`` column.
        person_name: ``keep`` a name, shorten it to its ``initial``s ("K. N."),
            replace it with the person's ``role`` ("a friend"), or ``drop`` it.
        reference_id: ``keep``, ``mask`` or ``drop`` a value from the
            enrichment's ``references`` (a booking code, an order number).
        financial_id: ``keep``, ``mask`` or ``drop`` a card, IBAN, bank or
            crypto account, or national identity number the ``pii`` scan finds:
            a card or account number and a US SSN only beside a context word,
            so a bare one passes (see ``docs/tiers.md``).
        email_address: ``keep`` or ``drop`` an email address.
        phone: ``keep`` or ``drop`` a phone number.
        place_name: ``keep`` or ``drop`` the name of a place in the ``facts``
            column (a trip's origin and destination), which keeps its country
            either way. A kept name is redacted like the free text, but no
            class finds a street address in it.
    """

    organization: KeepOrDrop = "keep"
    person_name: NameAction = "role"
    reference_id: IdAction = "mask"
    financial_id: IdAction = "mask"
    email_address: KeepOrDrop = "drop"
    phone: KeepOrDrop = "drop"
    place_name: KeepOrDrop = "drop"


#: Every class kept: the free text exactly as the enrichment wrote it.
KEEP_ALL = Redaction(
    organization="keep", person_name="keep", reference_id="keep", financial_id="keep",
    email_address="keep", phone="keep", place_name="keep",
)

#: Bump when the projection's output changes for an unchanged policy (the
#: matching, a placeholder), so the next sync re-projects every row.
REDACTION_VERSION = 2


class Policy(_Config):
    """A projected tier's policy: its columns, its gate, and its redaction.

    Attributes:
        fields: The projected columns; any other column is stored as NULL.
        sensitivity_gate: The sensitivity at/above which ``gate_action`` applies.
        gate_action: At/above the gate, ``redact`` the free text like any other
            row, or ``stub`` it: a content-free line naming only the domain and
            the action. A row with no entity lists is stubbed either way.
        redact: What happens to each entity class in the free text.
    """

    fields: tuple[str, ...] = COLS
    sensitivity_gate: SensitivityLevel = SensitivityLevel.high
    gate_action: GateAction = "redact"
    redact: Redaction = Redaction()

    def digest(self) -> str:
        """A fingerprint of the effective policy, stored with each projected row.

        Every setting is written out, defaults included, so a changed default
        changes the digest as much as a changed file does. The column order is
        not part of it: it does not change a row. Nor is ``place_name`` when the
        policy does not project ``facts``, the only column it applies to, so
        such a tier is not re-projected for a setting that changes nothing.
        """
        redact = {name: getattr(self.redact, name) for name in Redaction.__struct_fields__}
        if "facts" not in self.fields:
            del redact["place_name"]
        effective = {
            "version": REDACTION_VERSION,
            "fields": sorted(set(self.fields)),
            "sensitivity_gate": self.sensitivity_gate.value,
            "gate_action": self.gate_action,
            "redact": redact,
        }
        blob = json.dumps(effective, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]


#: The sanitized tier's policy when the tiers file sets none.
DEFAULT_POLICY = Policy()
#: The projection before redaction policies: free text verbatim below the gate,
#: a stub at or above it.
STUB_POLICY = Policy(gate_action="stub", redact=KEEP_ALL)

#: The projected columns holding free text a model wrote, which the policy
#: redacts (``facts``: its carrier, and any place name the policy keeps). The
#: sanitized table has no other: ``sender_domain`` and ``thread_id`` come from mail headers, and the
#: enrichment's other free-text fields (``key_points``, ``appointments``,
#: ``topics``, ``locations``, where a name can still appear) have no column, so
#: no ``fields`` list can project them.
REDACTED_COLS = ("one_line", "action_summary", "organizations", "facts")


def _sender_domain(from_addr: str | None) -> str | None:
    """Return the sending organization's domain (e.g. 'chase.com'), never the person.

    Personal-provider senders (gmail.com) yield little; the descriptive signal for
    those lives in one_line / organizations instead.
    """
    if not from_addr or "@" not in from_addr:
        return None
    dom = from_addr.rsplit("@", 1)[-1].strip().strip(">").lower()
    return dom or None


#: Columns every projected row keeps, whatever the tier's ``fields``: the key,
#: and the version the incremental sync compares.
BOOKKEEPING = ("id", "enriched_at")


#: The longest uri projected. A provider web link is far shorter; a longer one is
#: not a link a consumer should be handed.
MAX_URI = 2048


def web_link(uri: object) -> str | None:
    """Return *uri* if it is an https URL without credentials, else ``None``.

    A Gmail message's uri is a web link a consumer can hand the owner. An IMAP
    uri (``imap://host/<folder>;UIDVALIDITY=…``) would expose the mail host and
    private folder names to the cloud-facing tier, so only https passes.

    The column is trusted, so the check is on the whole string, not on what
    ``urlsplit`` parses: it silently drops tab, CR and LF, which would let text
    after a line break ride into the column. Any whitespace or unprintable
    character, or more than :data:`MAX_URI` characters, is refused.
    """
    if not isinstance(uri, str) or len(uri) > MAX_URI:
        return None
    if any(c.isspace() or not c.isprintable() for c in uri):
        return None
    try:
        parts = urlsplit(uri)
        if parts.scheme != "https" or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None:
            return None
    except ValueError:  # a malformed netloc (e.g. a bad port or IPv6 literal)
        return None
    return uri


# --- redaction -------------------------------------------------------------------

#: ``pii`` span types and the class that governs them.
_PII_CLASS = {
    "email": "email_address",
    "phone": "phone",
    **{t: "financial_id" for t in (
        "credit_card", "iban", "us_bank_number", "crypto_wallet", "no_national_id",
        "no_bank_account", "us_ssn",
    )},
}
#: A name's word: letters, with inner apostrophes or hyphens ("O'Neil", "Anne-Marie").
_NAME_TOKEN = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")
#: Titles that precede a name, too common to match on their own.
_TITLES = frozenset({"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "herr", "fru", "frk"})
#: The shortest name word matched on its own; shorter ones are initials.
_MIN_NAME_TOKEN = 2
#: The shortest folded name word matched on its own by its folded spelling.
_MIN_FOLDED_WORD = 4
#: Reference values with fewer letters and digits than this are not matched:
#: a "7" would hit every 7 in the text.
_MIN_REFERENCE = 3
#: A masked value shows its last two letters or digits, and only when it has at
#: least this many, so a short value is hidden whole and a longer one shows at
#: most a quarter of itself.
_MASK_TAIL_FROM = 8
_MASK = "••••"
_ARTICLES = ("a ", "an ", "the ", "my ", "your ", "his ", "her ", "their ", "our ")
_REFERENCE_KINDS = frozenset(k.value for k in ReferenceKind)
#: What may stand between two characters of a reference: a little space and up
#: to three punctuation marks ("AB-1234", "AB 12 34", "AB - 1234", "AB---1234"),
#: never a line break or a word. Atomic, so a near miss fails in linear time.
_REFERENCE_SEP = r"(?>[ \t]{0,2}(?:[^\w\s]|_){0,3}[ \t]{0,2})"
#: Letters folded to their base before matching, beyond what NFKD decomposes:
#: "Bjørn Ås" is often written "Bjorn As", "Kjær" as "Kjaer".
_FOLD = str.maketrans({"ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe"})
_SPACES = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?)])")


def fold(text: str) -> str:
    """*text* as names are matched: "Bjørn Ås" -> "bjorn as", "Kjær" -> "kjaer"."""
    return _folded(text)[0]


def _folded(text: str) -> tuple[str, list[int], list[int]]:
    """*text* folded, and where each folded character came from.

    Each character is decomposed (NFKD), loses its accents, has the letters
    NFKD leaves whole replaced (:data:`_FOLD`) and is casefolded. A character
    can fold to several ("æ" to "ae") or to none (a combining accent), so the
    lengths differ; ``starts[i]`` and ``ends[i]`` are the original span of
    folded character ``i``. A character that folds to none is counted into
    the one before, so a match ending there takes its accent along.
    """
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for i, ch in enumerate(text):
        decomposed = unicodedata.normalize("NFKD", ch.translate(_FOLD))
        f = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
        if not f:
            if ends:
                ends[-1] = i + 1
            continue
        out.append(f)
        starts += [i] * len(f)
        ends += [i + 1] * len(f)
    return "".join(out), starts, ends


def _bounded(parts: list[str], sep: str) -> re.Pattern[str]:
    """Match *parts* in order, joined by *sep*, as whole words, ignoring case."""
    body = sep.join(re.escape(p) for p in parts)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def _spelled(words: list[str]) -> tuple[re.Pattern[str], bool]:
    """A pattern for the name *words*, and whether it runs on the folded text.

    A name is matched folded, so "Bjorn As" finds "Bjørn Ås". A single short
    word that folding changes can fold into an ordinary one ("Ås" into "as"),
    so it is matched only as it is spelled, ignoring case.
    """
    folded = [fold(w) for w in words]
    short = len(folded) == 1 and len(folded[0]) < _MIN_FOLDED_WORD
    if short and folded[0] != words[0].casefold():
        return _bounded(words, ""), False
    return _bounded(folded, r"\s+"), True


def mask(label: str, value: str) -> str:
    """The masked form of *value*: ``[<label> ••••<last two>]``.

    The shape says what was there and lets a reader tell two values apart
    ("the booking ending 42"); the value itself is hidden. A value shorter than
    :data:`_MASK_TAIL_FROM` letters and digits shows no tail.
    """
    chars = [c for c in value if c.isalnum()]
    tail = "".join(chars[-2:]) if len(chars) >= _MASK_TAIL_FROM else ""
    return f"[{label} {_MASK}{tail}]"


def initials(name: str) -> str:
    """Each word of *name* as its initial: "Kari Nordmann" -> "K. N."."""
    return " ".join(f"{t[0].upper()}." for t in _NAME_TOKEN.findall(name))


def role_phrase(role: str | None) -> str:
    """The words that stand in for a person: their role with an article ("a friend").

    A record enriched before roles has the role ``unknown``, which reads as
    "someone".
    """
    role = " ".join((role or "").split())
    if not role or role.lower() == UNKNOWN_ROLE:
        return "someone"
    if role.lower().startswith(_ARTICLES):
        return role
    return f"{'an' if role[0].lower() in 'aeiou' else 'a'} {role}"


def _entries(enr: dict, key: str) -> list[dict]:
    value = enr.get(key)
    return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []


def has_entities(enr: dict) -> bool:
    """Whether the record names any person or reference to redact against.

    A record without either cannot show its summaries were written in roles, so
    at/above the gate it is stubbed rather than redacted.
    """
    return bool(_entries(enr, "people") or _entries(enr, "references"))


class _Redactor:
    """One record's redaction: its entity patterns, compiled once for its fields."""

    def __init__(self, enr: dict, rules: Redaction) -> None:
        self._rules = rules
        self._active = rules != KEEP_ALL
        # (pattern, whether it runs on the folded text, class, replacement for
        # a match): the entity lists, the authoritative names and values,
        # first, so they win a tie.
        self._patterns: list[tuple[re.Pattern[str], bool, str, Callable[[str], str]]] = []
        if not self._active:
            return
        if rules.person_name != "keep":
            for person in _entries(enr, "people"):
                self._add_person(person)
        if rules.reference_id != "keep":
            for ref in _entries(enr, "references"):
                self._add_reference(ref)
        if rules.organization == "drop":
            for org in enr.get("organizations") or []:
                words = str(org).split()
                if words:
                    self._patterns.append((*_spelled(words), "organization", _gone))

    def _add_person(self, person: dict) -> None:
        name = person.get("name")
        if not isinstance(name, str):
            return
        spelled = [w for w in _NAME_TOKEN.findall(name) if fold(w) not in _TITLES]
        action = self._rules.person_name
        if action == "initial":
            replace = initials
        elif action == "role":
            phrase = role_phrase(person.get("role"))
            replace = lambda _m: phrase
        else:
            replace = _gone
        if len(spelled) > 1:
            self._patterns.append((*_spelled(spelled), "person_name", replace))
        # A name often appears in part: the first name alone, or the surname.
        for word in spelled:
            if len(word) >= _MIN_NAME_TOKEN:
                self._patterns.append((*_spelled([word]), "person_name", replace))

    def _add_reference(self, ref: dict) -> None:
        value = ref.get("value")
        if not isinstance(value, str):
            return
        chars = [c for c in fold(value) if c.isalnum()]
        if len(chars) < _MIN_REFERENCE:
            return
        kind = ref.get("kind") if ref.get("kind") in _REFERENCE_KINDS else "reference"
        if self._rules.reference_id == "mask":
            replace = lambda m: mask(kind, m)
        else:
            replace = _gone
        # Written with or without separators, wherever they fall: "AB-1234",
        # "AB 12 34" and "AB1234" are one value.
        self._patterns.append((_bounded(chars, _REFERENCE_SEP), True, "reference_id", replace))

    def _spans(self, text: str) -> Iterator[tuple[Span, str]]:
        # The entity patterns are folded (see fold), so they run on the folded
        # text; each match maps back to the original characters it came from.
        folded, starts, ends = _folded(text)
        for pattern, on_folded, cls, replace in self._patterns:
            for m in pattern.finditer(folded if on_folded else text):
                if on_folded:
                    start, end = starts[m.start()], ends[m.end() - 1]
                else:
                    start, end = m.span()
                yield Span(start, end, cls, "pii"), replace(text[start:end])
        from corpus_detect import pii

        for span in pii.scan_spans(text):
            # A type the scan learns later is an identifier until classified.
            cls = _PII_CLASS.get(span.entity_type, "financial_id")
            action = getattr(self._rules, cls)
            # A kept match claims nothing: a name or a reference inside a kept
            # email address is still redacted.
            if action == "mask":
                yield span, mask(span.entity_type, text[span.start:span.end])
            elif action == "drop":
                yield span, ""

    def text(self, value: object) -> str | None:
        """*value* with every credential and governed entity replaced.

        ``None`` if nothing is left. Credentials are replaced whatever the
        policy, before anything else: no match the policy keeps can cover one.
        """
        if not isinstance(value, str) or not value:
            return value if isinstance(value, str) else None
        secrets = credential_spans(value)
        if not self._active and not secrets:
            return value
        replacements: dict[Span, str] = {s: credential_placeholder(s) for s in secrets}
        if self._active:
            found: dict[Span, str] = {}
            for span, replacement in self._spans(value):
                if not any(_overlap(span, s) for s in secrets):
                    found.setdefault(span, replacement)
            replacements |= _strictest(found)
        kept = resolve_overlaps(list(replacements))
        out: list[str] = []
        cursor = 0
        dropped = False
        for span in kept:
            out.append(value[cursor:span.start])
            out.append(replacements[span])
            dropped = dropped or not replacements[span]
            cursor = span.end
        out.append(value[cursor:])
        redacted = "".join(out)
        if dropped:
            redacted = _SPACE_BEFORE_PUNCT.sub(r"\1", _SPACES.sub(" ", redacted)).strip()
        return redacted or None

    def names(self, values: object) -> list[str] | None:
        """The organizations column: ``None`` when dropped, else each entry redacted."""
        if self._rules.organization == "drop" or not isinstance(values, list):
            return None
        out = [r for r in (self.text(v) for v in values) if r]
        return out or None


def _gone(_match: str) -> str:
    return ""


def _overlap(a: Span, b: Span) -> bool:
    return a.start < b.end and b.start < a.end


def _merged(spans: Iterable[Span]) -> list[Span]:
    """*spans* as runs: any that overlap merged into one span covering them all."""
    runs: list[Span] = []
    for span in sorted(spans, key=lambda s: s.start):
        if runs and span.start < runs[-1].end:
            last = runs[-1]
            runs[-1] = Span(last.start, max(last.end, span.end), last.entity_type, last.category)
        else:
            runs.append(span)
    return runs


def _strictest(found: dict[Span, str]) -> dict[Span, str]:
    """Non-overlapping replacements from *found*, the stricter action winning.

    A dropped match beats a masked or replaced one it overlaps, and takes its
    extent too: dropping only the overlap would leave the rest of the other
    value in the text. Dropped matches merge into one run wherever they touch,
    so none is cut short by another; a run that widens can reach another
    masked match, which it then takes as well. The masked and replaced matches
    left are resolved leftmost-longest (``resolve_overlaps``).
    """
    runs = _merged(s for s, r in found.items() if not r)
    others = [s for s, r in found.items() if r]
    while hit := [s for s in others if any(_overlap(s, r) for r in runs)]:
        others = [s for s in others if s not in hit]
        runs = _merged([*runs, *hit])
    return {**dict.fromkeys(runs, ""), **{s: found[s] for s in resolve_overlaps(others)}}


def credential_spans(text: str) -> list[Span]:
    """The credentials in *text* (``leaks``' local rules), overlaps resolved.

    A summary should hold none, but a model can copy a token from the mail
    into one; no policy can keep it.
    """
    return resolve_overlaps(leaks.scan_spans(text))


def credential_placeholder(span: Span) -> str:
    """The token a credential becomes: ``redact``'s ``[REDACTED:<type>]`` form."""
    return f"[REDACTED:{span.entity_type}]"


#: A carrier that reads as an address or a person rather than an operator: a
#: possessive ("Kari's"), or a number beside a street word ("Storgata 5",
#: "12 Rue de Rivoli", "1 Main Street"). Matched on the folded text (see :func:`fold`).
_STREET = (
    r"(?:\w*(?:gata|gate|gaten|veien|vegen|vei|veg|strasse|plass)"
    r"|street|st|road|rd|avenue|ave|lane|ln|boulevard|blvd|rue|via|calle|alle)"
)
_NOT_A_CARRIER = re.compile(
    rf"\w['\u2019]s\b|\ws['\u2019](?:\s|$)"
    rf"|\d+[a-z]?\s+(?:\w+\s+){{0,2}}{_STREET}\b|\b{_STREET}\s+\d+"
)


def _carrier(value: str | None) -> str | None:
    """*value* unless it reads as an address or a person (:data:`_NOT_A_CARRIER`)."""
    if value is None or _NOT_A_CARRIER.search(fold(value)):
        return None
    return value


def project_facts(
    facts: object, text: Callable[[object], str | None], place_name: KeepOrDrop = "drop"
) -> dict | None:
    """The ``facts`` column for an enrichment's *facts*: its travel, names redacted.

    The record is checked against the schema here (``enrichment.Travel``, whose
    places keep only an ISO country code), so the column holds ISO dates and
    upper-case country codes, which the query model compares and casts.

    A place keeps its country and, only if *place_name* is ``keep``, its name:
    a place a message names can be someone's home ("Kari's place, Storgata 5"),
    and no redaction class finds a street address. The carrier is kept, unless
    it reads as an address or a person. *text* redacts each name kept as the
    free text is. ``None`` when there are no travel facts, or none survive.
    """
    raw = facts.get("travel") if isinstance(facts, dict) else None
    try:
        travel = msgspec.convert(raw, Travel | None)
    except msgspec.ValidationError:
        return None
    if travel is None:
        return None

    def place(p: Place | None) -> dict | None:
        if p is None:
            return None
        kept = {"country": country_code(p.country)}
        if place_name == "keep":
            kept = {"name": text(p.name), **kept}
        return kept if any(kept.values()) else None

    out = {
        "start": travel.start.isoformat() if travel.start else None,
        "end": travel.end.isoformat() if travel.end else None,
        "origin": place(travel.origin),
        "destination": place(travel.destination),
        "carrier": text(_carrier(travel.carrier)),
    }
    return {"travel": out} if any(out.values()) else None


def project(
    doc_id: str,
    meta: dict | None,
    enr: dict | None,
    policy: Policy = DEFAULT_POLICY,
) -> dict:
    """Build the sanitized row for one enriched document under *policy*.

    The free-text columns are redacted per entity class. At/above the gate, a
    ``stub`` policy, or a record with no ``people`` or ``references`` to redact
    against, yields a content-free stub that keeps the item plannable. A column
    outside the policy's ``fields`` is stored as NULL; the bookkeeping columns
    are kept.
    """
    meta = meta or {}
    enr = enr or {}
    sens = enr.get("sensitivity_level") or "none"
    domain = enr.get("domain") or "other"
    action_type = enr.get("action_type") or "none"
    gated = _RANK.get(sens, 0) >= _RANK[policy.sensitivity_gate.value]
    if gated and (policy.gate_action == "stub" or not has_entities(enr)):
        stub = f"[sensitive] {domain} item"
        if enr.get("requires_action"):
            stub += f" — action: {action_type}"
        one_line, action_summary, organizations, facts = stub, None, None, None
    else:
        redactor = _Redactor(enr, policy.redact)
        one_line = redactor.text(enr.get("one_line"))
        action_summary = redactor.text(enr.get("action_summary"))
        organizations = redactor.names(enr.get("organizations") or None)
        facts = project_facts(enr.get("facts"), redactor.text, policy.redact.place_name)
    row = {
        "id": doc_id,
        "source": meta.get("source"),
        "account": meta.get("account"),
        "thread_id": meta.get("thread_id"),
        "sent_at": meta.get("sent_at") or None,
        "sender_domain": _sender_domain(meta.get("from_addr")),
        "domain": domain,
        "category": enr.get("category"),
        "transactional_type": enr.get("transactional_type"),
        "importance": enr.get("importance") or "low",
        "requires_action": bool(enr.get("requires_action")),
        "action_type": action_type,
        "action_summary": action_summary,
        "deadline": enr.get("deadline") or None,
        "waiting_on": enr.get("waiting_on") or "none",
        "time_sensitive": bool(enr.get("time_sensitive")),
        "suggested_disposition": enr.get("suggested_disposition") or "keep",
        "sensitivity_level": sens,
        "one_line": one_line,
        "organizations": organizations,
        "enriched_at": None,  # set by run_sync from the source enriched_at
        "uri": web_link(meta.get("uri")),
        "facts": facts,
    }
    keep = set(policy.fields) | set(BOOKKEEPING)
    return {col: (value if col in keep else None) for col, value in row.items()}


#: Why a sync embedded nothing in its embedding phase (``reembed_skipped``).
REEMBED_SKIPPED = "re-embedding skipped: another sync of this tier holds the re-embed lock"
#: What a sync that waits for the re-embed lock (``reembed_wait``) reports first.
REEMBED_WAITING = "waiting for the re-embed lock held by another sync of this tier…"


def run_sync(
    store,
    *,
    documents: DocumentStore,
    source: str | None = None,
    limit: int = 0,
    force: bool = False,
    policy: Policy = DEFAULT_POLICY,
    reembed_limit: int | None = None,
    reembed_wait: bool = False,
    on_reembed_wait: Callable[[], None] | None = None,
    embedder: Embedder | None = None,
    batch_size: int = 64,
    projector: Callable[[str, dict | None, dict | None, Policy], dict] = project,
    tier: Tier | None = None,
    metrics: telemetry.SyncMetrics | None = None,
) -> dict[str, int]:
    """Project enriched documents into the sanitized store under *policy*.

    Runs in two phases. The **text phase** projects a row when it is new, when
    its source enrichment changed since the last sync, or when it was projected
    under another policy (its stored ``policy_hash``), so a changed policy
    reaches every row without a flag; ``force`` re-projects every row
    regardless. The **embedding phase** then re-embeds rows whose text the
    text phase changed, at most ``reembed_limit`` of them (0: all; ``None``
    skips the phase). One sync re-embeds a tier at a time: when another holds
    the tier's re-embed lock (``SanitizedStore.reembed_lock``), the phase is
    skipped, and the counts say so with ``reembed_skipped`` (1). With
    ``reembed_wait`` the phase waits for the lock instead, and is never
    skipped; ``on_reembed_wait`` is called before it blocks, only if it must.
    The text phase takes no such lock.

    A new or re-enriched row is embedded in the text phase when its
    ``one_line`` (the embedded text) changed, as part of keeping the tier
    current. A row re-projected only because of the policy or ``force`` is
    never embedded there: its columns and ``policy_hash`` are rewritten and its
    stored vector kept, so a policy change replaces every placeholder in one
    run with no embedding calls. If its ``one_line`` changed, the row is left
    *embed-stale* (its ``embedded_policy_hash`` differs from its
    ``policy_hash``) for the embedding phase of this or a later run, which
    takes the newest first.

    ``limit`` caps the embedding calls of the text phase (0: no cap), which
    embeds new rows and re-enriched rows whose ``one_line`` changed. A new row
    past the cap is not written, and is counted as ``capped``: it has no
    stored row, so nothing is served for it until a later run takes it, in the
    same newest-first order. A re-enriched row past the cap is projected all
    the same, keeping its stored vector and left embed-stale for the embedding
    phase, as a re-projection is. Every other row the text phase projects keeps
    its vector too, so the cap never leaves a row a placeholder. ``projector``
    builds each row (``project`` unless a caller substitutes its own and
    accepts the same arguments). ``store`` is an open SanitizedStore and
    ``documents`` an open DocumentStore on the source tier, whose lifecycles
    the caller owns.

    Rows whose written columns are unchanged are not rewritten, so a run
    interrupted and run again redoes nothing it finished, and counts those
    rows as skipped.

    Returns the counts: ``scanned``, ``synced`` (rows written), ``skipped``,
    ``reprojected`` (rows rewritten only because of the policy or ``force``),
    ``embedded`` (vectors stored, in either phase), ``capped`` (new rows
    ``limit`` left for a later run), and, after the run,
    ``pending`` (rows still projected under another policy, which a reader
    serves as a placeholder) and ``embed_stale`` (rows still awaiting a new
    vector). ``reembed_skipped`` is present only when the embedding phase was
    skipped for another sync's lock.

    Given the ``tier`` synced into, the counts are also recorded as metrics of
    that tier, on ``metrics`` (``telemetry.sync_metrics`` unless a caller
    substitutes its own); without one, nothing is recorded.
    """
    embedder = embedder or Embedder()
    digest = policy.digest()
    state = store.synced_state()
    counts = {
        "scanned": 0, "synced": 0, "skipped": 0, "reprojected": 0, "embedded": 0, "capped": 0,
    }
    to_embed: list[dict] = []
    # Rows rewritten keeping their vector: re-enriched ones, and re-projected ones.
    to_update: list[dict] = []
    to_reproject: list[dict] = []
    # Rows queued for embedding. The limit caps these, not rows scanned: the
    # reader's order is stable, so a scan cap would re-scan the same
    # already-synced prefix on every run and never reach older unsynced rows.
    embeds = 0

    def flush() -> None:
        for rows, reprojected in ((to_update, False), (to_reproject, True)):
            if rows:
                changed = store.update_projections(rows)
                counts["synced"] += changed
                counts["skipped"] += len(rows) - changed
                if reprojected:
                    counts["reprojected"] += changed
                rows.clear()
        if to_embed:
            vectors = embedder.embed([r["one_line"] or "" for r in to_embed])
            for row, vec in zip(to_embed, vectors):
                store.save_message(row, vec)
            counts["synced"] += len(to_embed)
            counts["embedded"] += len(to_embed)
            to_embed.clear()

    with closing(documents.iter_enriched(source=source)) as enriched:
        for doc_id, meta, enr, enriched_at in enriched:
            counts["scanned"] += 1
            stored = state.get(doc_id)
            current = stored is not None and stored[0] == enriched_at
            if current and not force and stored[2] == digest:
                counts["skipped"] += 1
                continue
            row = projector(doc_id, meta, enr, policy)
            row["enriched_at"] = enriched_at
            row["policy_hash"] = digest
            if current:
                row["embedded_policy_hash"] = _embedded_under(stored, row["one_line"], digest)
                pending = to_reproject
            elif _embeds(stored, row["one_line"]):
                row["embedded_policy_hash"] = digest
                pending = to_update
            elif limit and embeds >= limit:
                if stored is None:
                    counts["capped"] += 1
                    continue
                # Embed-stale until the embedding phase reaches it. Not the
                # stored embedded_policy_hash: that policy was applied to the
                # old enrichment, so a revert to it would mark the old vector
                # current for the new text (see _vector_holds).
                row["embedded_policy_hash"] = None
                pending = to_update
            else:
                row["embedded_policy_hash"] = digest
                pending = to_embed
                embeds += 1
            pending.append(row)
            if len(pending) >= batch_size:
                flush()
    flush()
    if reembed_limit is not None:
        with store.reembed_lock(wait=reembed_wait, on_wait=on_reembed_wait) as held:
            if held:
                counts["embedded"] += _reembed(store, digest, embedder, reembed_limit, batch_size)
            else:
                counts["reembed_skipped"] = 1
                log.info(REEMBED_SKIPPED)
    counts["pending"], counts["embed_stale"] = store.backlog(digest)
    if tier is not None:
        (metrics or telemetry.sync_metrics).record(counts, tier)
    log.info(
        "synced %d, skipped %d of %d scanned; re-projected %d, embedded %d",
        counts["synced"],
        counts["skipped"],
        counts["scanned"],
        counts["reprojected"],
        counts["embedded"],
    )
    if counts["capped"]:
        log.info(
            "%d new rows held back by the limit; the next sync takes them",
            counts["capped"],
        )
    if counts["pending"]:
        log.info(
            "%d rows still projected under an older policy, served as placeholders; "
            "the next sync re-projects them",
            counts["pending"],
        )
    if counts["embed_stale"]:
        log.info(
            "%d rows await a new embedding; each sync re-embeds up to %s",
            counts["embed_stale"],
            reembed_limit or "all",
        )
    return counts


#: A synced row's state: ``enriched_at``, ``one_line``, ``policy_hash`` and
#: ``embedded_policy_hash`` (``SanitizedStore.synced_state``).
_State = tuple[object, str | None, str | None, str | None]


def _embeds(stored: _State | None, one_line: str | None) -> bool:
    """Whether a row's stored vector is the embedding of *one_line*.

    It is when the vector was computed from the stored ``one_line`` (its
    ``embedded_policy_hash`` is its ``policy_hash``) and that is *one_line*.
    """
    return stored is not None and stored[3] == stored[2] and stored[1] == one_line


def _vector_holds(stored: _State, one_line: str | None, digest: str) -> bool:
    """Whether a row re-projected to *one_line* under *digest* keeps a current vector.

    For a row whose enrichment is unchanged: besides :func:`_embeds`, its vector
    also holds when it was computed under this very policy and the row was
    re-projected under another since (a policy changed, then reverted). The
    projection is deterministic, so this policy writes the text the vector was
    computed from again. Every new or re-enriched row is embedded under the
    policy of the time, or, past the text phase's ``limit``, given no
    ``embedded_policy_hash``, so one always names a policy applied to the row's
    current enrichment.
    """
    return _embeds(stored, one_line) or stored[3] == digest != stored[2]


def _embedded_under(
    stored: _State,
    one_line: str | None,
    digest: str,
) -> str | None:
    """The ``embedded_policy_hash`` of a row re-projected to *one_line* under *digest*.

    *digest* when its vector holds for the new text. Otherwise the policy the
    vector was computed under, so a revert to it finds the vector current
    again; or ``None`` when that is *digest* itself (the text changed under an
    unchanged policy, on a forced re-projection), since a value equal to the
    row's ``policy_hash`` would mark it current.
    """
    if _vector_holds(stored, one_line, digest):
        return digest
    embedded = stored[3]
    return None if embedded == digest else embedded


def _reembed(store, digest: str, embedder: Embedder, limit: int, batch_size: int) -> int:
    """Re-embed up to *limit* (0: all) embed-stale rows under *digest*, newest first.

    Returns how many vectors were stored. Each batch commits as it goes, so an
    interrupted run keeps what it finished.
    """
    stale = store.stale_embeddings(digest, limit)
    stored = 0
    for start in range(0, len(stale), batch_size):
        batch = stale[start:start + batch_size]
        vectors = embedder.embed([text or "" for _, text in batch])
        stored += store.save_embeddings(
            digest, [(id_, text, vec) for (id_, text), vec in zip(batch, vectors)]
        )
    return stored
