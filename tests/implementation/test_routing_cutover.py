"""
Routing-cutover contract (Tier 1, observational) — the strangler-fig front door.

Asserts the REAL app's routing agrees with the cutover plan (2026-07-14 flip):
GET / serves the RN bundle, every legacy surface answers under /legacy/*, and
the old top-level paths 301 to their /legacy twins. If a refactor breaks any of
this, the operator loses a surface (worst case: Matter) — this suite makes that
a red build instead of a morning surprise.
"""
import pytest

pytestmark = [pytest.mark.implementation, pytest.mark.observational]

# The canonical map — mirrors apps/legacy_web/router.py (LEGACY_SURFACES + the
# instance pages). Deliberately duplicated as literals: the test must fail if
# the router changes unilaterally, not follow it.
LEGACY_PAGES = [
    "/legacy",
    "/legacy/instance/new",
    "/legacy/matter",
    "/legacy/sonos",
    "/legacy/admin/settings",
]

REDIRECTS = {  # old top-level path -> /legacy twin (301)
    "/matter": "/legacy/matter",
    "/sonos": "/legacy/sonos",
    "/admin/settings": "/legacy/admin/settings",
    "/instance/new": "/legacy/instance/new",
    "/hubs": "/legacy/admin/settings",
}


def test_root_serves_the_rn_bundle(client):
    """GET / is the RN admin app (Expo web export), not the Jinja dashboard."""
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "_expo" in body or "expo" in body.lower(), (
        "GET / does not look like the RN (Expo web) bundle — the cutover front "
        f"door regressed. First 200 chars: {body[:200]}"
    )
    assert "dashboard.html" not in body


@pytest.mark.parametrize("path", LEGACY_PAGES)
def test_legacy_surface_serves(client, path):
    """Every legacy surface answers 200 under /legacy/*."""
    r = client.get(path)
    assert r.status_code == 200, f"GET {path} -> {r.status_code}"
    assert "<html" in r.text.lower()


@pytest.mark.parametrize("old,new", sorted(REDIRECTS.items()))
def test_old_paths_redirect_to_legacy(client, old, new):
    """The pre-cutover paths 301 to their /legacy twins (bookmarks + the legacy
    navbars keep working). /hubs is 301 or 307 (it was already a redirect)."""
    r = client.get(old, allow_redirects=False)
    assert r.status_code in (301, 307, 308), f"GET {old} -> {r.status_code}, expected a redirect"
    loc = r.headers.get("location", "")
    assert loc == new, f"GET {old} redirects to {loc!r}, expected {new!r}"


def test_legacy_instance_detail_serves(client, real_instances):
    """The legacy edit page works per-instance (uses a REAL instance id)."""
    iid = real_instances[0]["id"]
    r = client.get(f"/legacy/instance/{iid}")
    assert r.status_code == 200
    old = client.get(f"/instance/{iid}", allow_redirects=False)
    assert old.status_code == 301
    assert old.headers.get("location") == f"/legacy/instance/{iid}"
