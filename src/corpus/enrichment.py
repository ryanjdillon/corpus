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


class Person(msgspec.Struct):
    """A person referenced by the message."""

    name: str
    role: str | None = None  # best-effort: "classmate", "colleague", "vendor"


class Appointment(msgspec.Struct):
    """An appointment or event referenced by the message."""

    who: str | None = None
    where: str | None = None
    when: str | None = None  # ISO if parseable, else natural ("next Tue 3pm")


class Money(msgspec.Struct):
    """A monetary amount with its currency."""

    amount: float
    currency: str


class Enrichment(msgspec.Struct):
    """Per-message structured metadata.

    Verbose by design so the index can answer many queries without re-enrichment.
    """

    # --- summary (free text, secret-free) ---
    one_line: str  # <=120 chars
    abstract: str  # 2-3 sentences

    # --- classification (orthogonal axes) ---
    category: Category  # structural type
    domain: Domain = Domain.other  # life-domain (granular)
    transactional_type: TransactionalType = TransactionalType.none
    unsubscribe_available: bool = False

    key_points: list[str] = msgspec.field(default_factory=list)

    # --- intent / action ---
    requires_action: bool = False
    action_type: ActionType = ActionType.none
    action_summary: str | None = None
    deadline: date | None = None
    waiting_on: WaitingOn = WaitingOn.none

    # --- importance ---
    importance: Importance = Importance.low
    time_sensitive: bool = False

    # --- entities / topics / events ---
    people: list[Person] = msgspec.field(default_factory=list)
    organizations: list[str] = msgspec.field(default_factory=list)
    topics: list[str] = msgspec.field(default_factory=list)
    projects: list[str] = msgspec.field(default_factory=list)
    locations: list[str] = msgspec.field(default_factory=list)
    appointments: list[Appointment] = msgspec.field(default_factory=list)
    monetary_amounts: list[Money] = msgspec.field(default_factory=list)

    # --- sensitivity (level only; the area is `domain`; secrets are section B) ---
    sensitivity_level: SensitivityLevel = SensitivityLevel.none

    # --- triage hint (advisory) ---
    suggested_disposition: Disposition = Disposition.keep


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

    type: str
    severity: SecretSeverity
    note: str = ""


class SecretAudit(msgspec.Struct):
    """LLM verdict over a message's deterministic secret candidates.

    The precision layer that separates a real disclosure from an incidental match.
    """

    contains_secret: bool
    findings: list[ConfirmedSecret] = msgspec.field(default_factory=list)


#: Classification axes the model must always decide. They carry struct defaults so
#: non-LLM construction stays convenient, but if they are left OPTIONAL in the
#: guided-decoding schema the model simply omits them and every record collapses to
#: the default (domain -> "other", transactional_type -> "none", …). Marking them
#: required forces a real choice per message.
_REQUIRED_AXES = (
    "domain",
    "transactional_type",
    "unsubscribe_available",
    "requires_action",
    "action_type",
    "waiting_on",
    "importance",
    "time_sensitive",
    "sensitivity_level",
    "suggested_disposition",
)


def json_schema() -> dict:
    """Render the Enrichment struct to a JSON Schema for guided decoding.

    The classification axes are forced required (see ``_REQUIRED_AXES``).
    """
    schema = msgspec.json.schema(Enrichment)
    target = schema["$defs"]["Enrichment"] if "$defs" in schema else schema
    target["required"] = sorted(set(target.get("required", ())) | set(_REQUIRED_AXES))
    return schema


def secret_audit_schema() -> dict:
    """Render the SecretAudit struct to a JSON Schema for guided decoding."""
    return msgspec.json.schema(SecretAudit)


#: Fingerprint of the enrichment schema, stored alongside each record so a later
#: migration knows which schema produced it. Derived from the schema itself, so it
#: changes automatically when the struct set changes — no manual bump.
SCHEMA_VERSION = hashlib.sha1(msgspec.json.encode(json_schema())).hexdigest()[:12]


def decode(data: bytes | str) -> Enrichment:
    """Parse a model's JSON output into an ``Enrichment``.

    Raise on malformed JSON or a value outside the schema.
    """
    if isinstance(data, str):
        data = data.encode()
    return msgspec.json.decode(data, type=Enrichment)
