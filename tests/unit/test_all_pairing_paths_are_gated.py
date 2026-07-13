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
    }


def _enclosing_function(src: str, index: int) -> str:
    """Name of the def/async def that lexically encloses `index`."""
    head = src[:index]
    matches = re.findall(r"^\s*(?:async\s+)?def\s+(\w+)", head, re.MULTILINE)
    return matches[-1] if matches else "<module>"


class TestEveryPairingPathIsGated:
    # Functions that legitimately call commission_with_code WITHOUT taking the
    # lock themselves, because a caller provably holds it for them. Each entry is
    # a promise that must stay true — the companion test below enforces it.
    HOLDS_LOCK_VIA_CALLER = {
        "_do_commission_with_code",      # matter_commission holds it
        "_auto_commission_device",       # endpoint OR bulk worker holds it
        "_commission_devices_locked",    # _commission_devices holds it
    }

    def test_every_commission_with_code_caller_is_under_the_mutex(self):
        """No pairing call may exist outside a function that either takes the
        mutex itself or is explicitly delegated-to by one that does."""
        offenders = []
        for name, src in _sources().items():
            for m in re.finditer(r"\bcommission_with_code\(", src):
                fn = _enclosing_function(src, m.start())
                if fn.startswith("commission_with_code"):
                    continue  # the definition itself, in the matter client
                takes_lock = "matter_pairing_lock(" in src[max(0, m.start() - 4000):m.start()]
                if fn in self.HOLDS_LOCK_VIA_CALLER or takes_lock:
                    continue
                offenders.append(f"{name}:{fn}")
        assert not offenders, (
            "UNGATED Matter pairing path(s) — every caller of commission_with_code "
            "must hold matter_pairing_lock (a radio pairs ONE device at a time): "
            f"{offenders}"
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
