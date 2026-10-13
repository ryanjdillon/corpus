"""Span-level redaction over free text, built on the detectors.

All vectors are non-secret by construction: the canonical AWS docs example key, a
structurally-valid but meaningless PEM block, and reserved-range identity numbers.
"""

from __future__ import annotations

import random
import re
from itertools import pairwise

import pytest
from corpus_detect import Span, detect_spans, redact, resolve_overlaps, validate_spans
from corpus_detect.span import resolve_strictest

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


# A synthetic, meaningless token standing in for a credential an outside detector found.
_TOKEN = "zq7Xv2Lm9Pw4Rt8Ky3Hn"
_SIDECAR_TYPE = "secret:betterleaks:generic-api-key"


def _extra(text: str, value: str, entity_type: str = _SIDECAR_TYPE) -> Span:
    start = text.index(value)
    return Span(start, start + len(value), entity_type, "secret")


def _assert_no_run_survives(output: str, value: str) -> None:
    """No two adjacent characters of ``value`` remain outside the placeholders."""
    bare = re.sub(r"\[REDACTED:[^\]]*\]", "\0", output)
    for i in range(len(value) - 1):
        assert value[i : i + 2] not in bare


def test_extra_secret_span_masks_every_character_with_type_only_label():
    text = f"token={_TOKEN} end"
    result = redact(text, extra_spans=[_extra(text, _TOKEN)])
    assert result.text == f"token=[REDACTED:{_SIDECAR_TYPE}] end"
    _assert_no_run_survives(result.text, _TOKEN)
    assert result.counts == {_SIDECAR_TYPE: 1}
    assert result.findings == (Span(6, 6 + len(_TOKEN), _SIDECAR_TYPE, "secret"),)


def test_extra_secret_span_of_an_unknown_type_never_shows_a_tail():
    text = f"key {_TOKEN}"
    result = redact(text, extra_spans=[_extra(text, _TOKEN, "secret:betterleaks:aws-access-token")])
    assert result.text == "key [REDACTED:secret:betterleaks:aws-access-token]"
    assert "••••" not in result.text


def test_extra_secret_span_beats_an_overlapping_detected_email():
    text = f"mail alice@example.org={_TOKEN} now"
    email = next(s for s in redact(text).findings if s.entity_type == "email")
    # The secret starts inside the email, so leftmost-longest alone would keep the email.
    secret = Span(text.index("example"), text.index(_TOKEN) + len(_TOKEN), _SIDECAR_TYPE, "secret")
    assert email.start < secret.start < email.end
    result = redact(text, extra_spans=[secret])
    assert result.text == f"mail [REDACTED:email][REDACTED:{_SIDECAR_TYPE}] now"
    assert result.counts == {"email": 1, _SIDECAR_TYPE: 1}
    _assert_no_run_survives(result.text, "example.org=" + _TOKEN)


def test_extra_secret_span_extends_an_overlapping_detected_secret():
    aws = "AKIAIOSFODNN7EXAMPLE"
    text = f"pair {aws} {_TOKEN} done"
    # Starts before the detected key and ends inside it: the key's tail must not survive.
    extra = Span(text.index(aws) - 1, text.index(aws) + 8, _SIDECAR_TYPE, "secret")
    result = redact(text, extra_spans=[extra])
    assert result.text == (
        f"pair[REDACTED:{_SIDECAR_TYPE}][REDACTED:aws_access_key] {_TOKEN} done"
    )
    _assert_no_run_survives(result.text, aws)
    assert result.counts == {_SIDECAR_TYPE: 1, "aws_access_key": 1}


def test_extra_span_offsets_are_code_points():
    text = f"Året ø {_TOKEN} slutt"
    start = 7  # "Året ø " is seven code points (and more UTF-8 bytes).
    assert text[start : start + len(_TOKEN)] == _TOKEN
    span = Span(start, start + len(_TOKEN), _SIDECAR_TYPE, "secret")
    result = redact(text, extra_spans=[span])
    assert result.text == f"Året ø [REDACTED:{_SIDECAR_TYPE}] slutt"


def test_extra_pii_span_is_masked_like_a_detected_one():
    text = "member Kari Nordmann joined"
    span = Span(7, 20, "person", "pii")
    assert redact(text, extra_spans=[span]).text == "member [REDACTED:person] joined"


@pytest.mark.parametrize(
    ("span", "field"),
    [
        (Span(-1, 3, _SIDECAR_TYPE, "secret"), "start/end"),
        (Span(0, 99, _SIDECAR_TYPE, "secret"), "start/end"),
        (Span(4, 4, _SIDECAR_TYPE, "secret"), "start/end"),
        (Span(5, 4, _SIDECAR_TYPE, "secret"), "start/end"),
        (Span(0, 3, _SIDECAR_TYPE, "credential"), "category"),
        (Span(0, 3, "Secret:Aws", "secret"), "entity_type"),
        (Span(0, 3, "generic api key", "secret"), "entity_type"),
        (Span(0, 3, "secret/aws", "secret"), "entity_type"),
        (Span(0, 3, "a" * 65, "secret"), "entity_type"),
        (Span(0, 3, "", "secret"), "entity_type"),
        (Span(0.0, 3, _SIDECAR_TYPE, "secret"), "start"),  # type: ignore[arg-type]
        (Span(True, 3, _SIDECAR_TYPE, "secret"), "start"),
        (("0", "3", _SIDECAR_TYPE, "secret"), "Span"),
    ],
)
def test_invalid_extra_span_is_rejected_naming_the_field(span, field):
    text = f"value {_TOKEN}"
    with pytest.raises(ValueError, match=r"extra_spans\[0\]") as err:
        redact(text, extra_spans=[span])
    assert field in str(err.value)
    assert _TOKEN[:4] not in str(err.value)


