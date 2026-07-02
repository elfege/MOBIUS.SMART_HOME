"""
Rules — declarative rule schema (Phase 1 of the agentic-rule-authoring pivot).

See ``docs/plans/agentic_rule_authoring_via_user_local_claude_code_cli_and_mobius_mcp_server.md``
for the full strategic context. Short version: the Rules app used to be a
case dispatcher (one hand-coded ``case`` per automation type), and the
operator's design call is to make it a small interpreter over a declarative
schema instead — so the LLM agent's job becomes "natural language → validated
schema instance" rather than "natural language → generated code." Execution
stays in trusted Python (the interpreter); the schema is the safety bound.

What lives in this module
=========================
Pydantic models for the schema. NOT the interpreter (see ``interpreter.py``)
and NOT the app shell (see ``app.py``). This file is pure data definitions
so they can be imported by the MCP tools (Phase 2) and by validation in the
instance-create / instance-update routes.

Schema shape
============
A rule spec is a list of ``Rule`` objects. Each rule has a ``trigger``
(device-category + event-type + optional value filter) and a sequence of
``actions``. The first rule whose trigger matches an incoming event fires
that rule's action sequence. Subsequent rules in the list are independent —
this is NOT first-match-wins across the list; every rule whose trigger
matches its predicate is evaluated.

Action primitives
=================
Three primitives. Validated on rule-spec load via Pydantic discriminator on
the ``kind`` field. Add a new primitive ONLY by extending this discriminated
union and the matching dispatch in the interpreter — never by emitting
freeform code from the agent.

``set_state``
    Drive a device-category group to a fixed switch state (``"on"`` or
    ``"off"``). Used for definite-state actions like the pool button's
    "hold → everything off."

``toggle_uniform``
    Toggle a device-category group to the SAME target state, computed from
    the group as a whole: if every device is currently ``on`` → target
    ``off``; otherwise → target ``on``. Used for the pool button's
    "single-tap pool-water switches" — enforces the no-asymmetry invariant
    even when the group started mismatched.

``toggle_independent``
    Flip each device individually: each device that is currently ``on`` goes
    ``off``, each that is currently ``off`` goes ``on``. For single-device
    categories this collapses to a plain toggle. Used for the pool button's
    "double-tap pump switch."

Notes on intentional NON-features
=================================
- No raw command strings. ``set_state`` is constrained to ``on``/``off``;
  richer command vocabulary (dim/colorTemperature/...) will go through new
  named primitives with their own validation, NOT through a free-form
  ``command`` field. Same reason as no-code-generation: keep the safety
  bound tight.
- No ``condition`` field on the trigger yet (Phase 1 scope). Conditions
  (mode / time-of-day / device-state-gates) will arrive in a follow-on
  phase as a separate ``conditions`` list on the ``Rule`` object, again
  as discriminated primitives.
- No cross-rule sequencing primitives ("wait 5s then do X"). Time-delayed
  follow-ups are a deliberate gap until scheduling is added — the existing
  pool_button case does not need them.
"""

from __future__ import annotations

from typing import Annotated, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

# ============================================================================
# Trigger
# ============================================================================


class Trigger(BaseModel):
    """
    The pattern an incoming event must match for the rule to fire.

    Attributes
    ----------
    device_category :
        Name of the device-category slot on the parent app (e.g.
        ``"trigger_button"``). Resolved at runtime via
        ``BaseApp.get_devices(category)`` to the actual canonical-device-id
        list — the event's ``device_id`` must be in that list.
    event_type :
        The event attribute name (e.g. ``"pushed"``, ``"held"``,
        ``"doubleTapped"``, ``"motion"``, ``"switch"``). Compared as-is to
        ``DeviceEvent.event_type``.
    value :
        Optional event-value filter. When set, ``str(event.value).strip()``
        must equal ``str(value).strip()``. Used to filter by button number
        (e.g. ``"1"``) or motion direction (``"active"``).
    """

    model_config = ConfigDict(extra="forbid")

    device_category: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    value: Optional[str] = None


# ============================================================================
# Action primitives (discriminated union on ``kind``)
# ============================================================================


class ActionSetState(BaseModel):
    """Drive every device in ``device_category`` to ``target`` (on / off)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["set_state"]
    device_category: str = Field(..., min_length=1)
    target: Literal["on", "off"]


class ActionToggleUniform(BaseModel):
    """
    Toggle a device-category group to the SAME target, computed group-wise.

    If every device reads ``on`` → target = ``off``. Otherwise → target =
    ``on``. Enforces the no-asymmetry invariant even when the group started
    in a mismatched state.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["toggle_uniform"]
    device_category: str = Field(..., min_length=1)


