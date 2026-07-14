"""
matter_pairing_codes.manual_code — the Matter 11-digit MANUAL PAIRING CODE codec.

Pure arithmetic, zero I/O, fully testable. This is the code that was executed by
hand on 2026-07-13 to rescue a plug whose advertised discriminator had drifted
away from the one printed on its label (see repair() below); it is promoted into
the codebase so the next incident is a button, not a chat transcript.

FORMAT (Matter core spec, 11 digits, no VID/PID variant):
    digit  0      : chunk1 = (VID_PID_present << 2) | (short_discriminator >> 2)
    digits 1-5    : chunk2 = ((short_discriminator & 0x3) << 14) | (passcode & 0x3FFF)
    digits 6-9    : chunk3 = passcode >> 14
    digit  10     : Verhoeff check digit over the first 10 digits

WHAT THE PARTS MEAN — and why the distinction is load-bearing:
  * PASSCODE (27 bits, the SECRET): the SPAKE2+ shared secret. It is what
    actually authenticates commissioning. It CANNOT be read out of a device,
    derived, or guessed — it exists on the label, in the device's firmware, and
    (for us) in the code vault if we captured it. Everything in this module
    requires already knowing it.
  * SHORT DISCRIMINATOR (4 bits, a LOCATOR): the top 4 bits of the device's
    12-bit discriminator, used ONLY to filter mDNS/BLE advertisements while
    hunting for the right device. It carries no security weight — and, crucially,
    a device can come back from a reboot/factory-reset advertising a DIFFERENT
    12-bit discriminator than the one its label was printed with. When that
    happens the correct passcode is rejected with "no commissionable device was
    discovered" — a discovery failure masquerading as a bad code.

repair() exists for exactly that case: keep the passcode, re-encode against the
discriminator the device is ACTUALLY advertising right now.
"""

from typing import NamedTuple

# Verhoeff tables (dihedral group D5) — the check-digit scheme Matter mandates.
_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]
_INV = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


class InvalidPairingCode(ValueError):
    """The string is not a well-formed 11-digit manual pairing code."""


class DecodedCode(NamedTuple):
    """A manual code taken apart. `passcode` is the secret; `short_discriminator`
    is only the mDNS/BLE locator (see module docstring)."""
    short_discriminator: int  # 0-15 (top 4 bits of the 12-bit discriminator)
    passcode: int             # 27-bit SPAKE2+ passcode


def check_digit(first_ten: str) -> int:
    """The Verhoeff check digit for the first 10 digits of a manual code."""
    c = 0
    for i, ch in enumerate(reversed(first_ten)):
        c = _D[c][_P[(i + 1) % 8][int(ch)]]
    return _INV[c]


def normalize(code: str) -> str:
    """Strip spaces/dashes from an operator-typed code. Digits only, no length check."""
    return "".join(ch for ch in str(code) if ch.isdigit())


def decode(code: str) -> DecodedCode:
    """Take an 11-digit manual code apart.

    Raises InvalidPairingCode if it is not 11 digits or the check digit is wrong
    (a wrong check digit means a TYPO — say so, rather than failing later with a
    misleading "device not found").
    """
    digits = normalize(code)
    if len(digits) != 11:
        raise InvalidPairingCode(
            f"expected 11 digits, got {len(digits)} — this codec handles the "
            f"11-digit manual code (a 21-digit code carries a VID/PID and is "
            f"not supported)")
    if check_digit(digits[:10]) != int(digits[10]):
        raise InvalidPairingCode(
            "check digit is wrong — the code was mistyped (scan the QR instead)")

    c1, c2, c3 = int(digits[0]), int(digits[1:6]), int(digits[6:10])
    return DecodedCode(
        short_discriminator=((c1 & 0x3) << 2) | (c2 >> 14),
        passcode=(c2 & 0x3FFF) | (c3 << 14),
    )


def encode(short_discriminator: int, passcode: int) -> str:
    """Build an 11-digit manual code from a short discriminator + passcode.

    The inverse of decode(); round-trip is verified in tests. VID_PID_present is
    always 0 (the 21-digit VID/PID variant is out of scope).
    """
    if not 0 <= short_discriminator <= 0xF:
        raise ValueError("short discriminator must be 0-15")
    if not 0 <= passcode < (1 << 27):
        raise ValueError("passcode must fit in 27 bits")
    c1 = short_discriminator >> 2                                   # VID_PID_present = 0
    c2 = ((short_discriminator & 0x3) << 14) | (passcode & 0x3FFF)
    c3 = passcode >> 14
    body = f"{c1}{c2:05d}{c3:04d}"
    return body + str(check_digit(body))


def short_discriminator_of(discriminator: int) -> int:
    """The 4-bit short discriminator advertised in a 12-bit discriminator.

    mDNS TXT advertises the FULL 12-bit value in `D=` (e.g. D=2048); the manual
    code only carries its top 4 bits (2048 >> 8 == 8).
    """
    return (discriminator >> 8) & 0xF


def repair(code: str, advertised_discriminator: int) -> str:
    """Re-encode a code so it targets the discriminator a device is ACTUALLY
    advertising, keeping its (unchanged) passcode.

    THE CASE THIS SOLVES: a device is sitting in pairing mode and answering
    mDNS, but commissioning fails with "no commissionable device was
    discovered" — because the label's short discriminator no longer matches what
    the device advertises (typically after an interrupted pairing or a reboot).
    The passcode is still correct; only the locator is stale.

    Returns the label code UNCHANGED when the discriminators already agree, so
    callers can invoke this unconditionally.
    """
    decoded = decode(code)
    target = short_discriminator_of(advertised_discriminator)
    if decoded.short_discriminator == target:
        return normalize(code)
    return encode(target, decoded.passcode)
