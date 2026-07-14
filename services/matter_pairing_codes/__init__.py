"""
matter_pairing_codes — "Get Code" for any Matter device: retrieve, generate, or
honestly explain why neither is possible.

Operator request 2026-07-13, after a plug refused to commission because its
advertised discriminator had drifted from its printed label and the code had to
be re-derived by hand.

Modules (one responsibility each):
    manual_code.py — the 11-digit manual-code codec (Verhoeff, decode/encode/repair)
    sources.py     — the four places a working code can come from
    resolver.py    — ONE "Get Code" action: tries all four, in the best order
    router.py      — the HTTP surface (/api/matter/pairing-code*)

THE ONE THING A READER MUST UNDERSTAND: a Matter passcode is a SPAKE2+ secret.
It cannot be read out of a device, derived from its mDNS advertisement, or
generated. A code therefore exists only if (a) we administer the device and can
command it to open a window, (b) we captured its factory code in the vault, or
(c) the operator has its label in hand. There is no fourth way, and this package
never pretends otherwise — see resolver.resolve's UnreachableCode.

This __init__ imports nothing, so circular imports are structurally impossible.
"""
