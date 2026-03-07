"""
Timeout calculation and next-run scheduling.

Groovy parity: getTimeout() — supports a default timeout and optional
per-mode overrides when timeWithMode is enabled.
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

        Groovy parity: getTimeout() + per-mode settings["noMotionTime_${mode}"]

        Returns:
            Timeout in seconds
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

        self.logger.debug(f"_get_timeout_seconds() → {timeout}s")
        return timeout

    def _schedule_next_run(self) -> None:
        """Schedule master() to run again after the configured timeout."""
        timeout = self._get_timeout_seconds()
        self.schedule_timeout(timeout)
