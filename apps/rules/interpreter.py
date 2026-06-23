"""
Rules — declarative-schema interpreter.

This module is the **single trusted code path** that executes any
schema-valid ``RuleSpec`` produced by:

  - the legacy preset ``pool_button_preset()`` (Phase 1 backfill), or
  - an agent-authored rule POSTed through the future MCP ``create_rule``
    tool (Phase 2+).

The interpreter takes a ``RuleSpec`` and a ``DeviceEvent`` and, for each
``Rule`` whose ``Trigger`` matches the event predicate, executes that rule's
action sequence. It never generates or evaluates code — every action is
dispatched through a fixed ``ACTION_DISPATCH`` table keyed on the Pydantic
discriminator's ``kind`` field. Adding a new action primitive is a code
change in this file (and a matching Pydantic model in ``schema.py``), never
something the agent does on its own.

Why all the dispatch sits here
==============================
The plan's safety bound is: the *only* surface the agent can drive is
producing schema data. Execution is in a trusted file (this one) that the
agent does not modify. If you find yourself tempted to expose a "raw
command" or "Python snippet" primitive, stop — that's the line that
collapses the whole design back to arbitrary code execution.

Pause + debounce semantics
==========================
The interpreter is host-app-agnostic but expects its host to provide:

  - ``host.is_paused``                  → bool (universal pause contract)
  - ``host.update_last_activity()``     → record event arrival for the dashboard
  - ``host.get_devices(category)``      → list[canonical_id] from the slot pickers
  - ``host.get_device_state(id)``       → cached state dict (for toggle decisions)
  - ``host.send_command(id, cmd, ...)`` → driver-routed command + optional verify
  - ``host.logger``                     → per-instance logger

The interpreter's per-event-type debounce table lives on the host so it
survives across events for the lifetime of the instance — see
``RulesApp.initialize`` for where it gets seeded.

Pause-guard rule
================
Per the universal pause-guard rule (``feedback_pause_guard_on_every_action_method``
in instance memory), every action method on a Rules instance must re-check
``self.is_paused`` at its own top — transitive-only guards are NOT
sufficient. The interpreter checks pause once at ``execute_event`` entry,
AND each action-primitive executor re-checks before sending commands. Belt
and braces: a pause that takes effect mid-rule-fire still stops the next
action.
"""

from __future__ import annotations

import time as _monotonic_time
from typing import Any, Dict, List, Protocol

from apps.rules.schema import (
    Action,
    ActionSetState,
    ActionToggleIndependent,
    ActionToggleUniform,
    Rule,
    RuleSpec,
    Trigger,
)

# Colour shortcuts matching the rest of the apps' logging style.
_C = "\033[96m"
_Y = "\033[93m"
_G = "\033[92m"
_R_RED = "\033[91m"
_R = "\033[0m"


# ============================================================================
# Host protocol — what the interpreter assumes about its parent app
# ============================================================================


class RuleHost(Protocol):
    """
    Methods the interpreter calls on its host (a ``BaseApp`` subclass).

    Kept as a Protocol rather than a concrete class so the interpreter is
    test-friendly: a unit test can pass a stub object that records the
    sequence of ``send_command`` calls without spinning up a real instance.
    """

    @property
    def is_paused(self) -> bool: ...

    @property
    def logger(self) -> Any: ...

    def update_last_activity(self) -> None: ...

    def get_devices(self, category: str) -> List[Any]: ...

    def get_device_state(self, device_id: Any) -> Dict[str, Any] | None: ...

    def send_command(self, device_id: Any, command: str, **kwargs) -> Any: ...


# ============================================================================
# Public entry point
# ============================================================================


def execute_event(
    host: RuleHost,
    spec: RuleSpec,
    event: Any,
    debounce_state: Dict[str, float],
) -> None:
    """
    Run every rule in ``spec`` against ``event``; fire actions for matches.

    Guards applied in order:
        1. event-type debounce          (per-event-type window from spec)
        2. host pause                    (universal pause-guard rule)
        3. per-rule trigger match        (device-category + event-type + value)

    The trigger-match check is intentionally LAST among these so that a
    debounced or paused instance does not even hit the rule loop —
    the cheap guards short-circuit before we touch device pickers.

    ``debounce_state`` is a mutable dict owned by the host (typically on the
    runtime cache). Keyed by ``event.event_type`` → monotonic timestamp of
    the last accepted action of that type.
    """
    host.update_last_activity()

    event_type = getattr(event, "event_type", None)
    if not event_type:
        return

    # ----- 1) debounce ------------------------------------------------------
    window = float(spec.debounce_per_event.get(event_type, 0) or 0)
    if window > 0:
        now = _monotonic_time.monotonic()
        last = float(debounce_state.get(event_type, 0.0))
        if (now - last) < window:
            host.logger.debug(
                f"debounced duplicate {event_type} within {window}s"
            )
            return
        debounce_state[event_type] = now

    # ----- 2) host pause ----------------------------------------------------
    if host.is_paused:
        host.logger.info(
            f"paused — ignoring {event_type} (resume to re-enable)"
        )
        return

    # ----- 3) match + fire each independent rule ---------------------------
    event_device_id = getattr(event, "device_id", None)
    event_value = getattr(event, "value", None)

    fired_any = False
    for rule in spec.rules:
        if not _trigger_matches(host, rule.when, event_type, event_device_id, event_value):
            continue
        fired_any = True
        host.logger.info(
            f"{_Y}rule fired: {rule.description or rule.when.event_type}{_R}"
        )
        for action in rule.do:
            if host.is_paused:   # belt + braces — pause taking effect mid-rule
                host.logger.info("paused mid-rule — aborting remaining actions")
                return
            _dispatch_action(host, action)

    if not fired_any:
        host.logger.debug(f"no rule matched {event_type} on device={event_device_id}")


