"""
Event dispatcher — routes incoming device events to the right handler.

Button events bypass the paused check: they are the unpause mechanism and
must always be processed. All other events are dropped when paused.
"""

from apps.advanced_motion_lighting.constants import _C, _R


class EventDispatchMixin:
    """Mixin: top-level on_event() dispatcher."""

    def on_event(self, event) -> None:
        """
        Handle an incoming device event from the webhook router.

        Routing:
          - pushed/held/doubleTapped → _handle_button() (always, even when paused)
          - motion                   → _handle_motion()
          - switch                   → _handle_switch()
          - illuminance              → _handle_illuminance()
          - contact                  → _handle_contact()

        Args:
            event: DeviceEvent with device_id, event_type, value, device_name, etc.
        """
        try:
            # Button events are always processed — they're the unpause mechanism
            if event.event_type in ('pushed', 'held', 'doubleTapped'):
                self._handle_button(event)
                return

            if self.is_paused:
                self.logger.debug(f"Paused, ignoring event: {event}")
                return

            self.update_last_activity()

            if event.event_type == 'motion':
                self._handle_motion(event)
            elif event.event_type == 'switch':
                self._handle_switch(event)
            elif event.event_type == 'illuminance':
                self._handle_illuminance(event)
            elif event.event_type == 'contact':
                self._handle_contact(event)

        except Exception as e:
            self.logger.error(
                f"on_event() failed for {self.label}, event={event}: {e}",
                exc_info=True
            )

    def _handle_illuminance(self, event) -> None:
        """Re-evaluate light state when a new lux reading arrives."""
        self.logger.debug(f"Illuminance: {event.value} lux")
        self.master()

    def _handle_contact(self, event) -> None:
        """Turn on lights when a door/window opens."""
        if event.is_contact_open:
            self.logger.debug(f"Contact opened: {_C}{event.device_name}{_R}")
            self.master(motion_active_event=True)
