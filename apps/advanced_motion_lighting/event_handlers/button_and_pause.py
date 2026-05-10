"""
Button event handler and pause-switch control.

Button events always reach this handler even when the instance is paused
(they are the unpause mechanism). Only the configured buttonEventType is
acted on — other event types (pushed vs held vs doubleTapped) are ignored
to prevent double-toggle when Hubitat sends multiple event types for one press.

Per-device debounce against driver retransmits
----------------------------------------------
The Hubitat button driver (Lutron Pico / Zigbee scene controllers) emits
multiple `held=1` events per physical long-press — typically a hold-while-
held retransmit at roughly 2-second cadence. The investigation report at
`docs/README_investigation_lighting_reliability_1746851000.md` (2026-05-10)
verified this is NOT a mesh-mirror leak — all duplicates arrive from the
same native hub with the same canonical id, so the router's mesh-drop has
nothing to filter. The fix is a per-(instance, device) cooldown window
that suppresses duplicates within ~2.5s.
"""

import time

from apps.advanced_motion_lighting.constants import _C, _R

# Cooldown window in seconds. Tuned just above the observed ~2s driver
# retransmit cadence. Legitimate repeated user presses inside this window
# are rare (a button hold is one logical action regardless of how many
# retransmit pulses the driver emits).
_BUTTON_DEBOUNCE_SECS = 2.5


class ButtonAndPauseMixin:
    """Mixin: button-triggered pause/resume and pause-switch actuation."""

    # Per-device cooldown tracker: device_id -> monotonic timestamp of last
    # accepted press. Lazily created in _handle_button so existing instances
    # don't need an __init__ change.
    _button_last_accepted: dict

    def _handle_button(self, event) -> None:
        """
        Handle a button event — toggle pause/resume state.

        Only responds to the configured buttonEventType (default: 'held').
        The pause duration and unit come from settings.

        Args:
            event: DeviceEvent with event_type and device_name
        """
        expected_type = self.get_setting('buttonEventType', 'held')
        if event.event_type != expected_type:
            self.logger.debug(
                f"Button {event.event_type}: {_C}{event.device_name}{_R}"
                f" — ignoring (configured for '{expected_type}')"
            )
            return

        # Suppress driver retransmits within the cooldown window.
        if not hasattr(self, '_button_last_accepted'):
            self._button_last_accepted = {}
        now = time.monotonic()
        last = self._button_last_accepted.get(event.device_id, 0.0)
        elapsed = now - last
        if elapsed < _BUTTON_DEBOUNCE_SECS:
            self.logger.info(
                f"Button {event.event_type}: {_C}{event.device_name}{_R}"
                f" — driver retransmit suppressed "
                f"({elapsed:.2f}s since last accepted press, "
                f"cooldown {_BUTTON_DEBOUNCE_SECS}s)"
            )
            return
        self._button_last_accepted[event.device_id] = now

        self.logger.info(f"Button {event.event_type}: {_C}{event.device_name}{_R}")

        pause_duration = self.get_setting('pauseDuration', 60)
        if self.get_setting('pauseDurationUnit', 'Minutes') == 'Hours':
            pause_duration *= 60

        if self.is_paused:
            try:
                success = self.instance_manager.resume_instance(self.instance_id)
                if success:
                    self._control_pause_switches(resuming=True)
                else:
                    self.logger.error("resume_instance() returned False — pause switches unchanged")
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
                    self.logger.error("pause_instance() returned False — pause switches unchanged")
            except Exception as e:
                self.logger.error(f"Pause failed: {e}")

    def _control_pause_switches(self, resuming: bool) -> None:
        """
        Actuate pause-switch devices when pausing or resuming.

        Pause switches can overlap with motion-controlled and keep switches.
        Each device is handled independently — one failure doesn't block the rest.

        Skip rules (to avoid conflicting with keep enforcement):
          - Resuming → skip keep_off devices (enforcement will handle them)
          - Pausing  → skip keep_on devices (enforcement will handle them)

        Args:
            resuming: True if unpausing, False if pausing
        """
        switch_ids = self.get_devices('pause_switches')
        if not switch_ids:
            return

        action = self.get_setting('pauseSwitchAction', 'toggle')
        keep_off_ids = set(self.get_devices('keep_off_switches'))
        keep_on_ids = set(self.get_devices('keep_on_switches'))

        for device_id in switch_ids:
            try:
                if resuming and device_id in keep_off_ids:
                    continue  # Stay off — enforcement handles it
                if not resuming and device_id in keep_on_ids:
                    continue  # Stay on — enforcement handles it

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
                    f"Failed to control pause switch {device_id}: {e}", exc_info=True
                )
