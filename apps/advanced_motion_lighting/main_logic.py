"""
Main automation logic — master() and exception state checks.

master() is the central decision point. It is called:
  - When motion goes active (motion_active_event=True)
  - When the no-motion timeout expires
  - After a mode change
  - After resume from pause

Exception states prevent light control (illuminance too high, exclusion mode).
"""


class MainLogicMixin:
    """Mixin: master() orchestration and exception state detection."""

    def master(self, motion_active_event: bool = False, **kwargs) -> None:
        """
        Main logic loop — decide and execute device state.

        Decision flow:
          1. Skip if paused
          2. Skip if in exception state (reschedule for next check)
          3. Motion active → turn on lights
          4. No motion → turn off lights
          5. Enforce keep-on/keep-off switches (runs after normal control)
          6. Schedule next run

        Args:
            motion_active_event: True if called directly from a motion-active event.
                                  Bypasses the three-tier motion check (fast path).
        """
        try:
            if self.is_paused:
                return

            if self._in_exception_state():
                self.logger.debug("In exception state — scheduling next check")
                self._schedule_next_run()
                return

            if motion_active_event or self._is_motion_active():
                self._control_lights('on')
            else:
                self._control_lights('off')

            # Enforce keep-on/keep-off AFTER normal motion-based control
            self._enforce_keep_switches()

            self._schedule_next_run()

        except Exception as e:
            self.logger.error(
                f"master() failed for instance {self.label}: {e}", exc_info=True
            )

    def _in_exception_state(self) -> bool:
        """
        Check if we are in a state where light control should be suppressed.

        Groovy parity: exceptions() + InRestrictedModeOrTime()

        Checks:
          - Exclusion mode: current mode is in the exclusionModes list
          - Illuminance threshold: lux is above the configured threshold

        Returns:
            True if light control should be skipped this cycle
        """
        # Exclusion mode check (belt-and-suspenders: on_mode_change also pauses,
        # but this catches startup before the first mode-change event fires)
        exclusion_modes = self.get_setting('exclusionModes', [])
        if exclusion_modes:
            current_mode = self._get_current_mode()
            if current_mode and current_mode in exclusion_modes:
                self.logger.debug(f"In exclusion mode: {current_mode}")
                return True

        # Illuminance threshold check
        if self.get_setting('useIlluminance', False):
            threshold = self.get_setting('illuminanceThreshold', 50)
            current_lux = self._get_current_illuminance()
            if current_lux is not None and current_lux > threshold:
                self.logger.debug(f"Illuminance {current_lux} > threshold {threshold}")
                return True

        return False
