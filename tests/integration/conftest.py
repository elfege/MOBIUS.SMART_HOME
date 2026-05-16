"""
Integration-tier fixtures — talk to the LIVE smart-home stack on the host.

Container port mappings (per docker-compose.yml):
  smarthome-postgres   → host 5433
  smarthome-postgrest  → host 3002
  smarthome-app        → host 5001

All integration tests skip cleanly if the stack is not running.

CLEANUP CONTRACT
----------------
Any test that inserts rows MUST clean up after itself, or the user's
real event_log / event_routings / device_commands will fill with junk.
Use `device_name LIKE '__test__%'` and matching prefixes so cleanup
is one DELETE per table per test.
"""

import os
import socket
from typing import Generator

import pytest
import requests

POSTGREST_LIVE_URL = "http://localhost:3002"
POSTGRES_LIVE_HOST = "localhost"
POSTGRES_LIVE_PORT = 5433

# Marker substring all integration test rows include in identifying columns
# so cleanup is unambiguous.
TEST_PREFIX = "__test__"


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _postgrest_alive() -> bool:
    try:
        r = requests.get(POSTGREST_LIVE_URL, timeout=1)
        return r.status_code in (200, 404)  # any response means it's up
    except Exception:
        return False


@pytest.fixture(scope="session")
def live_postgrest_url() -> str:
    """URL of the live PostgREST. Skips the test if not reachable."""
    if not _postgrest_alive():
        pytest.skip(
            "smarthome-postgrest not reachable at "
            f"{POSTGREST_LIVE_URL} — integration tests skipped"
        )
    return POSTGREST_LIVE_URL


@pytest.fixture
def cleanup_test_rows(live_postgrest_url):
    """Yields, then DELETEs every row whose distinguishing column starts
    with TEST_PREFIX. Run before AND after the test for paranoia."""

    def _purge():
        # event_routings cascade-deletes via event_log FK, but be explicit
        # in case a test orphaned routings.
        for endpoint, key in [
            ("event_routings", "drop_reason"),
            ("device_commands", "hubitat_device_id"),
            ("event_log", "device_name"),
            ("hub_health", None),  # never purge
        ]:
            if endpoint == "hub_health":
                continue
            try:
                requests.delete(
                    f"{live_postgrest_url}/{endpoint}",
                    params={key: f"like.{TEST_PREFIX}%"},
                    timeout=5,
                )
            except Exception:
                pass

    _purge()
    yield
    _purge()
