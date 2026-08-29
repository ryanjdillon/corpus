"""Cheap, deterministic message classification.

Rule/header heuristics decide the common cases (promotional, bulk, transactional,
notification) with high precision and zero model calls. Everything else is left
"personal" until a richer classifier (embedding prototypes, optional local LLM)
is layered on top.
"""

from __future__ import annotations

import re

from .models import Classification, Record

_ESP_DOMAINS = re.compile(
    r"(mailchimp|sendgrid|mailgun|sparkpost|amazonses|sendinblue|"
    r"constantcontact|hubspot|klaviyo|mailjet)",
    re.IGNORECASE,
)


def _h(record: Record, name: str) -> str:
    for k, v in record.headers.items():
        if k.lower() == name.lower():
            return v
    return ""


def classify(record: Record) -> Classification:
    """Classify a record using cheap header/rule heuristics.

    Non-email records are labelled ``document``; emails fall through the
    promotional/newsletter/notification/bulk gates and default to ``personal``.
    """
    if record.kind != "email":
        return Classification(label="document", confidence=1.0)

    signals: dict[str, object] = {}

    list_unsub = _h(record, "List-Unsubscribe")
    precedence = _h(record, "Precedence").lower()
    auto_submitted = _h(record, "Auto-Submitted").lower()
    from_addr = (record.from_addr or "").lower()

    if list_unsub:
        signals["list_unsubscribe"] = True
    if precedence in {"bulk", "list", "junk"}:
        signals["precedence"] = precedence
    if auto_submitted and auto_submitted != "no":
        signals["auto_submitted"] = auto_submitted
    if _ESP_DOMAINS.search(from_addr) or _ESP_DOMAINS.search(list_unsub):
        signals["esp"] = True

    # Promotional: unsubscribe link + a bulk/ESP marker.
    if list_unsub and (signals.get("precedence") or signals.get("esp")):
        return Classification(label="promotional", confidence=0.9, signals=signals)
    if list_unsub:
        return Classification(label="newsletter", confidence=0.7, signals=signals)
    if signals.get("auto_submitted"):
        return Classification(label="notification", confidence=0.8, signals=signals)
    if signals.get("precedence") == "bulk":
        return Classification(label="bulk", confidence=0.7, signals=signals)

    return Classification(label="personal", confidence=0.5, signals=signals)
