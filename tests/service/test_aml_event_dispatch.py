"""
AdvancedMotionLighting's EventDispatchMixin.on_event() is the top-level
router for incoming device events. Key behaviors:

  - Button events ('pushed'/'held'/'doubleTapped') ALWAYS process, even
    when the instance is paused — they are the unpause mechanism.
  - All other event types are dropped when is_paused=True.
  - on_event swallows exceptions from any handler so one bad event doesn't
    crash the worker loop for the instance.
"""

from unittest.mock import MagicMock, call

import pytest

from apps.advanced_motion_lighting.event_handlers.dispatch import (
    EventDispatchMixin,
)


def make_event(*, event_type, device_id="1", device_name="Test", value="active",
               is_motion_active=None, is_contact_open=None):
    ev = MagicMock()
    ev.event_type = event_type
    ev.device_id = device_id
    ev.device_name = device_name
    ev.value = value
    if is_motion_active is not None:
        ev.is_motion_active = is_motion_active
    if is_contact_open is not None:
        ev.is_contact_open = is_contact_open
    return ev


class HostInstance(EventDispatchMixin):
    """Minimal harness with mocked handler methods."""

    def __init__(self, *, paused=False):
        self.logger = MagicMock()
        self.label = "test-instance"
        self.is_paused = paused
        self._handle_button = MagicMock(name="handle_button")
        self._handle_motion = MagicMock(name="handle_motion")
        self._handle_switch = MagicMock(name="handle_switch")
        self._handle_level_color = MagicMock(name="handle_level_color")
        self.update_last_activity = MagicMock()
        # _handle_illuminance + _handle_contact are not overridden by tests
        # because the dispatch mixin itself provides them and they call
        # self.master() — we mock master directly.
        self.master = MagicMock()


@pytest.mark.service
class TestPausedBehavior:
    def test_motion_event_dropped_when_paused(self):
        host = HostInstance(paused=True)
        host.on_event(make_event(event_type="motion"))
        host._handle_motion.assert_not_called()
        host._handle_button.assert_not_called()

    def test_switch_event_dropped_when_paused(self):
        host = HostInstance(paused=True)
        host.on_event(make_event(event_type="switch"))
        host._handle_switch.assert_not_called()

    def test_button_held_event_processes_even_when_paused(self):
        # Buttons are the unpause mechanism — they MUST process.
        host = HostInstance(paused=True)
        host.on_event(make_event(event_type="held"))
        host._handle_button.assert_called_once()

    def test_button_pushed_event_processes_even_when_paused(self):
        host = HostInstance(paused=True)
        host.on_event(make_event(event_type="pushed"))
        host._handle_button.assert_called_once()

    def test_button_doubletapped_event_processes_even_when_paused(self):
        host = HostInstance(paused=True)
        host.on_event(make_event(event_type="doubleTapped"))
        host._handle_button.assert_called_once()


@pytest.mark.service
class TestRouting:
    def test_motion_routes_to_motion_handler(self):
        host = HostInstance(paused=False)
        ev = make_event(event_type="motion")
        host.on_event(ev)
        host._handle_motion.assert_called_once_with(ev)

    def test_switch_routes_to_switch_handler(self):
        host = HostInstance(paused=False)
        ev = make_event(event_type="switch", value="on")
        host.on_event(ev)
        host._handle_switch.assert_called_once_with(ev)

    def test_level_routes_to_level_color_handler(self):
        host = HostInstance(paused=False)
        ev = make_event(event_type="level", value="75")
        host.on_event(ev)
        host._handle_level_color.assert_called_once_with(ev)

    def test_colortemperature_routes_to_level_color_handler(self):
        host = HostInstance(paused=False)
        ev = make_event(event_type="colorTemperature", value="2700")
        host.on_event(ev)
        host._handle_level_color.assert_called_once_with(ev)

    def test_illuminance_triggers_master(self):
        host = HostInstance(paused=False)
        host.on_event(make_event(event_type="illuminance", value="100"))
        host.master.assert_called_once()

    def test_contact_open_triggers_master_with_motion_active(self):
        host = HostInstance(paused=False)
        host.on_event(make_event(
            event_type="contact",
            value="open",
            is_contact_open=True,
        ))
        host.master.assert_called_once_with(motion_active_event=True)

    def test_contact_closed_does_not_trigger_master(self):
        # The current implementation only triggers master on contact open.
        host = HostInstance(paused=False)
        host.on_event(make_event(
            event_type="contact",
            value="closed",
            is_contact_open=False,
        ))
        host.master.assert_not_called()

    def test_unknown_event_type_silently_ignored(self):
        host = HostInstance(paused=False)
        host.on_event(make_event(event_type="batteryLow"))  # not in routing table
        host._handle_motion.assert_not_called()
        host._handle_switch.assert_not_called()
        host._handle_button.assert_not_called()
        host._handle_level_color.assert_not_called()
        host.master.assert_not_called()

    def test_activity_timestamp_updated_for_non_button_events(self):
        host = HostInstance(paused=False)
        host.on_event(make_event(event_type="motion"))
        host.update_last_activity.assert_called_once()

    def test_activity_timestamp_not_updated_for_button_events(self):
        # Button handler manages its own bookkeeping; dispatch doesn't
        # bump activity for buttons.
        host = HostInstance(paused=False)
        host.on_event(make_event(event_type="held"))
        host.update_last_activity.assert_not_called()


@pytest.mark.service
class TestExceptionIsolation:
    def test_handler_exception_does_not_propagate(self):
        # If a handler raises, on_event must catch it — otherwise the
        # per-instance worker task would crash and stop processing all events.
        host = HostInstance(paused=False)
        host._handle_motion.side_effect = RuntimeError("boom")

        # Should NOT raise
        host.on_event(make_event(event_type="motion"))

        host.logger.error.assert_called()  # error was logged

    def test_button_handler_exception_does_not_propagate(self):
        host = HostInstance(paused=True)  # paused so only buttons run
        host._handle_button.side_effect = RuntimeError("button boom")

        host.on_event(make_event(event_type="held"))

        host.logger.error.assert_called()
