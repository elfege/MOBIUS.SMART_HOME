"""
Light control coordinator.

Iterates over configured switches, applies memoization checks, and
delegates per-device on/off to SwitchCommandsMixin. Excludes devices
managed by keep-switch enforcement (they have their own dedicated cycle).

Memo updates are batched: one DB write per _control_lights() call rather
than one write per device.
"""

from apps.advanced_motion_lighting.constants import _C, _R


class LightControllerMixin:
    """Mixin: coordinate motion-based light on/off across all switches."""

    def _control_lights(self, action: str) -> None:
        """
        Turn all configured switches on or off based on motion state.

        Groovy compare-and-skip pattern:
          1. Skip devices in keep_off/keep_on (handled by _enforce_keep_switches)
          2. Memo check: if memo already == action, skip (unless memo is stale)
          3. Stale memo detection: if device actual state contradicts memo, clear
             memo entry and proceed with the command
          4. Send command (checks actual device state first, see SwitchCommandsMixin)
          5. Batch-save memo after all devices processed

        Args:
            action: 'on' or 'off'
        """
        switch_ids = self.get_devices('switches')
        keep_off_ids = set(self.get_devices('keep_off_switches'))
        keep_on_ids = set(self.get_devices('keep_on_switches'))
        memo_dirty = False

        for device_id in switch_ids:
            # Skip keep devices — _enforce_keep_switches() handles them
            if device_id in keep_off_ids or device_id in keep_on_ids:
                continue

            device = self.get_device_state(device_id)
            device_name = (
                device.get('device_label', device.get('device_name', device_id))
                if device else device_id
            )

            # Memo check with stale detection
            if self._should_skip_due_to_memo(device_name, action):
                if device:
                    actual = device.get('attributes', {}).get('switch')
                    if actual is not None and actual != action:
                        # Stale memo — device is in a different state than recorded
                        self.logger.info(
                            f"Stale memo for {_C}{device_name}{_R}: "
                            f"memo='{action}' but device='{actual}' — clearing, proceeding"
                        )
                        self._memoization.get('switch_state', {}).pop(device_name, None)
                        memo_dirty = True
                    else:
                        continue  # Memo is accurate, skip
                else:
                    continue  # No device state, trust memo

            # Send command
            if action == 'on':
                changed = self._turn_on_switch(device_id, device_name, device)
            else:
                changed = self._turn_off_switch(device_id, device_name, device)

            if changed:
                memo_dirty = True

        # Batch save: one DB write for all devices instead of per-device
        if memo_dirty:
            self._save_memoization()

    def _should_skip_due_to_memo(self, device_name: str, action: str) -> bool:
        """
        Return True if memoization indicates this device is already in the target state.

        Only applies when memoize setting is enabled. Handles both dict format
        (new: {'state': 'on', 'source': 'app'}) and legacy string format.

        Args:
            device_name: Device name key in switch_state memo
            action: Target action ('on' or 'off')

        Returns:
            True if memo says device is already in desired state (skip command)
        """
        if not self.get_setting('memoize', False):
            return False

        memo_entry = self._memoization.get('switch_state', {}).get(device_name)
        if isinstance(memo_entry, dict):
            memo_state = memo_entry.get('state')
        else:
            memo_state = memo_entry  # Legacy string format

        if memo_state == action:
            self.logger.debug(f"Skip {_C}{device_name}{_R}: memo={action}")
            return True
        return False
