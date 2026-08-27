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
