"""
AML-specific memoization initialization.

Extends the base _reset_memoization() to re-seed keep-device entries after
clearing. Seeds use setdefault so they never overwrite existing entries.

Key format: keep:{device_id} — uses device_id (not name) to guarantee
consistency across webhook events, cache, and live API lookups.
"""


class AMLMemoizationMixin:
    """Mixin: AML memoization key seeding and reset override."""

    def _init_memoization_keys(self) -> None:
        """
        Ensure memoization has required keys and seed keep devices.

        Called at __init__ time and after every _reset_memoization().
        Uses setdefault — never overwrites existing entries.

        keep_off devices get seeded as: {'state': 'off', 'source': 'app'}
        keep_on  devices get seeded as: {'state': 'on',  'source': 'app'}

        The 'app' source tells _enforce_keep_switches() to enforce immediately
        on the next cycle (as opposed to 'manual' which means skip).
        """
        self._memoization.setdefault('switch_state', {})
        self._memoization.setdefault('dim_level', {})
        self._memoization.setdefault('color_state', {})

        switch_state = self._memoization['switch_state']

        for device_id in self.get_devices('keep_off_switches'):
            key = f"keep:{device_id}"
            if key not in switch_state:
                switch_state[key] = {'state': 'off', 'source': 'app'}

        for device_id in self.get_devices('keep_on_switches'):
            key = f"keep:{device_id}"
            if key not in switch_state:
                switch_state[key] = {'state': 'on', 'source': 'app'}

    def _reset_memoization(self) -> None:
        """
        Reset all memoization and re-seed keep-device entries.

        Calls base class first (clears dict + persists empty state),
        then re-seeds keep devices so enforcement kicks in immediately.

        Called on: mode change, pause, resume. Not on motion events.
        """
        super()._reset_memoization()
        self._init_memoization_keys()

    def resume(self) -> None:
        """
        Resume from paused state.

        Overrides base class to ensure _enforce_keep_switches() runs immediately
        via master(). The override chain:
          _reset_memoization() → _init_memoization_keys() → source='app'
          master() → _enforce_keep_switches() → forces keep devices to state

        Args: none (called by instance_manager.resume_instance())
        """
        self._is_paused = False
        self.logger.info("Resumed")

        # Cancel any pending auto-resume job
        if self._runtime.auto_resume_job_id:
            from services.scheduler_service import get_scheduler
            scheduler = get_scheduler()
            scheduler.cancel(self._runtime.auto_resume_job_id)
            self._runtime.auto_resume_job_id = None

        # Reset + re-seed (seeds keep devices with source='app')
        self._reset_memoization()

        # master() enforces keep switches and re-evaluates motion state
        self.master()
