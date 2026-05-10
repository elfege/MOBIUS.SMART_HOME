"""
Pause and resume lifecycle management.

Handles timed and indefinite pauses, auto-resume scheduling, and
memoization reset on state transitions.

Design rule: memoization resets on pause AND resume so manual overrides
never carry forward across a pause/resume cycle.
"""


class PauseResumeMixin:
    """Mixin: pause/resume with optional auto-resume scheduling."""

    def pause(self, duration_minutes: int = 0) -> None:
        """
        Pause the instance.

        Resets memoization (Groovy resetStates pattern) so stale override
        records don't survive into the next active period.
        Schedules auto-resume if duration_minutes > 0.

        Args:
            duration_minutes: How long to pause. 0 = indefinite (manual resume required).
        """
        self._is_paused = True
        self.logger.info(f"Paused for {duration_minutes} minutes")

        # Memoization reset: manual overrides expire on pause
        self._reset_memoization()

        from services.scheduler_service import get_scheduler
        scheduler = get_scheduler()

        # Cancel any pending timeout (lights-off check)
        if self._runtime.timeout_job_id:
            scheduler.cancel(self._runtime.timeout_job_id)

        # Schedule auto-resume if duration is specified
        if duration_minutes > 0:
            job_id = f"auto_resume_{self.instance_id}"
            if self._runtime.auto_resume_job_id:
                scheduler.cancel(self._runtime.auto_resume_job_id)
            scheduler.schedule_once(
                job_id=job_id,
                delay_seconds=duration_minutes * 60,
                callback=lambda **kwargs: self._auto_resume(),
                instance_id=self.instance_id,
                job_type='auto_resume'
            )
            self._runtime.auto_resume_job_id = job_id
            self.logger.info(f"Auto-resume scheduled in {duration_minutes} minutes")

    def _auto_resume(self) -> None:
        """Triggered by the APScheduler job when pause duration expires."""
        self.logger.info("Auto-resume triggered")
        self._runtime.auto_resume_job_id = None
        self.instance_manager.resume_instance(self.instance_id)

    def resume(self) -> None:
        """
        Resume from paused state.

        Resets memoization so the instance starts with clean override state.
        Cancels any pending auto-resume job.
        Calls master() to re-evaluate current device state.

        Subclasses (e.g., AdvancedMotionLightingApp) may override to run
        additional enforcement logic (keep_on/keep_off switches) on resume.
        """
        self._is_paused = False
        self.logger.info("Resumed")

        # Cancel scheduled auto-resume if we're being manually resumed
        if self._runtime.auto_resume_job_id:
            from services.scheduler_service import get_scheduler
            scheduler = get_scheduler()
            scheduler.cancel(self._runtime.auto_resume_job_id)
            self._runtime.auto_resume_job_id = None

        # Memoization reset: manual overrides expire on resume
        self._reset_memoization()

        self.master()

    @property
    def is_paused(self) -> bool:
        """True if this instance is currently paused."""
        return self._is_paused
