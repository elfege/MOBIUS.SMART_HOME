"""
Motion sensor event handler.

Updates the in-memory last_motion_time on active events and triggers
the main logic loop. On inactive events, schedules the no-motion timeout.
"""

from datetime import datetime

from apps.advanced_motion_lighting.constants import _C, _R


class MotionEventMixin:
    """Mixin: handle motion sensor active/inactive events."""

    def _handle_motion(self, event) -> None:
        """
        Process a motion sensor event.

        Active:   Update last_motion_time → call master(motion_active_event=True)
        Inactive: Schedule the no-motion timeout (master() runs when it fires)

        Args:
            event: DeviceEvent with is_motion_active property
        """
        # Mark sensor as functional (it just emitted an event)
        self._functional_sensors[event.device_id] = True

        if event.is_motion_active:
            self.logger.debug(f"Motion active: {_C}{event.device_name}{_R}")
            self._runtime.last_motion_time = datetime.now()
            self.master(motion_active_event=True)
        else:
            self.logger.debug(f"Motion inactive: {_C}{event.device_name}{_R}")
            timeout = self._get_timeout_seconds()
            self.schedule_timeout(timeout)
