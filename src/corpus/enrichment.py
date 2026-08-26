"""The v1 enrichment schema: the structured metadata a local model produces for
each message, expressed as ``msgspec`` structs.

The struct set is the source of truth. ``json_schema()`` renders it to a JSON
Schema, which is what vLLM guided decoding constrains generation against, so the
model's output is always a valid ``Enrichment`` and ``msgspec.json.decode`` never
has to cope with drift. No Pydantic, no secret values (those are section B —
deterministic ``pii`` scan — and are referenced by name here, never quoted).
"""

from __future__ import annotations

import enum
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
    reply = "reply"
    pay = "pay"
    schedule = "schedule"
    review = "review"
    sign = "sign"
    submit = "submit"
    none = "none"


class WaitingOn(enum.Enum):
    me = "me"
    them = "them"
    none = "none"


class Importance(enum.Enum):
    high = "high"
    medium = "medium"
    low = "low"


class SensitivityLevel(enum.Enum):
    none = "none"
    low = "low"
    medium = "medium"
    high = "high"


class Disposition(enum.Enum):
    keep = "keep"
    archive = "archive"
    trash = "trash"
    review = "review"


class Person(msgspec.Struct):
    name: str
    role: str | None = None  # best-effort: "classmate", "colleague", "vendor"


class Appointment(msgspec.Struct):
    who: str | None = None
    where: str | None = None
    when: str | None = None  # ISO if parseable, else natural ("next Tue 3pm")


class Money(msgspec.Struct):
    amount: float
    currency: str


class Enrichment(msgspec.Struct):
    """Per-message structured metadata. Verbose by design so the index can answer
    many queries without re-enrichment."""

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


#: Bumped when the struct set changes; stored alongside each enriched record so a
#: later migration knows which schema produced it.
SCHEMA_VERSION = 1


def json_schema() -> dict:
    """Render the Enrichment struct to a JSON Schema for guided decoding."""
    return msgspec.json.schema(Enrichment)


def decode(data: bytes | str) -> Enrichment:
    """Parse a model's JSON output into an Enrichment (raises on malformed JSON or
    a value outside the schema)."""
    if isinstance(data, str):
        data = data.encode()
    return msgspec.json.decode(data, type=Enrichment)
