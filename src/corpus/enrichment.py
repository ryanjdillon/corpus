"""Define the v1 enrichment schema as ``msgspec`` structs.

The structured metadata a local model produces for each message.

The struct set is the source of truth. ``json_schema()`` renders it to a JSON
Schema, which is what vLLM guided decoding constrains generation against, so the
model's output is always a valid ``Enrichment`` and ``msgspec.json.decode`` never
has to cope with drift. No Pydantic, no secret values (those are section B —
deterministic ``pii`` scan — and are referenced by name here, never quoted).
"""

from __future__ import annotations

import enum
import hashlib
from datetime import date
from typing import Annotated

import msgspec


class Category(enum.Enum):
    """Structural type — how the message functions, regardless of subject."""

    personal = "personal"
    newsletter = "newsletter"
    promotional = "promotional"
    notification = "notification"
    transactional = "transactional"
    bulk = "bulk"
    other = "other"


class Domain(enum.Enum):
    """Life-domain — what the message is about; orthogonal to Category."""

    work = "work"
    job_search = "job_search"
    education = "education"
    banking = "banking"
    investing = "investing"
    bills = "bills"
    taxes = "taxes"
    insurance = "insurance"
    health = "health"
    legal = "legal"
    government = "government"
    shopping = "shopping"
    travel = "travel"
    housing = "housing"
    social = "social"
    entertainment = "entertainment"
    subscriptions = "subscriptions"
    other = "other"


class TransactionalType(enum.Enum):
    """Specific kind of a transactional message."""

    receipt = "receipt"
    order_confirmation = "order_confirmation"
    shipping = "shipping"
    invoice = "invoice"
    statement = "statement"
    payment = "payment"
    refund = "refund"
    booking = "booking"
    subscription = "subscription"
    alert = "alert"
    none = "none"
    other = "other"


class ActionType(enum.Enum):
    """Action the message asks the reader to take."""

    reply = "reply"
    pay = "pay"
    schedule = "schedule"
    review = "review"
    sign = "sign"
    submit = "submit"
    none = "none"


class WaitingOn(enum.Enum):
    """Who a pending action is waiting on."""

    me = "me"
    them = "them"
    none = "none"


class Importance(enum.Enum):
    """Relative importance of the message."""

    high = "high"
    medium = "medium"
    low = "low"


class SensitivityLevel(enum.Enum):
    """How sensitive the message content is."""

    none = "none"
    low = "low"
    medium = "medium"
    high = "high"


class Disposition(enum.Enum):
    """Suggested triage disposition for the message."""

    keep = "keep"
    archive = "archive"
    trash = "trash"
    review = "review"


# Length bounds for guided decoding. The grammar enforces the schema, so a bound
# makes an endless list or string impossible: the model has to close it. Without
# them a newsletter can send a model into a loop that only the output-token cap
# stops, and the record is lost. The bounds sit well above a legitimate record.
#: A name or short label: a person, an organisation, a topic, a place.
Name = Annotated[str, msgspec.Meta(max_length=120)]
#: A sentence or two of free text.
Text = Annotated[str, msgspec.Meta(max_length=400)]
#: Items in an entity list.
MAX_ITEMS = 12
_LIST = msgspec.Meta(max_length=MAX_ITEMS)
Names = Annotated[list[Name], _LIST]
Texts = Annotated[list[Text], _LIST]


# Role and ReferenceValue must not be blank, but that is checked after decoding
# (``__post_init__``), not with ``minLength``: the guided schema keeps to the
# keywords every supported provider already accepts, and a provider that rejects
# an unfamiliar one fails every request, not just the odd record.
#: A short noun for how a person relates to the message or its owner.
Role = Annotated[str, msgspec.Meta(max_length=60)]
#: An identifier as written: a booking code, an order or case number.
ReferenceValue = Annotated[str, msgspec.Meta(max_length=120)]
#: The role a person decoded from a record written before roles were required.
UNKNOWN_ROLE = "unknown"


def _require_text(value: str, field: str) -> None:
    """Refuse a blank string; msgspec reports the ValueError as a ValidationError."""
    if not value.strip():
        raise ValueError(f"{field} must not be empty")


