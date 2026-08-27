from corpus import pii


def test_detects_ssn_with_adjacent_context():
    # 900-xx-xxxx is in the SSA's permanently-reserved (never-issued) range, so it
    # cannot be anyone's real SSN, yet Presidio still detects the format. "SSN" sits
    # right beside it, so it counts.
    result = pii.scan("My SSN is 900-12-3456, keep it safe.")
    assert "us_ssn" in result.secret_types
    assert result.has_secrets
    assert result.secret_counts["us_ssn"] >= 1
    # the value must never be retained anywhere in the result
    assert "900-12-3456" not in repr(result)


def test_ssn_shaped_number_without_context_is_ignored():
    # A 9-digit value with no SSN wording nearby (a datalogger reading, an id) is the
    # dominant false positive the adjacency gate is meant to drop.
    result = pii.scan("Sensor sample 900123456 was logged at noon near buoy 7.")
    assert "us_ssn" not in result.secret_types


def test_credit_card_requires_adjacent_context():
    with_ctx = pii.scan("card on file: 4111 1111 1111 1111")
    assert "credit_card" in with_ctx.secret_types
    # same Luhn-valid number as an order/tracking id, no card wording -> not flagged
    no_ctx = pii.scan("Order 4111 1111 1111 1111 has shipped, thanks!")
    assert "credit_card" not in no_ctx.secret_types


def test_iban_counts_on_format_alone():
    # IBAN carries its own checksum and is specific enough to trust without context.
    result = pii.scan("Please wire to GB82 WEST 1234 5698 7654 32 by Friday.")
    assert "iban" in result.secret_types
    assert "GB82" not in repr(result)


def test_driver_license_and_passport_are_not_detected():
    # These recognizers were removed for near-zero precision; ensure they can't fire.
    assert "us_driver_license" not in pii.scan("License A1234567 issued 2020").secret_types
    assert "us_passport" not in pii.scan("Passport 123456789 expires soon").secret_types


def test_clean_text_has_no_secrets():
    result = pii.scan("Hi — are we still on for lunch tomorrow?")
    assert not result.has_secrets
    assert result.secret_types == ()


def test_empty_and_none():
    assert pii.scan("").has_secrets is False
    assert pii.scan(None).has_secrets is False


def test_context_near_requires_words():
    # A recognizer with no context words can never satisfy the adjacency gate.
    assert pii._context_near("some 900-12-3456 text", 5, 15, []) is False
