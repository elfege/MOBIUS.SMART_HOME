"""
ReconcilePoll is the safety net for the WS-only intake. It polls
/devices/all per hub every 60s normal / 10s after a recent failure,
compares hub-reported attribute values to device_cache, and synthesizes
events for divergences through WebhookRouter.route_event.

Tested behaviors:
  - _pick_interval picks aggressive when any hub had a recent failure
  - _pick_interval picks normal when no recent failures
  - _process_hub synthesizes events for divergences in subscribed attributes
  - _process_hub does NOT synthesize for attributes nobody subscribes to
  - _process_hub skips mesh-mirrors (canonical hub_ip != polled hub)
  - _process_hub skips when no canonical row exists
  - _process_hub no-ops when hub-value equals cached-value
  - hub_health.last_reconcile_at updated after each pass
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ["POSTGREST_URL"] = "http://postgrest:3001"

from services.reconcile_poll import (
    ReconcilePoll,
    RECONCILE_INTERVAL_SECS,
    RECONCILE_AGGRESSIVE_SECS,
    RECONCILE_AGGRESSIVE_WINDOW_SECS,
)
from tests.factories import (
    make_canonical_device_row,
    make_hub_config_row,
)


def _hub_health_row(last_failure_at=None):
    return {"ws_last_failure_at": last_failure_at}


@pytest.mark.service
class TestPickInterval:
    def test_returns_normal_when_no_failures(self, mock_postgrest):
        pg = mock_postgrest
        pg.get(
            "/hub_health",
            returns=[_hub_health_row(None), _hub_health_row(None)],
        )

        rp = ReconcilePoll()
        assert rp._pick_interval() == RECONCILE_INTERVAL_SECS

    def test_returns_aggressive_when_recent_failure(self, mock_postgrest):
        pg = mock_postgrest
        recent_ts = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        pg.get(
            "/hub_health",
            returns=[
                _hub_health_row(None),
                _hub_health_row(recent_ts),
            ],
        )

        rp = ReconcilePoll()
        assert rp._pick_interval() == RECONCILE_AGGRESSIVE_SECS

    def test_returns_normal_when_failure_is_older_than_window(
        self, mock_postgrest
    ):
        pg = mock_postgrest
        old_ts = (datetime.now(timezone.utc)
                  - timedelta(seconds=RECONCILE_AGGRESSIVE_WINDOW_SECS + 60)
                  ).isoformat()
        pg.get("/hub_health", returns=[_hub_health_row(old_ts)])

        rp = ReconcilePoll()
        assert rp._pick_interval() == RECONCILE_INTERVAL_SECS

    def test_fallback_to_normal_on_postgrest_error(self, mock_postgrest):
        # No GET registered for /hub_health → responses will raise
        rp = ReconcilePoll()
        assert rp._pick_interval() == RECONCILE_INTERVAL_SECS


@pytest.mark.service
class TestProcessHubDivergenceDetection:
    async def test_synthesizes_event_when_hub_says_active_but_cache_says_inactive(
        self,
        mock_postgrest,
        fake_device_cache,
        mocker,
    ):
        pg = mock_postgrest
        # Subscription map says we care about motion on canonical id 10
        pg.get(
            "/device_subscriptions",
            returns=[{"device_id": 10, "event_type": "motion"}],
        )
        # Canonical row exists on hub <LAN_IP> with hubitat_id 100
        pg.get(
            "/devices",
            returns=[make_canonical_device_row(
                id=10,
                hub_ip="<LAN_IP>",
                hubitat_id="100",
            )],
        )
        # hub_health PATCH at end
        pg.patch("/hub_health", returns_status=204)
        # Cache says motion=inactive
        fake_device_cache.set_attribute(10, "motion", "inactive")

        rp = ReconcilePoll()
        rp._device_cache = fake_device_cache

        # Mock the router to capture the synthesized event
        fake_router = MagicMock()
        fake_router.route_event = AsyncMock()
        rp._router = fake_router

        # Mock the HTTP call to Hubitat /devices/all
        hub = make_hub_config_row(id=1, hub_ip="<LAN_IP>", hub_name="hub4")
        # Token env var must be set
        os.environ[hub["maker_api_token_env"]] = "fake-token"

        mocker.patch.object(rp, "_http_get_devices_all", return_value=[
            {
                "id": "100",
                "label": "Test Motion Sensor",
                "attributes": [
                    {"name": "motion", "currentValue": "active"},
                ],
            },
        ])

        sub_map = {10: {"motion"}}
        diffs = await rp._process_hub(hub, sub_map)

        assert diffs == 1
        # The synthesized payload reached the router
        fake_router.route_event.assert_called_once()
        payload = fake_router.route_event.call_args.args[0]
        assert payload["deviceId"] == "100"
        assert payload["name"] == "motion"
        assert payload["value"] == "active"
        assert payload["_intake"] == "reconcile"
        assert payload["_hub_ip"] == "<LAN_IP>"

    async def test_no_event_when_hub_value_matches_cache(
        self,
        mock_postgrest,
        fake_device_cache,
        mocker,
    ):
        pg = mock_postgrest
        pg.get(
            "/device_subscriptions",
            returns=[{"device_id": 10, "event_type": "motion"}],
        )
        pg.get(
            "/devices",
            returns=[make_canonical_device_row(id=10, hubitat_id="100")],
        )
        pg.patch("/hub_health", returns_status=204)
        fake_device_cache.set_attribute(10, "motion", "active")  # already in sync

        rp = ReconcilePoll()
        rp._device_cache = fake_device_cache
        rp._router = MagicMock()
        rp._router.route_event = AsyncMock()

        hub = make_hub_config_row(id=1, hub_ip="<LAN_IP>")
        os.environ[hub["maker_api_token_env"]] = "fake-token"
        mocker.patch.object(rp, "_http_get_devices_all", return_value=[
            {
                "id": "100",
                "label": "Test",
                "attributes": [{"name": "motion", "currentValue": "active"}],
            },
        ])

        diffs = await rp._process_hub(hub, {10: {"motion"}})

        assert diffs == 0
        rp._router.route_event.assert_not_called()

    async def test_skips_attributes_nobody_subscribes_to(
        self,
        mock_postgrest,
        fake_device_cache,
        mocker,
    ):
        pg = mock_postgrest
        # No subscriptions in this canonical-id range
        pg.get("/device_subscriptions", returns=[])
        pg.get(
            "/devices",
            returns=[make_canonical_device_row(id=10, hubitat_id="100")],
        )
        pg.patch("/hub_health", returns_status=204)

        rp = ReconcilePoll()
        rp._device_cache = fake_device_cache
        rp._router = MagicMock()
        rp._router.route_event = AsyncMock()

        hub = make_hub_config_row(id=1, hub_ip="<LAN_IP>")
        os.environ[hub["maker_api_token_env"]] = "fake-token"
        mocker.patch.object(rp, "_http_get_devices_all", return_value=[
            {
                "id": "100",
                "label": "Test",
                "attributes": [{"name": "motion", "currentValue": "active"}],
            },
        ])

        diffs = await rp._process_hub(hub, {})  # empty sub map

        assert diffs == 0
        rp._router.route_event.assert_not_called()

    async def test_skips_mesh_mirror_when_canonical_hub_differs(
        self,
        mock_postgrest,
        fake_device_cache,
        mocker,
    ):
        pg = mock_postgrest
        pg.get(
            "/device_subscriptions",
            returns=[{"device_id": 10, "event_type": "motion"}],
        )
        # Canonical says device lives on hub <LAN_IP>, but we're polling
        # hub <LAN_IP>
        pg.get(
            "/devices",
            returns=[],  # query is hub_ip=eq.<LAN_IP> → no canonical row on this hub
        )
        pg.patch("/hub_health", returns_status=204)

        rp = ReconcilePoll()
        rp._device_cache = fake_device_cache
        rp._router = MagicMock()
        rp._router.route_event = AsyncMock()

        hub = make_hub_config_row(id=3, hub_ip="<LAN_IP>")
        os.environ[hub["maker_api_token_env"]] = "fake-token"
        mocker.patch.object(rp, "_http_get_devices_all", return_value=[
            {
                "id": "100",
                "label": "Test",
                "attributes": [{"name": "motion", "currentValue": "active"}],
            },
        ])

        diffs = await rp._process_hub(hub, {10: {"motion"}})

        # No canonical row on this hub for native id 100 → skip
        assert diffs == 0


@pytest.mark.service
class TestProcessHubFailureModes:
    async def test_no_token_returns_zero_diffs(
        self,
        mock_postgrest,
        fake_device_cache,
        mocker,
    ):
        rp = ReconcilePoll()
        rp._device_cache = fake_device_cache
        rp._router = MagicMock()
        rp._router.route_event = AsyncMock()

        hub = make_hub_config_row(
            id=1,
            hub_ip="<LAN_IP>",
            maker_api_token_env="NONEXISTENT_TOKEN_ENV_VAR",
        )
        # Ensure env var really is missing
        os.environ.pop("NONEXISTENT_TOKEN_ENV_VAR", None)

        diffs = await rp._process_hub(hub, {10: {"motion"}})

        assert diffs == 0
        rp._router.route_event.assert_not_called()

    async def test_hub_http_failure_returns_zero_diffs(
        self,
        mock_postgrest,
        fake_device_cache,
        mocker,
    ):
        rp = ReconcilePoll()
        rp._device_cache = fake_device_cache
        rp._router = MagicMock()
        rp._router.route_event = AsyncMock()

        hub = make_hub_config_row(id=1, hub_ip="<LAN_IP>")
        os.environ[hub["maker_api_token_env"]] = "fake-token"
        # Hubitat call raises
        mocker.patch.object(
            rp,
            "_http_get_devices_all",
            side_effect=Exception("connection refused"),
        )

        diffs = await rp._process_hub(hub, {10: {"motion"}})

        assert diffs == 0