class ActionToggleIndependent(BaseModel):
    """
    Flip each device individually (on → off, off → on, per device).

    For a single-device category this is a plain toggle.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["toggle_independent"]
    device_category: str = Field(..., min_length=1)


class ActionSetMode(BaseModel):
    """Set the Hubitat LOCATION mode (not a device command).

    Used by mode-automation rules — e.g. a TV turning on → ``set_mode`` to
    "WatchingTV" (replaces the disabled HE Mode Manager / Rule-Machine apps).
    The mode is given by NAME; the interpreter resolves it to the hub's mode id.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["set_mode"]
    mode: str = Field(..., min_length=1, description="Location mode name, e.g. 'WatchingTV'.")


Action = Annotated[
    Union[ActionSetState, ActionToggleUniform, ActionToggleIndependent, ActionSetMode],
    Field(discriminator="kind"),
]


# ============================================================================
# Rule + RuleSpec
# ============================================================================


class Rule(BaseModel):
    """
    One trigger → action-sequence pair.

    Multiple rules in a spec are evaluated independently against each
    incoming event; every rule whose trigger matches its predicate fires
    its full action sequence in declared order.
    """

    model_config = ConfigDict(extra="forbid")

    when: Trigger
    do: List[Action] = Field(..., min_length=1)
    description: Optional[str] = Field(
        default=None,
        description=(
            "Human-readable summary, surfaced in the dashboard. Optional. "
            "When the agent (Phase 2+) authors a rule it should record the "
            "originating natural-language prompt here for audit."
        ),
    )


class RuleSpec(BaseModel):
    """
    The full declarative rule program for one Rules instance.

    Attributes
    ----------
    rules :
        Ordered list of independent rules. Evaluated all-at-once on each
        incoming event (NOT first-match-wins across the list).
    debounce_per_event :
        Mapping ``event_type → seconds`` used by the interpreter to collapse
        duplicate / retransmitted events of the same type into one action.
        Default 0 = no debounce.
    """

    model_config = ConfigDict(extra="forbid")

    rules: List[Rule] = Field(default_factory=list)
    debounce_per_event: Dict[str, int] = Field(default_factory=dict)


# ============================================================================
# Built-in presets (proves the schema is expressive enough for pool_button)
# ============================================================================


def pool_button_preset(
    button_number: str = "1",
    debounce_seconds: int = 3,
) -> RuleSpec:
    """
    Return the pool_button case re-expressed as a declarative RuleSpec.

    This is the Phase 1 validation: every behaviour the hand-coded
    ``pool_button`` case shipped with (single-tap → toggle-uniform pool
    water, double-tap → toggle-independent pump, hold → set-state-off
    everything, button-number filter, per-event debounce) is expressible
    in pure data.

    Returned spec uses the legacy device-category names
    (``trigger_button`` / ``pool_water_switches`` / ``pump_switch``) so an
    existing pool_button instance's device pickers keep working unchanged.

    Parameters
    ----------
    button_number :
        Event-value filter applied to all three triggers. Defaults to
        ``"1"`` (the physical single button — phantom-button-2 events
        fabricated by post-2.5.0.x firmware are dropped by mismatch).
    debounce_seconds :
        Per-event-type debounce window. Applied uniformly to ``pushed`` /
        ``held`` / ``doubleTapped``.
    """
    return RuleSpec(
        rules=[
            Rule(
                when=Trigger(
                    device_category="trigger_button",
                    event_type="pushed",
                    value=button_number,
                ),
                do=[
                    ActionToggleUniform(
                        kind="toggle_uniform",
                        device_category="pool_water_switches",
                    ),
                ],
                description="single tap → toggle pool-water switches together",
            ),
            Rule(
                when=Trigger(
                    device_category="trigger_button",
                    event_type="doubleTapped",
                    value=button_number,
                ),
                do=[
                    ActionToggleIndependent(
                        kind="toggle_independent",
                        device_category="pump_switch",
                    ),
                ],
                description="double tap → toggle pump switch",
            ),
            Rule(
                when=Trigger(
                    device_category="trigger_button",
                    event_type="held",
                    value=button_number,
                ),
                do=[
                    ActionSetState(
                        kind="set_state",
                        device_category="pool_water_switches",
                        target="off",
                    ),
                    ActionSetState(
                        kind="set_state",
                        device_category="pump_switch",
                        target="off",
                    ),
                ],
                description="hold → everything off",
            ),
        ],
        debounce_per_event={
            "pushed": debounce_seconds,
            "doubleTapped": debounce_seconds,
            "held": debounce_seconds,
        },
    )
