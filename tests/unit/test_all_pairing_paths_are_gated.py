"""
STRUCTURAL INVARIANT: every Matter pairing path holds the global mutex.

Operator directive, 2026-07-13: "'two pairing storms on one radio' must be
strictly gated."

A Matter radio completes ONE pairing handshake at a time. The constraint is
FLEET-WIDE — it does not matter which feature initiates it (manual commission,
single auto-commission, Commission All, hub->hub copy, or a background
auto-commissioner). Every one of them must hold `matter_pairing_lock`, and a
second attempt must be REFUSED (409), never queued onto the same radio.

This is a source-level test on purpose. The earlier per-feature guards were
correct in isolation and still left holes:
  - the single auto-commission endpoint was left UNLOCKED as a workaround to
    avoid deadlocking the bulk worker (which calls it while already holding the
    mutex) — so a double-click on a device card stormed the radio;
  - MatterDiscoveryService._commission_devices called commission_with_code with
    no lock at all — dead code today, a bypass the moment anyone wires it up.
Both were invisible to any behavioural test that only exercised the happy path.
So we assert the STRUCTURE: find every caller of commission_with_code, and prove
each one sits under the mutex.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _sources():
    return {
        "app.py": (REPO / "app.py").read_text(),
        "matter_discovery.py": (REPO / "services/matter_discovery.py").read_text(),
        "matter_hub_port/orchestrator.py": (
            REPO / "services/matter_hub_port/orchestrator.py"
        ).read_text(),
        "matter_hub_port/hub_endpoints.py": (
            REPO / "services/matter_hub_port/hub_endpoints.py"
        ).read_text(),
        "matter_pairing_codes/sources.py": (
            REPO / "services/matter_pairing_codes/sources.py"
        ).read_text(),
    }


# Every idiom that acquires the global Matter-pairing mutex. The get-code sources
# use a custom hold-helper (the window outlives the call, so a context manager
# would release too early), so recognising only `matter_pairing_lock(` would
# miss them — MSG-1025's exact point.
# Note the trailing '(' on each: we require a CALL to a gating primitive within
# the enclosing function's body, not merely the token's presence (a def line or a
# docstring mention must not count as "gated").
GATING_TOKENS = (
    "matter_pairing_lock(",
    "_hold_pairing_lock_for_window(",
    "_try_acquire_sync(",
)


def _gated_in_enclosing_function(src: str, index: int) -> bool:
    """True iff a gating primitive is CALLED in the enclosing function's body,
    before `index`. Bounded to [enclosing-def-start, index] so it cannot be
    fooled by a gate call in a sibling function or the helper's own definition."""
    _, body_start = _enclosing_def(src, index)
    body_prefix = src[body_start:index]
    return any(tok in body_prefix for tok in GATING_TOKENS)


def _enclosing_def(src: str, index: int):
    """(name, body_start_index) of the def/async def enclosing `index`, or
    ('<module>', 0). body_start_index is the offset of that def line, so callers
    can bound a search to THIS function's body — never bleeding into a sibling
    function, a helper's definition, or the module header."""
    last = None
    for m in re.finditer(r"^[ \t]*(?:async\s+)?def\s+(\w+)", src[:index], re.MULTILINE):
        last = m
    if last is None:
        return "<module>", 0
    return last.group(1), last.start()


def _enclosing_function(src: str, index: int) -> str:
    return _enclosing_def(src, index)[0]


class TestEveryPairingPathIsGated:
    # Functions that legitimately touch a pairing primitive WITHOUT taking the
    # lock themselves, because a caller provably holds it for them. Each entry is
    # a promise that must stay true — the companion test below enforces it.
    HOLDS_LOCK_VIA_CALLER = {
        "_do_commission_with_code",      # matter_commission holds it
        "_auto_commission_device",       # endpoint OR bulk worker holds it
        "_commission_devices_locked",    # _commission_devices holds it
        "_open_hubitat_window",          # from_hubitat holds it
        "open_pairing_window",           # hub-port orchestrator holds it
    }

    # BOTH halves of pairing are gated primitives:
    #   consuming  — commission_with_code(...)         (finishes a pairing)
    #   producing  — opening a commissioning window    (MSG-1025: opening a window
    #                storms a radio exactly as hard as a commission)
    # The producers are HTTP/command strings, not function calls, so match the
    # concrete usage (URL path / quoted command), never a docstring mention.
    PRIMITIVES = (
        r"\bcommission_with_code\(",
        r"/hub/matter/openPairingWindow",
        r'"open_commissioning_window"',
    )

    def test_every_pairing_primitive_is_under_the_mutex(self):
        """No pairing primitive — consuming OR producing — may appear outside a
        function that takes the mutex itself or is a delegate of one that does."""
        offenders = []
        seen_in_function = 0
        for name, src in _sources().items():
            for pat in self.PRIMITIVES:
                for m in re.finditer(pat, src):
                    fn = _enclosing_function(src, m.start())
                    if fn.startswith("commission_with_code"):
                        continue  # the client-side definition itself
                    if fn == "<module>":
                        continue  # a module-header docstring mention, not a call
                    seen_in_function += 1
                    if fn in self.HOLDS_LOCK_VIA_CALLER or _gated_in_enclosing_function(src, m.start()):
                        continue
                    offenders.append(f"{name}:{fn} [{pat}]")
        assert not offenders, (
            "UNGATED Matter pairing path(s) — every consumer (commission_with_code) "
            "AND every producer (opening a commissioning window) must hold the global "
            f"pairing mutex (a radio pairs ONE device at a time): {offenders}"
        )
        # Teeth: the scan must actually be finding in-function primitives, or a
        # refactor could make it vacuously pass. We know there are several.
        assert seen_in_function >= 5, (
            f"only {seen_in_function} in-function pairing primitives found — the "
            "scan patterns have gone stale; the test is no longer protecting anything."
        )

    def test_the_delegated_functions_really_do_have_a_locking_caller(self):
        """Guard against the promise above rotting: each delegated function must
        be invoked from a body that takes the mutex."""
        src = _sources()["app.py"] + _sources()["matter_discovery.py"]
        for fn in ("_do_commission_with_code", "_auto_commission_device",
                   "_commission_devices_locked"):
            # find a call site (not the definition)
            calls = [m.start() for m in re.finditer(rf"\b{fn}\(", src)
                     if not re.match(rf"\s*(async\s+)?def\s+{fn}",
                                     src[src.rfind("\n", 0, m.start()) + 1:m.start() + 40])]
            assert calls, f"{fn} is never called — dead delegate"
            assert any(
                "matter_pairing_lock(" in src[max(0, c - 3000):c] for c in calls
            ), f"{fn} is called WITHOUT the caller holding matter_pairing_lock"

    def test_single_commission_endpoints_take_the_lock(self):
        """The two user-facing single-device endpoints must gate, not just bulk."""
        src = _sources()["app.py"]
        for endpoint in ("matter_commission", "matter_auto_commission"):
            m = re.search(rf"^async def {endpoint}\(", src, re.MULTILINE)
            assert m, f"{endpoint} not found"
            body = src[m.start():m.start() + 3000]
            assert "matter_pairing_lock(" in body, (
                f"{endpoint} does NOT take the global pairing mutex — a "
                f"double-click would storm the radio"
            )
            assert "409" in body, f"{endpoint} must refuse with 409 when busy"
