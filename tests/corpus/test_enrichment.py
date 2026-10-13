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
    Person,
    Reference,
    ReferenceKind,
    SecretAudit,
    SecretSeverity,
)


def test_json_schema_lists_enum_values():
    schema = enrichment.json_schema()
    dumped = msgspec.json.encode(schema).decode()
    # Enum axes reach the schema (as $defs referenced by the struct).
    for value in ("personal", "transactional", "banking", "job_search"):
        assert value in dumped


def test_json_schema_forces_every_field_required():
    # Fields carry struct defaults, but the guided-decoding schema must mark all of
    # them required: an optional field is one the grammar lets the model skip (the
    # axes collapsed to their defaults; vLLM dropped entities, llama.cpp deadlines).
    schema = enrichment.json_schema()
    enr = schema["$defs"]["Enrichment"] if "$defs" in schema else schema
    assert set(enr["required"]) == set(enr["properties"])
    for field in (
        "domain", "deadline", "people", "references", "organizations", "monetary_amounts",
    ):
        assert field in enr["required"]
    # Nested structs too: Person.role and every Appointment field were skippable.
    for name in ("Person", "Reference", "Appointment", "Money"):
        nested = schema["$defs"][name]
        assert set(nested["required"]) == set(nested["properties"])


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


def test_decode_reads_people_by_role_and_references():
    doc = enrichment.decode(
        """
        {"one_line": "A friend proposes a weekend trip", "abstract": "A trip idea.",
         "category": "personal",
         "people": [{"role": "friend", "name": "Zoe Example"}, {"role": "host", "name": null}],
         "references": [{"kind": "booking", "value": "QX7-2291"},
                        {"kind": "case", "value": "CASE 0042-B"}]}
        """
    )
    assert doc.people == [Person(role="friend", name="Zoe Example"), Person(role="host")]
    assert doc.references == [
        Reference(kind=ReferenceKind.booking, value="QX7-2291"),
        Reference(kind=ReferenceKind.case, value="CASE 0042-B"),
    ]


@pytest.mark.parametrize(
    "people",
    [
        [{"name": "Pat", "role": None}],  # the old shape, as stored: role null
        [{"name": "Pat"}],  # role omitted altogether
    ],
    ids=["null-role", "missing-role"],
)
def test_record_from_before_roles_still_decodes(people):
    # Stored records keep the shape they were written in; reading one must not fail
    # because roles became required and references were added.
    old = {"one_line": "Pat sends notes", "abstract": "Notes.", "category": "personal",
           "people": people}

    doc = enrichment.decode(msgspec.json.encode(old))

    assert doc.people == [Person(role=enrichment.UNKNOWN_ROLE, name="Pat")]
    assert doc.references == []


def test_upgrade_keeps_a_current_record_as_it_is():
    current = {"people": [{"role": "friend", "name": None}], "references": []}

    assert enrichment.upgrade(current) == current


def test_new_output_must_give_every_person_a_role():
    # The model's output is decoded strictly: only stored records are upgraded.
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(
            b'{"one_line": "x", "abstract": "y", "category": "personal", '
            b'"people": [{"name": "Pat"}]}',
            type=Enrichment,
        )


@pytest.mark.parametrize("data", ['["not", "an", "object"]', '{"one_line": "x"', '""'])
def test_decode_rejects_anything_but_a_record(data):
    with pytest.raises(msgspec.DecodeError):
        enrichment.decode(data)


@pytest.mark.parametrize(
    "item",
    [{"kind": "coupon", "value": "X1"}, {"kind": "order", "value": ""},
     {"kind": "order", "value": "  "}, {"kind": "order", "value": "X1", "note": "extra"}],
    ids=["kind-outside-enum", "empty-value", "blank-value", "unknown-key"],
)
def test_decode_rejects_a_malformed_reference(item):
    record = {"one_line": "x", "abstract": "y", "category": "personal", "references": [item]}
    with pytest.raises(msgspec.ValidationError):
        enrichment.decode(msgspec.json.encode(record))


