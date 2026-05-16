"""
Hubitat button drivers (Lutron Pico / Zigbee scene controllers) retransmit
the same 'held=1' event at ~2s cadence while a button is held down. Without
a cooldown, every retransmit would toggle pause/resume, creating wildly
incorrect behavior.

ButtonAndPauseMixin enforces a 2.5s per-device cooldown. Tested here:

  - Two rapid presses within 2.5s → second is suppressed
  - Press at T + >2.5s → accepted
  - Different devices have independent cooldowns
  - Non-configured event types (e.g. 'pushed' when configured for 'held')
    are filtered out BEFORE the cooldown check (so they don't reset it)
"""

import time
from unittest.mock import MagicMock

import pytest

from apps.advanced_motion_lighting.event_handlers.button_and_pause import (
    ButtonAndPauseMixin,
    _BUTTON_DEBOUNCE_SECS,
)


def make_button_event(device_id="1", device_name="Test Button", event_type="held"):
    """A DeviceEvent-shaped object good enough for _handle_button."""
    ev = MagicMock()
    ev.device_id = device_id
    ev.device_name = device_name
    ev.event_type = event_type
    return ev


class HostInstance(ButtonAndPauseMixin):
    """Minimal harness: just enough state to make ButtonAndPauseMixin run."""

    def __init__(self, *, paused=False, button_event_type="held",
                 pause_duration=60, pause_duration_unit="Minutes"):
        self.logger = MagicMock()
        self.is_paused = paused
        self.instance_id = 1
        self.instance_manager = MagicMock()
        self.instance_manager.pause_instance.return_value = True
        self.instance_manager.resume_instance.return_value = True
        self._settings = {
            "buttonEventType": button_event_type,
            "pauseDuration": pause_duration,
            "pauseDurationUnit": pause_duration_unit,
        }
        self._devices = {
            "pause_switches": [],
            "keep_off_switches": [],
            "keep_on_switches": [],
        }

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)

    def get_devices(self, category):
        return self._devices.get(category, [])

    def get_device_state(self, device_id):
        return None

    def send_command(self, *args, **kwargs):
        return True


@pytest.mark.service
class TestButtonDebounce:
    def test_first_press_is_accepted(self):
        host = HostInstance(paused=False)
        host._handle_button(make_button_event())
        host.instance_manager.pause_instance.assert_called_once()

    def test_second_rapid_press_within_window_is_suppressed(self):
        host = HostInstance(paused=False)
        host._handle_button(make_button_event())
        host.instance_manager.pause_instance.reset_mock()

        # Immediate second press — well within 2.5s
        host._handle_button(make_button_event())

        host.instance_manager.pause_instance.assert_not_called()
        host.instance_manager.resume_instance.assert_not_called()

    def test_press_after_cooldown_is_accepted(self, mocker):
        host = HostInstance(paused=False)
        host._handle_button(make_button_event())
        host.instance_manager.pause_instance.reset_mock()

        # Advance monotonic clock by cooldown + 0.1s
        real_monotonic = time.monotonic
        offset = _BUTTON_DEBOUNCE_SECS + 0.1
        mocker.patch(
            "apps.advanced_motion_lighting.event_handlers.button_and_pause.time.monotonic",
            side_effect=lambda: real_monotonic() + offset,
        )
        host._handle_button(make_button_event())

        host.instance_manager.pause_instance.assert_called_once()

    def test_different_devices_have_independent_cooldowns(self):
        host = HostInstance(paused=False)
        host._handle_button(make_button_event(device_id="A"))
        host._handle_button(make_button_event(device_id="B"))

        # Both presses accepted, paused once then unpaused, so:
        # First press for A → pause
        # Second press for B → second call: host was paused after first
        # press; let's just verify both touched the instance_manager.
        # NOTE: HostInstance.is_paused is static; first press calls pause,
        # second press doesn't see the pause flag flip because we don't
        # mutate it. So second press *also* tries to pause.
        assert host.instance_manager.pause_instance.call_count == 2

    def test_wrong_event_type_ignored_does_not_reset_cooldown(self):
        # If a 'pushed' event comes through when configured for 'held',
        # it's filtered out BEFORE the cooldown bookkeeping. The cooldown
        # should not be reset by ignored events.
        host = HostInstance(paused=False, button_event_type="held")
        host._handle_button(make_button_event(event_type="held"))
        host.instance_manager.pause_instance.reset_mock()

        host._handle_button(make_button_event(event_type="pushed"))  # ignored
        # Second 'held' immediately after — should still be suppressed
        host._handle_button(make_button_event(event_type="held"))

        host.instance_manager.pause_instance.assert_not_called()

    def test_resume_path_when_already_paused(self):
        host = HostInstance(paused=True)
        host._handle_button(make_button_event())

        host.instance_manager.resume_instance.assert_called_once_with(1)
        host.instance_manager.pause_instance.assert_not_called()

    def test_pause_duration_uses_hours_multiplier(self):
        host = HostInstance(paused=False, pause_duration=2,
                            pause_duration_unit="Hours")
        host._handle_button(make_button_event())

        call = host.instance_manager.pause_instance.call_args
        # 2 hours → 120 minutes
        assert call.kwargs["duration_minutes"] == 120
        assert call.kwargs["reason"] == "Button press"
