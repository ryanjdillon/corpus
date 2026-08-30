"""Span-level redaction over free text, built on the existing detectors.

All vectors are non-secret by construction: the canonical AWS docs example key, a
structurally-valid but meaningless PEM block, and reserved-range identity numbers.
"""

from __future__ import annotations

from corpus.redact import Span, redact, resolve_overlaps

# A structurally-valid but meaningless private key block (fake body).
_FAKE_PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n"
    "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n"
    "-----END OPENSSH PRIVATE KEY-----"
)


def test_redacts_fake_api_key():
    result = redact("deploy with key AKIAIOSFODNN7EXAMPLE please")
    assert "AKIAIOSFODNN7EXAMPLE" not in result.text
    assert "[REDACTED:aws_access_key]" in result.text
    assert result.counts == {"aws_access_key": 1}
    assert result.redacted


def test_redacts_whole_private_key_block():
    # Redaction must remove the key *material*, not merely the BEGIN marker.
    result = redact(f"my key follows\n{_FAKE_PRIVATE_KEY}\nregards")
    assert "PRIVATE KEY" not in result.text
    assert "QUJDREVG" not in result.text  # the base64 body is gone too
    assert result.text == "my key follows\n[REDACTED:private_key]\nregards"
    assert result.counts == {"private_key": 1}


def test_redacts_email():
    result = redact("reach me at alice@example.org anytime")
    assert "alice@example.org" not in result.text
    assert result.counts == {"email": 1}


def test_redacts_credit_card_only_with_context():
    with_ctx = redact("card on file: 4111 1111 1111 1111")
    assert "4111" not in with_ctx.text
    assert with_ctx.counts == {"credit_card": 1}
    # Same Luhn-valid number as an order id, no card wording -> left untouched.
    without_ctx = redact("Order 4111 1111 1111 1111 has shipped, thanks!")
    assert without_ctx.text == "Order 4111 1111 1111 1111 has shipped, thanks!"
    assert not without_ctx.redacted


def test_clean_text_is_untouched():
    result = redact("Are we still on for lunch tomorrow?")
    assert result.text == "Are we still on for lunch tomorrow?"
    assert result.findings == ()
    assert result.counts == {}
    assert not result.redacted


def test_empty_and_none():
    assert redact("").text == ""
    assert redact(None).text == ""
    assert redact(None).counts == {}


def test_redaction_is_idempotent():
    once = redact(f"key AKIAIOSFODNN7EXAMPLE and {_FAKE_PRIVATE_KEY}")
    twice = redact(once.text)
    assert twice.text == once.text
    assert twice.counts == {}  # placeholders match no detector


def test_no_matched_value_in_result_repr():
    result = redact("SSN 900-12-3456 on file; card 4111 1111 1111 1111 charged")
    assert "900-12-3456" not in repr(result)
    assert "4111" not in repr(result)


def test_resolve_overlaps_keeps_leftmost_longest():
    spans = [
        Span(0, 10, "private_key", "secret"),
        Span(3, 7, "email", "pii"),  # nested inside the first -> dropped
        Span(10, 15, "email", "pii"),  # abuts, no overlap -> kept
    ]
    kept = resolve_overlaps(spans)
    assert kept == [Span(0, 10, "private_key", "secret"), Span(10, 15, "email", "pii")]


def test_resolve_overlaps_prefers_secret_on_tie():
    spans = [Span(0, 5, "email", "pii"), Span(0, 5, "aws_access_key", "secret")]
    assert resolve_overlaps(spans) == [Span(0, 5, "aws_access_key", "secret")]
