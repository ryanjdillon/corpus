from corpus import pii


def test_detects_ssn_without_leaking_value():
    # 900-xx-xxxx is in the SSA's permanently-reserved (never-issued) range, so it
    # cannot be anyone's real SSN, yet Presidio still detects the format. (The
    # classic 123-45-6789 / 078-05-1120 examples are denylisted, so they don't test
    # detection.)
    result = pii.scan("My SSN is 900-12-3456, keep it safe.")
    assert "us_ssn" in result.secret_types
    assert result.has_secrets
    assert result.secret_counts["us_ssn"] >= 1
    # the value must never be retained anywhere in the result
    assert "900-12-3456" not in repr(result)


def test_detects_credit_card_luhn():
    result = pii.scan("card on file: 4111 1111 1111 1111")
    assert "credit_card" in result.secret_types


def test_detects_private_key_header():
    result = pii.scan("-----BEGIN OPENSSH PRIVATE KEY-----\nQUJD\n-----END OPENSSH PRIVATE KEY-----")
    assert "private_key" in result.secret_types


def test_recovery_code_requires_context():
    with_ctx = pii.scan("Your backup codes: ABCD-1234, EFGH-5678 — store them safely.")
    assert "recovery_code" in with_ctx.secret_types
    # same-shaped token without recovery context is not a secret
    no_ctx = pii.scan("Meeting room ABCD-1234 is booked for us.")
    assert "recovery_code" not in no_ctx.secret_types


def test_recovery_context_without_code_token():
    # recovery wording but no code-shaped token -> not flagged
    result = pii.scan("Click the link to start account recovery.")
    assert "recovery_code" not in result.secret_types


def test_clean_text_has_no_secrets():
    result = pii.scan("Hi — are we still on for lunch tomorrow?")
    assert not result.has_secrets
    assert result.secret_types == ()


def test_empty_and_none():
    assert pii.scan("").has_secrets is False
    assert pii.scan(None).has_secrets is False
