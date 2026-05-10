"""
Periodic sensor health check and keep-switch enforcement.

Scheduled every 10 minutes by initialize(). Checks whether each motion
sensor has been heard from recently and enforces keep-on/keep-off rules.
"""


class HealthCheckMixin:
    """Mixin: periodic sensor health monitoring and keep-switch re-enforcement."""

    def _health_check(self) -> None:
        """
        Check sensor health and enforce keep-on/keep-off rules.

        Called every 10 minutes by the APScheduler job started in initialize().

        Sensor health: flags sensors as potentially unresponsive if they haven't
        emitted any events. The _functional_sensors dict is updated in real-time
        by _handle_motion() when events arrive.

        Keep-switch enforcement: runs _enforce_keep_switches() on each cycle
        to catch any state drift that wasn't detected via webhook events.
        """
        try:
            for device_id in self.get_devices('motion_sensors'):
                is_functional = self._functional_sensors.get(device_id, False)
                if not is_functional:
                    self.logger.warning(f"Sensor {device_id} may be unresponsive")

            if not self.is_paused:
                self._enforce_keep_switches()

        except Exception as e:
            self.logger.error(
                f"_health_check failed for instance {self.label}: {e}",
                exc_info=True
            )
