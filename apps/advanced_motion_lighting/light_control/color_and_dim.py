"""
Color, color-temperature, and dim-level control.

Handles best-effort color setting after a switch is turned on.
Color commands use verify=False — they don't affect memoization or
overall success status.
"""

from typing import Optional, Dict, Any

from apps.advanced_motion_lighting.constants import COLOR_PRESETS


class ColorAndDimMixin:
    """Mixin: set color/temperature and dim level on capable devices."""

    def _device_has_capability(
        self, device: Optional[Dict[str, Any]], capability: str
    ) -> bool:
        """
        Check if a device supports a given Hubitat capability.

        Args:
            device: Device dict from cache (must include 'capabilities' list)
            capability: Capability name (e.g., 'SwitchLevel', 'ColorTemperature')

        Returns:
            True if the device has the capability, False if absent or device is None
        """
        if not device:
            return False
        return capability in device.get('capabilities', [])

    def _get_current_dim_level(self) -> int:
        """
        Get the dim level to use when turning on lights.

        Currently returns defaultDimLevel from settings.
        TODO: extend to support per-mode dim levels.

        Returns:
            Brightness integer 0-100
        """
        return self.get_setting('defaultDimLevel', 50)

    def _get_current_illuminance(self) -> Optional[int]:
        """
        Get the current illuminance reading from the configured lux sensor.

        Returns:
            Integer lux value, or None if no sensor configured or read fails
        """
        sensor_ids = self.get_devices('illuminance_sensor')
        if not sensor_ids:
            return None

        device = self.get_device_state(sensor_ids[0])
        if device:
            try:
                return int(device.get('attributes', {}).get('illuminance'))
            except (ValueError, TypeError):
                pass
        return None

    def _set_color(
        self, device_id: str, device: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Set color or color temperature on a device after it is turned on.

        Only sends the command if the device has the required capability.
        Uses verify=False — color is best-effort and does not affect memo.

        Args:
            device_id: Hubitat device ID
            device: Cached device dict (used for capability check)
        """
        try:
            preset_name = self.get_setting('colorPreset', 'Warm White')
            has_ct = self._device_has_capability(device, 'ColorTemperature')
            has_color = self._device_has_capability(device, 'ColorControl')

            if preset_name == 'Custom':
                if has_ct:
                    temp = self.get_setting('customColorTemperature', 2700)
                    result = self.send_command(
                        device_id, 'setColorTemperature', [temp], verify=False
                    )
                    if not result.success:
                        self.logger.warning(
                            f"setColorTemperature failed for {device_id}: {result.error}"
                        )
                return

            preset = COLOR_PRESETS.get(preset_name)
            if not preset:
                return

            if 'temperature' in preset and has_ct:
                result = self.send_command(
                    device_id, 'setColorTemperature',
                    [preset['temperature']], verify=False
                )
                if not result.success:
                    self.logger.warning(
                        f"setColorTemperature failed for {device_id}: {result.error}"
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
                f"_set_color failed for device {device_id}: {e}", exc_info=True
            )
