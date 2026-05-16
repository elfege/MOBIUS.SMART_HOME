"""
Eventsocket frames arrive as JSON strings. The client must:
  - Skip non-DEVICE source frames (LOCATION mode changes, APP_STATUS, etc.)
  - Convert DEVICE frames to the canonical webhook-payload shape that
    WebhookRouter.route_event consumes
  - Inject _hub_ip from the connection's hub
  - Inject _intake='eventsocket'
  - Inject _received_at_monotonic_ms for processing latency calculation
  - Hand the payload to the configured on_event callback
  - Survive an on_event handler raising (must not kill the stream)
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.hubitat_eventsocket_client import HubitatEventsocketClient
from tests.factories import make_eventsocket_frame, make_hub_config_row


def _make_client_with_fake_ws(hubs=None, frames=()):
    """Build a client that, when _drain is called with a fake ws, will
    receive the queued frames and then a sentinel close."""
    client = HubitatEventsocketClient(hubs=hubs or [make_hub_config_row()])
    return client


class _FakeWs:
    def __init__(self):
        self._q: asyncio.Queue = asyncio.Queue()
        self._closed = False

    def push(self, frame: str):
        self._q.put_nowait(frame)

    async def recv(self):
        if self._closed:
            raise RuntimeError("ws closed (test)")
        return await self._q.get()

    async def close(self):
        self._closed = True


@pytest.mark.service
class TestPayloadShape:
    async def test_device_frame_converted_to_router_payload(self, mocker):
        # Capture the payload the client sends to the router
        captured = []

        async def fake_router(payload):
            captured.append(payload)

        client = _make_client_with_fake_ws()
        # Skip the real router; install our capturer directly
        client._router = MagicMock()
        client._router.route_event = AsyncMock(side_effect=fake_router)

        ws = _FakeWs()
        ws.push(json.dumps(make_eventsocket_frame(
            deviceId="100",
            name="motion",
            value="active",
            displayName="Test Motion Sensor",
        )))
        # Wrap _drain in a task with a tight watchdog so it returns after
        # exhausting the queue (watchdog will fire on empty queue).
        # We mock DATA_WATCHDOG_SECS to 0.05 for fast test.
        mocker.patch(
            "services.hubitat_eventsocket_client.DATA_WATCHDOG_SECS", 0.05
        )

        result = await client._drain("hub4", "<LAN_IP>", 1, ws)

        assert result is True  # got at least one event
        assert len(captured) == 1
        p = captured[0]
        assert p["deviceId"] == "100"
        assert p["name"] == "motion"
        assert p["value"] == "active"
        assert p["displayName"] == "Test Motion Sensor"
        assert p["_hub_ip"] == "<LAN_IP>"
        assert p["_intake"] == "eventsocket"
        assert isinstance(p["_received_at_monotonic_ms"], float)
        assert p["_received_at_monotonic_ms"] > 0

    async def test_non_device_source_is_skipped(self, mocker):
        captured = []

        async def fake_router(payload):
            captured.append(payload)

        client = _make_client_with_fake_ws()
        client._router = MagicMock()
        client._router.route_event = AsyncMock(side_effect=fake_router)

        ws = _FakeWs()
        # Mode-change comes through as source=LOCATION; we don't handle it yet
        ws.push(json.dumps({
            "source": "LOCATION",
            "name": "mode",
            "value": "Night",
            "displayName": "Mode Changed",
        }))
        # APP_STATUS too — skip
        ws.push(json.dumps({
            "source": "APP_STATUS",
            "name": "appstatus",
            "value": "running",
        }))
        # A real DEVICE frame at the end so we know the drain didn't bail
        ws.push(json.dumps(make_eventsocket_frame(
            deviceId="100",
            name="motion",
            value="active",
        )))
        mocker.patch(
            "services.hubitat_eventsocket_client.DATA_WATCHDOG_SECS", 0.05
        )

        await client._drain("hub4", "<LAN_IP>", 1, ws)

        # Only the DEVICE frame was forwarded
        assert len(captured) == 1
        assert captured[0]["name"] == "motion"

    async def test_invalid_json_frame_skipped_silently(self, mocker):
        captured = []

        async def fake_router(payload):
            captured.append(payload)

        client = _make_client_with_fake_ws()
        client._router = MagicMock()
        client._router.route_event = AsyncMock(side_effect=fake_router)

        ws = _FakeWs()
        ws.push("not json at all {{{")
        ws.push(json.dumps(make_eventsocket_frame()))
        mocker.patch(
            "services.hubitat_eventsocket_client.DATA_WATCHDOG_SECS", 0.05
        )

        await client._drain("hub4", "<LAN_IP>", 1, ws)

        # Bad frame skipped, good one through
        assert len(captured) == 1

    async def test_router_exception_does_not_kill_stream(self, mocker):
        call_count = 0

        async def flaky_router(payload):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first call boom")
            # Second call succeeds

        client = _make_client_with_fake_ws()
        client._router = MagicMock()
        client._router.route_event = AsyncMock(side_effect=flaky_router)

        ws = _FakeWs()
        ws.push(json.dumps(make_eventsocket_frame(deviceId="1")))
        ws.push(json.dumps(make_eventsocket_frame(deviceId="2")))
        mocker.patch(
            "services.hubitat_eventsocket_client.DATA_WATCHDOG_SECS", 0.05
        )

        result = await client._drain("hub4", "<LAN_IP>", 1, ws)

        # Both frames attempted; the stream survives the first exception
        assert call_count == 2
        assert result is True


@pytest.mark.service
class TestWatchdog:
    async def test_watchdog_returns_false_when_no_events(self, mocker):
        client = _make_client_with_fake_ws()
        client._router = MagicMock()
        client._router.route_event = AsyncMock()
        # PATCH the hub_health write so it doesn't try real HTTP
        client._patch_health = MagicMock()

        ws = _FakeWs()  # no frames pushed — recv blocks forever
        mocker.patch(
            "services.hubitat_eventsocket_client.DATA_WATCHDOG_SECS", 0.05
        )

        result = await client._drain("hub4", "<LAN_IP>", 1, ws)

        # No data arrived → got_data stays False
        assert result is False
        # Watchdog fired → hub_health marked failure with watchdog_no_events
        client._patch_health.assert_called()
        # Find a call with watchdog_no_events in failure reason
        failure_calls = [
            c for c in client._patch_health.call_args_list
            if c[0][1].get("ws_last_failure_reason") == "watchdog_no_events"
        ]
        assert len(failure_calls) == 1

    async def test_watchdog_does_not_fire_if_events_arrive_in_time(
        self, mocker
    ):
        client = _make_client_with_fake_ws()
        client._router = MagicMock()
        client._router.route_event = AsyncMock()
        client._patch_health = MagicMock()

        ws = _FakeWs()
        ws.push(json.dumps(make_eventsocket_frame()))
        mocker.patch(
            "services.hubitat_eventsocket_client.DATA_WATCHDOG_SECS", 0.2
        )

        result = await client._drain("hub4", "<LAN_IP>", 1, ws)

        # Event arrived → got_data=True. After that, queue is empty and
        # the SECOND recv waits 0.2s and watchdog fires — but got_data is
        # already True, so result is True.
        assert result is True
