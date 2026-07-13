"""
Tests for the Matter manual-pairing-code codec.

The anchor cases are REAL: they come from the 2026-07-13 incident in which a
smart plug refused to commission ("no commissionable device was discovered")
while plainly sitting in pairing mode. Its label code was 25803812418; the
device was advertising discriminator 2048. The rescue was to keep the passcode
and re-encode against the advertised discriminator, producing 20888612410.

If these tests ever fail, the codec has drifted from the spec — and every
"Get Code" answer built on it is suspect.
"""

import pytest

from services.matter_pairing_codes import manual_code

# The plug's printed label code, and what it must decode to.
LABEL_CODE = "25803812418"
LABEL_SHORT_DISCRIMINATOR = 11
LABEL_PASSCODE = 20341430

# What the device was ACTUALLY advertising, and the code that targets it.
ADVERTISED_DISCRIMINATOR = 2048   # 12-bit, from the mDNS TXT record (D=2048)
ADVERTISED_SHORT = 8              # 2048 >> 8
REPAIRED_CODE = "20888612410"


def test_decode_real_label_code():
    """The label code decodes to the passcode + short discriminator it encodes."""
    decoded = manual_code.decode(LABEL_CODE)
    assert decoded.short_discriminator == LABEL_SHORT_DISCRIMINATOR
    assert decoded.passcode == LABEL_PASSCODE


def test_encode_is_the_inverse_of_decode():
    """Round-trip: re-encoding a decoded code reproduces it exactly."""
    decoded = manual_code.decode(LABEL_CODE)
    assert manual_code.encode(decoded.short_discriminator, decoded.passcode) == LABEL_CODE


def test_short_discriminator_extraction():
    """The manual code carries only the TOP 4 BITS of the 12-bit discriminator."""
    assert manual_code.short_discriminator_of(ADVERTISED_DISCRIMINATOR) == ADVERTISED_SHORT
    assert manual_code.short_discriminator_of(0xABC) == 0xA


def test_repair_reproduces_the_incident_fix():
    """The 2026-07-13 rescue: same passcode, re-encoded for the advertised
    discriminator. This exact string was pasted into the UI and accepted."""
    assert manual_code.repair(LABEL_CODE, ADVERTISED_DISCRIMINATOR) == REPAIRED_CODE
    # ...and the repaired code still carries the ORIGINAL passcode — the secret
    # is never altered, only the locator.
    assert manual_code.decode(REPAIRED_CODE).passcode == LABEL_PASSCODE
    assert manual_code.decode(REPAIRED_CODE).short_discriminator == ADVERTISED_SHORT


def test_repair_is_a_noop_when_the_discriminator_already_matches():
    """Callers may invoke repair() unconditionally: a matching discriminator
    leaves the code untouched."""
    matching = LABEL_SHORT_DISCRIMINATOR << 8  # any 12-bit value with short == 11
    assert manual_code.repair(LABEL_CODE, matching) == LABEL_CODE


def test_check_digit_rejects_a_typo():
    """A mistyped code is caught by the Verhoeff digit — so the UI can say
    'you mistyped it' instead of failing later with a misleading 'device not
    found'."""
    typo = LABEL_CODE[:-2] + "9" + LABEL_CODE[-1]  # corrupt one body digit
    with pytest.raises(manual_code.InvalidPairingCode, match="check digit"):
        manual_code.decode(typo)


def test_wrong_length_is_rejected_with_an_explanation():
    with pytest.raises(manual_code.InvalidPairingCode, match="11 digits"):
        manual_code.decode("1234567890")  # 10 digits


def test_normalize_accepts_operator_formatting():
    """Operators paste codes with spaces/dashes; the codec must not care."""
    assert manual_code.decode("2580-3812 418").passcode == LABEL_PASSCODE


@pytest.mark.parametrize("short_disc", range(16))
@pytest.mark.parametrize("passcode", [1, 20341430, 20202021, (1 << 27) - 1])
def test_round_trip_across_the_whole_discriminator_space(short_disc, passcode):
    """Encode -> decode is identity for every short discriminator and a spread
    of passcodes (including the max 27-bit value)."""
    code = manual_code.encode(short_disc, passcode)
    decoded = manual_code.decode(code)
    assert decoded.short_discriminator == short_disc
    assert decoded.passcode == passcode