# ============================================================================
# Trigger predicate
# ============================================================================


def _trigger_matches(
    host: RuleHost,
    trigger: Trigger,
    event_type: str,
    event_device_id: Any,
    event_value: Any,
) -> bool:
    """
    Return True iff this event's (event_type, device_id, value) tuple
    matches the trigger's (event_type, device_category, value) tuple.

    Device-category match: the event's device must be in the category's
    canonical-id list as resolved by ``host.get_devices(category)``. If the
    category is empty (no devices configured), the trigger cannot match
    and we return False silently — an idle instance.
    """
    if trigger.event_type != event_type:
        return False

    category_ids = host.get_devices(trigger.device_category) or []
    if not category_ids:
        return False
    # Canonical ids are usually ints; the event's device_id may be int or str.
    # Compare as strings to be tolerant.
    cid_strs = {str(c) for c in category_ids}
    if str(event_device_id) not in cid_strs:
        return False

    if trigger.value is not None:
        if str(event_value).strip() != str(trigger.value).strip():
            return False

    return True


# ============================================================================
# Action dispatch
# ============================================================================


def _dispatch_action(host: RuleHost, action: Action) -> None:
    """
    Route ``action`` to the executor for its ``kind``. The dispatch table is
    closed — adding a new primitive requires editing this file AND
    ``schema.py``.
    """
    if isinstance(action, ActionSetState):
        _exec_set_state(host, action)
    elif isinstance(action, ActionToggleUniform):
        _exec_toggle_uniform(host, action)
    elif isinstance(action, ActionToggleIndependent):
        _exec_toggle_independent(host, action)
    else:
        host.logger.error(
            f"unknown action kind {type(action).__name__} — schema/dispatch out of sync"
        )


def _exec_set_state(host: RuleHost, action: ActionSetState) -> None:
    """Drive the category group to ``action.target``."""
    if host.is_paused:
        return
    ids = host.get_devices(action.device_category) or []
    if not ids:
        host.logger.warning(
            f"set_state: no devices in category {action.device_category!r}"
        )
        return
    host.logger.info(
        f"{_Y}set_state {action.device_category} → {action.target}{_R} "
        f"({len(ids)} device{'s' if len(ids) != 1 else ''})"
    )
    _send_to_all(host, ids, action.target)


def _exec_toggle_uniform(host: RuleHost, action: ActionToggleUniform) -> None:
    """
    Group toggle: target = ``off`` iff every device currently reads ``on``;
    else target = ``on``. Drives every device to that single target.
    """
    if host.is_paused:
        return
    ids = host.get_devices(action.device_category) or []
    if not ids:
        host.logger.warning(
            f"toggle_uniform: no devices in category {action.device_category!r}"
        )
        return
    all_on = all(_switch_is_on(host, d) for d in ids)
    target = "off" if all_on else "on"
    host.logger.info(
        f"{_Y}toggle_uniform {action.device_category} → {target}{_R} "
        f"(was {'all on' if all_on else 'mixed/off'}, "
        f"{len(ids)} device{'s' if len(ids) != 1 else ''})"
    )
    _send_to_all(host, ids, target)


def _exec_toggle_independent(host: RuleHost, action: ActionToggleIndependent) -> None:
    """Flip each device individually: ``on`` → ``off``, else → ``on``."""
    if host.is_paused:
        return
    ids = host.get_devices(action.device_category) or []
    if not ids:
        host.logger.warning(
            f"toggle_independent: no devices in category {action.device_category!r}"
        )
        return
    for cid in ids:
        if host.is_paused:    # belt + braces inside per-device loop
            return
        target = "off" if _switch_is_on(host, cid) else "on"
        host.logger.info(f"{_Y}toggle_independent {cid} → {target}{_R}")
        _send_to_all(host, [cid], target)


# ============================================================================
# Switch helpers (intentionally narrow — keep the interpreter's reach small)
# ============================================================================


def _switch_is_on(host: RuleHost, canonical_id: Any) -> bool:
    """
    Read a switch's cached attribute. Returns True only on an explicit
    ``"on"``; unknown / missing / off all read as False (so the
    ``toggle_uniform`` "are they all on?" test fails safe toward turning
    things ON, matching the legacy pool_button case's behaviour).
    """
    device = host.get_device_state(canonical_id)
    if not device:
        return False
    return (device.get("attributes", {}) or {}).get("switch") == "on"


def _send_to_all(host: RuleHost, canonical_ids: List, target: str) -> None:
    """
    Send ``target`` (``"on"``/``"off"``) to every id; log per-device
    success/failure; one bad device does NOT abort the rest.
    """
    for cid in canonical_ids:
        try:
            result = host.send_command(cid, target, verify=True)
            if getattr(result, "success", False) and getattr(result, "verified", False):
                host.logger.info(f"  {_G}{cid} → {target}{_R}")
            elif getattr(result, "success", False):
                host.logger.warning(
                    f"  {cid} → {target} sent but NOT verified "
                    f"(actual={getattr(result, 'actual_state', None)})"
                )
            else:
                host.logger.warning(
                    f"  {_R_RED}{cid} → {target} FAILED: "
                    f"{getattr(result, 'error', None)}{_R}"
                )
        except Exception as e:
            host.logger.error(f"  {cid} → {target} raised: {e}", exc_info=True)