class Person(msgspec.Struct, forbid_unknown_fields=True):
    """A person referenced by the message, identified by role first.

    The role ("friend", "mechanic", "landlord", "colleague") is what the
    summaries say instead of the name, so a tier that must not see names can
    drop ``name`` and still read every summary. ``name`` is the name as written,
    when the message gives one.
    """

    role: Role
    name: Name | None = None

    def __post_init__(self) -> None:
        _require_text(self.role, "role")


class ReferenceKind(enum.Enum):
    """What a reference identifies."""

    booking = "booking"
    order = "order"
    case = "case"
    invoice = "invoice"
    ticket = "ticket"
    parking = "parking"
    membership = "membership"
    tracking = "tracking"
    account = "account"
    other = "other"


class Reference(msgspec.Struct, forbid_unknown_fields=True):
    """An identifier the message quotes: a booking code, an order or case number.

    Kept out of the summaries, like names, so a later policy can decide per tier
    whether the value is shown. Never a secret: card and bank account numbers,
    passwords and one-time codes are the ``pii`` scan's, not a reference.
    """

    kind: ReferenceKind
    value: ReferenceValue

    def __post_init__(self) -> None:
        _require_text(self.value, "value")


class Appointment(msgspec.Struct):
    """An appointment or event referenced by the message."""

    who: Name | None = None
    where: Name | None = None
    when: Name | None = None  # ISO if parseable, else natural ("next Tue 3pm")


class Money(msgspec.Struct):
    """A monetary amount with its currency."""

    amount: float
    currency: Annotated[str, msgspec.Meta(max_length=8)]


class Enrichment(msgspec.Struct):
    """Per-message structured metadata.

    Verbose by design so the index can answer many queries without re-enrichment.
    The summaries (``one_line``, ``abstract``, ``action_summary``) name people by
    role and quote no reference values; names and identifiers live only in
    ``people`` and ``references``, where a per-tier policy can withhold them.
    """

    # --- summary (free text, secret-free, names and reference values left out) ---
    one_line: Annotated[str, msgspec.Meta(max_length=160)]  # aim for <=120 chars
    abstract: Text  # 2-3 sentences

    # --- classification (orthogonal axes) ---
    category: Category  # structural type
    domain: Domain = Domain.other  # life-domain (granular)
    transactional_type: TransactionalType = TransactionalType.none
    unsubscribe_available: bool = False

    key_points: Texts = msgspec.field(default_factory=list)

    # --- intent / action ---
    requires_action: bool = False
    action_type: ActionType = ActionType.none
    action_summary: Text | None = None
    deadline: date | None = None
    waiting_on: WaitingOn = WaitingOn.none

    # --- importance ---
    importance: Importance = Importance.low
    time_sensitive: bool = False

    # --- entities / topics / events ---
    people: Annotated[list[Person], _LIST] = msgspec.field(default_factory=list)
    references: Annotated[list[Reference], _LIST] = msgspec.field(default_factory=list)
    organizations: Names = msgspec.field(default_factory=list)
    topics: Names = msgspec.field(default_factory=list)
    projects: Names = msgspec.field(default_factory=list)
    locations: Names = msgspec.field(default_factory=list)
    appointments: Annotated[list[Appointment], _LIST] = msgspec.field(default_factory=list)
    monetary_amounts: Annotated[list[Money], _LIST] = msgspec.field(default_factory=list)

    # --- sensitivity (level only; the area is `domain`; secrets are section B) ---
    sensitivity_level: SensitivityLevel = SensitivityLevel.none

    # --- triage hint (advisory) ---
    suggested_disposition: Disposition = Disposition.keep


#: The ``Enrichment`` fields a model writes as free text, which can repeat an
#: injected instruction from the email it summarises. Every other field is a schema
#: enum, a boolean, a date, or a bounded amount and currency.
FREE_TEXT_FIELDS = (
    "one_line", "abstract", "key_points", "action_summary", "people", "references",
    "organizations", "topics", "projects", "locations", "appointments",
)


