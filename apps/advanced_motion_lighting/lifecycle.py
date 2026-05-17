"""
AML instance lifecycle — initialize().

Called once when the instance is loaded (container start or new instance save).

2026-05-17: removed the old "no master() on startup" rule. Reason: user
reported externally-driven ON states (Hubitat-side automation turning a
light on) were never reverted because AML had no event to evaluate against.
initialize() now schedules a near-immediate master() (5s, configurable
via system_settings.aml_init_master_delay_seconds) so AML takes ownership
of the room state right after boot. The 5s window lets any in-flight motion
event arrive first, avoiding "off → on" races on busy rooms.
"""


class AMLLifecycleMixin:
    """Mixin: Advanced Motion Lighting startup initialization."""

    def initialize(self) -> None:
        """
        Set up the instance on startup.

        Actions:
          1. Seed _functional_sensors so motion detection works before first event
          2. Schedule the periodic health check (every 10 minutes)
          3. Enforce keep-on/keep-off immediately (Groovy parity)
          4. Schedule a quick FIRST master() run (~5s) — closes the gap
             where externally-driven switch changes are never re-evaluated
          5. _schedule_next_run() at the end of that first master() arms
             the recurring chain
        """
        self.logger.info(f"Initializing: {self.label}")

        # Pre-populate functional sensor map from config
        # (otherwise _is_motion_active() would have no sensors to check on startup)
        for sensor_id in self.get_devices('motion_sensors'):
            self._functional_sensors.setdefault(sensor_id, True)

        # Schedule periodic health check
        from services.scheduler_service import get_scheduler
        scheduler = get_scheduler()
        health_job_id = f"health_{self.instance_id}"
        scheduler.schedule_recurring(
            job_id=health_job_id,
            interval_seconds=600,  # Every 10 minutes
            callback=lambda **kwargs: self._health_check(),
            instance_id=self.instance_id,
            job_type='health_check'
        )
        self._runtime.health_check_job_id = health_job_id

        # Enforce keep switches immediately. This handles startup-time
        # divergences from manual changes during the previous restart window.
        self._enforce_keep_switches()

        # Schedule the first master() — short delay so in-flight motion
        # events can land. After that runs, _schedule_next_run() inside
        # master() arms the recurring chain at the full timeout cadence.
        try:
            from services.settings_resolver import get_resolver
            resolver = get_resolver()
            init_delay = int(resolver.get_system(
                'aml_init_master_delay_seconds', 5
            ))
        except Exception:
            init_delay = 5
            resolver = None
        self.schedule_timeout(max(1, init_delay))

        # GUARANTEED periodic master() evaluation, independent of the
        # event-driven self-rescheduling chain. Survives missed events,
        # stuck timers, externally-driven switch toggles. Default 60s,
        # tunable via system_settings.aml_periodic_eval_interval_seconds.
        # Note: this is a SEPARATE scheduler job from the timeout chain —
        # both fire master(); the timeout chain's self-reschedule cadence
        # respects the configured motion timeout, while this one is a
        # hard-coded floor cadence the user can adjust globally.
        try:
            eval_interval = (
                int(resolver.get_system('aml_periodic_eval_interval_seconds', 60))
                if resolver else 60
            )
        except Exception:
            eval_interval = 60
        eval_job_id = f"periodic_eval_{self.instance_id}"
        try:
            scheduler.schedule_recurring(
                job_id=eval_job_id,
                interval_seconds=max(10, eval_interval),
                callback=lambda **kwargs: self.master(),
                instance_id=self.instance_id,
                job_type='periodic_eval'
            )
            self._runtime.periodic_eval_job_id = eval_job_id
            self.logger.info(
                f"Scheduled periodic master() every {eval_interval}s"
            )
        except Exception as e:
            self.logger.warning(
                f"Failed to schedule periodic master(): {e}"
            )
