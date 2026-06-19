"""
Rules
=====

Case-based button / event automations. **Deliberately NOT a generic rule
engine** (operator directive 2026-06-19): each behavior is hand-coded as a
named ``case``. When a new automation is needed, add a case here rather
than building a configurable condition/action DSL. If the number of cases
ever grows past comfort, *that* is the signal to graduate to a real engine.

First shipped case: ``pool_button``
-----------------------------------
A single physical Samsung Zigbee button ("Button Pool") drives three pool
loads. The driver post-firmware-2.5.0.x fabricates phantom *button 2*
events (held + pushed + doubleTapped simultaneously) on every press of the
physically-single button 1 — so this app acts ONLY on the configured
``triggerButtonNumber`` (default "1") and ignores everything else, which
sidesteps the fabrication entirely. A short per-event-type debounce
collapses retransmits (held repeats roughly every 2 s while held).

  - single tap  (``pushed``)       → toggle BOTH pool-water switches
                                     together, enforcing the SAME state
                                     (both on or both off — never one of
                                     each). Target: if both are currently
                                     on → off; otherwise → on (so an
                                     asymmetric or all-off start resolves
                                     to both-on).
  - double tap  (``doubleTapped``) → toggle the small swimming-pool pump.
  - hold        (``held``)         → everything off (both water + pump).

Subscriptions
-------------
Only ``trigger_button`` is an INPUT. It is mapped in
``services/instance_manager.py::_create_subscriptions`` to ALL THREE button
event types (``pushed`` / ``held`` / ``doubleTapped``) via a list value —
the only category in that map that fans out to multiple event types.
``pool_water_switches`` and ``pump_switch`` are pure OUTPUTS and are
deliberately NOT subscribed (subscribing an output re-feeds our own
commands back as events — the 2026-06-05 fan-storm failure mode).

Pause
-----
Honors the universal pause contract (``apps/base/pause_settings.py``). Per
the hard pause-guard rule, every action method checks ``self.is_paused`` at
its own top, and ``on_event`` short-circuits before dispatch when paused —
a paused Rules instance ignores the button until resumed from the
dashboard. (Unlike AML, the button is NOT an unpause mechanism here; it IS
the function, so pausing disables it on purpose.)
"""

from __future__ import annotations

import logging
import time as _monotonic_time
from typing import Any, Dict, List, Optional

from apps.base_app import BaseApp
from apps.base.pause_settings import UNIVERSAL_PAUSE_SETTINGS
from models.event import DeviceEvent

logger = logging.getLogger(__name__)

# Log color shortcuts (mirrors the other apps' style).
_C = "\033[96m"   # cyan — device names
_Y = "\033[93m"   # yellow — decisions
_G = "\033[92m"   # green — on
_R_RED = "\033[91m"
_R = "\033[0m"

# Button event types this app understands. Anything else is ignored.
_BUTTON_EVENTS = ('pushed', 'held', 'doubleTapped')


