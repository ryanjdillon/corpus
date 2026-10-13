"""The credential detector: local regexes always, Betterleaks when configured.

All test vectors are non-secret by construction (the canonical AWS docs example
key, and structurally-valid but meaningless keys/tokens).
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import create_autospec

import pytest
from corpus_detect import leaks
from corpus_detect.leaks import LeaksSettings

_BETTERLEAKS = LeaksSettings(bin="betterleaks", timeout=5.0)


@pytest.fixture
def run():
    """Autospec of the subprocess.run boundary the Betterleaks scan shells out to."""
    return create_autospec(subprocess.run)


def test_detects_private_key_block():
    counts = leaks.scan(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nQUJD\n-----END OPENSSH PRIVATE KEY-----"
    )
    assert counts.get("private_key") == 1


def test_detects_aws_and_github_and_jwt():
    text = (
        "key AKIAIOSFODNN7EXAMPLE and token "
        "ghp_0123456789abcdefghijklmnopqrstuvwxyz and "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijKLMNOP"
    )
    counts = leaks.scan(text)
    assert counts.get("aws_access_key") == 1
    assert counts.get("github_token") == 1
    assert counts.get("jwt") == 1
    # values are never echoed back
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(counts)


def test_clean_text_and_empty():
    assert leaks.scan("just a normal sentence about lunch") == {}
    assert leaks.scan("") == {}
    assert leaks.scan(None) == {}


def test_betterleaks_unions_new_rule_types(run):
    run.return_value.stdout = json.dumps(
        [{"RuleID": "aws-access-token"}, {"RuleID": "generic-api-key"}]
    ).encode()

    # text has no local matches, so the external findings are what surface
    counts = leaks.scan("nothing structured here", config=_BETTERLEAKS, run=run)
    assert counts.get("aws_access_key") == 1  # mapped rule id
    assert counts.get("leak_generic-api-key") == 1  # unmapped -> kept verbatim
    run.assert_called_once()


def test_betterleaks_missing_binary_degrades_to_local(run):
    run.side_effect = FileNotFoundError("betterleaks")

    # local regex still fires; the missing binary must not crash the scan
    counts = leaks.scan("key AKIAIOSFODNN7EXAMPLE", config=_BETTERLEAKS, run=run)
    assert counts.get("aws_access_key") == 1


def test_local_regex_takes_precedence_over_external(run):
    run.return_value.stdout = json.dumps([{"RuleID": "aws-access-token"}]).encode()

    # local regex already counted the AWS key once; external must not double it
    counts = leaks.scan("key AKIAIOSFODNN7EXAMPLE", config=_BETTERLEAKS, run=run)
    assert counts["aws_access_key"] == 1


def test_bad_external_report_is_ignored(run):
    run.return_value.stdout = b"not json"

    assert leaks.scan("just text", config=_BETTERLEAKS, run=run) == {}


def test_external_not_invoked_when_unconfigured(run):
    leaks.scan("key AKIAIOSFODNN7EXAMPLE", config=LeaksSettings(), run=run)
    run.assert_not_called()


def test_betterleaks_runs_the_configured_binary_with_its_timeout(run):
    run.return_value.stdout = b"[]"
    leaks.scan("text", config=_BETTERLEAKS, run=run)
    args, kwargs = run.call_args
    assert args[0][0] == "betterleaks"
    assert kwargs["timeout"] == 5.0


def test_settings_read_the_corpus_environment_names():
    env = {"CORPUS_LEAKS_BIN": "/usr/local/bin/betterleaks", "CORPUS_LEAKS_TIMEOUT": "12.5"}
    assert LeaksSettings.from_env(env) == LeaksSettings(
        bin="/usr/local/bin/betterleaks", timeout=12.5
    )


def test_settings_default_to_local_rules_only():
    assert LeaksSettings.from_env({}) == LeaksSettings(bin="", timeout=30.0)


def test_a_bad_timeout_names_its_variable():
    with pytest.raises(ValueError, match="CORPUS_LEAKS_TIMEOUT"):
        LeaksSettings.from_env({"CORPUS_LEAKS_TIMEOUT": "soon"})


def test_scan_spans_masks_whole_private_key_block():
    block = "-----BEGIN OPENSSH PRIVATE KEY-----\nQUJDREVG\n-----END OPENSSH PRIVATE KEY-----"
    spans = leaks.scan_spans(f"before\n{block}\nafter")
    block_spans = [s for s in spans if s.entity_type == "private_key"]
    # the whole PEM block is spanned so redaction removes the key material
    whole = max(block_spans, key=lambda s: s.end - s.start)
    assert "QUJDREVG" in f"before\n{block}\nafter"[whole.start : whole.end]
    assert whole.category == "secret"


def test_scan_spans_truncated_block_falls_back_to_header():
    # No END line: the block matcher can't fire, but the header rule still spans it.
    spans = leaks.scan_spans("-----BEGIN RSA PRIVATE KEY-----\nQUJD\n(no end marker)")
    assert any(s.entity_type == "private_key" for s in spans)


def test_scan_spans_reports_key_offsets():
    spans = leaks.scan_spans("deploy key AKIAIOSFODNN7EXAMPLE now")
    aws = next(s for s in spans if s.entity_type == "aws_access_key")
    assert aws.end > aws.start


def test_scan_spans_empty_and_none():
    assert leaks.scan_spans("") == []
    assert leaks.scan_spans(None) == []
