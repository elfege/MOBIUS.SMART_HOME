"""
Scheduler Service

Manages background scheduled tasks using APScheduler. Provides:
- Timeout scheduling (turn off lights after N minutes)
- Health checks
- Pause expiration
- Persistent job storage (survives restarts)

Jobs are stored in PostgreSQL for durability. When the service restarts,
pending jobs are restored from the database.
"""

import os
import logging
import traceback
from datetime import datetime, timedelta
from typing import Callable, Dict, Any, Optional, List
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.executors.pool import ThreadPoolExecutor
import requests


class SchedulerService:
    """
    Background task scheduler with persistent job storage.

    Uses APScheduler for in-memory scheduling with PostgreSQL as
    a backup store for job metadata (enables recovery after restart).

    Job types:
    - 'turn_off': Turn off lights after motion timeout
    - 'pause_expire': Resume instance after pause duration
    - 'health_check': Periodic sensor health check

    Example usage:
        scheduler = SchedulerService()
        scheduler.start()

        # Schedule a one-time job
        scheduler.schedule_once(
            job_id='timeout_123_instance_1',
            delay_seconds=300,
            callback=my_callback_function,
            instance_id=1,
            job_type='turn_off',
            payload={'device_ids': ['456', '789']}
        )

        # Schedule a recurring job
        scheduler.schedule_recurring(
            job_id='health_check_instance_1',
            interval_seconds=300,
            callback=health_check_function,
            instance_id=1
        )

        # Cancel a job
        scheduler.cancel('timeout_123_instance_1')
    """

    def __init__(
        self,
        postgrest_url: str = None,
        max_workers: int = 10,
        misfire_grace_time: int = 60
    ):
        """
        Initialize the scheduler service.

        Args:
            postgrest_url: URL to PostgREST service for job persistence
            max_workers: Maximum concurrent job execution threads
            misfire_grace_time: Seconds a job can be late and still run
        """
        self.postgrest_url = postgrest_url or os.environ.get(
            'POSTGREST_URL', 'http://postgrest:3001'
        )
        self.logger = logging.getLogger(__name__)

        # Configure APScheduler
        jobstores = {
            'default': MemoryJobStore()
        }
        executors = {
            'default': ThreadPoolExecutor(max_workers)
        }
        job_defaults = {
            'coalesce': True,  # Combine missed runs into one
            'max_instances': 3,
            'misfire_grace_time': misfire_grace_time
        }

        self._scheduler = BackgroundScheduler(
            jobstores=jobstores,
            executors=executors,
            job_defaults=job_defaults
        )

        # Track scheduled jobs for instance lookup
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def start(self) -> None:
        """Start the scheduler and restore pending jobs from database."""
        if not self._scheduler.running:
            self._scheduler.start()
            self._restore_pending_jobs()

            # Register a daily cleanup to keep scheduled_jobs table lean.
            # Runs 1 hour after startup, then every 24 hours.
            self._scheduler.add_job(
                func=self._purge_old_records,
                trigger='interval',
                hours=24,
                id='_system_purge_old_jobs',
                replace_existing=True,
                misfire_grace_time=3600,
            )

            self.logger.info("Scheduler started")

    def shutdown(self, wait: bool = True) -> None:
        """
        Shutdown the scheduler.

        Args:
            wait: Whether to wait for running jobs to complete
        """
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
            self.logger.info("Scheduler shutdown")

    # =========================================================================
    # Job Scheduling
    # =========================================================================

    def schedule_once(
        self,
        job_id: str,
        delay_seconds: int,
        callback: Callable,
        instance_id: int = None,
        job_type: str = 'generic',
        payload: Dict[str, Any] = None
    ) -> bool:
        """
        Schedule a one-time job to run after a delay.

        Args:
            job_id: Unique job identifier (e.g., 'timeout_123_instance_1')
            delay_seconds: Seconds until job runs
            callback: Function to call when job runs
            instance_id: Associated app instance ID (optional)
            job_type: Job type for logging/filtering
            payload: Additional data passed to callback

        Returns:
            True if job was scheduled successfully
        """
        execute_at = datetime.now() + timedelta(seconds=delay_seconds)

        # Cancel existing job with same ID
        self.cancel(job_id)

        try:
            # Add to APScheduler
            self._scheduler.add_job(
                func=callback,
                trigger='date',
                run_date=execute_at,
                id=job_id,
                kwargs={'job_id': job_id, 'payload': payload or {}},
                replace_existing=True
            )

            # Track job metadata
            self._jobs[job_id] = {
                'job_id': job_id,
                'instance_id': instance_id,
                'job_type': job_type,
                'execute_at': execute_at.isoformat(),
                'payload': payload or {},
                'status': 'pending'
            }

            # Persist to database
            self._persist_job(job_id, instance_id, job_type, execute_at, payload)

            self.logger.debug(
                f"Scheduled job: {job_id}, type={job_type}, "
                f"runs_at={execute_at.isoformat()}"
            )
            return True

        except Exception as e:
            self.logger.error(f"Failed to schedule job {job_id}: {e}", exc_info=True)
            return False

    def schedule_recurring(
        self,
        job_id: str,
        interval_seconds: int,
        callback: Callable,
        instance_id: int = None,
        job_type: str = 'recurring',
        payload: Dict[str, Any] = None
    ) -> bool:
        """
        Schedule a recurring job.

        Args:
            job_id: Unique job identifier
            interval_seconds: Seconds between runs
            callback: Function to call on each run
            instance_id: Associated app instance ID (optional)
            job_type: Job type for logging/filtering
            payload: Additional data passed to callback

        Returns:
            True if job was scheduled successfully
        """
        # Cancel existing job with same ID
        self.cancel(job_id)

        try:
            self._scheduler.add_job(
                func=callback,
                trigger='interval',
                seconds=interval_seconds,
                id=job_id,
                kwargs={'job_id': job_id, 'payload': payload or {}},
                replace_existing=True
            )

            self._jobs[job_id] = {
                'job_id': job_id,
                'instance_id': instance_id,
                'job_type': job_type,
                'interval_seconds': interval_seconds,
                'payload': payload or {},
                'status': 'running'
            }

            self.logger.debug(
                f"Scheduled recurring job: {job_id}, interval={interval_seconds}s"
            )
            return True

        except Exception as e:
            self.logger.error(f"Failed to schedule recurring job {job_id}: {e}", exc_info=True)
            return False

    def reschedule(
        self,
        job_id: str,
        delay_seconds: int
    ) -> bool:
        """
        Reschedule an existing job to a new time.

        Useful for resetting motion timeouts when new motion is detected.

        Args:
            job_id: Job ID to reschedule
            delay_seconds: New delay from now

        Returns:
            True if rescheduled successfully
        """
        execute_at = datetime.now() + timedelta(seconds=delay_seconds)

        try:
            job = self._scheduler.get_job(job_id)
            if job:
                self._scheduler.reschedule_job(
                    job_id,
                    trigger='date',
                    run_date=execute_at
                )

                # Update tracking
                if job_id in self._jobs:
                    self._jobs[job_id]['execute_at'] = execute_at.isoformat()

                # Update database
                self._update_job_time(job_id, execute_at)

                self.logger.debug(f"Rescheduled job: {job_id} to {execute_at}")
                return True

        except Exception as e:
            self.logger.error(f"Failed to reschedule job {job_id}: {e}", exc_info=True)

        return False

    def cancel(self, job_id: str) -> bool:
        """
        Cancel a scheduled job.

        Args:
            job_id: Job ID to cancel

        Returns:
            True if cancelled (or didn't exist)
        """
        try:
            job = self._scheduler.get_job(job_id)
            if job:
                self._scheduler.remove_job(job_id)

            # Remove from tracking
            self._jobs.pop(job_id, None)

            # Update database
            self._mark_job_cancelled(job_id)

            self.logger.debug(f"Cancelled job: {job_id}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to cancel job {job_id}: {e}", exc_info=True)
            return False

    def cancel_for_instance(self, instance_id: int) -> int:
        """
        Cancel all jobs for an instance.

        Called when an instance is deleted or paused.

        Args:
            instance_id: App instance ID

        Returns:
            Number of jobs cancelled
        """
        cancelled = 0

        jobs_to_cancel = [
            job_id for job_id, job in self._jobs.items()
            if job.get('instance_id') == instance_id
        ]

        for job_id in jobs_to_cancel:
            if self.cancel(job_id):
                cancelled += 1

        self.logger.info(f"Cancelled {cancelled} jobs for instance {instance_id}")
        return cancelled

    # =========================================================================
    # Job Queries
    # =========================================================================

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get job metadata by ID."""
        return self._jobs.get(job_id)

    def get_jobs_for_instance(self, instance_id: int) -> List[Dict[str, Any]]:
        """Get all jobs for an instance."""
        return [
            job for job in self._jobs.values()
            if job.get('instance_id') == instance_id
        ]

    def is_job_pending(self, job_id: str) -> bool:
        """Check if a job is pending (not yet executed)."""
        job = self._scheduler.get_job(job_id)
        return job is not None

    # =========================================================================
    # Persistence (PostgreSQL via PostgREST)
    # =========================================================================

    def _persist_job(
        self,
        job_id: str,
        instance_id: int,
        job_type: str,
        execute_at: datetime,
        payload: Dict[str, Any]
    ) -> None:
        """Save job to database for recovery after restart."""
        try:
            requests.post(
                f"{self.postgrest_url}/scheduled_jobs",
                json={
                    'job_id': job_id,
                    'instance_id': instance_id,
                    'job_type': job_type,
                    'execute_at': execute_at.isoformat(),
                    'payload': payload or {},
                    'status': 'pending'
                },
                headers={
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates"
                },
                timeout=5
            )
        except Exception as e:
            self.logger.warning(f"Failed to persist job {job_id}: {e}", exc_info=True)

    def _update_job_time(self, job_id: str, execute_at: datetime) -> None:
        """Update job execution time in database."""
        try:
            requests.patch(
                f"{self.postgrest_url}/scheduled_jobs",
                params={"job_id": f"eq.{job_id}"},
                json={"execute_at": execute_at.isoformat()},
                headers={"Content-Type": "application/json"},
                timeout=5
            )
        except Exception as e:
            self.logger.warning(f"Failed to update job time {job_id}: {e}", exc_info=True)

    def _mark_job_cancelled(self, job_id: str) -> None:
        """Mark job as cancelled in database."""
        try:
            requests.patch(
                f"{self.postgrest_url}/scheduled_jobs",
                params={"job_id": f"eq.{job_id}"},
                json={"status": "cancelled"},
                headers={"Content-Type": "application/json"},
                timeout=5
            )
        except Exception as e:
            self.logger.warning(f"Failed to mark job cancelled {job_id}: {e}", exc_info=True)

    def _mark_job_completed(self, job_id: str) -> None:
        """Mark job as completed in database."""
        try:
            requests.patch(
                f"{self.postgrest_url}/scheduled_jobs",
                params={"job_id": f"eq.{job_id}"},
                json={
                    "status": "completed",
                    "completed_at": datetime.now().isoformat()
                },
                headers={"Content-Type": "application/json"},
                timeout=5
            )
        except Exception as e:
            self.logger.warning(f"Failed to mark job completed {job_id}: {e}", exc_info=True)

    def _restore_pending_jobs(self) -> None:
        """
        Restore pending jobs from database after restart.

        Timeout jobs are ephemeral — they were relative to a previous process
        lifetime and are meaningless after a restart.  They are bulk-discarded
        in a single request rather than iterated one-by-one (which caused
        catastrophic slowdown when millions of missed jobs had accumulated).

        Non-timeout jobs that should have run while we were down are marked
        failed.  Jobs still in the future are re-added to the scheduler.
        """
        try:
            # --- Step 1: Bulk-discard all pending timeout jobs in one request.
            # Timeout jobs are ephemeral: motion-off timers etc. that were valid
            # only during the previous process run.  Re-running them after a
            # restart makes no sense and creates massive overhead.
            discard_resp = requests.patch(
                f"{self.postgrest_url}/scheduled_jobs",
                params={"status": "eq.pending", "job_type": "eq.timeout"},
                json={"status": "failed"},
                headers={"Content-Type": "application/json"},
                timeout=15
            )
            if discard_resp.status_code in (200, 204):
                self.logger.info(
                    "Bulk-discarded stale pending timeout jobs on startup"
                )
            else:
                self.logger.warning(
                    f"Bulk-discard of timeout jobs returned HTTP "
                    f"{discard_resp.status_code}"
                )

            # --- Step 2: Also purge old terminal records to keep the table lean.
            self._purge_old_records()

            # --- Step 3: Restore remaining non-timeout pending jobs.
            response = requests.get(
                f"{self.postgrest_url}/scheduled_jobs",
                params={"status": "eq.pending", "job_type": "neq.timeout"},
                timeout=10
            )

            if response.status_code != 200:
                return

            jobs = response.json()
            now = datetime.now()

            for job in jobs:
                execute_at = datetime.fromisoformat(
                    job['execute_at'].replace('Z', '+00:00')
                )

                # Remove timezone for comparison
                if execute_at.tzinfo:
                    execute_at = execute_at.replace(tzinfo=None)

                if execute_at < now:
                    # Job missed — mark as failed in bulk later; warn and skip.
                    self.logger.warning(
                        f"Non-timeout job {job['job_id']} missed execution, "
                        f"marking failed"
                    )
                    requests.patch(
                        f"{self.postgrest_url}/scheduled_jobs",
                        params={"job_id": f"eq.{job['job_id']}"},
                        json={"status": "failed"},
                        headers={"Content-Type": "application/json"},
                        timeout=5
                    )
                else:
                    # Job in future — re-add to scheduler.
                    self._jobs[job['job_id']] = job
                    self.logger.info(f"Restored pending job: {job['job_id']}")

            self.logger.info(
                f"Restored {len(jobs)} non-timeout pending jobs from database"
            )

        except Exception as e:
            self.logger.error(
                f"Failed to restore pending jobs: {e}", exc_info=True
            )

    def _purge_old_records(self, max_age_hours: int = 24) -> None:
        """
        Delete terminal (completed/cancelled/failed) job records older than
        max_age_hours from the database.

        This prevents the scheduled_jobs table from growing without bound.
        Called at startup and can also be wired to a recurring scheduler job.

        Args:
            max_age_hours: Records older than this are deleted (default 24 h).
        """
        try:
            cutoff = (
                datetime.now() - timedelta(hours=max_age_hours)
            ).isoformat()
            resp = requests.delete(
                f"{self.postgrest_url}/scheduled_jobs",
                params={
                    "status": "in.(completed,cancelled,failed)",
                    "created_at": f"lt.{cutoff}"
                },
                timeout=15
            )
            if resp.status_code in (200, 204):
                self.logger.info(
                    f"Purged terminal job records older than {max_age_hours}h"
                )
            else:
                self.logger.warning(
                    f"Purge of old records returned HTTP {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
        except Exception as e:
            self.logger.warning(
                f"Failed to purge old job records: {e}", exc_info=True
            )


# Global scheduler instance
_scheduler: Optional[SchedulerService] = None


def get_scheduler() -> SchedulerService:
    """Get the global scheduler instance, creating if needed."""
    global _scheduler
    if _scheduler is None:
        _scheduler = SchedulerService()
        _scheduler.start()
    return _scheduler