class SecretSeverity(enum.Enum):
    """How dangerous a confirmed secret is, worst first."""

    live = "live"  # a currently-usable secret (API key, private key, unexpired code)
    expired = "expired"  # a real value, but no longer usable (old OTP, past statement)
    reference = "reference"  # mentions a secret exists, but no value is present
    none = "none"  # candidate not actually present


class ConfirmedSecret(msgspec.Struct):
    """One secret the LLM confirmed (or rejected) among the deterministic candidates.

    ``note`` describes it in words and MUST NOT contain the value itself.
    """

    type: Name
    severity: SecretSeverity
    note: Text = ""


class SecretAudit(msgspec.Struct):
    """LLM verdict over a message's deterministic secret candidates.

    The precision layer that separates a real disclosure from an incidental match.
    """

    contains_secret: bool
    findings: Annotated[list[ConfirmedSecret], _LIST] = msgspec.field(default_factory=list)


def json_schema() -> dict:
    """Render the Enrichment struct to a JSON Schema for guided decoding.

    Every field of every struct is forced required. The fields carry struct defaults so
    non-LLM construction stays convenient, but a field left OPTIONAL in the
    guided-decoding schema is one the grammar lets the model skip -- and which
    fields get skipped depends on the server, not the message. The classification
    axes collapsed to their defaults (domain -> "other", ...) until they were forced;
    vLLM then dropped every entity list and llama.cpp every deadline. Required
    means "decide", not "non-empty": an empty list or a null deadline is still a
    valid answer.

    ``additionalProperties: false`` (from ``forbid_unknown_fields``) is dropped:
    the grammar already emits only the listed properties, and decoding still
    refuses an unknown key, so the keyword would only add one more a provider
    must support.
    """
    schema = msgspec.json.schema(Enrichment)
    for definition in schema.get("$defs", {}).values():
        if "properties" in definition:
            definition["required"] = sorted(definition["properties"])
            definition.pop("additionalProperties", None)
    return schema


def secret_audit_schema() -> dict:
    """Render the SecretAudit struct to a JSON Schema for guided decoding."""
    return msgspec.json.schema(SecretAudit)


#: Fingerprint of the enrichment schema, stored alongside each record so a later
#: migration knows which schema produced it. Derived from the schema itself, so it
#: changes automatically when the struct set changes — no manual bump.
SCHEMA_VERSION = hashlib.sha1(msgspec.json.encode(json_schema())).hexdigest()[:12]
#: Appended to the schema version of a record recovered from a stalled reply (see
#: ``enricher.decode_stalled``). The result never equals ``SCHEMA_VERSION``, so the
#: record counts as stale; ``--redo-recovered`` finds it by the marker, under any
#: schema, and enriches it again in full.
RECOVERED_MARKER = "+recovered"
RECOVERED_SCHEMA_VERSION = f"{SCHEMA_VERSION}{RECOVERED_MARKER}"


def upgrade(fields: dict) -> dict:
    """Return a stored enrichment's fields in the current shape.

    A record written before roles were required has people with a null or
    missing ``role``; they take :data:`UNKNOWN_ROLE`. A record written before
    ``references`` existed gets an empty list. The result is still a plain
    dict, so a reader of raw stored rows (``DocumentStore.iter_enriched``) sees
    the same shape as one that decodes them.
    """
    upgraded = {**fields}
    upgraded.setdefault("references", [])
    people = fields.get("people")
    if isinstance(people, list):
        upgraded["people"] = [
            {**p, "role": p.get("role") or UNKNOWN_ROLE} if isinstance(p, dict) else p
            for p in people
        ]
    return upgraded


def decode(data: bytes | str) -> Enrichment:
    """Parse an enrichment's JSON, current or written under an older schema.

    Raise on malformed JSON or a value outside the schema.
    """
    if isinstance(data, str):
        data = data.encode()
    fields = msgspec.json.decode(data)
    if not isinstance(fields, dict):
        raise msgspec.ValidationError(f"expected an object, got {type(fields).__name__}")
    return msgspec.convert(upgrade(fields), type=Enrichment)
