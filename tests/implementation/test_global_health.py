"""
GLOBAL implementation tests (Tier 1, observational) — the whole app, real, read-only.

These assert stable contracts of the REAL running system. They MUTATE NOTHING
(the `client` fixture is the read-only guard), so they are safe against the live
home and run in CI against it.

`test_devices_endpoint_is_served` is a REGRESSION GUARD: on 2026-07-14 the live
`GET /api/devices` was returning 500 because of `pg_statistic` TOAST corruption
(latent fallout of the 2026-07-13 two-postmaster incident). Nothing in the mocked
logic tiers could ever have caught that — only hitting the real endpoint does.
That is the operator's whole point: "test the real thing, not logic."
"""
import pytest

pytestmark = [pytest.mark.implementation, pytest.mark.observational]


def test_app_is_live(client):
    """The real app answers its liveness probe. (Reachability is also asserted in
    the `client` fixture — a down app FAILS, per R2 is-not=FAIL.)"""
    r = client.get("/api/app-types")
    assert r.status_code == 200
    assert isinstance(r.json(), (list, dict))


def test_instances_enumerate_with_valid_structure(client, app_types):
    """Every REAL instance is well-formed and bound to a known app type."""
    data = client.get_json("/api/instances")
    instances = data if isinstance(data, list) else data.get("instances", data)
    assert instances, "the real app reports ZERO instances — unexpected on the live home"
    for i in instances:
        assert isinstance(i.get("id"), int), f"instance missing int id: {i!r}"
        atid = i.get("app_type_id")
        assert atid in app_types["by_id"], (
            f"instance {i.get('id')} has app_type_id {atid} with no known type "
            f"(known: {sorted(app_types['by_id'])})"
        )
        assert isinstance(i.get("is_paused"), bool)
        assert isinstance(i.get("is_enabled"), bool)
        # device_selections + settings are the operator's real config — must be maps.
        assert isinstance(i.get("device_selections"), dict)
        assert isinstance(i.get("settings"), dict)


def test_devices_endpoint_is_served(client):
    """REGRESSION (2026-07-14 live outage): the device roster must serve 200 with
    a real, non-empty list. A 500 here means the real data path is broken (as it
    was, from pg_statistic corruption) — exactly what a logic test cannot see."""
    r = client.get("/api/devices")
    assert r.status_code == 200, (
        f"GET /api/devices -> {r.status_code}; the live device roster is broken. "
        f"Body: {r.text[:300]}"
    )
    data = r.json()
    devices = data if isinstance(data, list) else data.get("devices", data)
    assert isinstance(devices, list) and devices, "device roster is empty/non-list on the live app"
    sample = devices[0]
    assert "id" in sample and ("label" in sample or "name" in sample)


def test_panel_roster_requires_enrolled_token(client):
    """The tiles panel is enrolled-token gated (confirmed real contract): an
    unauthenticated request is refused 401, never served. This guards the
    confused-deputy surface the cutover auth ruling closes."""
    r = client.get("/api/panel/devices?profile=default")
    assert r.status_code in (401, 403), (
        f"/api/panel/devices unauthenticated -> {r.status_code}; the panel MUST "
        "reject unauthenticated reads (enrolled-token auth)."
    )


@pytest.mark.parametrize("path", [
    "/api/matter/nodes",
    "/api/matter/hubitat-devices",
    "/api/matter/status",
])
def test_matter_read_endpoints_serve(client, path):
    """The Matter read surface answers 200 on the real app."""
    r = client.get(path)
    assert r.status_code == 200, f"GET {path} -> {r.status_code}: {r.text[:200]}"


def test_readonly_guard_blocks_live_mutation(client):
    """META: prove the Tier-1 safety guard itself works — a mutating verb against
    the live app raises BEFORE any request leaves the machine. This is what makes
    running the observational suite against the live home safe."""
    from tests.implementation.conftest import LiveMutationError
    with pytest.raises(LiveMutationError):
        client.request("POST", "/api/instances")
    with pytest.raises(LiveMutationError):
        client.request("DELETE", "/api/matter/map")
