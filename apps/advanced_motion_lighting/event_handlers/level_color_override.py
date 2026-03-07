"""
Manual dim-level and color override detection.

When Hubitat fires a 'level' or 'colorTemperature' event for a managed switch
and that event is NOT an echo of our own command (UPDATING guard), we treat it
as a user manual override and memo it with source='manual'.

These overrides are used on the next on-cycle by:
  _get_current_dim_level(device_name)  — returns memoized level
  _set_color(device_id, device_name)   — uses memoized color dict

Overrides expire on: mode change, pause, resume. Not on motion.
"""

from apps.advanced_motion_lighting.constants import _C, _R


class LevelColorOverrideMixin:
    """Mixin: detect and memo manual dim-level and color-temperature overrides."""

    def _handle_level_color(self, event) -> None:
        """
        Process a level or colorTemperature event from a managed switch.

        Applies the UPDATING guard first. If the guard passes, the event is a
        genuine user action and is memoized as source='manual'.

        Args:
            event: DeviceEvent with event_type ('level' or 'colorTemperature'),
                   device_id, device_name, value
        """
        # Only care about managed switches
        managed_ids = set(self.get_devices('switches'))
        if event.device_id not in managed_ids:
            return

        # UPDATING guard: skip if DeviceCommander is executing a command for this device
        try:
            from services.device_commander import get_device_commander, CommandStatus
            commander = get_device_commander()
            if commander.get_device_status(str(event.device_id)) == CommandStatus.UPDATING:
                self.logger.debug(
                    f"{event.event_type} event for {_C}{event.device_name}{_R} "
                    f"suppressed — command in-flight (UPDATING)"
                )
                return
        except Exception as e:
            self.logger.debug(f"Could not check UPDATING guard for {event.device_id}: {e}")

        device_name = event.device_name or self._resolve_device_name(event.device_id)

        if event.event_type == 'level':
            self._memo_dim_override(device_name, event.value)
        elif event.event_type == 'colorTemperature':
            self._memo_color_temp_override(device_name, event.value)

    def _memo_dim_override(self, device_name: str, value) -> None:
        """
        Record a manual dim-level change in memoization.

        Args:
            device_name: Device name key in dim_level memo
            value: Raw level value from Hubitat event (string or int)
        """
        try:
            level = int(value)
        except (ValueError, TypeError):
            self.logger.debug(f"Could not parse level value '{value}' for {device_name}")
            return

        self._memoization.setdefault('dim_level', {})
        self._memoization['dim_level'][device_name] = {'level': level, 'source': 'manual'}
        self._save_memoization()
        self.logger.info(
            f"Dim override memoized — {_C}{device_name}{_R}: level={level} source=manual"
        )

    def _memo_color_temp_override(self, device_name: str, value) -> None:
        """
        Record a manual color-temperature change in memoization.

        Args:
            device_name: Device name key in color_state memo
            value: Raw colorTemperature value from Hubitat event (string or int)
        """
        try:
            temp = int(value)
        except (ValueError, TypeError):
            self.logger.debug(f"Could not parse colorTemp value '{value}' for {device_name}")
            return

        self._memoization.setdefault('color_state', {})
        self._memoization['color_state'][device_name] = {
            'type': 'temperature', 'value': temp, 'source': 'manual'
        }
        self._save_memoization()
        self.logger.info(
            f"Color override memoized — {_C}{device_name}{_R}: "
            f"colorTemperature={temp}K source=manual"
        )
