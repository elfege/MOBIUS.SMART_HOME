"""
Low-level switch on/off commands.

Each method checks the actual device state before sending — if the device
is already in the target state, the command is skipped AND memo is NOT
updated (preserving override detection accuracy).

Memoization is only updated on VERIFIED commands. Unverified means the
device may not have actually changed, so recording it would produce stale
memo entries that suppress future corrections.
"""

from typing import Optional, Dict, Any

from apps.advanced_motion_lighting.constants import _C, _R


class SwitchCommandsMixin:
    """Mixin: turn_on and turn_off with state-check and memo update."""

    def _turn_on_switch(
        self,
        device_id: str,
        device_name: str,
        device: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Turn on a switch, applying dim level if configured.

        Skips the command if the device is already on (no memo update either).
        Only updates memo if the command is VERIFIED by DeviceCommander.

        Args:
            device_id: Hubitat device ID
            device_name: Human-readable name for log output
            device: Cached device dict (for state check and capability check)

        Returns:
            True if memo was updated (caller should batch-save), False otherwise
        """
        try:
            # Skip if already on — preserves override detection accuracy
            if device:
                if device.get('attributes', {}).get('switch') == 'on':
                    self.logger.debug(f"Skip ON {_C}{device_name}{_R}: already on")
                    return False

            self.logger.info(f"Turning on: {_C}{device_name}{_R}")

            use_dim = self.get_setting('useDim', False)
            has_level = self._device_has_capability(device, 'SwitchLevel')

            if use_dim and has_level:
                level = self._get_current_dim_level(device_name)
                result = self.send_command(device_id, 'setLevel', [level])
            else:
                result = self.send_command(device_id, 'on')

            if not result.success:
                self.logger.warning(
                    f"ON command failed for {device_name}: {result.error}"
                )
                return False

            if not result.verified:
                self.logger.warning(
                    f"ON command sent but NOT verified for {device_name}: "
                    f"expected={result.expected_state}, actual={result.actual_state}, "
                    f"retries={result.retries_used}, elapsed={result.elapsed_ms:.0f}ms"
                )
                # Do NOT update memo — device may not have actually changed
                return False

            # Apply color after confirmed on (best-effort); memo the applied color
            if self.get_setting('useColor', False):
                applied_color = self._set_color(device_id, device_name, device)
                if applied_color:
                    self._memoization['color_state'][device_name] = applied_color

            # Record verified state in memo
            self._memoization['switch_state'][device_name] = {'state': 'on', 'source': 'app'}
            if use_dim and has_level:
                self._memoization['dim_level'][device_name] = {'level': level, 'source': 'app'}
            return True

        except Exception as e:
            self.logger.error(
                f"_turn_on_switch failed for {device_name} (id={device_id}): {e}",
                exc_info=True
            )
            return False

    def _turn_off_switch(
        self,
        device_id: str,
        device_name: str,
        device: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Turn off a switch.

        Skips the command if the device is already off (no memo update either).
        Only updates memo if the command is VERIFIED by DeviceCommander.

        Args:
            device_id: Hubitat device ID
            device_name: Human-readable name for log output
            device: Cached device dict (for state check)

        Returns:
            True if memo was updated (caller should batch-save), False otherwise
        """
        try:
            # Skip if already off — preserves override detection accuracy
            if device:
                if device.get('attributes', {}).get('switch') == 'off':
                    self.logger.debug(f"Skip OFF {_C}{device_name}{_R}: already off")
                    return False

            self.logger.info(f"Turning off: {_C}{device_name}{_R}")
            result = self.send_command(device_id, 'off')

            if not result.success:
                self.logger.warning(
                    f"OFF command failed for {device_name}: {result.error}"
                )
                return False

            if not result.verified:
                self.logger.warning(
                    f"OFF command sent but NOT verified for {device_name}: "
                    f"expected={result.expected_state}, actual={result.actual_state}, "
                    f"retries={result.retries_used}, elapsed={result.elapsed_ms:.0f}ms"
                )
                # Do NOT update memo — device may not have actually changed
                return False

            # Record verified state in memo
            self._memoization['switch_state'][device_name] = {'state': 'off', 'source': 'app'}
            return True

        except Exception as e:
            self.logger.error(
                f"_turn_off_switch failed for {device_name} (id={device_id}): {e}",
                exc_info=True
            )
            return False
