"""
E2E Test Runner

Defines and executes test scenarios for automation instances.
Each scenario is a sequence of steps that interact with real Hubitat
devices and verify the automation app behaves correctly.

Scenarios are built dynamically from an instance's device_selections
and settings, so they adapt to whatever devices are configured.

Step types:
    command  — Send real command to real device via HubitatClient
    webhook  — Inject synthetic event via internal POST to webhook endpoint
    wait     — Async sleep with 1-second countdown ticks broadcast to UI
    verify   — Poll device state and assert attribute == expected value
    api_call — Call internal API (run, pause, resume, status)
"""

import asyncio
import logging
import time
import traceback
from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class StepResult(Enum):
    """Possible outcomes for a test step."""
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    RUNNING = "running"
    PENDING = "pending"


@dataclass
class TestStep:
    """
    A single step in a test scenario.

    Attributes:
        name: Short step name (shown in UI)
        description: Longer explanation
        action: Step type ('command', 'webhook', 'wait', 'verify', 'api_call')
        params: Action-specific parameters
        result: Outcome after execution
        message: Human-readable result detail
        duration_ms: Execution time in milliseconds
    """
    name: str
    description: str
    action: str
    params: Dict[str, Any] = field(default_factory=dict)
    result: StepResult = StepResult.PENDING
    message: str = ""
    duration_ms: float = 0


@dataclass
class TestScenario:
    """
    A named test scenario with ordered steps.

    Attributes:
        id: Machine-readable identifier (e.g., 'motion_activation')
        name: Human-readable name
        description: What this scenario tests
        steps: Ordered list of test steps
    """
    id: str
    name: str
    description: str
    steps: List[TestStep] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Test Runner
# ---------------------------------------------------------------------------

# Active runners by instance_id. Each E2ETestRunner registers itself in
# __init__ and removes itself in cancel() / on completion. The /stop
# endpoint looks up here to find an in-flight run for a given instance.
_active_runners: Dict[int, "E2ETestRunner"] = {}


def get_active_runner(instance_id: int) -> Optional["E2ETestRunner"]:
    """Return the in-flight runner for an instance, or None if none."""
    return _active_runners.get(instance_id)


