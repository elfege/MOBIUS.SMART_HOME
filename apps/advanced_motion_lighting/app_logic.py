"""
Advanced Motion Lighting Management

Ported from Hubitat Groovy: Advanced_Motion_Lighting_Management_V2.groovy

This is a sophisticated motion-activated lighting automation with:
- Multi-sensor motion detection with health monitoring
- Memoization (remembers user overrides vs app control)
- Mode-specific timeouts and dimming levels
- Illuminance threshold checking
- Pause/resume with button control
- Color and color temperature support

Key behaviors:
1. Motion active → Turn on lights (unless illuminance above threshold)
2. No motion for timeout → Turn off lights
3. User manually changes lights → Remember that override (memoization)
4. Mode changes → Reset memoization, recalculate timeout
"""

from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import logging
import traceback

from apps.base_app import BaseApp
from models.event import DeviceEvent
from models.command import CommandResult
from services.hubitat_client import HubitatClient

# ANSI color for device names in logs
_C = "\033[96m"   # bright cyan
_R = "\033[0m"    # reset


class AdvancedMotionLightingApp(BaseApp):
    """
    Motion-triggered lighting automation with advanced features.

    Settings:
        memoize: Remember user overrides (default: False)
        useDim: Enable dimming (default: False)
        defaultDimLevel: Base dimming level 0-100 (default: 50)
        useColor: Enable color control (default: False)
        colorPreset: Color preset name (default: 'Warm White')
        customColorTemperature: Color temp in Kelvin (default: 2700)
        timeUnit: 'seconds' or 'minutes' (default: 'minutes')
        noMotionTime: Timeout value (default: 5)
        useIlluminance: Enable lux threshold (default: False)
        illuminanceThreshold: Lux threshold (default: 50)
        considerActiveWhenFail: Treat sensor failure as active (default: False)

    Device Categories:
        motion_sensors: Motion sensors that trigger automation
        switches: Switches/dimmers to control
        illuminance_sensor: Optional lux sensor
        pause_buttons: Optional buttons for pause/resume
        contacts: Optional contact sensors
        pause_switches: Switches to control on pause/resume (can overlap with switches)
    """

    TYPE_NAME = "advanced_motion_lighting"
    DISPLAY_NAME = "Advanced Motion Lighting"
    DESCRIPTION = "Motion-triggered lighting with dimming, color, and memoization"
    VERSION = "2.0.0"

    # Color presets (Kelvin or HSV)
    COLOR_PRESETS = {
        'Soft White': {'temperature': 2700},
        'Warm White': {'temperature': 3000},
        'Cool White': {'temperature': 4000},
        'Daylight': {'temperature': 6500},
        'Red': {'hue': 0, 'saturation': 100},
        'Green': {'hue': 33, 'saturation': 100},
        'Blue': {'hue': 66, 'saturation': 100},
        'Yellow': {'hue': 16, 'saturation': 100},
        'Purple': {'hue': 75, 'saturation': 100},
        'Pink': {'hue': 83, 'saturation': 56}
    }

    def __init__(self, instance_data: Dict[str, Any], instance_manager):
        super().__init__(instance_data, instance_manager)
        self._init_memoization_keys()

        # Track functional sensors
        self._functional_sensors: Dict[str, bool] = {}

    def _init_memoization_keys(self) -> None:
        """
        Ensure memoization dict has required keys for this app type.

        Seeds keep_off/keep_on devices with their expected state and
        source='app' so enforcement knows to enforce after a reset.
        Uses setdefault — won't overwrite existing entries.

        Keep device memo keys use device_id (not name) to guarantee
        consistency across webhook events, cache, and live API lookups.
        """
        self._memoization.setdefault('switch_state', {})
        self._memoization.setdefault('dim_level', {})
        # Seed keep devices with expected states (keyed by device_id)
        switch_state = self._memoization['switch_state']
        for device_id in self.get_devices('keep_off_switches'):
            key = f"keep:{device_id}"
            if key not in switch_state:
                switch_state[key] = {'state': 'off', 'source': 'app'}
        for device_id in self.get_devices('keep_on_switches'):
            key = f"keep:{device_id}"
            if key not in switch_state:
                switch_state[key] = {'state': 'on', 'source': 'app'}

    def _reset_memoization(self) -> None:
        """Reset memoization and reinitialize required keys."""
        super()._reset_memoization()
        self._init_memoization_keys()

    def resume(self) -> None:
        """
        Resume from paused state.

        Overrides base class to ensure keep-switch enforcement runs
        immediately. Calls master() which:
        1. Evaluates motion → _control_lights (excludes keep devices)
        2. _enforce_keep_switches() — forces keep_off OFF, keep_on ON
        3. _schedule_next_run()
        """
        self._is_paused = False
        self.logger.info("Resumed")

        # Cancel auto-resume if manually resumed
        if self._runtime.auto_resume_job_id:
            from services.scheduler_service import get_scheduler
            scheduler = get_scheduler()
            scheduler.cancel(self._runtime.auto_resume_job_id)
            self._runtime.auto_resume_job_id = None

        # Reset memoization (seeds keep devices with source='app')
        self._reset_memoization()

        # Evaluate motion state + enforce keep switches immediately
        self.master()

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def initialize(self) -> None:
        """Set up the instance on startup."""
        self.logger.info(f"Initializing: {self.label}")

        # Seed functional sensors from config so _is_motion_active() knows
        # sensors exist even before the first event arrives
        for sensor_id in self.get_devices('motion_sensors'):
            self._functional_sensors.setdefault(sensor_id, True)

        # Schedule health check
        from services.scheduler_service import get_scheduler
        scheduler = get_scheduler()

        health_job_id = f"health_{self.instance_id}"
        scheduler.schedule_recurring(
            job_id=health_job_id,
            interval_seconds=600,  # Every 10 minutes
            callback=lambda **kwargs: self._health_check(),
            instance_id=self.instance_id,
            job_type='health_check'
        )
        self._runtime.health_check_job_id = health_job_id

        # Groovy parity: initialize() does NOT call master().
        # Calling master() on startup/save turns off all lights because
        # no motion event has arrived yet → _is_motion_active() = False
        # → _control_lights('off') on every switch. Instead, only enforce
        # keep-on/keep-off rules and schedule the periodic run.
        self._enforce_keep_switches()
        self._schedule_next_run()

    # =========================================================================
    # Event Handling
    # =========================================================================

    def on_event(self, event: DeviceEvent) -> None:
        """Handle device events."""
        try:
            # Button events ALWAYS processed — they're the unpause mechanism
            if event.event_type in ('pushed', 'held', 'doubleTapped'):
                self._handle_button(event)
                return

            if self.is_paused:
                self.logger.debug(f"Paused, ignoring event: {event}")
                return

            self.update_last_activity()

            if event.event_type == 'motion':
                self._handle_motion(event)
            elif event.event_type == 'switch':
                self._handle_switch(event)
            elif event.event_type == 'illuminance':
                self._handle_illuminance(event)
            elif event.event_type == 'contact':
                self._handle_contact(event)
        except Exception as e:
            self.logger.error(
                f"on_event() failed for instance {self.label}, "
                f"event={event}: {e}",
                exc_info=True
            )

    def _handle_motion(self, event: DeviceEvent) -> None:
        """Handle motion sensor event."""
        # Mark sensor as functional
        self._functional_sensors[event.device_id] = True

        if event.is_motion_active:
            self.logger.debug(f"Motion active: {_C}{event.device_name}{_R}")
            self._runtime.last_motion_time = datetime.now()
            self.master(motion_active_event=True)
        else:
            self.logger.debug(f"Motion inactive: {_C}{event.device_name}{_R}")
            # Schedule check for timeout
            timeout = self._get_timeout_seconds()
            self.schedule_timeout(timeout)

    def _handle_switch(self, event: DeviceEvent) -> None:
        """
        Handle switch event.

        Guard: if DeviceCommander has an in-flight command for this device
        (status == UPDATING), the event is likely an echo of our own command
        (possibly from Matter succeeding before Hubitat's verify cycle). In
        that case, skip override detection entirely. This prevents the
        dual-command strategy from causing phantom memoization overrides.
        See docs/dual_command_flow.html for the full timing diagram.

        For keep_off/keep_on devices: only tag as source='manual' when the
        event CONTRADICTS the expected state. This distinguishes a genuine
        user override from an echo of our own enforcement command.

        - keep_off + event='on' → override (expected off, user turned on)
        - keep_off + event='off' → echo of our command → ignore
        - keep_on  + event='off' → override (expected on, user turned off)
        - keep_on  + event='on'  → echo of our command → ignore

        For regular switches: no memo update.
        """
        self.logger.debug(f"Switch: {_C}{event.device_name}{_R} → {event.value}")

        # --- UPDATING guard ---
        # If the DeviceCommander is currently executing a command for this
        # device, this switch event is an echo of our own command (either
        # from Hubitat or Matter). Do NOT interpret as a manual override.
        try:
            from services.device_commander import (
                get_device_commander,
                CommandStatus,
            )
            commander = get_device_commander()
            status = commander.get_device_status(str(event.device_id))
            if status == CommandStatus.UPDATING:
                self.logger.debug(
                    f"Switch event for {_C}{event.device_name}{_R} "
                    f"(id:{event.device_id}) suppressed — command in-flight "
                    f"(status=UPDATING)"
                )
                return
        except Exception as e:
            # If we can't check, proceed with normal logic (safe fallback)
            self.logger.debug(
                f"Could not check UPDATING guard for {event.device_id}: {e}"
            )

        keep_off_ids = set(self.get_devices('keep_off_switches'))
        keep_on_ids = set(self.get_devices('keep_on_switches'))

        # keep_off device received 'on' → contradicts expected 'off' → user override
        is_keep_off_override = (
            event.device_id in keep_off_ids and event.value == 'on'
        )
        # keep_on device received 'off' → contradicts expected 'on' → user override
        is_keep_on_override = (
            event.device_id in keep_on_ids and event.value == 'off'
        )

        if is_keep_off_override or is_keep_on_override:
            # Use keep:{device_id} key — matches _init_memoization_keys
            # and _enforce_keep_switches for guaranteed consistency
            key = f"keep:{event.device_id}"
            self._memoization.setdefault('switch_state', {})
            self._memoization['switch_state'][key] = {
                'state': event.value, 'source': 'manual'
            }
            self._save_memoization()
            self.logger.info(
                f"\033[1;93m{'='*60}\033[0m\n"
                f"\033[1;93m  OVERRIDE MEMOIZED — {_C}{event.device_name}{_R}"
                f" \033[1;93m[id:{event.device_id}]\033[0m\n"
                f"\033[1;93m  event={event.event_type}  value={event.value}"
                f"  source=manual\033[0m\n"
                f"\033[1;93m{'='*60}\033[0m"
            )

    def _handle_illuminance(self, event: DeviceEvent) -> None:
        """Handle illuminance change."""
        self.logger.debug(f"Illuminance: {event.value} lux")
        # Re-evaluate state with new illuminance
        self.master()

    def _handle_button(self, event: DeviceEvent) -> None:
        """
        Handle button event (pause/resume toggle).

        Only responds to the configured buttonEventType (default: 'held').
        Ignores other button event types to prevent double-toggle when
        Hubitat sends both 'pushed' and 'held' on a long press.
        """
        expected_type = self.get_setting('buttonEventType', 'held')
        if event.event_type != expected_type:
            self.logger.debug(
                f"Button {event.event_type}: {_C}{event.device_name}{_R}"
                f" — ignoring (configured for {expected_type})"
            )
            return
        self.logger.info(f"Button {event.event_type}: {_C}{event.device_name}{_R}")

        pause_duration = self.get_setting('pauseDuration', 60)
        if self.get_setting('pauseDurationUnit', 'Minutes') == 'Hours':
            pause_duration *= 60

        # Toggle pause state — only control pause switches if operation succeeds
        if self.is_paused:
            try:
                success = self.instance_manager.resume_instance(self.instance_id)
                if success:
                    self._control_pause_switches(resuming=True)
                else:
                    self.logger.error("Resume returned False, pause switches unchanged")
            except Exception as e:
                self.logger.error(f"Resume failed: {e}")
        else:
            try:
                success = self.instance_manager.pause_instance(
                    self.instance_id,
                    duration_minutes=pause_duration,
                    reason='Button press'
                )
                if success:
                    self._control_pause_switches(resuming=False)
                else:
                    self.logger.error("Pause returned False, pause switches unchanged")
            except Exception as e:
                self.logger.error(f"Pause failed: {e}")

    def _control_pause_switches(self, resuming: bool) -> None:
        """
        Control pause switches when pausing/resuming.

        These switches get actuated on pause/resume events. They can overlap
        with motion-controlled switches and keep_off/keep_on switches.
        Each device is handled independently so one failure doesn't block
        the rest.

        Updates memo with source='pause' for any keep devices so enforcement
        knows this was an app-initiated action (not a user override).

        Args:
            resuming: True if resuming (un-pausing), False if pausing
        """
        switch_ids = self.get_devices('pause_switches')
        if not switch_ids:
            return

        action = self.get_setting('pauseSwitchAction', 'toggle')
        # Build keep sets for memo tagging
        keep_off_ids = set(self.get_devices('keep_off_switches'))
        keep_on_ids = set(self.get_devices('keep_on_switches'))

        for device_id in switch_ids:
            try:
                # On resume: skip keep_off (must stay off, enforcement handles)
                # On pause:  skip keep_on (must stay on, enforcement handles)
                if resuming and device_id in keep_off_ids:
                    continue
                if not resuming and device_id in keep_on_ids:
                    continue

                cmd = None
                if action == 'toggle':
                    device = self.get_device_state(device_id)
                    if device:
                        current = device.get('attributes', {}).get('switch', 'off')
                        cmd = 'off' if current == 'on' else 'on'
                    else:
                        cmd = 'off' if resuming else 'on'
                elif action == 'on':
                    cmd = 'off' if resuming else 'on'
                elif action == 'off':
                    cmd = 'on' if resuming else 'off'

                if cmd:
                    self.send_command(device_id, cmd, verify=False)
            except Exception as e:
                self.logger.error(
                    f"Failed to control pause switch {device_id}: {e}",
                    exc_info=True
                )

    def _handle_contact(self, event: DeviceEvent) -> None:
        """Handle contact sensor (door open → lights on)."""
        if event.is_contact_open:
            self.logger.debug(f"Contact opened: {event.device_name}")
            # Turn on lights when door opens
            self.master(motion_active_event=True)

    # =========================================================================
    # Main Logic
    # =========================================================================

    def master(self, motion_active_event: bool = False, **kwargs) -> None:
        """
        Main logic loop.

        Called when:
        - Motion detected (motion_active_event=True)
        - Timeout expires
        - Mode changes
        - Resume from pause
        """
        try:
            if self.is_paused:
                return

            # Check exceptions (restricted mode, time, illuminance)
            if self._in_exception_state():
                self.logger.debug("In exception state, scheduling next check")
                self._schedule_next_run()
                return

            # Decide action
            if motion_active_event or self._is_motion_active():
                self._control_lights('on')
            else:
                self._control_lights('off')

            # Enforce keep-on/keep-off AFTER normal motion-based control
            self._enforce_keep_switches()

            self._schedule_next_run()
        except Exception as e:
            self.logger.error(
                f"master() failed for instance {self.label}: {e}",
                exc_info=True
            )

    def _is_motion_active(self) -> bool:
        """
        Check if any motion sensor is currently active.

        Groovy-parity three-tier check (mirrors Active() in Groovy source):
        1. In-memory timestamp: fastest, covers normal runtime operation
        2. Live device state: query Hubitat currentValue('motion') per sensor
        3. Event history: query eventsSince(timeout) from Hubitat API

        Tiers 2 and 3 are essential on startup/reload when the in-memory
        timestamp is None, so the app doesn't falsely assume "no motion"
        and turn off all lights.
        """
        # Check functional sensors
        functional = [
            sensor_id for sensor_id, is_functional in self._functional_sensors.items()
            if is_functional
        ]

        if not functional:
            # No functional sensors — use fail-safe setting
            consider_active = self.get_setting('considerActiveWhenFail', False)
            if consider_active:
                self.logger.warning("No functional sensors, assuming active")
                return True
            return False

        timeout_seconds = self._get_timeout_seconds()

        # --- Tier 1: in-memory timestamp (fast path, normal runtime) ---
        if self._runtime.last_motion_time:
            age = (datetime.now() - self._runtime.last_motion_time).total_seconds()
            if age < timeout_seconds:
                return True

        # --- Tier 2: live device state from Hubitat (Groovy: currentValue) ---
        # Queries each functional sensor's current attributes via the API.
        try:
            for sensor_id in functional:
                device = self.hubitat.get_device(sensor_id)
                if device and 'attributes' in device:
                    for attr in device['attributes']:
                        if attr.get('name') == 'motion' and attr.get('currentValue') == 'active':
                            self.logger.debug(
                                f"Sensor {sensor_id} currently reports motion=active (live)"
                            )
                            return True
        except Exception as e:
            self.logger.warning(f"Failed to check live device state: {e}")

        # --- Tier 3: event history from Hubitat (Groovy: eventsSince) ---
        # Queries recent events within the timeout window. Catches the case
        # where motion went active→inactive recently but is still within
        # the configured timeout period.
        try:
            for sensor_id in functional:
                events = self.hubitat.get_device_events(sensor_id, max_events=20)
                for event in events:
                    if event.get('name') == 'motion' and event.get('value') == 'active':
                        # Parse the event date and check if within timeout
                        event_date_str = event.get('date', '')
                        if event_date_str:
                            try:
                                # Hubitat event dates: "2026-02-23T04:15:30+0000"
                                event_time = datetime.fromisoformat(
                                    event_date_str.replace('+0000', '+00:00')
                                )
                                # Compare in UTC-aware or naive depending on what we get
                                now = datetime.now(event_time.tzinfo) if event_time.tzinfo else datetime.now()
                                age = (now - event_time).total_seconds()
                                if age < timeout_seconds:
                                    self.logger.debug(
                                        f"Sensor {sensor_id} had motion=active "
                                        f"{age:.0f}s ago (within {timeout_seconds}s timeout)"
                                    )
                                    return True
                            except (ValueError, TypeError) as parse_err:
                                self.logger.debug(
                                    f"Could not parse event date '{event_date_str}': {parse_err}"
                                )
        except Exception as e:
            self.logger.warning(f"Failed to check event history: {e}")

        return False

    def _in_exception_state(self) -> bool:
        """Check if we're in an exception state (should not control lights)."""
        # Check illuminance threshold
        if self.get_setting('useIlluminance', False):
            threshold = self.get_setting('illuminanceThreshold', 50)
            current_lux = self._get_current_illuminance()
            if current_lux is not None and current_lux > threshold:
                self.logger.debug(f"Illuminance {current_lux} > threshold {threshold}")
                return True

        # Could add more exceptions here (restricted modes, time windows, etc.)
        return False

    # =========================================================================
    # Light Control
    # =========================================================================

    def _control_lights(self, action: str) -> None:
        """
        Control all switches based on action.

        Groovy compare-and-skip pattern:
        1. Check memo: if memo == action → skip (user may have overridden)
        2. Check actual device state: if already in desired state → skip
           command AND preserve memo (critical for override detection)
        3. Only update memo when app actually sends a command

        Args:
            action: 'on' or 'off'
        """
        switch_ids = self.get_devices('switches')
        # Exclude devices managed by keep-off/keep-on enforcement
        keep_off_ids = set(self.get_devices('keep_off_switches'))
        keep_on_ids = set(self.get_devices('keep_on_switches'))
        memo_dirty = False

        for device_id in switch_ids:
            # Skip devices handled by _enforce_keep_switches()
            if device_id in keep_off_ids or device_id in keep_on_ids:
                continue  # Handled by _enforce_keep_switches()
            device = self.get_device_state(device_id)
            device_name = device.get('device_label', device.get('device_name', device_id)) if device else device_id

            # Memoization check: if app already set this to desired state, skip
            if self._should_skip_due_to_memo(device_name, action):
                continue

            # Send command (checks actual device state before sending)
            if action == 'on':
                changed = self._turn_on_switch(device_id, device_name, device)
            else:
                changed = self._turn_off_switch(device_id, device_name, device)

            if changed:
                memo_dirty = True

        # Batch save: one DB write for all devices instead of per-device
        if memo_dirty:
            self._save_memoization()

    def _device_has_capability(
        self, device: Optional[Dict[str, Any]], capability: str
    ) -> bool:
        """
        Check if a device supports a given capability.

        Args:
            device: Device dict from cache (includes 'capabilities' list)
            capability: Capability name (e.g. 'SwitchLevel', 'ColorTemperature')

        Returns:
            True if device has the capability, False otherwise
        """
        if not device:
            return False
        capabilities = device.get('capabilities', [])
        return capability in capabilities

    def _turn_on_switch(
        self, device_id: str, device_name: str,
        device: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Turn on a switch with appropriate level/color.

        Checks actual device state first. If device is already on,
        skips command AND does not update memo (preserves override detection).
        Only sends setLevel/setColorTemperature if device has the capability.
        Only updates memo if the command is VERIFIED (device state confirmed).

        Returns:
            True if memo was updated (caller should batch-save), False otherwise.
        """
        try:
            # Check cached device state — kept fresh by DeviceCacheRefreshService
            # (background thread polls Matter/API every ~2 min, webhooks update
            # in real-time, commander writes back after verified commands).
            if device:
                actual_state = device.get('attributes', {}).get('switch')
                if actual_state == 'on':
                    self.logger.debug(f"Skip ON {_C}{device_name}{_R}: already on")
                    return False

            self.logger.info(f"Turning on: {_C}{device_name}{_R}")

            use_dim = self.get_setting('useDim', False)
            has_level = self._device_has_capability(device, 'SwitchLevel')

            if use_dim and has_level:
                level = self._get_current_dim_level()
                result = self.send_command(device_id, 'setLevel', [level])
            else:
                result = self.send_command(device_id, 'on')

            if not result.success:
                self.logger.warning(
                    f"Command failed for {device_name}: {result.error}",
                    exc_info=True
                )
                return False

            if not result.verified:
                self.logger.warning(
                    f"Command sent but NOT verified for {device_name}: "
                    f"expected={result.expected_state}, "
                    f"actual={result.actual_state}, "
                    f"retries={result.retries_used}, "
                    f"elapsed={result.elapsed_ms:.0f}ms"
                )
                # DO NOT update memoization for unverified commands —
                # the device may not have actually changed state
                return False

            # Set color only if device supports it (best-effort, no memo impact)
            if self.get_setting('useColor', False):
                self._set_color(device_id, device)

            # Update memo ONLY on VERIFIED command
            self._memoization['switch_state'][device_name] = {'state': 'on', 'source': 'app'}
            if use_dim and has_level:
                self._memoization['dim_level'][device_name] = level
            return True

        except Exception as e:
            self.logger.error(
                f"_turn_on_switch failed for {device_name} "
                f"(id={device_id}): {e}",
                exc_info=True
            )
            return False

    def _turn_off_switch(
        self, device_id: str, device_name: str,
        device: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Turn off a switch.

        Checks actual device state first. If device is already off,
        skips command AND does not update memo (preserves override detection).
        Only updates memo if the command is VERIFIED (device state confirmed).

        Returns:
            True if memo was updated (caller should batch-save), False otherwise.
        """
        try:
            # Check cached device state — kept fresh by DeviceCacheRefreshService
            if device:
                actual_state = device.get('attributes', {}).get('switch')
                if actual_state == 'off':
                    self.logger.debug(f"Skip OFF {_C}{device_name}{_R}: already off")
                    return False

            self.logger.info(f"Turning off: {_C}{device_name}{_R}")
            result = self.send_command(device_id, 'off')

            if not result.success:
                self.logger.warning(
                    f"OFF command failed for {device_name}: {result.error}",
                    exc_info=True
                )
                return False

            if not result.verified:
                self.logger.warning(
                    f"OFF command sent but NOT verified for {device_name}: "
                    f"expected={result.expected_state}, "
                    f"actual={result.actual_state}, "
                    f"retries={result.retries_used}, "
                    f"elapsed={result.elapsed_ms:.0f}ms"
                )
                # DO NOT update memoization for unverified commands
                return False

            # Update memo ONLY on VERIFIED command
            self._memoization['switch_state'][device_name] = {'state': 'off', 'source': 'app'}
            return True

        except Exception as e:
            self.logger.error(
                f"_turn_off_switch failed for {device_name} "
                f"(id={device_id}): {e}",
                exc_info=True
            )
            return False

    def _should_skip_due_to_memo(self, device_name: str, action: str) -> bool:
        """Check if we should skip this device due to memoization."""
        if not self.get_setting('memoize', False):
            return False

        memo_entry = self._memoization.get('switch_state', {}).get(device_name)
        # Handle both new dict format and old string format (backward compat)
        if isinstance(memo_entry, dict):
            memo_state = memo_entry.get('state')
        else:
            memo_state = memo_entry
        if memo_state == action:
            # Already in desired state per our memo
            self.logger.debug(f"Skip {_C}{device_name}{_R}: memo={action}")
            return True

        return False

    def _get_current_dim_level(self) -> int:
        """Get the dim level to use (may be mode-specific)."""
        # TODO: Add mode-specific dim levels
        return self.get_setting('defaultDimLevel', 50)

    def _set_color(
        self, device_id: str, device: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Set color on a device, only if it has the required capability.

        Checks for ColorTemperature or ColorControl capability before sending.
        Color commands are best-effort (verify=False) — they don't affect
        memoization or the overall command success status.
        """
        try:
            preset_name = self.get_setting('colorPreset', 'Warm White')
            has_ct = self._device_has_capability(device, 'ColorTemperature')
            has_color = self._device_has_capability(device, 'ColorControl')

            if preset_name == 'Custom':
                if has_ct:
                    temp = self.get_setting('customColorTemperature', 2700)
                    result = self.send_command(
                        device_id, 'setColorTemperature', [temp],
                        verify=False
                    )
                    if not result.success:
                        self.logger.warning(
                            f"setColorTemperature failed for {device_id}: "
                            f"{result.error}"
                        )
            elif preset_name in self.COLOR_PRESETS:
                preset = self.COLOR_PRESETS[preset_name]
                if 'temperature' in preset and has_ct:
                    result = self.send_command(
                        device_id, 'setColorTemperature',
                        [preset['temperature']], verify=False
                    )
                    if not result.success:
                        self.logger.warning(
                            f"setColorTemperature failed for {device_id}: "
                            f"{result.error}"
                        )
                elif 'hue' in preset and has_color:
                    result = self.send_command(
                        device_id, 'setColor',
                        [f"{{'hue':{preset['hue']},'saturation':{preset['saturation']}}}"],
                        verify=False
                    )
                    if not result.success:
                        self.logger.warning(
                            f"setColor failed for {device_id}: {result.error}"
                        )
        except Exception as e:
            self.logger.error(
                f"_set_color failed for device {device_id}: {e}",
                exc_info=True
            )

    def _get_current_illuminance(self) -> Optional[int]:
        """Get current illuminance reading."""
        sensor_ids = self.get_devices('illuminance_sensor')
        if not sensor_ids:
            return None

        device = self.get_device_state(sensor_ids[0])
        if device:
            attrs = device.get('attributes', {})
            lux = attrs.get('illuminance')
            if lux is not None:
                try:
                    return int(lux)
                except (ValueError, TypeError):
                    pass
        return None

    # =========================================================================
    # Always Off / Always On Enforcement
    # =========================================================================

    def _enforce_keep_switches(self) -> None:
        """
        Enforce always-off and always-on switch states.

        Called on every master() cycle. Uses source tracking in memoization
        to determine WHO changed a device and whether to enforce.

        Sources and enforcement rules:
        +----------+----------------------------------------------------+
        | Source   | Action                                             |
        +----------+----------------------------------------------------+
        | 'app'    | ENFORCE (app or Update seeded expected state)      |
        | 'pause'  | ENFORCE (pause/resume is app-initiated)            |
        | 'manual' | SKIP (user turned it on/off via physical/Hubitat)  |
        | 'unknown'| SKIP (no info, conservative)                       |
        +----------+----------------------------------------------------+

        Always-Off example flow:
          1. Update → memo seeded: {'state': 'off', 'source': 'app'}
          2. User turns on → webhook → memo: {'state': 'on', 'source': 'manual'}
          3. Next cycle → source='manual' → SKIP (user override)
          4. Update again → memo re-seeded: {'state': 'off', 'source': 'app'}
          5. Run → source='app', device=ON → FORCE OFF

        Mode changes / Update reset memo and re-seed expected states,
        clearing all overrides.
        """
        memo = self._memoization or {}
        switch_memo = memo.get('switch_state', {})
        current_mode = self._get_current_mode()

        # Safety: a device in both keep_off AND keep_on is a config error.
        # keep_off wins — exclude conflicts from keep_on.
        keep_off_ids = set(self.get_devices('keep_off_switches'))
        keep_on_ids = set(self.get_devices('keep_on_switches')) - keep_off_ids

        # Mode gate: empty list = all modes, non-empty = only listed modes
        keep_off_modes = self.get_setting('keepOffModes', [])
        enforce_off = not keep_off_modes or current_mode in keep_off_modes
        if not enforce_off:
            self.logger.debug(
                f"Always-Off: skipped (mode={current_mode},"
                f" active={keep_off_modes})"
            )

        for device_id in self.get_devices('keep_off_switches'):
            if not enforce_off:
                break
            try:
                live_device = self.hubitat.get_device(device_id)
                if not live_device:
                    continue
                device_name = self._extract_device_name(live_device, device_id)
                actual = self._extract_switch_state(live_device)
                key = f"keep:{device_id}"
                if actual == 'on':
                    device_entry = switch_memo.get(key)
                    if isinstance(device_entry, dict):
                        source = device_entry.get('source', 'unknown')
                    else:
                        source = 'unknown'
                    if source in ('manual', 'unknown'):
                        self.logger.debug(
                            f"Always-Off: {_C}{device_name}{_R} ON,"
                            f" source={source} — respecting override"
                        )
                        continue
                    self.logger.info(
                        f"Always-Off: {_C}{device_name}{_R} → off"
                        f" (source={source})"
                    )
                    self.send_command(device_id, 'off', verify=False)
                    self._memoization.setdefault('switch_state', {})[key] = {
                        'state': 'off', 'source': 'app'
                    }
                    self._save_memoization()
            except Exception as e:
                self.logger.error(
                    f"Always-Off failed for {device_id}: {e}",
                    exc_info=True
                )

        keep_on_modes = self.get_setting('keepOnModes', [])
        enforce_on = not keep_on_modes or current_mode in keep_on_modes
        if not enforce_on:
            self.logger.debug(
                f"Always-On: skipped (mode={current_mode},"
                f" active={keep_on_modes})"
            )

        for device_id in keep_on_ids:
            if not enforce_on:
                break
            try:
                live_device = self.hubitat.get_device(device_id)
                if not live_device:
                    continue
                device_name = self._extract_device_name(live_device, device_id)
                actual = self._extract_switch_state(live_device)
                key = f"keep:{device_id}"
                if actual == 'off':
                    device_entry = switch_memo.get(key)
                    if isinstance(device_entry, dict):
                        source = device_entry.get('source', 'unknown')
                    else:
                        source = 'unknown'
                    if source in ('manual', 'unknown'):
                        self.logger.debug(
                            f"Always-On: {_C}{device_name}{_R} OFF,"
                            f" source={source} — respecting override"
                        )
                        continue
                    self.logger.info(
                        f"Always-On: {_C}{device_name}{_R} → on"
                        f" (source={source})"
                    )
                    self.send_command(device_id, 'on', verify=False)
                    self._memoization.setdefault('switch_state', {})[key] = {
                        'state': 'on', 'source': 'app'
                    }
                    self._save_memoization()
            except Exception as e:
                self.logger.error(
                    f"Always-On failed for {device_id}: {e}",
                    exc_info=True
                )

    @staticmethod
    def _extract_switch_state(device_data: Dict[str, Any]) -> Optional[str]:
        """
        Extract switch state from Hubitat API response.

        Hubitat returns attributes as a list:
            [{"name": "switch", "currentValue": "on"}, ...]
        Cache stores them as a dict:
            {"switch": "on", ...}
        This handles both formats.
        """
        attrs = device_data.get('attributes', {})
        if isinstance(attrs, list):
            for attr in attrs:
                if attr.get('name') == 'switch':
                    return attr.get('currentValue')
        elif isinstance(attrs, dict):
            return attrs.get('switch')
        return None

    @staticmethod
    def _extract_device_name(
        device_data: Dict[str, Any], fallback: str = ''
    ) -> str:
        """Extract human-readable device name from Hubitat API or cache data."""
        return (
            device_data.get('label')
            or device_data.get('device_label')
            or device_data.get('name')
            or device_data.get('device_name')
            or fallback
        )

    def _resolve_device_name(self, device_id: str) -> str:
        """Get device name from cache without hitting live API."""
        device = self.get_device_state(device_id)
        if device:
            return device.get('device_label', device.get('device_name', device_id))
        return device_id

    def _get_current_mode(self) -> Optional[str]:
        """Get current Hubitat location mode name."""
        try:
            modes = self.hubitat.get_modes()
            if modes:
                for mode in modes:
                    if mode.get('active'):
                        return mode.get('name')
        except Exception as e:
            self.logger.warning(f"Failed to get current mode: {e}", exc_info=True)
        return None

    # =========================================================================
    # Timeout Calculation
    # =========================================================================

    def _get_timeout_seconds(self) -> int:
        """Get timeout in seconds based on settings and current mode."""
        timeout = self.get_setting('noMotionTime', 5)
        time_unit = self.get_setting('timeUnit', 'minutes')

        if time_unit == 'minutes':
            timeout *= 60

        # TODO: Add mode-specific timeouts
        return timeout

    def _schedule_next_run(self) -> None:
        """Schedule the next master() call."""
        timeout = self._get_timeout_seconds()
        self.schedule_timeout(timeout)

    # =========================================================================
    # Health Check
    # =========================================================================

    def _health_check(self) -> None:
        """Check sensor health and enforce keep-on/keep-off (called periodically)."""
        try:
            motion_ids = self.get_devices('motion_sensors')

            for device_id in motion_ids:
                # Check if sensor has been heard from recently
                is_functional = self._functional_sensors.get(device_id, False)

                if not is_functional:
                    self.logger.warning(
                        f"Sensor {device_id} may be unresponsive"
                    )

            # Periodic keep-on/keep-off enforcement
            if not self.is_paused:
                self._enforce_keep_switches()
        except Exception as e:
            self.logger.error(
                f"_health_check failed for instance {self.label}: {e}",
                exc_info=True
            )

    # =========================================================================
    # Settings Schema
    # =========================================================================

    @classmethod
    def get_settings_schema(cls) -> Dict[str, Any]:
        """Return JSON Schema for settings validation."""
        return {
            "type": "object",
            "properties": {
                "memoize": {
                    "type": "boolean",
                    "title": "Memoization",
                    "description": "Remember manual switch changes to avoid conflicts",
                    "default": False
                },
                "useDim": {
                    "type": "boolean",
                    "title": "Enable Dimming",
                    "description": "Set dim level when turning on lights",
                    "default": False
                },
                "defaultDimLevel": {
                    "type": "integer",
                    "title": "Default Dim Level",
                    "description": "Brightness level (0-100)",
                    "minimum": 0,
                    "maximum": 100,
                    "default": 50
                },
                "useColor": {
                    "type": "boolean",
                    "title": "Enable Color Control",
                    "description": "Set color/temperature when turning on lights",
                    "default": False
                },
                "colorPreset": {
                    "type": "string",
                    "title": "Color Preset",
                    "enum": [
                        "Soft White", "Warm White", "Cool White", "Daylight",
                        "Red", "Green", "Blue", "Yellow", "Purple", "Pink",
                        "Custom"
                    ],
                    "default": "Warm White"
                },
                "customColorTemperature": {
                    "type": "integer",
                    "title": "Custom Color Temperature",
                    "description": "Color temperature in Kelvin",
                    "minimum": 2000,
                    "maximum": 6500,
                    "default": 2700
                },
                "timeUnit": {
                    "type": "string",
                    "title": "Time Unit",
                    "enum": ["seconds", "minutes"],
                    "default": "minutes"
                },
                "noMotionTime": {
                    "type": "integer",
                    "title": "No Motion Timeout",
                    "description": "Time to wait before turning off lights",
                    "minimum": 1,
                    "default": 5
                },
                "useIlluminance": {
                    "type": "boolean",
                    "title": "Enable Illuminance Threshold",
                    "description": "Don't turn on lights if already bright",
                    "default": False
                },
                "illuminanceThreshold": {
                    "type": "integer",
                    "title": "Illuminance Threshold (lux)",
                    "description": "Don't turn on if illuminance above this",
                    "minimum": 0,
                    "default": 50
                },
                "considerActiveWhenFail": {
                    "type": "boolean",
                    "title": "Treat Sensor Failure as Active",
                    "description": "If all sensors fail, assume motion active (conservative)",
                    "default": False
                },
                "buttonEventType": {
                    "type": "string",
                    "title": "Button Event Type",
                    "description": "Which button action triggers pause/resume",
                    "enum": ["held", "pushed", "doubleTapped"],
                    "default": "held"
                },
                "pauseDuration": {
                    "type": "integer",
                    "title": "Pause Duration",
                    "description": "How long to pause when button pressed",
                    "minimum": 1,
                    "default": 60
                },
                "pauseDurationUnit": {
                    "type": "string",
                    "title": "Pause Duration Unit",
                    "enum": ["Minutes", "Hours"],
                    "default": "Minutes"
                },
                "pauseSwitchAction": {
                    "type": "string",
                    "title": "Pause Switch Action",
                    "description": "What to do with pause switches when pausing (reverses on resume)",
                    "enum": ["toggle", "on", "off"],
                    "default": "toggle"
                },
                "keepOffModes": {
                    "type": "array",
                    "title": "Always-Off: Active Modes",
                    "description": "Modes where Always-Off is enforced. Empty = all modes.",
                    "items": {"type": "string"},
                    "default": []
                },
                "keepOnModes": {
                    "type": "array",
                    "title": "Always-On: Active Modes",
                    "description": "Modes where Always-On is enforced. Empty = all modes.",
                    "items": {"type": "string"},
                    "default": []
                }
            }
        }

    @classmethod
    def get_device_categories(cls) -> List[Dict[str, Any]]:
        """Return device categories for wizard."""
        return [
            {
                "key": "motion_sensors",
                "label": "Motion Sensors",
                "capability": "motionSensor",
                "multiple": True,
                "required": True,
                "description": "Select motion sensors that trigger lighting"
            },
            {
                "key": "switches",
                "label": "Switches to Control",
                "capability": "switch",
                "multiple": True,
                "required": True,
                "description": "Select switches and dimmers to control"
            },
            {
                "key": "illuminance_sensor",
                "label": "Illuminance Sensor",
                "capability": "illuminanceMeasurement",
                "multiple": False,
                "required": False,
                "description": "Optional: Prevent lights if already bright"
            },
            {
                "key": "pause_buttons",
                "label": "Pause/Resume Buttons",
                "capability": "pushableButton",
                "multiple": True,
                "required": False,
                "description": "Optional: Buttons to pause automation"
            },
            {
                "key": "contacts",
                "label": "Contact Sensors",
                "capability": "contactSensor",
                "multiple": True,
                "required": False,
                "description": "Optional: Turn on lights when door opens"
            },
            {
                "key": "pause_switches",
                "label": "Switches to Control on Pause/Resume",
                "capability": "switch",
                "multiple": True,
                "required": False,
                "description": "Optional: Switches to turn on/off when pausing or resuming (can overlap with motion switches)"
            },
            {
                "key": "keep_off_switches",
                "label": "Always Off",
                "capability": "switch",
                "multiple": True,
                "required": False,
                "description": "These switches will always be turned off"
            },
            {
                "key": "keep_on_switches",
                "label": "Always On",
                "capability": "switch",
                "multiple": True,
                "required": False,
                "description": "These switches will always be turned on"
            }
        ]
