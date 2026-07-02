"""
Rules
=====

Declarative rule interpreter (Phase 1 of the agentic-rule-authoring pivot,
operator directive 2026-05-22 — supersedes the original 2026-06-19 case-only
design). The rule logic lives in the instance's ``rule_spec`` setting (a
JSONB blob conforming to ``apps.rules.schema.RuleSpec``) and the dispatch
lives in ``apps.rules.interpreter``. This module is the BaseApp shell:
lifecycle, settings/device-category contracts, and event-loop routing.

See ``docs/plans/agentic_rule_authoring_via_user_local_claude_code_cli_and_mobius_mcp_server.md``
for the full strategic context; ``apps/rules/schema.py`` for the schema
contract; ``apps/rules/interpreter.py`` for the executor.

Migration / backward compatibility
==================================
Instances created before this commit may not carry a ``rule_spec`` setting
at all — they instead carry the legacy ``case`` enum (only value
``"pool_button"``). For those instances, ``_resolve_spec()`` synthesises a
``RuleSpec`` on the fly from the legacy preset
(``schema.pool_button_preset``) using the existing
``triggerButtonNumber`` / ``debounceSeconds`` settings, so behaviour is
identical without requiring a settings migration. New instances should set
``rule_spec`` directly.

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
from typing import Any, Dict, List

from apps.base_app import BaseApp
from apps.base.pause_settings import UNIVERSAL_PAUSE_SETTINGS
from apps.rules import interpreter as _rule_interpreter
from apps.rules.schema import RuleSpec, pool_button_preset
from models.event import DeviceEvent
from pydantic import ValidationError

logger = logging.getLogger(__name__)


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
        """
        Return the JSON Schema for a Rules instance.

        The authoritative behaviour-defining field is ``rule_spec`` — a
        JSON object conforming to ``apps.rules.schema.RuleSpec``. Two paths:

          - **New instances** set ``rule_spec`` directly (usually authored
            by the Phase 2 MCP ``create_rule`` tool from a natural-language
            prompt; for now it can also be hand-edited).
          - **Legacy pool_button instances** that predate Phase 1 still
            carry only ``case`` / ``triggerButtonNumber`` / ``debounceSeconds``;
            ``_resolve_spec()`` reconstructs an equivalent ``RuleSpec`` for
            them on the fly, so they keep working without a settings
            migration.

        The ``case`` enum is kept as a legacy-only field — its only valid
        value remains ``"pool_button"`` (the synthesis path's preset key).
        New rule shapes should NOT add cases; they should ship as full
        ``rule_spec`` blobs.
        """
        return {
            "type": "object",
            "properties": {
                "rule_spec": {
                    "type": ["object", "null"],
                    "title": "Rule Spec",
                    "description": (
                        "Declarative rule program. JSON object conforming "
                        "to apps.rules.schema.RuleSpec — has a 'rules' list "
                        "of trigger→action pairs and an optional "
                        "'debounce_per_event' map. When null/missing, the "
                        "app falls back to synthesising the pool_button "
                        "preset from the legacy 'case' / 'triggerButtonNumber' "
                        "/ 'debounceSeconds' settings."
                    ),
                    "default": None,
                },
                "case": {
                    "type": "string",
                    "title": "Rule Case (legacy)",
                    "description": (
                        "LEGACY: kept for instances created before Phase 1. "
                        "Only 'pool_button' is recognised; new rule shapes "
                        "must use 'rule_spec' instead. When 'rule_spec' is "
                        "set, this field is ignored."
                    ),
                    "enum": ["pool_button"],
                    "default": "pool_button",
                },
                "triggerButtonNumber": {
                    "type": "string",
                    "title": "Trigger Button Number (legacy pool_button)",
                    "description": (
                        "LEGACY: feeds the pool_button synthesis path. "
                        "Default '1' — the physical single button. Ignored "
                        "when 'rule_spec' is set (the per-trigger 'value' "
                        "field in the spec controls filtering directly)."
                    ),
                    "default": "1",
                },
                "debounceSeconds": {
                    "type": "integer",
                    "title": "Debounce seconds (legacy pool_button)",
                    "description": (
                        "LEGACY: feeds the pool_button synthesis path. "
                        "When 'rule_spec' is set, the spec's "
                        "'debounce_per_event' map controls debouncing "
                        "directly and this field is ignored."
                    ),
                    "minimum": 0,
                    "default": 3,
                },
                # Universal pause contract (pauseDuration / pauseDurationUnit /
                # resumeOnModeChange). Applies regardless of rule_spec vs
                # legacy path — the interpreter and the legacy methods both
                # honour ``self.is_paused``.
                **UNIVERSAL_PAUSE_SETTINGS,
            },
        }

    @classmethod
    def get_device_categories(cls) -> List[Dict[str, Any]]:
        """Device pickers for the Rules wizard.

        The Rules app is multi-shape (pool_button, tv_mode, …): a given rule_spec
        references only SOME of these categories, so none is universally required.
        Each is ``required: False`` and the active rule_spec defines what an
        instance actually uses — pick the categories your rule references.
        """
        return [
            {
                "key": "trigger_button",
                "label": "Trigger Button (pool_button rule)",
                "capability": "pushableButton",
                "multiple": False,
                "required": False,
                "description": (
                    "The button whose pushed / doubleTapped / held events "
                    "drive a pool_button rule."
                ),
            },
            {
                "key": "pool_water_switches",
                "label": "Pool Water Switches (toggled together)",
                "capability": "switch",
                "multiple": True,
                "required": False,
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
                "required": False,
                "description": (
                    "Double tap toggles this switch (e.g. Swimming Pool "
                    "small pump)."
                ),
            },
            {
                "key": "tv",
                "label": "TV (tv_mode rule)",
                "capability": "switch",
                "multiple": False,
                "required": False,
                "description": (
                    "The TV device/driver whose on/off drives the location mode "
                    "(e.g. on → WatchingTV). Replaces the disabled HE Mode "
                    "Manager / Rule-Machine 'TV mode manager' apps."
                ),
            },
        ]

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def initialize(self) -> None:
        """
        Set up the interpreter's debounce-state dict and resolve the
        instance's rule spec exactly once at startup. No device actions.

        The resolved ``RuleSpec`` is cached on ``self._runtime.rule_spec``
        for the lifetime of the instance — if the operator edits the
        instance settings the app is re-initialised by the framework, so
        this stays consistent without a per-event re-parse cost.
        """
        spec = self._resolve_spec()
        self._runtime.rule_spec = spec
        self._runtime.debounce_state: Dict[str, float] = {}
        self.logger.info(
            f"Initializing: {self.label} "
            f"({len(spec.rules)} rule{'s' if len(spec.rules) != 1 else ''} "
            f"loaded)"
        )

        # Warn only if NO trigger device is wired for ANY rule (then the
        # instance genuinely can't fire). Spec-aware, so a tv_mode rule that
        # triggers on 'tv' is not falsely reported idle over a missing
        # 'trigger_button' (that check was pool_button-specific).
        trigger_cats = {r.when.device_category for r in spec.rules}
        if not any(self.get_devices(c) for c in trigger_cats):
            self.logger.warning(
                f"no trigger devices selected for {sorted(trigger_cats)} — instance is idle"
            )

    # =========================================================================
    # Spec resolution — new ``rule_spec`` field, with legacy ``case`` fallback
    # =========================================================================

    def _resolve_spec(self) -> RuleSpec:
        """
        Return the ``RuleSpec`` this instance should run.

        Priority:
            1. ``rule_spec`` setting present and parseable → use it.
            2. Otherwise synthesise from the legacy ``case`` /
               ``triggerButtonNumber`` / ``debounceSeconds`` settings via
               ``schema.pool_button_preset``.

        On a malformed ``rule_spec``, log the validation error and fall
        through to the legacy synthesis path — never raise from
        initialize(), since that would dead-instance the app.
        """
        raw = self.get_setting('rule_spec', None)
        if raw:
            try:
                return RuleSpec.model_validate(raw)
            except ValidationError as e:
                self.logger.error(
                    f"rule_spec is invalid — falling back to legacy "
                    f"pool_button synthesis. Errors: {e.errors()}"
                )

        button_number = str(self.get_setting('triggerButtonNumber', '1')).strip() or '1'
        debounce = int(self.get_setting('debounceSeconds', 3) or 3)
        return pool_button_preset(
            button_number=button_number,
            debounce_seconds=debounce,
        )

    # =========================================================================
    # Event dispatch — pure delegation to the interpreter
    # =========================================================================

    def on_event(self, event: DeviceEvent) -> None:
        """
        Hand the event off to the interpreter. All guards (debounce,
        pause, trigger match) live there now; this method exists only to
        bridge the BaseApp ``on_event`` contract and wrap the call in a
        per-instance exception boundary.
        """
        try:
            _rule_interpreter.execute_event(
                host=self,
                spec=self._runtime.rule_spec,
                event=event,
                debounce_state=self._runtime.debounce_state,
            )
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
