import pytest

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


def test_email_is_counted_only_in_the_span_view():
    # Email is ubiquitous in mail, so it stays out of the archive count scan, but the
    # egress redactor (span view) must catch it.
    text = "reach me at alice@example.org please"
    assert "email" not in pii.scan(text).secret_counts
    assert any(s.entity_type == "email" for s in pii.scan_spans(text))


def test_scan_spans_reports_gated_offsets():
    spans = pii.scan_spans("card on file: 4111 1111 1111 1111")
    card = next(s for s in spans if s.entity_type == "credit_card")
    assert card.category == "pii"
    assert card.end > card.start


def test_scan_spans_empty_and_none():
    assert pii.scan_spans("") == []
    assert pii.scan_spans(None) == []


# Synthetic Norwegian national id numbers: month + 80 marks the tax
# administration's test numbers (Tenor), which are never issued to a person.
FNR = "01819010001"
D_NUMMER = "41819010186"
FNR_LEAP_DAY = "29810410076"


@pytest.mark.parametrize("number", [FNR, D_NUMMER, FNR_LEAP_DAY])
def test_valid_national_ids_pass_the_checksum(number):
    assert pii._is_no_national_id(number)


@pytest.mark.parametrize(
    "number",
    [
        FNR[:-1] + str((int(FNR[-1]) + 1) % 10),  # second check digit off
        FNR[:9] + str((int(FNR[9]) + 1) % 10) + FNR[10],  # first check digit off
        "32819010001",  # day 32
        "0181901000",  # ten digits
        "12345678901",  # an order number
    ],
)
def test_invalid_national_ids_are_rejected(number):
    assert not pii._is_no_national_id(number)


@pytest.mark.parametrize("text", [f"fnr {FNR}", f"Fødselsnummer: {FNR[:6]} {FNR[6:]}", D_NUMMER])
def test_a_national_id_is_counted_and_redacted(text):
    assert pii.scan(text).secret_counts == {"no_national_id": 1}
    assert [s.entity_type for s in pii.scan_spans(text)] == ["no_national_id"]


def test_an_eleven_digit_number_inside_a_longer_run_is_not_an_id():
    assert not pii.scan(f"ref 9{FNR}9").has_secrets


@pytest.mark.parametrize(
    ("text", "phone"),
    [
        ("Tlf: 012 34 567", "012 34 567"),
        ("Mobil +47 012 34 567", "+47 012 34 567"),
        ("ring meg på +4701234567", "+4701234567"),
        ("phone: (202) 555-0123", "(202) 555-0123"),
        ("Tel. 02 33 44 55", "02 33 44 55"),
    ],
)
def test_a_clear_phone_number_is_redacted(text, phone):
    spans = pii.scan_spans(text)
    assert [(s.entity_type, text[s.start : s.end]) for s in spans] == [("phone", phone)]


@pytest.mark.parametrize(
    "text",
    [
        "Order 41234567 shipped",
        "Faktura 12345678, forfall 2026-10-12",
        "Beløp 12 345 678 kr",
        "Sak 2026-10-12-0042",
        "Sendt 2026-10-12T07:00:00+02:00",
        "Ref 012 34 567 i telefonmøtet i går",  # the phone word is not right before it
    ],
)
def test_digit_runs_that_are_not_clearly_phones_pass(text):
    assert not [s for s in pii.scan_spans(text) if s.entity_type == "phone"]


def test_phone_numbers_are_not_counted_in_the_audit():
    assert not pii.scan("Tlf: 012 34 567").has_secrets


def _with_check_digits(nine: str) -> str | None:
    """Append the two mod-11 check digits to *nine* digits (an independent oracle)."""
    def check(digits: str, weights: tuple[int, ...]) -> int | None:
        k = 11 - sum(int(d) * w for d, w in zip(digits, weights, strict=True)) % 11
        return 0 if k == 11 else None if k == 10 else k

    k1 = check(nine, (3, 7, 6, 1, 8, 9, 4, 5, 2))
    if k1 is None:
        return None
    k2 = check(nine + str(k1), (5, 4, 3, 2, 7, 6, 5, 4, 3, 2))
    return None if k2 is None else f"{nine}{k1}{k2}"


def _synthetic(ddmmyy: str) -> str:
    """The first checksum-valid number for a birth date (Tenor months are + 80)."""
    return next(n for i in range(100, 1000) if (n := _with_check_digits(f"{ddmmyy}{i}")))


@pytest.mark.parametrize(
    ("ddmmyy", "valid"),
    [
        ("018190", True),  # Tenor synthetic: month + 80
        ("418190", True),  # D-nummer day + 40, on a Tenor month
        ("298200", True),  # 29 February 2000 (Tenor month 82)
        ("298201", False),  # no 29 February in 1901 or 2001
        ("019390", False),  # month 13 even after the Tenor offset
        ("728190", False),  # day 32 even after the D-nummer offset
    ],
)
def test_the_birth_date_must_be_a_real_date_after_the_kind_offsets(ddmmyy, valid):
    number = next(n for i in range(100, 1000) if (n := _with_check_digits(f"{ddmmyy}{i}")))

    assert pii._is_no_national_id(number) is valid


def test_the_h_nummer_month_offset_is_accepted():
    # No synthetic H-nummer range exists, so a generated number with month + 40
    # could be a real one; the offset is checked on the date alone.
    assert pii._valid_birth_date("015190")


@pytest.mark.parametrize(
    ("text", "phone"),
    [
        ("+47 012 34 568 2026-10-12", "+47 012 34 568"),
        ("Mobil 012 34 568 | Org.nr 123 456 789", "012 34 568"),
        ("Tlf: 012 34 568 2026", "012 34 568"),
        ("telefonnummer: 01234568", "01234568"),
        ("mobilnummer 012 34 568", "012 34 568"),
        ("mobiltelefon 01234568", "01234568"),
        ("Phone number: 202 555 0123", "202 555 0123"),
        ("Tlf (privat) 01234568", "01234568"),
        ("Direkte: 02 33 44 55", "02 33 44 55"),
        ("0047 012 34 567", "0047 012 34 567"),
        ("+44 20 7946 0958", "+44 20 7946 0958"),
        ("+4701234568 2026", "+4701234568"),
    ],
)
def test_a_phone_is_cut_where_its_number_ends(text, phone):
    spans = [s for s in pii.scan_spans(text) if s.entity_type == "phone"]
    assert [text[s.start : s.end] for s in spans] == [phone]


def test_redaction_replaces_a_phone_and_a_national_id():
    from corpus.redact import redact

    fnr = _synthetic("018190")
    out = redact(f"Ring meg på tlf 012 34 568, fnr {fnr}.").text

    assert "[REDACTED:phone]" in out and "[REDACTED:no_national_id]" in out
    assert "012 34 568" not in out and fnr not in out