class RulesApp(BaseApp):
    """
    Case-based button/event automation. See the module docstring for the
    decision flow of the first case (``pool_button``).
    """

    TYPE_NAME    = 'rules'
    DISPLAY_NAME = 'Rules'
    DESCRIPTION  = (
        'Case-based button/event automations (hand-coded per case, not a '
        'generic rule engine). First case: a single pool button toggles '
        'the pool-water switches together (single tap), the small pump '
        '(double tap), and turns everything off (hold).'
    )
    VERSION      = '1.0.0'
    CATEGORY     = 'automation'

    # =========================================================================
    # Settings + device-category schema (drive validation + the wizard)
    # =========================================================================

    @classmethod
    def get_settings_schema(cls) -> Dict[str, Any]:
        """Return the JSON Schema for a Rules instance."""
        return {
            "type": "object",
            "properties": {
                "case": {
                    "type": "string",
                    "title": "Rule Case",
                    "description": (
                        "Which hard-coded behavior this instance runs. Only "
                        "'pool_button' exists today; add cases in app.py."
                    ),
                    "enum": ["pool_button"],
                    "default": "pool_button",
                },
                "triggerButtonNumber": {
                    "type": "string",
                    "title": "Trigger Button Number",
                    "description": (
                        "Only button events carrying THIS button number are "
                        "acted on. Default '1' — the physical single button. "
                        "Phantom 'button 2' events fabricated by the driver "
                        "after firmware 2.5.0.x are ignored."
                    ),
                    "default": "1",
                },
                "debounceSeconds": {
                    "type": "integer",
                    "title": "Debounce (seconds)",
                    "description": (
                        "Per-event-type window that collapses duplicate / "
                        "retransmitted button events (e.g. 'held' repeats "
                        "every ~2 s while held) into a single action."
                    ),
                    "minimum": 0,
                    "default": 3,
                },
                # Universal pause contract (pauseDuration / pauseDurationUnit /
                # resumeOnModeChange). New app → universal default of
                # pauseDuration=0 (indefinite) is correct; no legacy override.
                **UNIVERSAL_PAUSE_SETTINGS,
            },
        }

    @classmethod
    def get_device_categories(cls) -> List[Dict[str, Any]]:
        """Return the device pickers for the Rules wizard (pool_button case)."""
        return [
            {
                "key": "trigger_button",
                "label": "Trigger Button",
                "capability": "pushableButton",
                "multiple": False,
                "required": True,
                "description": (
                    "The button whose pushed / doubleTapped / held events "
                    "drive this rule."
                ),
            },
            {
                "key": "pool_water_switches",
                "label": "Pool Water Switches (toggled together)",
                "capability": "switch",
                "multiple": True,
                "required": True,
                "description": (
                    "Single tap toggles ALL of these to the SAME state "
                    "(e.g. Pool Water Hot + Pool Water Cold)."
                ),
            },
            {
                "key": "pump_switch",
                "label": "Pump Switch (double-tap toggles)",
                "capability": "switch",
                "multiple": False,
                "required": True,
                "description": (
                    "Double tap toggles this switch (e.g. Swimming Pool "
                    "small pump)."
                ),
            },
        ]

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def initialize(self) -> None:
        """Set up debounce state. No device actions at init."""
        self.logger.info(f"Initializing: {self.label} (case={self.get_setting('case', 'pool_button')})")
        # Per-event-type monotonic timestamp of the last accepted action,
        # for the debounce window. 0.0 == never.
        self._runtime.last_action_monotonic: Dict[str, float] = {}

        if not self.get_devices('trigger_button'):
            self.logger.warning("no trigger_button selected — instance is idle")

    # =========================================================================
    # Event dispatch
    # =========================================================================

    def on_event(self, event: DeviceEvent) -> None:
        """
        Dispatch a button event to the case handler.

        Guards, in order: event type → button number → debounce → pause.
        Only after all four pass do we act.
        """
        try:
            self.update_last_activity()

            if event.event_type not in _BUTTON_EVENTS:
                self.logger.debug(f"ignoring non-button event: {event.event_type}")
                return

            # Button-number filter — sidesteps the fabricated phantom-button
            # events (see module docstring).
            want = str(self.get_setting('triggerButtonNumber', '1')).strip()
            got = str(event.value).strip()
            if got != want:
                self.logger.debug(
                    f"ignoring {event.event_type} for button {got!r} "
                    f"(only acting on button {want!r})"
                )
                return

            # Debounce duplicate / retransmitted events of the same type.
            if self._debounced(event.event_type):
                self.logger.debug(
                    f"debounced duplicate {event.event_type} within "
                    f"{self.get_setting('debounceSeconds', 3)}s"
                )
                return

            # Pause guard — paused Rules instance ignores the button.
            if self.is_paused:
                self.logger.info(
                    f"paused — ignoring {event.event_type} (resume to re-enable)"
                )
                return

            self.logger.info(
                f"{_Y}Button {want}: {event.event_type}{_R} → dispatching"
            )
            if event.event_type == 'pushed':
                self._toggle_pool_water()
            elif event.event_type == 'doubleTapped':
                self._toggle_pump()
            elif event.event_type == 'held':
                self._all_off()

        except Exception as e:
            self.logger.error(f"on_event failed: {event}: {e}", exc_info=True)

    def master(self, **kwargs) -> None:
        """
        No-op. Rules is purely event-driven — there is no periodic/timeout
        evaluation. Implemented because BaseApp marks it abstract; the
        framework calls it on resume / mode change, where doing nothing is
        correct for this app.
        """
        self.logger.debug("master() no-op (event-driven app)")

    # =========================================================================
    # Debounce
    # =========================================================================

    def _debounced(self, event_type: str) -> bool:
        """
        Return True if an action of ``event_type`` was already accepted
        within ``debounceSeconds`` (so this one should be dropped). On
        False, records now as the new last-action time for that type.
        """
        window = float(self.get_setting('debounceSeconds', 3) or 0)
        now = _monotonic_time.monotonic()
        last = self._runtime.last_action_monotonic.get(event_type, 0.0)
        if window > 0 and (now - last) < window:
            return True
        self._runtime.last_action_monotonic[event_type] = now
        return False

    # =========================================================================
    # Case: pool_button — actions
    # =========================================================================

    def _toggle_pool_water(self) -> None:
        """
        Toggle every ``pool_water_switches`` device to the SAME target
        state. Target: both-on → off; otherwise → on. Enforces the
        no-asymmetry invariant even if the switches started mismatched.
        """
        if self.is_paused:   # hard pause-guard rule
            return
        ids = self.get_devices('pool_water_switches')
        if not ids:
            self.logger.warning("pool_water: no switches configured")
            return

        all_on = all(self._switch_is_on(d) for d in ids)
        target = 'off' if all_on else 'on'
        self.logger.info(
            f"{_Y}pool water → {target}{_R} (was "
            f"{'all on' if all_on else 'mixed/off'}, "
            f"{len(ids)} switch{'es' if len(ids) != 1 else ''})"
        )
        self._set_switches(ids, target)

    def _toggle_pump(self) -> None:
        """Toggle the single ``pump_switch`` (off→on / on→off)."""
        if self.is_paused:   # hard pause-guard rule
            return
        ids = self.get_devices('pump_switch')
        if not ids:
            self.logger.warning("pump: no switch configured")
            return
        target = 'off' if self._switch_is_on(ids[0]) else 'on'
        self.logger.info(f"{_Y}pump → {target}{_R}")
        self._set_switches(ids[:1], target)

    def _all_off(self) -> None:
        """Turn every pool-water switch AND the pump off."""
        if self.is_paused:   # hard pause-guard rule
            return
        ids = self.get_devices('pool_water_switches') + self.get_devices('pump_switch')
        if not ids:
            self.logger.warning("all-off: no switches configured")
            return
        self.logger.info(f"{_Y}all off{_R} ({len(ids)} switches)")
        self._set_switches(ids, 'off')

    # =========================================================================
    # Switch helpers
    # =========================================================================

    def _switch_is_on(self, canonical_id) -> bool:
        """
        Read a switch's cached state. Returns True only on an explicit
        'on'; unknown / missing / off all read as False (so the toggle's
        "are they all on?" test fails safe toward turning things ON).
        """
        device = self.get_device_state(canonical_id)
        if not device:
            return False
        return (device.get('attributes', {}) or {}).get('switch') == 'on'

    def _set_switches(self, canonical_ids: List, target: str) -> None:
        """
        Send ``target`` ('on'|'off') to each canonical switch id, verified.
        Logs per-device success / failure; one bad switch does not abort
        the rest.
        """
        for cid in canonical_ids:
            try:
                result = self.send_command(cid, target, verify=True)
                if result.success and result.verified:
                    self.logger.info(f"  {_G}{cid} → {target}{_R}")
                elif result.success:
                    self.logger.warning(
                        f"  {cid} → {target} sent but NOT verified "
                        f"(actual={result.actual_state})"
                    )
                else:
                    self.logger.warning(
                        f"  {_R_RED}{cid} → {target} FAILED: {result.error}{_R}"
                    )
            except Exception as e:
                self.logger.error(f"  {cid} → {target} raised: {e}", exc_info=True)
