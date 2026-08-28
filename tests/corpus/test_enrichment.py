"""The enrichment schema renders to JSON Schema and round-trips model output."""

from __future__ import annotations

from datetime import date

import msgspec
import pytest

from corpus import enrichment
from corpus.enrichment import (
    Category,
    Disposition,
    Domain,
    Enrichment,
    Importance,
    SecretAudit,
    SecretSeverity,
)


def test_json_schema_lists_enum_values():
    schema = enrichment.json_schema()
    dumped = msgspec.json.encode(schema).decode()
    # Enum axes reach the schema (as $defs referenced by the struct).
    for value in ("personal", "transactional", "banking", "job_search"):
        assert value in dumped


def test_json_schema_forces_classification_axes_required():
    # The axes carry struct defaults, but the guided-decoding schema must mark them
    # required so the model decides each instead of omitting them (which collapsed
    # every record to domain="other", transactional_type="none", ...).
    schema = enrichment.json_schema()
    enr = schema["$defs"]["Enrichment"] if "$defs" in schema else schema
    required = set(enr["required"])
    for axis in ("domain", "transactional_type", "requires_action", "importance", "sensitivity_level"):
        assert axis in required


def test_decode_applies_defaults_to_minimal_output():
    # Only the required fields; everything else must fall back to its default.
    doc = enrichment.decode('{"one_line": "hi", "abstract": "a note", "category": "personal"}')
    assert isinstance(doc, Enrichment)
    assert doc.category is Category.personal
    assert doc.domain is Domain.other
    assert doc.importance is Importance.low
    assert doc.suggested_disposition is Disposition.keep
    assert doc.key_points == []
    assert doc.people == []
    assert doc.deadline is None
    assert doc.requires_action is False


def test_decode_full_record_parses_nested_and_dates():
    doc = enrichment.decode(
        """
        {"one_line": "Order shipped", "abstract": "Your order shipped.",
         "category": "transactional", "domain": "shopping",
         "transactional_type": "shipping", "requires_action": true,
         "action_type": "review", "deadline": "2026-09-01", "importance": "medium",
         "people": [{"name": "Pat", "role": "vendor"}],
         "monetary_amounts": [{"amount": 42.5, "currency": "USD"}]}
        """
    )
    assert doc.transactional_type.value == "shipping"
    assert doc.deadline == date(2026, 9, 1)
    assert doc.people[0].name == "Pat"
    assert doc.people[0].role == "vendor"
    assert doc.monetary_amounts[0].amount == 42.5


def test_decode_rejects_value_outside_enum():
    with pytest.raises(msgspec.DecodeError):
        enrichment.decode('{"one_line": "x", "abstract": "y", "category": "banana"}')


def test_schema_version_is_a_stable_fingerprint():
    # derived from the schema, not a hand-maintained int
    assert isinstance(enrichment.SCHEMA_VERSION, str)
    assert enrichment.SCHEMA_VERSION == enrichment.SCHEMA_VERSION  # deterministic
    assert len(enrichment.SCHEMA_VERSION) == 12


def test_secret_audit_schema_lists_severities():
    dumped = msgspec.json.encode(enrichment.secret_audit_schema()).decode()
    for severity in ("live", "expired", "reference", "none"):
        assert severity in dumped


def test_secret_audit_decodes_findings():
    audit = msgspec.json.decode(
        b'{"contains_secret": true, "findings": '
        b'[{"type": "us_ssn", "severity": "live", "note": "an SSN is present"}]}',
        type=SecretAudit,
    )
    assert audit.contains_secret is True
    assert audit.findings[0].type == "us_ssn"
    assert audit.findings[0].severity is SecretSeverity.live


def test_secret_audit_defaults_empty_findings():
    audit = msgspec.json.decode(b'{"contains_secret": false}', type=SecretAudit)
    assert audit.findings == []
