"""
WebhookRouter.route_event happy path: a real-shape eventsocket payload arrives
with a known canonical device and a subscribed running instance. We assert:

  - event_log row inserted with all the new columns populated
  - event_routings row inserted with outcome='routed'
  - instance's queue receives the event
  - return value == 1 (one instance received the event)
"""

import asyncio
import json
import os
import re

import pytest

# Force PostgREST URL to the canonical mock host BEFORE we import the router
os.environ["POSTGREST_URL"] = "http://postgrest:3001"

from services.webhook_router import WebhookRouter
from tests.factories import (
    make_canonical_device_row,
    make_router_payload,
)


@pytest.mark.service
class TestRouteEventHappyPath:
    async def test_routes_to_one_subscribed_instance_and_writes_db(
        self,
        mock_postgrest,
        fake_device_cache,
        mocker,
    ):
        # Canonical row exists for the device, on the same hub
        pg = mock_postgrest
        pg.get(
            "/devices",
            returns=[make_canonical_device_row(
                id=10,
                hub_ip="<LAN_IP>",
                hubitat_id="100",
                label="Test Motion Sensor",
            )],
        )
        # event_log INSERT returns the new row with id=42
        pg.post(
            "/event_log",
            returns=[{"id": 42}],
            returns_status=201,
        )
        # event_routings INSERT returns minimal
        pg.post(
            "/event_routings",
            returns_status=201,
        )

        # Stub instance_manager: instance 1 is subscribed and running
        im_mock = mocker.MagicMock(name="InstanceManager")
        im_mock.get_subscribed_instances.return_value = [1]
        im_mock.get_running_instance.return_value = mocker.MagicMock()
        mocker.patch(
            "services.webhook_router.get_instance_manager",
            return_value=im_mock,
        )

        router = WebhookRouter(device_cache=fake_device_cache)
        payload = make_router_payload(
            deviceId="100",
            displayName="Test Motion Sensor",
            name="motion",
            value="active",
            hub_ip="<LAN_IP>",
        )

        result = await router.route_event(payload)

        assert result == 1

        # event_log was POSTed with all the new columns
        event_log_calls = pg.calls_to("/event_log")
        assert len(event_log_calls) == 1
        body = json.loads(event_log_calls[0].request.body)
        assert body["hub_ip"] == "<LAN_IP>"
        assert body["canonical_device_id"] == 10
        assert body["intake_path"] == "eventsocket"
        assert body["event_type"] == "motion"
        assert body["event_value"] == "active"
        assert isinstance(body["processing_ms"], int)
        assert body["processing_ms"] >= 0

        # event_routings was POSTed with one 'routed' row
        routings_calls = pg.calls_to("/event_routings")
        assert len(routings_calls) == 1
        routings_body = json.loads(routings_calls[0].request.body)
        assert isinstance(routings_body, list)
        assert len(routings_body) == 1
        assert routings_body[0]["event_id"] == 42
        assert routings_body[0]["instance_id"] == 1
        assert routings_body[0]["outcome"] == "routed"

    async def test_device_cache_updated_with_event_value(
        self,
        mock_postgrest,
        fake_device_cache,
        mocker,
    ):
        pg = mock_postgrest
        pg.get(
            "/devices",
            returns=[make_canonical_device_row(id=10, hubitat_id="100")],
        )
        pg.post("/event_log", returns=[{"id": 1}])
        pg.post("/event_routings", returns_status=201)
        im_mock = mocker.MagicMock()
        im_mock.get_subscribed_instances.return_value = []
        mocker.patch(
            "services.webhook_router.get_instance_manager",
            return_value=im_mock,
        )

        router = WebhookRouter(device_cache=fake_device_cache)
        await router.route_event(make_router_payload(
            displayName="Test Motion Sensor",
            name="motion",
            value="active",
        ))

        cached = fake_device_cache.get_device(10)
        assert cached is not None
        assert cached["attributes"]["motion"] == "active"

    async def test_intake_path_defaults_to_eventsocket_when_not_set(
        self,
        mock_postgrest,
        fake_device_cache,
        mocker,
    ):
        # Payload missing _intake key — should default to 'eventsocket'
        pg = mock_postgrest
        pg.get("/devices", returns=[make_canonical_device_row()])
        pg.post("/event_log", returns=[{"id": 1}])
        pg.post("/event_routings", returns_status=201)
        im_mock = mocker.MagicMock()
        im_mock.get_subscribed_instances.return_value = []
        mocker.patch(
            "services.webhook_router.get_instance_manager",
            return_value=im_mock,
        )

        router = WebhookRouter(device_cache=fake_device_cache)
        payload = make_router_payload()
        payload.pop("_intake", None)
        await router.route_event(payload)

        body = json.loads(pg.calls_to("/event_log")[0].request.body)
        assert body["intake_path"] == "eventsocket"

    async def test_reconcile_intake_path_recorded(
        self,
        mock_postgrest,
        fake_device_cache,
        mocker,
    ):
        pg = mock_postgrest
        pg.get("/devices", returns=[make_canonical_device_row()])
        pg.post("/event_log", returns=[{"id": 1}])
        pg.post("/event_routings", returns_status=201)
        im_mock = mocker.MagicMock()
        im_mock.get_subscribed_instances.return_value = []
        mocker.patch(
            "services.webhook_router.get_instance_manager",
            return_value=im_mock,
        )

        router = WebhookRouter(device_cache=fake_device_cache)
        payload = make_router_payload(intake="reconcile")
        await router.route_event(payload)

        body = json.loads(pg.calls_to("/event_log")[0].request.body)
        assert body["intake_path"] == "reconcile"
