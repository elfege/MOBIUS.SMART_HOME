"""
Timeout and job scheduling.

Wraps APScheduler for per-instance timeout management.
Only one timeout job runs at a time per instance — scheduling a new one
cancels any existing one to prevent scheduling leaks.
"""

from datetime import datetime, timezone


class SchedulingMixin:
    """Mixin: schedule, reschedule, and cancel per-instance timeout jobs."""

    def schedule_timeout(
        self,
        delay_seconds: int,
        callback_name: str = 'master'
    ) -> str:
        """
        Schedule a one-shot timeout job.

        Cancels any existing timeout first to prevent multiple jobs
        accumulating for the same instance.

        Args:
            delay_seconds: Seconds until job fires
            callback_name: Name of the method to call (default: 'master')

        Returns:
            New job ID string
        """
        from services.scheduler_service import get_scheduler
        scheduler = get_scheduler()

        if self._runtime.timeout_job_id:
            scheduler.cancel(self._runtime.timeout_job_id)

        job_id = f"timeout_{self.instance_id}_{datetime.now(timezone.utc).timestamp()}"
        callback = getattr(self, callback_name, self.master)

        scheduler.schedule_once(
            job_id=job_id,
            delay_seconds=delay_seconds,
            callback=lambda **kwargs: callback(),
            instance_id=self.instance_id,
            job_type='timeout'
        )

        self._runtime.timeout_job_id = job_id
        return job_id

    def reschedule_timeout(self, delay_seconds: int) -> bool:
        """
        Reschedule the current timeout to a new delay from now.

        If no timeout is currently running, schedules a fresh one.

        Args:
            delay_seconds: New delay from now

        Returns:
            True if rescheduled (or newly scheduled) successfully
        """
        if not self._runtime.timeout_job_id:
            self.schedule_timeout(delay_seconds)
            return True

        from services.scheduler_service import get_scheduler
        scheduler = get_scheduler()
        return scheduler.reschedule(self._runtime.timeout_job_id, delay_seconds)

    def cancel_timeout(self) -> None:
        """Cancel any pending timeout job for this instance."""
        if self._runtime.timeout_job_id:
            from services.scheduler_service import get_scheduler
            scheduler = get_scheduler()
            scheduler.cancel(self._runtime.timeout_job_id)
            self._runtime.timeout_job_id = None
