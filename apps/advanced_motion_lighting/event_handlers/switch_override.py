"""
Switch event handler — manual override detection.

Only keep_off and keep_on devices are tracked here. Regular switches are
not memoized on switch events (memoization happens on verified commands).

Override detection logic:
  keep_off + event='on'  → user contradicts expected OFF → source='manual'
  keep_off + event='off' → echo of our own command → ignore
  keep_on  + event='off' → user contradicts expected ON  → source='manual'
  keep_on  + event='on'  → echo of our own command → ignore

UPDATING guard: if DeviceCommander has an in-flight command for this device
(status=UPDATING), the event is almost certainly an echo of that command
(Hubitat or Matter confirmation). Skip override detection entirely.
See docs/dual_command_flow.html for the timing diagram.
"""

from apps.advanced_motion_lighting.constants import _C, _R


class SwitchOverrideMixin:
    """Mixin: detect and memoize user manual overrides of keep switches."""

    def _handle_switch(self, event) -> None:
        """
        Process a switch event for override detection.

        Only records a manual override when the event CONTRADICTS the device's
        expected state (i.e., the user flipped it the "wrong" way). Echo events
        (device confirming our own command) are filtered by the UPDATING guard.

        Args:
            event: DeviceEvent with device_id, device_name, value
        """
        self.logger.debug(f"Switch: {_C}{event.device_name}{_R} → {event.value}")

        # UPDATING guard: skip if DeviceCommander is executing a command for this device
        try:
            from services.device_commander import get_device_commander, CommandStatus
            commander = get_device_commander()
            if commander.get_device_status(str(event.device_id)) == CommandStatus.UPDATING:
                self.logger.debug(
                    f"Switch event for {_C}{event.device_name}{_R} "
                    f"(id:{event.device_id}) suppressed — command in-flight (UPDATING)"
                )
                return
        except Exception as e:
            # Can't check UPDATING guard — proceed with normal logic (safe fallback)
            self.logger.debug(f"Could not check UPDATING guard for {event.device_id}: {e}")

        keep_off_ids = set(self.get_devices('keep_off_switches'))
        keep_on_ids = set(self.get_devices('keep_on_switches'))

        # keep_off + event='on'  → user turned on a device that must stay off
        is_keep_off_override = event.device_id in keep_off_ids and event.value == 'on'
        # keep_on + event='off'  → user turned off a device that must stay on
        is_keep_on_override = event.device_id in keep_on_ids and event.value == 'off'

        if not (is_keep_off_override or is_keep_on_override):
            return  # Not a keep-device contradiction — nothing to memo

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
