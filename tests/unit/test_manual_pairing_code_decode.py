"""
The manual Matter setup code decoder — regression-locked to the 2026-07-13
incident.

WHY THIS TEST EXISTS: the operator spent an hour on a commissioning failure that
reported "no commissionable device discovered" while a device sat in pairing mode
right there. Root cause: his code targeted SHORT DISCRIMINATOR 11, the device
advertised 8, and discovery silently filtered it out. We now decode the code and
say so — but only if the decode is RIGHT. A first attempt treated the 11-digit
code as one flat integer and produced 3/30244409 instead of 11/20341430; it was
caught only by cross-checking against a hand-decode. Lock it down.
"""

from app import _short_discriminator_from_manual_code as short_disc


def _passcode(code: str) -> int:
    """The spec's passcode decode, mirrored here so the fixture is verified on
    BOTH fields (a decoder can get the discriminator right by luck)."""
    d = "".join(c for c in code if c.isdigit())
    chunk2, chunk3 = int(d[1:6]), int(d[6:10])
    return (chunk2 & 0x3FFF) | ((chunk3 & 0x1FFF) << 14)


class TestManualPairingCodeDecode:
    # The operator's REAL code from the incident. Hand-decoded independently
    # from the matter.js logs: short discriminator 11, passcode 20341430.
    OPERATOR_CODE = "25803812418"

    def test_operator_incident_code_short_discriminator(self):
        assert short_disc(self.OPERATOR_CODE) == 11

    def test_operator_incident_code_passcode(self):
        assert _passcode(self.OPERATOR_CODE) == 20341430

    def test_is_not_a_flat_integer_decode(self):
        """The naive `int(digits[:10]) >> 27` reading yields 3 — the bug we shipped
        against for ten minutes. Assert we do NOT reproduce it."""
        naive = (int(self.OPERATOR_CODE[:10]) >> 27) & 0x0F
        assert naive == 3
        assert short_disc(self.OPERATOR_CODE) != naive

    def test_device_discriminator_did_not_match_the_code(self):
        """The device at .135 advertised long discriminator 2048 → short 8, while
        the code targeted 11 — so discovery filtered it out. This is the whole
        diagnosis, encoded."""
        device_long = 2048
        device_short = (device_long >> 8) & 0x0F
        assert device_short == 8
        assert short_disc(self.OPERATOR_CODE) != device_short

    def test_separators_are_tolerated(self):
        assert short_disc("2580-381-2418") == 11

    def test_qr_and_garbage_return_none(self):
        assert short_disc("MT:Y.K90SO527JA0648G00") is None
        assert short_disc("") is None
        assert short_disc("123") is None
