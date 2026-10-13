"""scan.audit_candidates: the detectors' hits plus recovery wording."""

from __future__ import annotations

from corpus import scan


def test_audit_candidates_unions_deterministic_and_recovery_hint():
    # deterministic credential hit
    assert "aws_access_key" in scan.audit_candidates("deploy key AKIAIOSFODNN7EXAMPLE")
    # recovery wording with no deterministic value still selects the message for audit
    assert scan.audit_candidates("Here are your backup codes, keep them safe.") == ["recovery_code"]
    # nothing to audit
    assert scan.audit_candidates("just lunch plans tomorrow") == []
    assert scan.audit_candidates("") == []