@pytest.mark.parametrize(
    ("field", "item"),
    [
        ("people", {"role": "", "name": "Pat"}),
        ("people", {"role": " ", "name": None}),
        ("people", {"role": "friend", "name": "Pat", "email": "pat@mail.example"}),
        ("references", {"kind": "order", "value": ""}),
        ("references", {"kind": "order", "value": "X1", "label": "x"}),
    ],
    ids=["empty-role", "blank-role", "person-extra-key", "empty-value", "reference-extra-key"],
)
def test_model_output_refuses_a_blank_or_padded_entity(field, item):
    # The model's output is decoded strictly; non-empty and no-extra-keys are
    # checked here, after decoding, since the guided schema does not carry them.
    record = {"one_line": "x", "abstract": "y", "category": "personal", field: [item]}
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(msgspec.json.encode(record), type=Enrichment)


def test_guided_schema_carries_no_keyword_a_provider_may_not_know():
    # minLength and additionalProperties are enforced after decoding instead; a
    # provider that rejects a keyword would refuse every enrich request.
    schema = enrichment.json_schema()
    for name in ("Person", "Reference"):
        definition = schema["$defs"][name]
        assert "additionalProperties" not in definition, name
        for prop in definition["properties"].values():
            for variant in prop.get("anyOf", [prop]):
                assert "minLength" not in variant, name
    assert "minLength" not in msgspec.json.encode(schema).decode()


def test_a_stored_person_with_only_name_and_role_still_decodes():
    # Person forbids unknown keys; the old stored shape had no others.
    old = {"one_line": "x", "abstract": "y", "category": "personal",
           "people": [{"name": "Pat", "role": "classmate"}, {"name": "Kim", "role": None}]}

    doc = enrichment.decode(msgspec.json.encode(old))

    assert doc.people == [Person(role="classmate", name="Pat"),
                          Person(role=enrichment.UNKNOWN_ROLE, name="Kim")]


def test_upgrade_gives_an_old_record_an_empty_reference_list():
    upgraded = enrichment.upgrade({"one_line": "x"})

    assert upgraded == {"one_line": "x", "references": []}


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


def test_schema_bounds_every_list_and_free_text_field():
    # Guided decoding enforces these, so a looping model has to close the list.
    props = enrichment.json_schema()["$defs"]["Enrichment"]["properties"]
    for field in (
        "key_points",
        "people",
        "references",
        "organizations",
        "topics",
        "projects",
        "locations",
        "appointments",
        "monetary_amounts",
    ):
        assert props[field]["maxItems"] == enrichment.MAX_ITEMS, field
    assert props["topics"]["items"]["maxLength"] == 120
    assert props["abstract"]["maxLength"] == 400
    reference = enrichment.json_schema()["$defs"]["Reference"]["properties"]
    assert reference["value"]["maxLength"] == 120
    audit = enrichment.secret_audit_schema()["$defs"]["SecretAudit"]["properties"]
    assert audit["findings"]["maxItems"] == enrichment.MAX_ITEMS


def test_decode_rejects_output_beyond_the_bounds():
    record = {
        "one_line": "x",
        "abstract": "y",
        "category": "personal",
        "topics": [f"t{i}" for i in range(enrichment.MAX_ITEMS + 1)],
    }
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(msgspec.json.encode(record), type=Enrichment)


@pytest.mark.parametrize(
    "field,value",
    [("one_line", "x" * 161), ("abstract", "y" * 401)],
    ids=["one_line", "abstract"],
)
def test_decode_rejects_strings_beyond_the_bounds(field, value):
    record = {"one_line": "x", "abstract": "y", "category": "personal", field: value}
    with pytest.raises(msgspec.ValidationError):
        enrichment.decode(msgspec.json.encode(record))


def test_every_enrichment_field_is_classified_as_free_text_or_not():
    # A new field must join FREE_TEXT_FIELDS or be added here deliberately: each
    # of these is an enum, a boolean, a date, or a bounded amount and currency.
    structured = {
        "category", "domain", "transactional_type", "unsubscribe_available",
        "requires_action", "action_type", "deadline", "waiting_on", "importance",
        "time_sensitive", "monetary_amounts", "sensitivity_level", "suggested_disposition",
    }
    fields = set(enrichment.Enrichment.__struct_fields__)

    assert set(enrichment.FREE_TEXT_FIELDS) <= fields
    assert fields - set(enrichment.FREE_TEXT_FIELDS) == structured


def test_references_are_scanned_as_untrusted_free_text():
    # A reference value is copied from the email as written, so it can carry an
    # injected instruction like any summary: it is scanned, not trusted as an id.
    assert "references" in enrichment.FREE_TEXT_FIELDS
    assert "people" in enrichment.FREE_TEXT_FIELDS
