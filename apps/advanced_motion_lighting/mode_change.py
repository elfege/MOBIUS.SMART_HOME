"""
Location mode change handler.

Groovy parity: modeChangeHandler()

Steps:
  1. Check exclusion modes → auto-pause or auto-resume
  2. Reset memoization (Groovy: resetStates) — clears manual overrides
  3. Cancel pending timeouts (Groovy: unschedule(master))
  4. Re-evaluate state via master()

The new mode's timeout is picked up automatically by _get_timeout_seconds()
when master() calls _schedule_next_run().
"""

import requests as _req


class ModeChangeMixin:
    """Mixin: location mode change with exclusion-mode auto-pause/resume."""

    def on_mode_change(self, new_mode: str) -> None:
        """
        Handle a Hubitat location mode change.

        Overrides the base class default. Adds exclusion-mode auto-pause/resume
        logic before the standard reset + re-evaluate flow.

        Args:
            new_mode: Name of the newly active mode (e.g., 'Home', 'Away')
        """
        self.logger.info(f"Mode changed to: {new_mode}")
        exclusion_modes = self.get_setting('exclusionModes', [])

        if exclusion_modes and new_mode in exclusion_modes:
            # Entering an exclusion mode — auto-pause if not already paused for this reason
            if not self._is_paused or self._pause_reason != 'mode_exclusion':
                self.logger.info(f"Mode '{new_mode}' is in exclusion list → pausing")
                self._is_paused = True
                self._pause_reason = 'mode_exclusion'
                self.cancel_timeout()
                self._patch_pause_state(is_paused=True, reason='mode_exclusion')
            return  # Do not run master() while excluded

        # Leaving an exclusion mode — auto-resume ONLY if THIS specific pause
        # was caused by entering an exclusion mode. Any other pause reason
        # (Button press, ui_button, scheduled, manual, anything user-driven)
        # is LEFT PAUSED. Mode changes must NEVER undo a user-initiated pause —
        # the policy is: only user input can resume a user pause. See user
        # mandate 2026-05-17.
        if self._is_paused and self._pause_reason == 'mode_exclusion':
            self.logger.info(f"Mode '{new_mode}' not in exclusion list → resuming")
            self._is_paused = False
            self._pause_reason = None
            self._patch_pause_state(is_paused=False, reason=None)
        elif self._is_paused:
            # Paused for some OTHER reason — explicitly do NOT touch it.
            self.logger.debug(
                f"Mode '{new_mode}' changed but instance is paused with "
                f"reason={self._pause_reason!r}; leaving paused"
            )
            return  # Do not run master() — pause means pause.

        # Normal mode change: reset memoization, cancel timeouts, re-evaluate
        self._reset_memoization()
        self.cancel_timeout()
        self.master()

    def _patch_pause_state(self, is_paused: bool, reason) -> None:
        """
        Directly PATCH pause state to the database without going through instance_manager.

        Direct DB write avoids the callback loop that instance_manager.pause_instance()
        would create (it calls app.pause() which calls _reset_memoization() again).

        Args:
            is_paused: New pause state
            reason: Pause reason string or None
        """
        try:
            _req.patch(
                f"{self.instance_manager.postgrest_url}/app_instances",
                params={"id": f"eq.{self.instance_id}"},
                json={"is_paused": is_paused, "pause_reason": reason},
                headers={"Content-Type": "application/json"},
                timeout=5
            )
        except Exception as e:
            self.logger.warning(f"Failed to update pause state in DB: {e}")