class E2ETestRunner:
    """
    Executes test scenarios against a specific app instance.

    Each runner is bound to one instance_id. It reads the instance's
    device_selections and settings to build scenarios appropriate for
    that instance's capabilities.

    Args:
        instance_id: The app instance to test
    """

    def __init__(self, instance_id: int):
        self.instance_id = instance_id
        self._instance: Optional[Dict] = None
        self._scenarios: List[TestScenario] = []
        self._running = False
        self._cancel_flag = False
        # Device state snapshot for save/restore
        self._saved_device_states: Dict[str, Dict[str, str]] = {}
        # Register self in the active-runner table so external callers
        # (the /api/e2e/.../stop endpoint) can find an in-flight run by
        # instance_id and call cancel() on it. Last writer wins; tests
        # are not designed to run multiple times concurrently per
        # instance, and the registry is cleared on completion below.
        _active_runners[instance_id] = self

    async def initialize(self):
        """Load instance data and build test scenarios."""
        from services.instance_manager import get_instance_manager
        manager = get_instance_manager()
        self._instance = manager.get_instance(self.instance_id)
        if not self._instance:
            raise ValueError(f"Instance {self.instance_id} not found")
        self._scenarios = self._build_scenarios()

    def get_scenarios(self) -> List[Dict]:
        """
        Return scenario definitions for the UI.

        Returns:
            List of scenario dicts with id, name, description, steps
        """
        return [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "step_count": len(s.steps),
                "steps": [
                    {"name": st.name, "description": st.description}
                    for st in s.steps
                ]
            }
            for s in self._scenarios
        ]

    async def run_scenario(self, scenario_id: str) -> Dict:
        """
        Run a single scenario by ID.

        Executes steps sequentially, broadcasting progress via SSE.
        Each step's result is reported in real-time.

        Args:
            scenario_id: Scenario identifier

        Returns:
            Summary dict with pass/fail/skip counts and step details
        """
        scenario = next((s for s in self._scenarios if s.id == scenario_id), None)
        if not scenario:
            return {"error": f"Scenario '{scenario_id}' not found"}

        self._running = True
        self._cancel_flag = False

        await self._broadcast("scenario_start", {
            "scenario_id": scenario.id,
            "scenario_name": scenario.name,
            "total_steps": len(scenario.steps)
        })

        results = []
        for idx, step in enumerate(scenario.steps):
            if self._cancel_flag:
                step.result = StepResult.SKIP
                step.message = "Cancelled"
                results.append(self._step_result_dict(step, idx))
                continue

            step.result = StepResult.RUNNING
            await self._broadcast("step_start", {
                "scenario_id": scenario.id,
                "step_index": idx,
                "step_name": step.name,
                "step_description": step.description
            })

            t0 = time.monotonic()
            try:
                await self._execute_step(step)
            except Exception as e:
                step.result = StepResult.FAIL
                step.message = str(e)
                logger.error(f"Step '{step.name}' exception: {e}", exc_info=True)

            step.duration_ms = (time.monotonic() - t0) * 1000

            await self._broadcast("step_complete", {
                "scenario_id": scenario.id,
                "step_index": idx,
                "step_name": step.name,
                "result": step.result.value,
                "message": step.message,
                "duration_ms": round(step.duration_ms, 1)
            })

            results.append(self._step_result_dict(step, idx))

        self._running = False

        passed = sum(1 for r in results if r["result"] == "pass")
        failed = sum(1 for r in results if r["result"] == "fail")
        skipped = sum(1 for r in results if r["result"] == "skip")

        summary = {
            "scenario_id": scenario.id,
            "scenario_name": scenario.name,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "total": len(results),
            "steps": results
        }

        await self._broadcast("scenario_complete", summary)
        # Natural completion — remove from registry so a fresh run can
        # claim the instance slot.
        _active_runners.pop(self.instance_id, None)
        return summary

    async def cancel(self):
        """Cancel the currently running scenario."""
        self._cancel_flag = True
        # Remove from the active-runner registry immediately so a
        # follow-up start works cleanly, even if cancellation hasn't
        # propagated through all in-flight steps yet.
        _active_runners.pop(self.instance_id, None)

    # =========================================================================
    # Device State Save/Restore
    # =========================================================================

    async def save_device_states(self) -> Dict[str, Dict[str, str]]:
        """
        Snapshot current state of all test devices before running scenarios.

        Captures switch state and level for every device in the instance's
        device_selections. Uses live Hubitat API (not cache).

        Returns:
            Dict mapping device_id -> {attribute: value}
        """
        from services.hubitat_client import get_default_client, get_hub_client_by_ip
        from services.hub_classifier import (
            get_device_by_canonical_id, get_hub_for_device,
        )
        default_client = get_default_client()

        all_device_ids: set = set()
        ds = self._instance.get("device_selections", {})
        for category_ids in ds.values():
            all_device_ids.update(str(did) for did in category_ids)

        def _client_and_hubitat_id(did):
            try:
                row = get_device_by_canonical_id(did) or get_hub_for_device(did)
                if row and row.get("hub_ip"):
                    c = get_hub_client_by_ip(row["hub_ip"])
                    if c:
                        return c, str(row.get("hubitat_id") or did)
            except Exception:
                pass
            return default_client, str(did)

        saved: Dict[str, Dict[str, str]] = {}
        for device_id in all_device_ids:
            try:
                client, hubitat_id = _client_and_hubitat_id(device_id)
                device = client.get_device(hubitat_id)
                if not device:
                    logger.warning(
                        f"save_device_states: device {device_id} not found"
                    )
                    continue

                attrs = device.get("attributes", [])
                state: Dict[str, str] = {}
                if isinstance(attrs, list):
                    for a in attrs:
                        name = a.get("name")
                        if name in ("switch", "level"):
                            state[name] = str(a.get("currentValue", ""))
                elif isinstance(attrs, dict):
                    for key in ("switch", "level"):
                        if key in attrs:
                            state[key] = str(attrs[key])

                saved[device_id] = state
                logger.info(f"Saved state for device {device_id}: {state}")
            except Exception as e:
                logger.error(
                    f"Failed to save state for device {device_id}: {e}",
                    exc_info=True
                )

        self._saved_device_states = saved

        await self._broadcast("states_saved", {
            "device_count": len(saved),
            "devices": {did: st for did, st in saved.items()}
        })

        return saved

    async def restore_device_states(self) -> Dict[str, str]:
        """
        Restore all test devices to their saved states.

        Called after all scenarios complete. Sends commands to return
        each device to the state captured by save_device_states().

        Returns:
            Dict mapping device_id -> "restored"/"failed"/"skipped"
        """
        if not self._saved_device_states:
            logger.info("No saved device states to restore")
            return {}

        from services.device_commander import get_device_commander
        commander = get_device_commander()

        results: Dict[str, str] = {}
        for device_id, state in self._saved_device_states.items():
            switch_state = state.get("switch")
            if not switch_state:
                results[device_id] = "skipped"
                continue

            try:
                result = await commander.send_command(
                    device_id=device_id,
                    command=switch_state,  # "on" or "off"
                    verify=False,
                )

                # Restore level if device was on and had a level
                level = state.get("level")
                if switch_state == "on" and level:
                    await commander.send_command(
                        device_id=device_id,
                        command="setLevel",
                        args=[int(level)],
                        verify=False,
                    )

                results[device_id] = "restored" if result.success else "failed"
                logger.info(
                    f"Restore device {device_id}: {switch_state} -> "
                    f"{'OK' if result.success else 'FAILED'}"
                )
            except Exception as e:
                results[device_id] = "failed"
                logger.error(
                    f"Failed to restore device {device_id}: {e}",
                    exc_info=True
                )

        await self._broadcast("states_restored", {
            "results": results
        })

        return results

    # =========================================================================
    # Step Execution
    # =========================================================================

    async def _execute_step(self, step: TestStep):
        """
        Execute a single test step based on its action type.

        Args:
            step: The step to execute (result/message updated in-place)
        """
        action = step.action

        if action == "command":
            await self._step_send_command(step)
        elif action == "webhook":
            await self._step_inject_webhook(step)
        elif action == "wait":
            await self._step_wait(step)
        elif action == "verify":
            await self._step_verify_device(step)
        elif action == "api_call":
            await self._step_api_call(step)
        else:
            step.result = StepResult.FAIL
            step.message = f"Unknown action type: {action}"

    async def _step_send_command(self, step: TestStep):
        """Send a real command to a real device via DeviceCommander."""
        from services.device_commander import get_device_commander

        device_id = step.params["device_id"]
        command = step.params["command"]
        args = step.params.get("args")

        try:
            commander = get_device_commander()
            # E2E tests have their own verify steps — skip commander verification
            result = await commander.send_command(
                device_id=device_id,
                command=command,
                args=args,
                verify=False,
            )

            if result.success:
                step.result = StepResult.PASS
                step.message = f"Sent {command} to device {device_id}"
            else:
                step.result = StepResult.FAIL
                step.message = (
                    f"Command {command} failed for device {device_id}: "
                    f"{result.error}"
                )
        except Exception as e:
            step.result = StepResult.FAIL
            step.message = f"Command {command} exception for device {device_id}: {e}"
            logger.error(
                f"_step_send_command failed: {e}", exc_info=True
            )

    async def _step_inject_webhook(self, step: TestStep):
        """
        Inject a synthetic webhook event.

        POSTs to our own /api/webhook/event endpoint (internal loopback)
        to simulate a Hubitat device event. This triggers the full
        webhook routing pipeline, just like a real device event would.
        """
        import httpx

        payload = step.params["payload"]
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "http://localhost:5000/api/webhook/event",
                    json=payload,
                    timeout=10
                )
            if resp.status_code == 200:
                step.result = StepResult.PASS
                step.message = (
                    f"Injected {payload.get('name')}={payload.get('value')} "
                    f"for device {payload.get('deviceId')}"
                )
            else:
                step.result = StepResult.FAIL
                step.message = f"Webhook injection failed: HTTP {resp.status_code}"
        except Exception as e:
            step.result = StepResult.FAIL
            step.message = f"Webhook injection error: {e}"

    async def _step_wait(self, step: TestStep):
        """
        Wait for a specified duration.

        Broadcasts 1-second countdown ticks so the UI can show progress.
        Respects cancellation flag.
        """
        seconds = step.params.get("seconds", 1)
        for i in range(seconds):
            if self._cancel_flag:
                step.result = StepResult.SKIP
                step.message = "Cancelled during wait"
                return
            await asyncio.sleep(1)
            await self._broadcast("wait_tick", {
                "elapsed": i + 1,
                "total": seconds
            })
        step.result = StepResult.PASS
        step.message = f"Waited {seconds}s"

    async def _step_verify_device(self, step: TestStep):
        """
        Verify a device attribute matches an expected value.

        Polls the Hubitat Maker API (not cache) for the device's current
        state. Retries with delay to allow for command propagation.

        Params:
            device_id: Hubitat device ID
            attribute: Attribute name (e.g., 'switch', 'level')
            expected: Expected value (string comparison)
            retries: Number of attempts (default: 3)
            retry_delay: Seconds between retries (default: 1.0)
        """
        from services.hubitat_client import get_default_client, get_hub_client_by_ip
        from services.hub_classifier import (
            get_device_by_canonical_id, get_hub_for_device,
        )

        device_id = step.params["device_id"]
        attribute = step.params["attribute"]
        expected = str(step.params["expected"])
        retries = step.params.get("retries", 3)
        delay = step.params.get("retry_delay", 1.0)

        # Route verification to the hub that natively owns this device.
        # device_id may be either a canonical devices.id PK (post-Phase-5)
        # or a Hubitat per-hub id (legacy scenarios). Try canonical first.
        client = None
        hubitat_id = str(device_id)
        try:
            row = get_device_by_canonical_id(device_id) or get_hub_for_device(device_id)
            if row and row.get("hub_ip"):
                client = get_hub_client_by_ip(row["hub_ip"])
                hubitat_id = str(row.get("hubitat_id") or device_id)
        except Exception:
            pass
        if client is None:
            client = get_default_client()
        actual = None

        for attempt in range(retries):
            device = client.get_device(hubitat_id)
            if device:
                # Hubitat returns attributes as a list of {name, currentValue}
                attrs = device.get("attributes", [])
                if isinstance(attrs, list):
                    for a in attrs:
                        if a.get("name") == attribute:
                            actual = str(a.get("currentValue", ""))
                            break
                elif isinstance(attrs, dict):
                    actual = str(attrs.get(attribute, ""))

                if actual == expected:
                    step.result = StepResult.PASS
                    step.message = f"{attribute}={actual}"
                    return
            else:
                actual = "(device not found)"

            if attempt < retries - 1:
                await asyncio.sleep(delay)

        step.result = StepResult.FAIL
        step.message = f"{attribute}={actual} (expected {expected})"

    async def _step_api_call(self, step: TestStep):
        """
        Call an internal API endpoint.

        Used for instance-level operations like run, pause, resume.
        Checks the HTTP status code to determine pass/fail.

        Params:
            method: HTTP method (default: POST)
            endpoint: API path (e.g., '/api/instances/2/pause')
            body: Optional JSON body
        """
        import httpx

        method = step.params.get("method", "POST")
        endpoint = step.params["endpoint"]
        body = step.params.get("body")

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.request(
                    method,
                    f"http://localhost:5000{endpoint}",
                    json=body,
                    timeout=10
                )
            if resp.status_code < 400:
                step.result = StepResult.PASS
                step.message = f"{method} {endpoint} -> {resp.status_code}"
            else:
                step.result = StepResult.FAIL
                step.message = f"{method} {endpoint} -> {resp.status_code}"
        except Exception as e:
            step.result = StepResult.FAIL
            step.message = f"{method} {endpoint} failed: {e}"

    # =========================================================================
    # Scenario Builder
    # =========================================================================

    def _build_scenarios(self) -> List[TestScenario]:
        """
        Build test scenarios based on instance capabilities.

        Reads device_selections and settings to determine which
        scenarios are applicable and what devices/parameters to use.

        Returns:
            List of TestScenario objects
        """
        scenarios = []
        ds = self._instance.get("device_selections", {})
        settings = self._instance.get("settings", {})

        motion_ids = ds.get("motion_sensors", [])
        switch_ids = ds.get("switches", [])
        pause_btn_ids = ds.get("pause_buttons", [])

        # ------------------------------------------------------------------
        # Scenario 1: Motion Activation
        # ------------------------------------------------------------------
        if motion_ids and switch_ids:
            s = TestScenario(
                id="motion_activation",
                name="Motion Activation",
                description="Verify motion event turns on lights"
            )
            # Turn off first 2 switches to establish known state
            for sid in switch_ids[:2]:
                s.steps.append(TestStep(
                    name=f"Turn off switch {sid}",
                    description=f"Ensure switch {sid} starts in OFF state",
                    action="command",
                    params={"device_id": sid, "command": "off"}
                ))
            s.steps.append(TestStep(
                name="Wait for off state",
                description="Allow device state to propagate",
                action="wait",
                params={"seconds": 2}
            ))
            # Inject motion active webhook
            s.steps.append(TestStep(
                name=f"Inject motion active (sensor {motion_ids[0]})",
                description="Simulate motion sensor activation via webhook",
                action="webhook",
                params={"payload": {
                    "deviceId": str(motion_ids[0]),
                    "name": "motion",
                    "value": "active",
                    "displayName": f"E2E Test Motion {motion_ids[0]}"
                }}
            ))
            s.steps.append(TestStep(
                name="Wait for automation",
                description="Allow app to process event and send commands",
                action="wait",
                params={"seconds": 3}
            ))
            # Verify switches turned on
            for sid in switch_ids[:2]:
                s.steps.append(TestStep(
                    name=f"Verify switch {sid} is ON",
                    description=f"Device {sid} should now be ON",
                    action="verify",
                    params={
                        "device_id": str(sid),
                        "attribute": "switch",
                        "expected": "on",
                        "retries": 3,
                        "retry_delay": 1.0
                    }
                ))
            scenarios.append(s)

        # ------------------------------------------------------------------
        # Scenario 2: Motion Timeout
        # ------------------------------------------------------------------
        if motion_ids and switch_ids:
            timeout_val = settings.get("noMotionTime", 5)
            time_unit = settings.get("timeUnit", "minutes")
            timeout_sec = timeout_val * (60 if time_unit == "minutes" else 1)

            s = TestScenario(
                id="motion_timeout",
                name="Motion Timeout",
                description=(
                    f"Verify lights turn off after {timeout_val} {time_unit} "
                    f"of no motion"
                )
            )
            # Activate motion
            s.steps.append(TestStep(
                name="Inject motion active",
                description="Trigger motion to turn lights on",
                action="webhook",
                params={"payload": {
                    "deviceId": str(motion_ids[0]),
                    "name": "motion",
                    "value": "active",
                    "displayName": f"E2E Test Motion {motion_ids[0]}"
                }}
            ))
            s.steps.append(TestStep(
                name="Wait for lights on",
                description="Let automation process",
                action="wait",
                params={"seconds": 2}
            ))
            # Motion inactive
            s.steps.append(TestStep(
                name="Inject motion inactive",
                description="Motion sensor goes inactive",
                action="webhook",
                params={"payload": {
                    "deviceId": str(motion_ids[0]),
                    "name": "motion",
                    "value": "inactive",
                    "displayName": f"E2E Test Motion {motion_ids[0]}"
                }}
            ))
            # Only wait for timeout if it's short enough for automated test
            if timeout_sec <= 60:
                s.steps.append(TestStep(
                    name=f"Wait for timeout ({timeout_sec}s + 5s buffer)",
                    description="Wait for no-motion timeout to expire",
                    action="wait",
                    params={"seconds": timeout_sec + 5}
                ))
                for sid in switch_ids[:2]:
                    s.steps.append(TestStep(
                        name=f"Verify switch {sid} is OFF",
                        description=f"Device {sid} should turn off after timeout",
                        action="verify",
                        params={
                            "device_id": str(sid),
                            "attribute": "switch",
                            "expected": "off",
                            "retries": 5,
                            "retry_delay": 2.0
                        }
                    ))
            else:
                s.steps.append(TestStep(
                    name=f"Timeout too long ({timeout_sec}s)",
                    description=(
                        f"Configured timeout is {timeout_val} {time_unit}. "
                        f"Cannot wait in automated test. Observe manually."
                    ),
                    action="wait",
                    params={"seconds": 1}
                ))
            scenarios.append(s)

        # ------------------------------------------------------------------
        # Scenario 3: Button Pause/Resume
        # ------------------------------------------------------------------
        if pause_btn_ids:
            btn_event = settings.get("buttonEventType", "held")
            s = TestScenario(
                id="button_pause_resume",
                name="Button Pause/Resume",
                description=f"Verify button {btn_event} toggles pause state"
            )
            # Inject button event to pause
            s.steps.append(TestStep(
                name=f"Inject button {btn_event} (pause)",
                description=f"Simulate {btn_event} on button {pause_btn_ids[0]}",
                action="webhook",
                params={"payload": {
                    "deviceId": str(pause_btn_ids[0]),
                    "name": btn_event,
                    "value": "1",
                    "displayName": f"E2E Test Button {pause_btn_ids[0]}"
                }}
            ))
            s.steps.append(TestStep(
                name="Wait for pause processing",
                description="Allow pause to propagate",
                action="wait",
                params={"seconds": 2}
            ))
            s.steps.append(TestStep(
                name="Check instance status",
                description="Verify instance is paused via API",
                action="api_call",
                params={
                    "method": "GET",
                    "endpoint": f"/api/instances/{self.instance_id}/status"
                }
            ))
            # Resume
            s.steps.append(TestStep(
                name=f"Inject button {btn_event} (resume)",
                description=f"Simulate {btn_event} again to resume",
                action="webhook",
                params={"payload": {
                    "deviceId": str(pause_btn_ids[0]),
                    "name": btn_event,
                    "value": "1",
                    "displayName": f"E2E Test Button {pause_btn_ids[0]}"
                }}
            ))
            s.steps.append(TestStep(
                name="Wait for resume processing",
                description="Allow resume to propagate",
                action="wait",
                params={"seconds": 2}
            ))
            scenarios.append(s)

        # ------------------------------------------------------------------
        # Scenario 4: Direct Device Commands
        # ------------------------------------------------------------------
        if switch_ids:
            s = TestScenario(
                id="direct_device_commands",
                name="Direct Device Commands",
                description="Send on/off to each switch and verify state changes"
            )
            for sid in switch_ids:
                s.steps.append(TestStep(
                    name=f"Turn ON switch {sid}",
                    description=f"Send 'on' command to device {sid}",
                    action="command",
                    params={"device_id": str(sid), "command": "on"}
                ))
                s.steps.append(TestStep(
                    name="Wait",
                    description="Let command propagate",
                    action="wait",
                    params={"seconds": 2}
                ))
                s.steps.append(TestStep(
                    name=f"Verify {sid} ON",
                    description=f"Device {sid} should be ON",
                    action="verify",
                    params={
                        "device_id": str(sid),
                        "attribute": "switch",
                        "expected": "on",
                        "retries": 3,
                        "retry_delay": 1.0
                    }
                ))
                s.steps.append(TestStep(
                    name=f"Turn OFF switch {sid}",
                    description=f"Send 'off' command to device {sid}",
                    action="command",
                    params={"device_id": str(sid), "command": "off"}
                ))
                s.steps.append(TestStep(
                    name="Wait",
                    description="Let command propagate",
                    action="wait",
                    params={"seconds": 2}
                ))
                s.steps.append(TestStep(
                    name=f"Verify {sid} OFF",
                    description=f"Device {sid} should be OFF",
                    action="verify",
                    params={
                        "device_id": str(sid),
                        "attribute": "switch",
                        "expected": "off",
                        "retries": 3,
                        "retry_delay": 1.0
                    }
                ))
            scenarios.append(s)

        # ------------------------------------------------------------------
        # Scenario 5: Dim Level Verification (if useDim enabled)
        # ------------------------------------------------------------------
        if switch_ids and settings.get("useDim") and motion_ids:
            dim_level = settings.get("defaultDimLevel", 50)
            s = TestScenario(
                id="dim_level",
                name="Dim Level Verification",
                description=f"Verify lights set to level {dim_level} on motion"
            )
            # Use first switch only (to avoid testing non-dimmable devices)
            sid = switch_ids[0]
            s.steps.append(TestStep(
                name=f"Turn off switch {sid}",
                description="Ensure starting from OFF state",
                action="command",
                params={"device_id": str(sid), "command": "off"}
            ))
            s.steps.append(TestStep(
                name="Wait",
                description="Propagate off state",
                action="wait",
                params={"seconds": 2}
            ))
            s.steps.append(TestStep(
                name="Inject motion active",
                description="Trigger motion via webhook",
                action="webhook",
                params={"payload": {
                    "deviceId": str(motion_ids[0]),
                    "name": "motion",
                    "value": "active",
                    "displayName": f"E2E Test Motion {motion_ids[0]}"
                }}
            ))
            s.steps.append(TestStep(
                name="Wait for automation",
                description="Let app send setLevel command",
                action="wait",
                params={"seconds": 3}
            ))
            s.steps.append(TestStep(
                name=f"Verify level = {dim_level}",
                description=f"Switch {sid} level should be {dim_level}",
                action="verify",
                params={
                    "device_id": str(sid),
                    "attribute": "level",
                    "expected": str(dim_level),
                    "retries": 3,
                    "retry_delay": 1.0
                }
            ))
            scenarios.append(s)

        # ------------------------------------------------------------------
        # Scenario 6: Manual Override Detection
        # ------------------------------------------------------------------
        if motion_ids and switch_ids:
            s = TestScenario(
                id="manual_override",
                name="Manual Override Detection",
                description=(
                    "Verify that manually turning off a switch prevents "
                    "the automation from turning it back on"
                )
            )
            sid = switch_ids[0]
            # Step 1: Reset memoization
            s.steps.append(TestStep(
                name="Reset instance memoization",
                description="Clear stale override records",
                action="api_call",
                params={
                    "method": "POST",
                    "endpoint": f"/api/instances/{self.instance_id}/update"
                }
            ))
            s.steps.append(TestStep(
                name="Wait for reload",
                description="Let instance reload",
                action="wait",
                params={"seconds": 2}
            ))
            # Step 2: Motion on -> lights on
            s.steps.append(TestStep(
                name="Inject motion active",
                description="Trigger lights on via motion",
                action="webhook",
                params={"payload": {
                    "deviceId": str(motion_ids[0]),
                    "name": "motion",
                    "value": "active",
                    "displayName": f"E2E Test Motion {motion_ids[0]}"
                }}
            ))
            s.steps.append(TestStep(
                name="Wait for lights on",
                description="Let automation turn on lights",
                action="wait",
                params={"seconds": 3}
            ))
            s.steps.append(TestStep(
                name=f"Verify {sid} ON (baseline)",
                description="Confirm light turned on",
                action="verify",
                params={
                    "device_id": str(sid),
                    "attribute": "switch",
                    "expected": "on",
                    "retries": 3
                }
            ))
            # Step 3: Manually turn off
            s.steps.append(TestStep(
                name=f"Manually turn OFF switch {sid}",
                description="Simulate user override",
                action="command",
                params={"device_id": str(sid), "command": "off"}
            ))
            s.steps.append(TestStep(
                name="Wait for override to register",
                description="Let device state update",
                action="wait",
                params={"seconds": 2}
            ))
            # Step 4: Motion again -> should NOT turn it back on
            s.steps.append(TestStep(
                name="Inject motion active (again)",
                description="Motion after override — should NOT turn light on",
                action="webhook",
                params={"payload": {
                    "deviceId": str(motion_ids[0]),
                    "name": "motion",
                    "value": "active",
                    "displayName": f"E2E Test Motion {motion_ids[0]}"
                }}
            ))
            s.steps.append(TestStep(
                name="Wait for automation",
                description="Let app process — should detect override and skip",
                action="wait",
                params={"seconds": 3}
            ))
            s.steps.append(TestStep(
                name=f"Verify {sid} still OFF (override respected)",
                description="Light should stay OFF because user turned it off",
                action="verify",
                params={
                    "device_id": str(sid),
                    "attribute": "switch",
                    "expected": "off",
                    "retries": 3
                }
            ))
            scenarios.append(s)

        # ------------------------------------------------------------------
        # Scenario 7: Keep-Off Enforcement
        # ------------------------------------------------------------------
        keep_off_ids = ds.get("keep_off_switches", [])
        if keep_off_ids and motion_ids:
            ko_sid = keep_off_ids[0]
            s = TestScenario(
                id="keep_off_enforcement",
                name="Keep-Off Enforcement",
                description="Verify keep-off switches stay off despite motion"
            )
            # Turn ON the keep-off switch (simulating manual/external turn-on)
            s.steps.append(TestStep(
                name=f"Turn ON keep-off switch {ko_sid}",
                description="Manually turn on a keep-off switch to test enforcement",
                action="command",
                params={"device_id": str(ko_sid), "command": "on"}
            ))
            s.steps.append(TestStep(
                name="Wait for command",
                description="Allow state to propagate",
                action="wait",
                params={"seconds": 2}
            ))
            # Trigger motion (calls master() → _enforce_keep_switches())
            s.steps.append(TestStep(
                name="Inject motion active",
                description="Trigger master() which should enforce keep-off",
                action="webhook",
                params={"payload": {
                    "deviceId": str(motion_ids[0]),
                    "name": "motion",
                    "value": "active",
                    "displayName": f"E2E Test Motion {motion_ids[0]}"
                }}
            ))
            s.steps.append(TestStep(
                name="Wait for enforcement",
                description="Allow app to process and enforce keep-off",
                action="wait",
                params={"seconds": 5}
            ))
            # Verify the keep-off switch was forced OFF
            s.steps.append(TestStep(
                name=f"Verify keep-off switch {ko_sid} is OFF",
                description="Keep-off switch should be forced OFF despite motion",
                action="verify",
                params={
                    "device_id": str(ko_sid),
                    "attribute": "switch",
                    "expected": "off",
                    "retries": 5,
                    "retry_delay": 2.0
                }
            ))
            scenarios.append(s)

        # ------------------------------------------------------------------
        # Scenario 8: Keep-On Enforcement
        # ------------------------------------------------------------------
        keep_on_ids = ds.get("keep_on_switches", [])
        if keep_on_ids:
            ko_sid = keep_on_ids[0]
            s = TestScenario(
                id="keep_on_enforcement",
                name="Keep-On Enforcement",
                description="Verify keep-on switches stay on despite timeout"
            )
            # Turn OFF the keep-on switch
            s.steps.append(TestStep(
                name=f"Turn OFF keep-on switch {ko_sid}",
                description="Manually turn off a keep-on switch to test enforcement",
                action="command",
                params={"device_id": str(ko_sid), "command": "off"}
            ))
            s.steps.append(TestStep(
                name="Wait for command",
                description="Allow state to propagate",
                action="wait",
                params={"seconds": 2}
            ))
            # Trigger motion inactive → timeout → master() → enforce
            if motion_ids:
                s.steps.append(TestStep(
                    name="Inject motion inactive",
                    description="Trigger timeout path which calls master()",
                    action="webhook",
                    params={"payload": {
                        "deviceId": str(motion_ids[0]),
                        "name": "motion",
                        "value": "inactive",
                        "displayName": f"E2E Test Motion {motion_ids[0]}"
                    }}
                ))
            s.steps.append(TestStep(
                name="Wait for enforcement",
                description="Allow app to process and enforce keep-on",
                action="wait",
                params={"seconds": 5}
            ))
            # Verify the keep-on switch was forced ON
            s.steps.append(TestStep(
                name=f"Verify keep-on switch {ko_sid} is ON",
                description="Keep-on switch should be forced ON despite inactivity",
                action="verify",
                params={
                    "device_id": str(ko_sid),
                    "attribute": "switch",
                    "expected": "on",
                    "retries": 5,
                    "retry_delay": 2.0
                }
            ))
            scenarios.append(s)

        return scenarios

    # =========================================================================
    # Helpers
    # =========================================================================

    def _step_result_dict(self, step: TestStep, index: int) -> Dict:
        """Convert a TestStep to a serializable result dict."""
        return {
            "index": index,
            "name": step.name,
            "description": step.description,
            "result": step.result.value,
            "message": step.message,
            "duration_ms": round(step.duration_ms, 1)
        }

    async def _broadcast(self, event_type: str, data: Dict):
        """Broadcast an event to the E2E SSE stream for this instance."""
        from services.e2e_events import get_e2e_broadcaster
        broadcaster = get_e2e_broadcaster()
        await broadcaster.broadcast(self.instance_id, {
            "type": event_type,
            **data
        })
# reload-e2e-routing
# reload-stop-clear-reset
