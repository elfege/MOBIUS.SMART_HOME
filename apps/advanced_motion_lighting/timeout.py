"""
Timeout calculation and next-run scheduling.

Groovy parity: getTimeout() — supports a default timeout and optional
per-mode overrides when timeWithMode is enabled.

2026-05-17: added a SYSTEM-LEVEL FLOOR via system_settings cascade. PIR
sensors typically have 10-60s re-trigger cooldown; setting an app timeout
below that causes off/on flicker (e.g., Kitchen Night=5s incident).
The floor (default 60s) clamps the result of this function unless the
instance has `bypassTimeoutFloor=true` in its settings.
"""


class TimeoutMixin:
    """Mixin: compute motion timeout and schedule the next master() call."""

    def _get_timeout_seconds(self) -> int:
        """
        Get the no-motion timeout in seconds for the current mode.

        Logic:
          1. Start with noMotionTime (default: 5)
          2. If timeWithMode enabled, look up modeTimeouts[current_mode]
          3. Convert to seconds using timeUnit ('seconds' or 'minutes')
          4. Clamp to the system-level floor (motion_timeout_floor_seconds)
             unless this instance has bypassTimeoutFloor=true

        Returns:
            Timeout in seconds (after floor clamp)
        """
        timeout = self.get_setting('noMotionTime', 5)
        time_unit = self.get_setting('timeUnit', 'minutes')

        if self.get_setting('timeWithMode', False):
            mode_timeouts = self.get_setting('modeTimeouts', {})
            current_mode = self._get_current_mode()
            if current_mode and current_mode in mode_timeouts:
                mode_timeout = mode_timeouts[current_mode]
                if mode_timeout is not None:
                    self.logger.debug(
                        f"Per-mode timeout for '{current_mode}': {mode_timeout} {time_unit}"
                    )
                    timeout = mode_timeout
                else:
                    self.logger.debug(
                        f"No timeout for mode '{current_mode}', using default: {timeout}"
                    )
            else:
                self.logger.debug(
                    f"Mode '{current_mode}' not in modeTimeouts, using default: {timeout}"
                )

        if time_unit == 'minutes':
            timeout *= 60

        # System-level floor enforcement (cascade tier 3).
        # bypassTimeoutFloor is per-instance (tier 1, requires explicit opt-in
        # via the UI's "I acknowledge" modal). If true, no clamp.
        if not self.get_setting('bypassTimeoutFloor', False):
            try:
                from services.settings_resolver import get_resolver
                floor = get_resolver().get_system('motion_timeout_floor_seconds', 60)
                if isinstance(floor, (int, float)) and floor > 0 and timeout < floor:
                    self.logger.info(
                        f"_get_timeout_seconds: clamping {timeout}s → {floor}s "
                        f"(motion_timeout_floor_seconds); set bypassTimeoutFloor=true "
                        f"to disable"
                    )
                    timeout = int(floor)
            except Exception as e:
                # Never block automation on a resolver/DB failure
                self.logger.warning(
                    f"_get_timeout_seconds: floor lookup failed, using raw "
                    f"{timeout}s: {e}"
                )

        self.logger.debug(f"_get_timeout_seconds() → {timeout}s")
        return timeout

    def _schedule_next_run(self) -> None:
        """Schedule master() to run again after the configured timeout."""
        timeout = self._get_timeout_seconds()
        self.schedule_timeout(timeout)
