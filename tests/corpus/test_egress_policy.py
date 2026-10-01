"""The gateway-neutral egress policy: verdicts for request bodies.

The policy is a value passed to ``inspect``, so each test states the rules it
relies on. All secrets are fake vectors.
"""

from __future__ import annotations

import json

import pytest

from corpus.egress.policy import EgressPolicy, inspect, redact_payload

FAKE_PRIVATE_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n"
    "-----END OPENSSH PRIVATE KEY-----"
)


@pytest.fixture
def policy():
    return EgressPolicy(
        skip_models=frozenset({"qwen3-coder"}),
        batch_clients=frozenset({"corpus-enrich"}),
        batch_max_bytes=200,
    )


def chat(content: str, model: str = "DeepSeek-V4-Flash") -> bytes:
    return json.dumps({"model": model, "messages": [{"role": "user", "content": content}]}).encode()


# --------------------------------------------------------------------------- #
# payload walking
# --------------------------------------------------------------------------- #
def test_redact_payload_openai_string_content():
    data = {"messages": [{"role": "user", "content": "key AKIAIOSFODNN7EXAMPLE"}]}
    findings = redact_payload(data)
    assert "AKIAIOSFODNN7EXAMPLE" not in data["messages"][0]["content"]
    assert [f.entity_type for f in findings] == ["aws_access_key"]


def test_redact_payload_content_parts_and_anthropic_system():
    data = {
        "system": "contact alice@example.org",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "card 4111 1111 1111 1111 now"}]}
        ],
    }
    findings = redact_payload(data)
    assert "alice@example.org" not in data["system"]
    assert "4111" not in data["messages"][0]["content"][0]["text"]
    assert {f.entity_type for f in findings} == {"email", "credit_card"}


def test_redact_payload_anthropic_system_parts():
    data = {"system": [{"type": "text", "text": "mail bob@example.net"}], "messages": []}
    redact_payload(data)
    assert "bob@example.net" not in data["system"][0]["text"]


def test_redact_payload_ignores_non_dict_messages():
    data = {"messages": ["not a dict", {"role": "user", "content": "key AKIAIOSFODNN7EXAMPLE"}]}
    assert [f.entity_type for f in redact_payload(data)] == ["aws_access_key"]


# --------------------------------------------------------------------------- #
# verdicts
# --------------------------------------------------------------------------- #
def test_clean_body_passes(policy):
    assert inspect(chat("lunch at noon?"), policy=policy).action == "pass"


def test_dirty_body_is_replaced_with_a_redacted_one(policy):
    verdict = inspect(chat("key AKIAIOSFODNN7EXAMPLE"), policy=policy)
    assert verdict.action == "replace"
    assert "AKIAIOSFODNN7EXAMPLE" not in json.loads(verdict.body)["messages"][0]["content"]


def test_private_key_is_refused_403_without_echoing_it(policy):
    verdict = inspect(chat(FAKE_PRIVATE_KEY), policy=policy)
    assert (verdict.action, verdict.status) == ("refuse", 403)
    assert "private_key" in verdict.detail
    assert b"PRIVATE KEY" not in verdict.body and "PRIVATE KEY" not in verdict.detail


def test_without_block_types_a_private_key_is_redacted_instead():
    verdict = inspect(chat(FAKE_PRIVATE_KEY), policy=EgressPolicy(block_types=frozenset()))
    assert verdict.action == "replace"
    assert b"PRIVATE KEY" not in verdict.body


@pytest.mark.parametrize("body", [b"not json at all", b"[1, 2, 3]"])
def test_uninspectable_body_is_refused_when_failing_closed(body):
    verdict = inspect(body, policy=EgressPolicy(fail_open=False))
    assert (verdict.action, verdict.status) == ("refuse", 403)


def test_uninspectable_body_passes_when_failing_open():
    # Multipart audio uploads take this path when the gate fails open.
    assert inspect(b"--boundary\r\n...", policy=EgressPolicy(fail_open=True)).action == "pass"


def test_empty_body_passes(policy):
    assert inspect(b"", policy=policy).action == "pass"


def test_image_parts_pass_through_byte_identical(policy):
    # Only text is redacted: an inline image must come back unchanged, or the
    # provider receives a corrupt image.
    image = "data:image/jpeg;base64," + "QUtJQUlPU0ZPRE5ON0VYQU1QTEU+/9j/4AAQ" * 2000
    body = json.dumps(
        {
            "model": "DeepSeek-V4-Flash",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "who is alice@example.org?"},
                        {"type": "image_url", "image_url": {"url": image}},
                    ],
                }
            ],
        }
    ).encode()
    parts = json.loads(inspect(body, policy=policy).body)["messages"][0]["content"]
    assert "alice@example.org" not in parts[0]["text"]
    assert parts[1] == {"type": "image_url", "image_url": {"url": image}}


# --------------------------------------------------------------------------- #
# local models and batch clients
# --------------------------------------------------------------------------- #
def test_skipped_local_model_passes_unscanned(policy):
    # Served locally, so even a private key never leaves the network.
    assert inspect(chat(FAKE_PRIVATE_KEY, model="qwen3-coder"), policy=policy).action == "pass"


def test_model_not_in_the_skip_list_is_scanned(policy):
    verdict = inspect(chat(FAKE_PRIVATE_KEY, model="DeepSeek-V4-Flash"), policy=policy)
    assert verdict.action == "refuse"


def test_skip_list_matches_exact_names_only(policy):
    # A near-miss name is scanned: a typo can only make the gate scan more.
    verdict = inspect(chat(FAKE_PRIVATE_KEY, model="qwen3-coder-cloud"), policy=policy)
    assert verdict.action == "refuse"


def test_batch_client_over_the_limit_on_a_scanned_model_gets_413(policy):
    verdict = inspect(chat("x" * 500), "corpus-enrich", policy=policy)
    assert (verdict.action, verdict.status) == ("refuse", 413)
    assert json.loads(verdict.body)["error"]["type"] == "request_too_large"


def test_batch_client_over_the_limit_on_a_local_model_passes(policy):
    assert (
        inspect(chat("x" * 500, model="qwen3-coder"), "corpus-enrich", policy=policy).action
        == "pass"
    )


def test_other_client_over_the_limit_passes(policy):
    assert inspect(chat("x" * 500), "orchestrator", policy=policy).action == "pass"


def test_batch_client_under_the_limit_is_scanned_normally(policy):
    assert inspect(chat("hi"), "corpus-enrich", policy=policy).action == "pass"


def test_policy_reads_comma_separated_settings():
    policy = EgressPolicy.from_settings()
    assert isinstance(policy.skip_models, frozenset)
    assert isinstance(policy.batch_clients, frozenset)
    assert policy.batch_max_bytes > 0


def test_non_string_model_is_scanned(policy):
    body = json.dumps({"model": ["qwen3-coder"], "messages": [{"role": "user", "content": "x"}]})
    assert inspect(body.encode(), policy=policy).action == "pass"
