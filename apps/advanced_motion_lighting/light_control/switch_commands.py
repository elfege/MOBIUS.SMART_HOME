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

        Behavior matrix:
          device OFF                                    → send 'on' / 'setLevel'
          device ON  + useDim + level mismatch          → send 'setLevel' (re-level)
          device ON  + useDim + level matches target    → skip (no command, no memo)
          device ON  + useDim disabled OR no SwitchLevel → skip (no command, no memo)

        The "re-level when already on" branch was added 2026-06-17 to fix the
        modeDimLevels bug: when a mode change clears memo and master() re-runs,
        lights that are already on were short-circuiting at the device-state
        check and never picking up the new mode's dim level. The skip path is
        still required for non-dimming and matched-level cases so we don't
        clobber a manual override or fire pointless commands.

        Only updates memo if the command is VERIFIED by DeviceCommander.

        Args:
            device_id: Hubitat device ID
            device_name: Human-readable name for log output
            device: Cached device dict (for state check and capability check)

        Returns:
            True if memo was updated (caller should batch-save), False otherwise
        """
        try:
            use_dim = self.get_setting('useDim', False)
            has_level = self._device_has_capability(device, 'SwitchLevel')

            # Resolve target_level early — needed for both the off->on path and
            # the already-on re-level decision.
            target_level: Optional[int] = None
            if use_dim and has_level:
                target_level = self._get_current_dim_level(device_name)

            already_on = bool(
                device and device.get('attributes', {}).get('switch') == 'on'
            )

            if already_on:
                # Re-level path: device is already on, but if useDim is enabled
                # and the current level differs from target, push setLevel.
                # This fixes the modeDimLevels-not-applying bug where mode
                # changes never propagated to lights that were already lit.
                if target_level is not None:
                    current_level_raw = device.get('attributes', {}).get('level')
                    try:
                        current_level: Optional[int] = (
                            int(current_level_raw)
                            if current_level_raw is not None else None
                        )
                    except (ValueError, TypeError):
                        current_level = None

                    if current_level != target_level:
                        self.logger.info(
                            f"Re-leveling already-on {_C}{device_name}{_R}: "
                            f"current={current_level} → target={target_level}"
                        )
                        result = self.send_command(
                            device_id, 'setLevel', [target_level]
                        )
                        if not result.success:
                            self.logger.warning(
                                f"setLevel re-level failed for {device_name}: "
                                f"{result.error}"
                            )
                            return False
                        if not result.verified:
                            self.logger.warning(
                                f"setLevel re-level NOT verified for {device_name}: "
                                f"expected={result.expected_state}, "
                                f"actual={result.actual_state}, "
                                f"retries={result.retries_used}, "
                                f"elapsed={result.elapsed_ms:.0f}ms"
                            )
                            return False
                        # Verified — record applied level
                        self._memoization['dim_level'][device_name] = {
                            'level': target_level, 'source': 'app'
                        }
                        return True

                # Either useDim is off, no SwitchLevel cap, or level already
                # matches target. Preserve override detection accuracy: no
                # command, no memo update.
                self.logger.debug(f"Skip ON {_C}{device_name}{_R}: already on")
                return False

            # Device is OFF — normal turn-on flow.
            self.logger.info(f"Turning on: {_C}{device_name}{_R}")

            if use_dim and has_level:
                result = self.send_command(device_id, 'setLevel', [target_level])
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
                self._memoization['dim_level'][device_name] = {
                    'level': target_level, 'source': 'app'
                }
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