def test_type_of_64_characters_is_accepted():
    text = f"value {_TOKEN}"
    span = _extra(text, _TOKEN, "a" * 64)
    assert redact(text, extra_spans=[span]).counts == {"a" * 64: 1}


def test_extra_span_on_empty_text_is_rejected():
    with pytest.raises(ValueError, match="start/end"):
        redact("", extra_spans=[Span(0, 1, _SIDECAR_TYPE, "secret")])
    assert redact(None, extra_spans=()).text == ""


def test_no_extra_spans_matches_the_detected_redaction():
    text = (
        f"SSN 900-12-3456, key AKIAIOSFODNN7EXAMPLE, mail alice@example.org,\n"
        f"{_FAKE_PRIVATE_KEY}\ncard on file: 4111 1111 1111 1111"
    )
    expected = (
        "SSN [REDACTED:us_ssn], key [REDACTED:aws_access_key], mail [REDACTED:email],\n"
        "[REDACTED:private_key]\ncard on file: [REDACTED:credit_card]"
    )
    assert redact(text).text == expected
    assert redact(text, extra_spans=[]).text == expected
    assert resolve_strictest(detect_spans(text)) == resolve_overlaps(detect_spans(text))


def test_resolve_strictest_trims_pii_around_a_nested_secret():
    spans = [Span(0, 20, "email", "pii"), Span(5, 10, "aws_access_key", "secret")]
    assert resolve_strictest(spans) == [
        Span(0, 5, "email", "pii"),
        Span(5, 10, "aws_access_key", "secret"),
        Span(10, 20, "email", "pii"),
    ]


def test_resolve_strictest_drops_pii_wholly_inside_a_secret():
    spans = [Span(0, 20, "private_key", "secret"), Span(3, 7, "email", "pii")]
    assert resolve_strictest(spans) == [Span(0, 20, "private_key", "secret")]


def test_resolve_strictest_never_unmasks_what_resolve_overlaps_masked():
    # A pii span trimmed around a secret must not revive a piece that then
    # beats another pii span: 23-24 would stay in the text.
    spans = [
        Span(8, 18, "secret:betterleaks:k", "secret"),
        Span(15, 23, "email", "pii"),
        Span(21, 25, "phone", "pii"),
    ]
    assert resolve_strictest(spans) == [
        Span(8, 18, "secret:betterleaks:k", "secret"),
        Span(18, 23, "email", "pii"),
        Span(23, 25, "phone", "pii"),
    ]


def test_identical_secrets_take_the_smallest_type_whatever_the_order():
    a = Span(2, 9, "secret:betterleaks:a", "secret")
    b = Span(2, 9, "secret:betterleaks:b", "secret")
    assert resolve_strictest([a, b]) == resolve_strictest([b, a]) == [a]


def test_counts_a_span_cut_in_two_once():
    text = "x" * 20
    spans = [Span(0, 20, "person", "pii"), Span(5, 10, _SIDECAR_TYPE, "secret")]
    result = redact(text, extra_spans=spans)
    assert len(result.findings) == 3
    assert result.counts == {"person": 1, _SIDECAR_TYPE: 1}


def test_counts_skip_a_span_wholly_covered_by_others():
    text = "x" * 20
    spans = [Span(0, 20, _SIDECAR_TYPE, "secret"), Span(3, 7, "person", "pii")]
    assert redact(text, extra_spans=spans).counts == {_SIDECAR_TYPE: 1}


def test_validate_spans_is_exported_for_a_caller_to_check_first():
    validate_spans([Span(0, 3, _SIDECAR_TYPE, "secret")], 3)
    with pytest.raises(ValueError, match=r"spans\[0\]\.start/end"):
        validate_spans([Span(0, 4, _SIDECAR_TYPE, "secret")], 3)


_TYPES = ("a", "b", "c")


def _random_spans(rng: random.Random, length: int) -> list[Span]:
    spans = []
    for _ in range(rng.randint(0, 7)):
        start = rng.randrange(length)
        end = rng.randint(start + 1, min(length, start + 12))
        spans.append(Span(start, end, rng.choice(_TYPES), rng.choice(("secret", "pii"))))
    return spans


def _covered(spans: list[Span], category: str | None = None) -> set[int]:
    return {
        i
        for s in spans
        if category is None or s.category == category
        for i in range(s.start, s.end)
    }


def test_resolve_strictest_covers_the_union_of_its_inputs_in_any_order():
    rng = random.Random(694)
    for _ in range(20_000):
        spans = _random_spans(rng, 30)
        resolved = resolve_strictest(spans)
        # Disjoint, sorted masks: each character is replaced exactly once.
        assert all(a.end <= b.start for a, b in pairwise(resolved))
        assert _covered(resolved) == _covered(spans)
        assert _covered(spans, "secret") <= _covered(resolved, "secret")
        shuffled = rng.sample(spans, len(spans))
        assert resolve_strictest(shuffled) == resolved
