"""
Shared fixtures for the MOBIUS.SMART_HOME test suite.

Boundary mocks
--------------
We mock at *system boundaries* — never at module internals — so the code
under test runs as it does in production except for:
  - PostgREST HTTP (responses library)
  - HubitatClient HTTP (MagicMock)
  - websocket transport (custom fake)
  - DeviceCache (in-memory fake)

Everything else (router internals, factories, etc.) is real code.

How the fixtures plug together
------------------------------
- `mock_postgrest` returns a `RequestsMock` that catches every HTTP call to
  http://postgrest:3001/* and a helper to register canned responses.
- `mock_hubitat_client` is a MagicMock that returns whatever you tell it to.
- `fake_device_cache` is a dict-backed stub for DeviceCache.
- `instance_manager_clean` resets the singleton between tests so state from
  one test doesn't bleed into the next.
"""

import asyncio
import json
import os
import sys
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
import responses

# Add project root to sys.path so `services.*` and `apps.*` import.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force PostgREST URL to the canonical container-internal hostname so the
# `responses` mock catches everything regardless of test env.
os.environ.setdefault("POSTGREST_URL", "http://postgrest:3001")


# ---------------------------------------------------------------------------
# PostgREST mock
# ---------------------------------------------------------------------------


class PostgrestMock:
    """Wraps responses.RequestsMock with a tiny DSL for canned answers.

    Usage:
        pg.get("/devices", returns=[make_canonical_device_row()])
        pg.post("/event_log", returns_status=201, returns={"id": 42})
        pg.patch("/hub_health", returns_status=204)

    After the test, .calls inspects what was hit:
        assert any('event_log' in c.request.url for c in pg.calls)
    """

    def __init__(self, rsps: responses.RequestsMock):
        self.rsps = rsps
        self.base_url = os.environ["POSTGREST_URL"]

    def get(
        self,
        path: str,
        *,
        returns: Any = None,
        returns_status: int = 200,
        query: Optional[Dict[str, str]] = None,
    ):
        url = self.base_url + path
        self.rsps.add(
            method=responses.GET,
            url=url,
            json=returns if returns is not None else [],
            status=returns_status,
            match=[responses.matchers.query_param_matcher(query)] if query else [],
        )

    def post(
        self,
        path: str,
        *,
        returns: Any = None,
        returns_status: int = 201,
    ):
        url = self.base_url + path
        # Accept any body — tests assert on the request via `.calls`
        self.rsps.add(
            method=responses.POST,
            url=url,
            json=returns if returns is not None else [],
            status=returns_status,
        )

    def patch(
        self,
        path: str,
        *,
        returns_status: int = 204,
        returns: Any = None,
    ):
        url = self.base_url + path
        self.rsps.add(
            method=responses.PATCH,
            url=url,
            json=returns,
            status=returns_status,
        )

    @property
    def calls(self):
        return self.rsps.calls

    def calls_to(self, path_substring: str) -> List[Any]:
        """All recorded calls whose URL contains the given substring."""
        return [c for c in self.rsps.calls if path_substring in c.request.url]


@pytest.fixture
def mock_postgrest():
    """Per-test PostgREST mock. Use pg.get/post/patch to register canned
    responses; inspect pg.calls afterward to assert what was hit."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield PostgrestMock(rsps)


# ---------------------------------------------------------------------------
# HubitatClient mock
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_hubitat_client():
    """A MagicMock shaped like HubitatClient. Tests configure return values
    on the methods they actually call."""
    client = MagicMock(name="HubitatClient")
    # Sensible defaults — override per-test
    client.send_command.return_value = True
    client.get_device.return_value = None
    client.get_all_devices.return_value = []
    # Config attribute that some code paths read (hub_ip is on client.config.hub_ip)
    client.config = MagicMock()
    client.config.hub_ip = "<LAN_IP>"
    return client


# ---------------------------------------------------------------------------
# DeviceCache fake (in-memory, dict-backed)
# ---------------------------------------------------------------------------


class FakeDeviceCache:
    """In-memory stand-in for services.device_cache.DeviceCache.

    Implements just the methods the code under test actually calls:
      - get_device(canonical_id) → dict or None
      - update_device_attribute(canonical_id, attr_name, value)
      - update_all(devices, hub_ip=None)
    """

    def __init__(self):
        self._store: Dict[str, Dict[str, Any]] = {}

    def get_device(self, device_id) -> Optional[Dict[str, Any]]:
        return self._store.get(str(device_id))

    def update_device_attribute(self, device_id, attr_name: str, value: Any):
        row = self._store.setdefault(str(device_id), {
            "id": device_id,
            "attributes": {},
        })
        row.setdefault("attributes", {})[attr_name] = value

    def update_all(self, devices, hub_ip=None):
        # Minimal — tests that need this should call set_attribute directly
        pass

    # Helpers for tests
    def set_attribute(self, device_id, attr_name: str, value: Any):
        self.update_device_attribute(device_id, attr_name, value)

    def get_all(self) -> List[Dict[str, Any]]:
        return list(self._store.values())


@pytest.fixture
def fake_device_cache():
    """Per-test FakeDeviceCache."""
    return FakeDeviceCache()


# ---------------------------------------------------------------------------
# Fake websocket transport
# ---------------------------------------------------------------------------


class FakeWebSocket:
    """Pretends to be a websockets-library connection.

    Tests pre-load frames via push() and the client's recv() returns them
    in order. When the queue is empty, recv() blocks until a new frame is
    pushed OR close() is called (raises ConnectionClosed-like).
    """

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self._closed = True

    def push(self, frame: str):
        """Queue a frame for the next recv() to return."""
        self._queue.put_nowait(frame)

    async def recv(self):
        if self._closed:
            import websockets
            raise websockets.exceptions.ConnectionClosed(None, None)
        return await self._queue.get()

    async def close(self):
        self._closed = True


@pytest.fixture
def fake_websocket():
    return FakeWebSocket()


# ---------------------------------------------------------------------------
# Reset singletons between tests (so the global router doesn't leak state)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_global_singletons():
    """Reset module-level singletons that would otherwise leak across tests."""
    # webhook_router
    try:
        import services.webhook_router as wr
        wr._webhook_router = None
    except Exception:
        pass
    # instance_manager
    try:
        import services.instance_manager as im
        if hasattr(im, "_instance_manager"):
            im._instance_manager = None
    except Exception:
        pass
    # eventsocket client
    try:
        import services.hubitat_eventsocket_client as ec
        ec._client = None
    except Exception:
        pass
    # reconcile poll
    try:
        import services.reconcile_poll as rp
        rp._service = None
    except Exception:
        pass
    yield
