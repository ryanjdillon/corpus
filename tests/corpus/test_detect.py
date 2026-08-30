"""scan.detect merges the identity (pii) and credential (leaks) detectors."""

from __future__ import annotations

from corpus import scan


def test_detect_merges_pii_and_leaks():
    counts = scan.detect("My SSN is 900-12-3456 and deploy key AKIAIOSFODNN7EXAMPLE")
    assert "us_ssn" in counts  # from pii
    assert "aws_access_key" in counts  # from leaks


def test_detect_empty():
    assert scan.detect("") == {}
    assert scan.detect(None) == {}


def test_detect_spans_merges_pii_and_leaks():
    spans = scan.detect_spans("SSN 900-12-3456 and deploy key AKIAIOSFODNN7EXAMPLE")
    kinds = {s.entity_type for s in spans}
    assert {"us_ssn", "aws_access_key"} <= kinds
    # offsets bound real slices of the input, never the value itself is stored
    assert all(0 <= s.start < s.end for s in spans)


def test_detect_spans_empty():
    assert scan.detect_spans("") == []
    assert scan.detect_spans(None) == []


def test_audit_candidates_unions_deterministic_and_recovery_hint():
    # deterministic credential hit
    assert "aws_access_key" in scan.audit_candidates("deploy key AKIAIOSFODNN7EXAMPLE")
    # recovery wording with no deterministic value still selects the message for audit
    assert scan.audit_candidates("Here are your backup codes, keep them safe.") == ["recovery_code"]
    # nothing to audit
    assert scan.audit_candidates("just lunch plans tomorrow") == []
    assert scan.audit_candidates("") == []
