"""
Color, color-temperature, and dim-level control.

Dim level and color are both memoized per device:
  - dim_level[device_name]  = {'level': int, 'source': 'app'|'manual'}
  - color_state[device_name] = {'type': 'temperature'|'color', 'value': int,
                                 'source': 'app'|'manual'}
                             or {'type': 'color', 'hue': int, 'saturation': int,
                                 'source': 'app'|'manual'}

source='manual' entries come from _handle_level_color() when Hubitat fires
a level/colorTemperature event that wasn't triggered by our own command.
These persist until mode change, pause, or resume resets the memo.
"""

from typing import Optional, Dict, Any

from apps.advanced_motion_lighting.constants import COLOR_PRESETS, _C, _R


class ColorAndDimMixin:
    """Mixin: set color/temperature and dim level on capable devices, with memo."""

    def _device_has_capability(
        self, device: Optional[Dict[str, Any]], capability: str
    ) -> bool:
        """
        Check if a device supports a given Hubitat capability.

        Args:
            device: Device dict from cache (must include 'capabilities' list)
            capability: Capability name (e.g., 'SwitchLevel', 'ColorTemperature')

        Returns:
            True if device has the capability, False if absent or device is None
        """
        if not device:
            return False
        return capability in device.get('capabilities', [])

    def _get_current_dim_level(self, device_name: str = '') -> int:
        """
        Get the dim level to use when turning on a device.

        Priority:
          1. Memoized manual override for this device (source='manual')
          2. defaultDimLevel from settings

        Args:
            device_name: Device name key in dim_level memo

        Returns:
            Brightness integer 0-100
        """
        if device_name:
            entry = self._memoization.get('dim_level', {}).get(device_name)
            if isinstance(entry, dict) and entry.get('source') == 'manual':
                level = entry.get('level')
                if level is not None:
                    self.logger.debug(
                        f"Using memoized dim level for {_C}{device_name}{_R}: {level}"
                    )
                    return int(level)
        return self.get_setting('defaultDimLevel', 50)

    def _get_current_color(self, device_name: str = '') -> Optional[Dict[str, Any]]:
        """
        Get the memoized color state for a device, if a manual override exists.

        Returns the color dict only when source='manual' (user set it explicitly).
        App-set colors are ignored — they always re-apply from settings.

        Args:
            device_name: Device name key in color_state memo

        Returns:
            Color dict {'type': 'temperature', 'value': int} or
                       {'type': 'color', 'hue': int, 'saturation': int}
            or None if no manual override exists
        """
        if not device_name:
            return None
        entry = self._memoization.get('color_state', {}).get(device_name)
        if isinstance(entry, dict) and entry.get('source') == 'manual':
            self.logger.debug(
                f"Using memoized color for {_C}{device_name}{_R}: {entry}"
            )
            return entry
        return None

    def _get_current_illuminance(self) -> Optional[int]:
        """
        Get current illuminance reading from the configured lux sensor.

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
        self,
        device_id: str,
        device_name: str = '',
        device: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Set color or color temperature on a device after it is turned on.

        Priority:
          1. Memoized manual override for this device (source='manual')
          2. colorPreset / customColorTemperature from settings

        Uses verify=False — color commands are best-effort.

        Args:
            device_id:   Hubitat device ID
            device_name: Used to look up color memo
            device:      Cached device dict (capability check)

        Returns:
            Color dict that was applied (for memo write by caller), or None
        """
        try:
            has_ct = self._device_has_capability(device, 'ColorTemperature')
            has_color = self._device_has_capability(device, 'ColorControl')

            # --- Priority 1: memoized manual override ---
            memo_color = self._get_current_color(device_name)
            if memo_color:
                return self._apply_color_dict(device_id, memo_color, has_ct, has_color)

            # --- Priority 2: settings preset ---
            preset_name = self.get_setting('colorPreset', 'Warm White')

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
                        return None
                    return {'type': 'temperature', 'value': temp, 'source': 'app'}
                return None

            preset = COLOR_PRESETS.get(preset_name)
            if not preset:
                return None

            if 'temperature' in preset and has_ct:
                color_dict = {'type': 'temperature', 'value': preset['temperature'], 'source': 'app'}
                return self._apply_color_dict(device_id, color_dict, has_ct, has_color)
            elif 'hue' in preset and has_color:
                color_dict = {
                    'type': 'color',
                    'hue': preset['hue'],
                    'saturation': preset['saturation'],
                    'source': 'app',
                }
                return self._apply_color_dict(device_id, color_dict, has_ct, has_color)

        except Exception as e:
            self.logger.error(
                f"_set_color failed for device {device_id}: {e}", exc_info=True
            )
        return None

    def _apply_color_dict(
        self,
        device_id: str,
        color_dict: Dict[str, Any],
        has_ct: bool,
        has_color: bool,
    ) -> Optional[Dict[str, Any]]:
        """
        Send the appropriate Hubitat color command for a color dict.

        Args:
            device_id:  Hubitat device ID
            color_dict: Color specification dict
            has_ct:     Device has ColorTemperature capability
            has_color:  Device has ColorControl capability

        Returns:
            color_dict if command succeeded, None otherwise
        """
        if color_dict.get('type') == 'temperature' and has_ct:
            result = self.send_command(
                device_id, 'setColorTemperature', [color_dict['value']], verify=False
            )
            if result.success:
                return color_dict
            self.logger.warning(
                f"setColorTemperature failed for {device_id}: {result.error}"
            )
        elif color_dict.get('type') == 'color' and has_color:
            result = self.send_command(
                device_id,
                'setColor',
                [f"{{'hue':{color_dict['hue']},'saturation':{color_dict['saturation']}}}"],
                verify=False,
            )
            if result.success:
                return color_dict
            self.logger.warning(
                f"setColor failed for {device_id}: {result.error}"
            )
        return None
