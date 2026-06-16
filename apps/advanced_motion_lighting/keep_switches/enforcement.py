"""
Always-off / always-on switch enforcement.

Called on every master() cycle to ensure designated switches are in
their required states. Uses source tracking in memoization to distinguish
app-initiated states from user manual overrides.

Override policy:
  source='app'     → ENFORCE (app set expected state, or Update re-seeded)
  source='pause'   → ENFORCE (pause/resume is app-initiated)
  source='manual'  → SKIP (user turned it on/off manually — respect override)
  source='unknown' → SKIP (no information — conservative, never force)

Overrides are cleared on: mode change, pause, resume. Not on motion.
"""

from apps.advanced_motion_lighting.constants import _C, _R


class KeepSwitchEnforcementMixin:
    """Mixin: enforce always-off and always-on designated switch states."""

    def _enforce_keep_switches(self) -> None:
        """
        Enforce always-off and always-on switch states on every cycle.

        Mode gating: keepOffModes / keepOnModes settings restrict enforcement
        to specific modes. Empty list = enforce in all modes.

        Conflict resolution: a device in both keep_off AND keep_on is a config
        error — keep_off wins and the device is excluded from keep_on.

        Always-Off flow example:
          1. Update → memo seeded: {'state': 'off', 'source': 'app'}
          2. User turns on → webhook → memo: {'state': 'on', 'source': 'manual'}
          3. Next cycle → source='manual' → SKIP (respect user override)
          4. Mode change / pause / resume → _reset_memoization() → re-seeded
          5. Next cycle → source='app', device=ON → FORCE OFF
        """
        memo = self._memoization or {}
        switch_memo = memo.get('switch_state', {})
        current_mode = self._get_current_mode()

        # Safety: keep_off wins over keep_on for devices in both lists
        keep_off_ids = set(self.get_devices('keep_off_switches'))
        keep_on_ids = set(self.get_devices('keep_on_switches')) - keep_off_ids

        # --- Always-Off enforcement ---
        # Q1=B (operator directive 2026-06-16): the keepOffEnabled toggle
        # is the AUTHORITY for whether this feature runs at all. Empty mode
        # list no longer silently means "all modes" — the toggle must be
        # explicitly true. Migration backfilled toggle=true + filled
        # keepOffModes for existing instances that had keep_off_switches
        # selected, preserving their behavior.
        if not self.get_setting('keepOffEnabled', False):
            self.logger.debug("Always-Off: feature disabled by toggle")
            enforce_off = False
            keep_off_modes = []  # don't bother logging mode breakdown
        else:
            keep_off_modes = self.get_setting('keepOffModes', [])
            enforce_off = not keep_off_modes or current_mode in keep_off_modes
            if not enforce_off:
                self.logger.debug(
                    f"Always-Off: skipped (mode={current_mode}, active={keep_off_modes})"
                )

        for device_id in self.get_devices('keep_off_switches'):
            if not enforce_off:
                break
            try:
                live_device = self.get_device_state_live(device_id)
                if not live_device:
                    continue
                device_name = self._extract_device_name(live_device, device_id)
                actual = self._extract_switch_state(live_device)
                key = f"keep:{device_id}"

                if actual == 'on':
                    device_entry = switch_memo.get(key)
                    source = (
                        device_entry.get('source', 'unknown')
                        if isinstance(device_entry, dict)
                        else 'unknown'
                    )
                    if source in ('manual', 'unknown'):
                        self.logger.debug(
                            f"Always-Off: {_C}{device_name}{_R} ON, "
                            f"source={source} — respecting override"
                        )
                        continue
                    self.logger.info(
                        f"Always-Off: {_C}{device_name}{_R} → off (source={source})"
                    )
                    self.send_command(device_id, 'off', verify=False)
                    self._memoization.setdefault('switch_state', {})[key] = {
                        'state': 'off', 'source': 'app'
                    }
                    self._save_memoization()

            except Exception as e:
                self.logger.error(
                    f"Always-Off failed for {device_id}: {e}", exc_info=True
                )

        # --- Always-On enforcement ---
        # Same toggle contract as Always-Off above.
        if not self.get_setting('keepOnEnabled', False):
            self.logger.debug("Always-On: feature disabled by toggle")
            enforce_on = False
            keep_on_modes = []
        else:
            keep_on_modes = self.get_setting('keepOnModes', [])
            enforce_on = not keep_on_modes or current_mode in keep_on_modes
            if not enforce_on:
                self.logger.debug(
                    f"Always-On: skipped (mode={current_mode}, active={keep_on_modes})"
                )

        for device_id in keep_on_ids:
            if not enforce_on:
                break
            try:
                live_device = self.get_device_state_live(device_id)
                if not live_device:
                    continue
                device_name = self._extract_device_name(live_device, device_id)
                actual = self._extract_switch_state(live_device)
                key = f"keep:{device_id}"

                if actual == 'off':
                    device_entry = switch_memo.get(key)
                    source = (
                        device_entry.get('source', 'unknown')
                        if isinstance(device_entry, dict)
                        else 'unknown'
                    )
                    if source in ('manual', 'unknown'):
                        self.logger.debug(
                            f"Always-On: {_C}{device_name}{_R} OFF, "
                            f"source={source} — respecting override"
                        )
                        continue
                    self.logger.info(
                        f"Always-On: {_C}{device_name}{_R} → on (source={source})"
                    )
                    self.send_command(device_id, 'on', verify=False)
                    self._memoization.setdefault('switch_state', {})[key] = {
                        'state': 'on', 'source': 'app'
                    }
                    self._save_memoization()

            except Exception as e:
                self.logger.error(
                    f"Always-On failed for {device_id}: {e}", exc_info=True
                )
