"""
AML instance lifecycle — initialize().

Called once when the instance is loaded (container start or new instance save).

Groovy parity: initialize() does NOT call master(). Calling master() on startup
would turn off all lights because no motion event has arrived yet, making
_is_motion_active() return False → _control_lights('off') on every switch.
Instead, only enforce keep-on/keep-off rules and schedule the periodic run.
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
          4. Schedule the first master() run via _schedule_next_run()
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

        # Enforce keep switches and schedule next run (no master() on startup)
        self._enforce_keep_switches()
        self._schedule_next_run()
