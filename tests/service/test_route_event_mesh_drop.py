"""
Mesh-mirror filter: when event arrives on hub X for a device whose canonical
row says hub_ip=Y (different hub), the event is a Hub Mesh mirror and must
be dropped before any DB write or routing.

This is the cleanest data-integrity rule in the system. If this regresses,
mesh-meshed devices route 4x and double-fire timeouts.
"""

import json
import os

import pytest

os.environ["POSTGREST_URL"] = "http://postgrest:3001"

from services.webhook_router import WebhookRouter
from tests.factories import make_canonical_device_row, make_router_payload


@pytest.mark.service
class TestMeshMirrorDrop:
    async def test_drops_when_event_hub_differs_from_canonical_hub(
        self,
        mock_postgrest,
        fake_device_cache,
        mocker,
    ):
        pg = mock_postgrest
        # Canonical row says the device lives on hub <LAN_IP> (MAIN)
        pg.get(
            "/devices",
            returns=[make_canonical_device_row(
                id=10,
                hub_ip="<LAN_IP>",
                hubitat_id="100",
                label="Some Mesh-shared Device",
            )],
        )
        # No event_log / event_routings inserts expected — but register
        # them so an accidental write would not 404 silently.
        pg.post("/event_log", returns=[{"id": 1}])
        pg.post("/event_routings", returns_status=201)
        im_mock = mocker.MagicMock()
        mocker.patch(
            "services.webhook_router.get_instance_manager",
            return_value=im_mock,
        )

        router = WebhookRouter(device_cache=fake_device_cache)

        # Event arrives from hub <LAN_IP> (Home_2) for the same label.
        # That's a mesh mirror.
        payload = make_router_payload(
            displayName="Some Mesh-shared Device",
            hub_ip="<LAN_IP>",
        )

        result = await router.route_event(payload)

        # Drop → returns 0 and no DB writes happened
        assert result == 0
        assert pg.calls_to("/event_log") == []
        assert pg.calls_to("/event_routings") == []
        im_mock.get_subscribed_instances.assert_not_called()

    async def test_keeps_when_event_hub_matches_canonical_hub(
        self,
        mock_postgrest,
        fake_device_cache,
        mocker,
    ):
        pg = mock_postgrest
        pg.get(
            "/devices",
            returns=[make_canonical_device_row(
                id=10,
                hub_ip="<LAN_IP>",
                hubitat_id="100",
                label="Origin Device",
            )],
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
        payload = make_router_payload(
            displayName="Origin Device",
            hub_ip="<LAN_IP>",  # matches canonical
        )

        result = await router.route_event(payload)

        # Routes (or no-op routes if no subscribers, but DB write happens)
        assert len(pg.calls_to("/event_log")) == 1

    async def test_drops_mesh_mirror_with_on_home_n_suffix(
        self,
        mock_postgrest,
        fake_device_cache,
        mocker,
    ):
        # When Hub Mesh shares a device, the mirror's label gets ' on Home N'.
        # The router strips that suffix before looking up the canonical row,
        # then applies the mesh filter on hub_ip mismatch.
        pg = mock_postgrest
        pg.get(
            "/devices",
            returns=[make_canonical_device_row(
                id=10,
                hub_ip="<LAN_IP>",
                hubitat_id="100",
                label="Light Living Room",
            )],
        )
        pg.post("/event_log", returns=[{"id": 1}])
        pg.post("/event_routings", returns_status=201)
        im_mock = mocker.MagicMock()
        mocker.patch(
            "services.webhook_router.get_instance_manager",
            return_value=im_mock,
        )

        router = WebhookRouter(device_cache=fake_device_cache)
        # Mirror's label has ' on Home 2' suffix, event from Home_2
        payload = make_router_payload(
            displayName="Light Living Room on Home 2",
            hub_ip="<LAN_IP>",
        )

        result = await router.route_event(payload)

        assert result == 0
        assert pg.calls_to("/event_log") == []
