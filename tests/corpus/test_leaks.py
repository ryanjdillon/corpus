"""The credential detector: local regexes always, Betterleaks when configured.

All test vectors are non-secret by construction (the canonical AWS docs example
key, and structurally-valid but meaningless keys/tokens)."""

from __future__ import annotations

import json

from corpus import leaks
from corpus.config import settings


def test_detects_private_key_block():
    counts = leaks.scan("-----BEGIN OPENSSH PRIVATE KEY-----\nQUJD\n-----END OPENSSH PRIVATE KEY-----")
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


def test_betterleaks_unions_new_rule_types(monkeypatch):
    monkeypatch.setattr(settings, "leaks_bin", "betterleaks")

    class _Proc:
        stdout = json.dumps(
            [{"RuleID": "aws-access-token"}, {"RuleID": "generic-api-key"}]
        ).encode()

    monkeypatch.setattr(leaks.subprocess, "run", lambda *a, **k: _Proc())

    # text has no local matches, so the external findings are what surface
    counts = leaks.scan("nothing structured here")
    assert counts.get("aws_access_key") == 1  # mapped rule id
    assert counts.get("leak_generic-api-key") == 1  # unmapped -> kept verbatim


def test_betterleaks_missing_binary_degrades_to_local(monkeypatch):
    monkeypatch.setattr(settings, "leaks_bin", "betterleaks")

    def _boom(*a, **k):
        raise FileNotFoundError("betterleaks")

    monkeypatch.setattr(leaks.subprocess, "run", _boom)

    # local regex still fires; the missing binary must not crash the scan
    counts = leaks.scan("key AKIAIOSFODNN7EXAMPLE")
    assert counts.get("aws_access_key") == 1


def test_local_regex_takes_precedence_over_external(monkeypatch):
    monkeypatch.setattr(settings, "leaks_bin", "betterleaks")

    class _Proc:
        stdout = json.dumps([{"RuleID": "aws-access-token"}]).encode()

    monkeypatch.setattr(leaks.subprocess, "run", lambda *a, **k: _Proc())

    # local regex already counted the AWS key once; external must not double it
    counts = leaks.scan("key AKIAIOSFODNN7EXAMPLE")
    assert counts["aws_access_key"] == 1


def test_bad_external_report_is_ignored(monkeypatch):
    monkeypatch.setattr(settings, "leaks_bin", "betterleaks")

    class _Proc:
        stdout = b"not json"

    monkeypatch.setattr(leaks.subprocess, "run", lambda *a, **k: _Proc())
    assert leaks.scan("just text") == {}


def test_external_not_invoked_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "leaks_bin", "")
    called = {"n": 0}
    monkeypatch.setattr(
        leaks.subprocess, "run", lambda *a, **k: called.__setitem__("n", called["n"] + 1)
    )
    leaks.scan("key AKIAIOSFODNN7EXAMPLE")
    assert called["n"] == 0
