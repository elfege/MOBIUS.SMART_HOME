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

from apps.base_app import BaseApp
from models.event import DeviceEvent
from services.hubitat_client import HubitatClient


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

        # Initialize memoization maps if not present
        if 'switch_state' not in self._memoization:
            self._memoization['switch_state'] = {}
        if 'dim_level' not in self._memoization:
            self._memoization['dim_level'] = {}

        # Track functional sensors
        self._functional_sensors: Dict[str, bool] = {}

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def initialize(self) -> None:
        """Set up the instance on startup."""
        self.logger.info(f"Initializing: {self.label}")

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

        # Initial state check
        self.master()

    # =========================================================================
    # Event Handling
    # =========================================================================

    def on_event(self, event: DeviceEvent) -> None:
        """Handle device events."""
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

    def _handle_motion(self, event: DeviceEvent) -> None:
        """Handle motion sensor event."""
        # Mark sensor as functional
        self._functional_sensors[event.device_id] = True

        if event.is_motion_active:
            self.logger.debug(f"Motion active: {event.device_name}")
            self._runtime.last_motion_time = datetime.now()
            self.master(motion_active_event=True)
        else:
            self.logger.debug(f"Motion inactive: {event.device_name}")
            # Schedule check for timeout
            timeout = self._get_timeout_seconds()
            self.schedule_timeout(timeout)

    def _handle_switch(self, event: DeviceEvent) -> None:
        """
        Handle switch event.

        NOTE: Memoization is NOT updated from external switch events.
        The Groovy pattern only records memo when the APP sends commands.
        User overrides are detected at command time by comparing memo
        vs desired action: if memo == action, it means "I already did this;
        if the device is now different, user overrode me — skip."
        """
        self.logger.debug(f"Switch event: {event.device_name} -> {event.value}")

    def _handle_illuminance(self, event: DeviceEvent) -> None:
        """Handle illuminance change."""
        self.logger.debug(f"Illuminance: {event.value} lux")
        # Re-evaluate state with new illuminance
        self.master()

    def _handle_button(self, event: DeviceEvent) -> None:
        """
        Handle button event (pause/resume toggle).

        Triggers on pushed, held, or doubleTapped based on buttonEventType setting.
        Also controls pause indicator lights if configured.
        """
        self.logger.info(f"Button {event.event_type}: {event.device_name}")

        pause_duration = self.get_setting('pauseDuration', 60)
        if self.get_setting('pauseDurationUnit', 'Minutes') == 'Hours':
            pause_duration *= 60

        # Toggle pause state
        if self.is_paused:
            self.instance_manager.resume_instance(self.instance_id)
            self._control_pause_lights(resuming=True)
        else:
            self.instance_manager.pause_instance(
                self.instance_id,
                duration_minutes=pause_duration,
                reason='Button press'
            )
            self._control_pause_lights(resuming=False)

    def _control_pause_lights(self, resuming: bool) -> None:
        """
        Control pause indicator lights when pausing/resuming.

        These are separate devices (not the main controlled lights) that
        provide visual feedback when the automation is paused.

        Args:
            resuming: True if resuming (un-pausing), False if pausing
        """
        indicator_ids = self.get_devices('pause_switches')
        if not indicator_ids:
            return

        action = self.get_setting('pauseSwitchAction', 'toggle')

        for device_id in indicator_ids:
            if action == 'toggle':
                # Toggle: read current state and invert
                device = self.get_device_state(device_id)
                if device:
                    current = device.get('attributes', {}).get('switch', 'off')
                    cmd = 'off' if current == 'on' else 'on'
                else:
                    # Can't read state — default: on when pausing, off when resuming
                    cmd = 'off' if resuming else 'on'
                self.send_command(device_id, cmd)
            elif action == 'on':
                # "on" means: turn ON when pausing, OFF when resuming
                self.send_command(device_id, 'off' if resuming else 'on')
            elif action == 'off':
                # "off" means: turn OFF when pausing, ON when resuming
                self.send_command(device_id, 'on' if resuming else 'off')

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

        self._schedule_next_run()

    def _is_motion_active(self) -> bool:
        """
        Check if any motion sensor is currently active.

        Also checks event history for recent motion within timeout period.
        """
        # Check functional sensors
        functional = [
            sensor_id for sensor_id, is_functional in self._functional_sensors.items()
            if is_functional
        ]

        if not functional:
            # No functional sensors - use fail-safe setting
            consider_active = self.get_setting('considerActiveWhenFail', False)
            if consider_active:
                self.logger.warning("No functional sensors, assuming active")
                return True
            return False

        # Check if motion was recent (within timeout)
        timeout_seconds = self._get_timeout_seconds()
        if self._runtime.last_motion_time:
            age = (datetime.now() - self._runtime.last_motion_time).total_seconds()
            if age < timeout_seconds:
                return True

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

        for device_id in switch_ids:
            device = self.get_device_state(device_id)
            device_name = device.get('device_label', device.get('device_name', device_id)) if device else device_id

            # Memoization check: if app already set this to desired state, skip
            if self._should_skip_due_to_memo(device_name, action):
                continue

            # Send command (checks actual device state before sending)
            if action == 'on':
                self._turn_on_switch(device_id, device_name, device)
            else:
                self._turn_off_switch(device_id, device_name, device)

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
    ) -> None:
        """
        Turn on a switch with appropriate level/color.

        Checks actual device state first. If device is already on,
        skips command AND does not update memo (preserves override detection).
        Only sends setLevel/setColorTemperature if device has the capability.
        """
        # Check actual device state — if already on, skip to preserve memo
        if device:
            actual_state = device.get('attributes', {}).get('switch')
            if actual_state == 'on':
                self.logger.debug(f"Skip ON for {device_name}: already on")
                return

        self.logger.info(f"Turning on: {device_name}")

        use_dim = self.get_setting('useDim', False)
        has_level = self._device_has_capability(device, 'SwitchLevel')

        if use_dim and has_level:
            level = self._get_current_dim_level()
            self.send_command(device_id, 'setLevel', [level])
        else:
            self.send_command(device_id, 'on')

        # Set color only if device supports it
        if self.get_setting('useColor', False):
            self._set_color(device_id, device)

        # Update memo ONLY when app actually sends a command
        self._memoization.setdefault('switch_state', {})[device_name] = 'on'
        if use_dim and has_level:
            self._memoization.setdefault('dim_level', {})[device_name] = level
        self._save_memoization()

    def _turn_off_switch(
        self, device_id: str, device_name: str,
        device: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Turn off a switch.

        Checks actual device state first. If device is already off,
        skips command AND does not update memo (preserves override detection).
        """
        # Check actual device state — if already off, skip to preserve memo
        if device:
            actual_state = device.get('attributes', {}).get('switch')
            if actual_state == 'off':
                self.logger.debug(f"Skip OFF for {device_name}: already off")
                return

        self.logger.info(f"Turning off: {device_name}")
        self.send_command(device_id, 'off')

        # Update memo ONLY when app actually sends a command
        self._memoization.setdefault('switch_state', {})[device_name] = 'off'
        self._save_memoization()

    def _should_skip_due_to_memo(self, device_name: str, action: str) -> bool:
        """Check if we should skip this device due to memoization."""
        if not self.get_setting('memoize', False):
            return False

        memo_state = self._memoization.get('switch_state', {}).get(device_name)
        if memo_state == action:
            # Already in desired state per our memo
            self.logger.debug(f"Skipping {device_name}: memoized as {action}")
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
        """
        preset_name = self.get_setting('colorPreset', 'Warm White')
        has_ct = self._device_has_capability(device, 'ColorTemperature')
        has_color = self._device_has_capability(device, 'ColorControl')

        if preset_name == 'Custom':
            if has_ct:
                temp = self.get_setting('customColorTemperature', 2700)
                self.send_command(device_id, 'setColorTemperature', [temp])
        elif preset_name in self.COLOR_PRESETS:
            preset = self.COLOR_PRESETS[preset_name]
            if 'temperature' in preset and has_ct:
                self.send_command(
                    device_id, 'setColorTemperature', [preset['temperature']]
                )
            elif 'hue' in preset and has_color:
                self.send_command(
                    device_id, 'setColor',
                    [f"{{'hue':{preset['hue']},'saturation':{preset['saturation']}}}"]
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
        """Check sensor health (called periodically)."""
        motion_ids = self.get_devices('motion_sensors')

        for device_id in motion_ids:
            # Check if sensor has been heard from recently
            is_functional = self._functional_sensors.get(device_id, False)

            if not is_functional:
                self.logger.warning(f"Sensor {device_id} may be unresponsive")

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
            }
        ]
