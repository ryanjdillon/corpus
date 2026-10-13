"""The merged detectors: counts, spans, the type names, and the fingerprint inputs."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import create_autospec

import corpus_detect
import pytest
from corpus_detect import ENTITY_TYPES, detect, detect_spans, fingerprint_parts
from corpus_detect.leaks import LeaksSettings


def test_detect_merges_pii_and_leaks():
    counts = detect("My SSN is 900-12-3456 and deploy key AKIAIOSFODNN7EXAMPLE")
    assert "us_ssn" in counts  # from pii
    assert "aws_access_key" in counts  # from leaks


def test_detect_reports_the_credential_scanner():
    def callback(scanner: str) -> None: ...

    on_scanner = create_autospec(callback)

    detect(
        "deploy key AKIAIOSFODNN7EXAMPLE",
        on_scanner=on_scanner,
        leaks_config=LeaksSettings(bin=""),
    )

    on_scanner.assert_called_once_with("local")


def test_detect_empty():
    assert detect("") == {}
    assert detect(None) == {}


def test_detect_spans_merges_pii_and_leaks():
    spans = detect_spans("SSN 900-12-3456 and deploy key AKIAIOSFODNN7EXAMPLE")
    kinds = {s.entity_type for s in spans}
    assert {"us_ssn", "aws_access_key"} <= kinds
    # offsets bound real slices of the input, never the value itself is stored
    assert all(0 <= s.start < s.end for s in spans)


def test_detect_spans_empty():
    assert detect_spans("") == []
    assert detect_spans(None) == []


def test_entity_types_name_every_type_a_span_carries():
    text = (
        "SSN 900-12-3456, mail alice@example.org, tlf +47 412 34 567, "
        "key AKIAIOSFODNN7EXAMPLE"
    )
    assert {s.entity_type for s in detect_spans(text)} <= ENTITY_TYPES
    assert {"us_ssn", "email", "phone", "private_key", "aws_access_key"} <= ENTITY_TYPES


def test_fingerprint_parts_are_stable_across_calls():
    assert repr(fingerprint_parts()) == repr(fingerprint_parts())


def test_unknown_attribute_is_an_attribute_error():
    with pytest.raises(AttributeError, match="no_such_name"):
        corpus_detect.no_such_name  # noqa: B018


def test_dir_lists_the_public_interface():
    assert set(corpus_detect.__all__) <= set(dir(corpus_detect))


def test_importing_the_package_defers_presidio():
    # Span handling alone must not pay for loading the recognizers.
    code = (
        "import sys, corpus_detect; "
        "assert 'presidio_analyzer' not in sys.modules; "
        "corpus_detect.redact; "
        "assert 'presidio_analyzer' in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_a_submodule_import_does_not_shadow_a_function():
    code = (
        "import corpus_detect.redaction, corpus_detect.detectors, corpus_detect; "
        "assert callable(corpus_detect.redact) and callable(corpus_detect.detect)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
