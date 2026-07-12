"""
Pause and resume lifecycle management.

Handles timed and indefinite pauses, auto-resume scheduling, and
memoization reset on state transitions.

Design rules:
- Memoization resets on pause AND resume so manual overrides never carry
  forward across a pause/resume cycle.
- WRITE-THROUGH (DB-first audit 2026-07-11, findings F1/F8): pause() and
  resume() persist ``is_paused`` / ``pause_expires_at`` (and
  ``pause_reason`` when explicitly given) straight to ``app_instances``
  via PostgREST. Self-pausing apps (e.g. a power_management trip) are
  therefore restart-safe: the pause is restored from the DB row on boot
  instead of silently evaporating with the process.
- The PATCH is deliberately NOT routed through
  ``instance_manager.pause_instance()/resume_instance()`` — those
  orchestrators call back into ``app.pause()/app.resume()`` and would
  recurse. When THEY are the caller (UI/API path), our extra PATCH is an
  idempotent re-write of the same values; ``pause_reason`` is omitted
  here unless explicitly passed, so the orchestrator's reason is never
  clobbered.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import requests


class PauseResumeMixin:
    """Mixin: pause/resume with auto-resume scheduling and DB write-through."""

    def pause(self, duration_minutes: float = 0, reason: Optional[str] = None) -> None:
        """
        Pause the instance — durably.

        Resets memoization (Groovy resetStates pattern) so stale override
        records don't survive into the next active period. Write-through
        persists the pause so a container restart cannot silently resume
        the instance (audit F1/F8). Schedules an in-process auto-resume
        if duration_minutes > 0; the persisted ``pause_expires_at`` is the
        restart-safe backstop honored by the durable
        ``resume_expired_pauses`` reconciler.

        Args:
            duration_minutes: How long to pause. 0 = indefinite (manual
                resume required). Fractional minutes are honored (e.g.
                0.5 = a 30-second pause).
            reason: Optional pause reason persisted to
                ``app_instances.pause_reason``. None = leave the DB reason
                untouched (the UI path via instance_manager.pause_instance
                writes its own reason BEFORE calling us — omitting it here
                avoids clobbering it with a stale in-memory value).
        """
        self._is_paused = True
        if reason is not None:
            self._pause_reason = reason
        self.logger.info(f"Paused for {duration_minutes} minutes")

        # Memoization reset: manual overrides expire on pause.
        # NB for callers persisting their own memo records around a pause
        # (e.g. power_management's trip record): write them AFTER pause()
        # returns, or this reset wipes them.
        self._reset_memoization()

        # DB write-through BEFORE scheduling: if the process dies right
        # after this line, the pause (and its expiry) is already durable.
        expires_at = None
        if duration_minutes and duration_minutes > 0:
            # Tz-aware UTC so pause_expires_at compares correctly against
            # now(timezone.utc) in the resume_expired_pauses reconciler.
            expires_at = (datetime.now(timezone.utc)
                          + timedelta(minutes=duration_minutes)).isoformat()
        patch = {'is_paused': True, 'pause_expires_at': expires_at}
        if reason is not None:
            patch['pause_reason'] = reason
        self._persist_pause_patch(patch)

        from services.scheduler_service import get_scheduler
        scheduler = get_scheduler()

        # Cancel any pending timeout (lights-off check)
        if self._runtime.timeout_job_id:
            scheduler.cancel(self._runtime.timeout_job_id)

        # Schedule the in-process auto-resume (fast path). If the process
        # restarts before it fires, the reconciler picks the pause up from
        # pause_expires_at written above.
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
        Resume from paused state — durably.

        Resets memoization so the instance starts with clean override
        state. Cancels any pending auto-resume job. Write-through clears
        ``is_paused`` / ``pause_reason`` / ``pause_expires_at`` in the DB
        AND the in-memory ``_pause_reason`` (fixing the memory-vs-DB
        divergence where a stale reason survived a resume — audit M2).
        Calls master() to re-evaluate current device state.

        Subclasses (e.g., AdvancedMotionLightingApp) may override to run
        additional enforcement logic (keep_on/keep_off switches) on resume.
        """
        self._is_paused = False
        self._pause_reason = None
        self.logger.info("Resumed")

        # DB write-through: idempotent when instance_manager.resume_instance
        # is the caller (it PATCHed the same values already); load-bearing
        # when an app self-resumes (e.g. power_management auto-recovery).
        self._persist_pause_patch({
            'is_paused': False,
            'pause_expires_at': None,
            'pause_reason': None,
        })

        # Cancel scheduled auto-resume if we're being manually resumed
        if self._runtime.auto_resume_job_id:
            from services.scheduler_service import get_scheduler
            scheduler = get_scheduler()
            scheduler.cancel(self._runtime.auto_resume_job_id)
            self._runtime.auto_resume_job_id = None

        # Memoization reset: manual overrides expire on resume
        self._reset_memoization()

        self.master()

    def _persist_pause_patch(self, patch: dict) -> None:
        """
        Directly PATCH pause columns on ``app_instances`` via PostgREST.

        Deliberately NOT instance_manager.pause_instance()/resume_instance()
        — those call back into app.pause()/app.resume() and would recurse
        (same rationale as AML's _patch_pause_state). Failures are logged,
        never raised: persisting the pause must not crash the caller (e.g.
        a power-trip handler mid-cutoff).

        Args:
            patch: Column→value dict; only is_paused / pause_reason /
                pause_expires_at belong here.
        """
        try:
            requests.patch(
                f"{self.instance_manager.postgrest_url}/app_instances",
                params={"id": f"eq.{self.instance_id}"},
                json=patch,
                headers={"Content-Type": "application/json"},
                timeout=5,
            )
        except Exception as e:
            self.logger.warning(f"Pause-state DB write-through failed: {e}")

    @property
    def is_paused(self) -> bool:
        """True if this instance is currently paused."""
        return self._is_paused
