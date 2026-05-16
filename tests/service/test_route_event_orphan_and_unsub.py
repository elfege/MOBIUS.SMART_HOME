"""
Two related "nothing routes" cases:

1. ORPHAN — no canonical row for the device (devices table doesn't contain
   the label or its deviceId). event_log row IS written (so we can audit
   what unrecognised events arrived), routings get one row with
   outcome='dropped_orphan'.

2. UNSUB — canonical row exists but no instance is subscribed to the event
   type. event_log row written; no routings rows (nothing to drop, nothing
   routed). Subscription-not-running variant produces 'dropped_unsub'.
"""

import json
import os

import pytest

os.environ["POSTGREST_URL"] = "http://postgrest:3001"

from services.webhook_router import WebhookRouter
from tests.factories import make_canonical_device_row, make_router_payload


@pytest.mark.service
class TestOrphanEvent:
    async def test_no_canonical_row_writes_dropped_orphan_routing(
        self,
        mock_postgrest,
        fake_device_cache,
        mocker,
    ):
        pg = mock_postgrest
        # Label lookup returns empty list → no canonical match
        pg.get("/devices", returns=[])
        # Fallback path: GET /devices?id=eq.X for the canonical-PK fallback
        # also returns empty. We need to register this for the
        # get_device_by_canonical_id call.
        pg.get("/devices", returns=[])  # second GET
        pg.post("/event_log", returns=[{"id": 100}])
        pg.post("/event_routings", returns_status=201)
        im_mock = mocker.MagicMock()
        mocker.patch(
            "services.webhook_router.get_instance_manager",
            return_value=im_mock,
        )

        router = WebhookRouter(device_cache=fake_device_cache)
        # deviceId is non-digit so canonical-PK fallback is skipped entirely
        payload = make_router_payload(
            deviceId="not-a-number",
            displayName="Unknown Device",
            hub_ip="<LAN_IP>",
        )

        result = await router.route_event(payload)

        # event_log was written so we have a record of the orphan arrival
        event_log_calls = pg.calls_to("/event_log")
        assert len(event_log_calls) == 1
        body = json.loads(event_log_calls[0].request.body)
        assert body["canonical_device_id"] is None
        assert body["device_name"] == "Unknown Device"

        # event_routings was written with one 'dropped_orphan' row
        routings_calls = pg.calls_to("/event_routings")
        assert len(routings_calls) == 1
        routings_body = json.loads(routings_calls[0].request.body)
        assert len(routings_body) == 1
        assert routings_body[0]["outcome"] == "dropped_orphan"
        assert routings_body[0]["instance_id"] is None

        assert result == 0


@pytest.mark.service
class TestUnsubscribed:
    async def test_no_subscriptions_writes_event_log_but_no_routings(
        self,
        mock_postgrest,
        fake_device_cache,
        mocker,
    ):
        pg = mock_postgrest
        pg.get("/devices", returns=[make_canonical_device_row(id=10)])
        pg.post("/event_log", returns=[{"id": 200}])
        pg.post("/event_routings", returns_status=201)

        im_mock = mocker.MagicMock()
        im_mock.get_subscribed_instances.return_value = []  # nobody subscribed
        mocker.patch(
            "services.webhook_router.get_instance_manager",
            return_value=im_mock,
        )

        router = WebhookRouter(device_cache=fake_device_cache)
        result = await router.route_event(make_router_payload())

        assert result == 0
        # event_log row written
        assert len(pg.calls_to("/event_log")) == 1
        # No routings written — empty list means nothing to insert
        assert len(pg.calls_to("/event_routings")) == 0

    async def test_subscribed_but_not_running_writes_dropped_unsub(
        self,
        mock_postgrest,
        fake_device_cache,
        mocker,
    ):
        pg = mock_postgrest
        pg.get("/devices", returns=[make_canonical_device_row(id=10)])
        pg.post("/event_log", returns=[{"id": 300}])
        pg.post("/event_routings", returns_status=201)

        im_mock = mocker.MagicMock()
        im_mock.get_subscribed_instances.return_value = [99]
        im_mock.get_running_instance.return_value = None  # subscribed but not running
        mocker.patch(
            "services.webhook_router.get_instance_manager",
            return_value=im_mock,
        )

        router = WebhookRouter(device_cache=fake_device_cache)
        result = await router.route_event(make_router_payload())

        assert result == 0
        routings_body = json.loads(pg.calls_to("/event_routings")[0].request.body)
        assert len(routings_body) == 1
        assert routings_body[0]["outcome"] == "dropped_unsub"
        assert routings_body[0]["instance_id"] == 99
