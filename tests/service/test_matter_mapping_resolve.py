"""
Coverage for services.matter_mapping.resolve_node_to_device — the exact
(hub_ip, hubitat_id) anchor that replaced the frozen #660 mapping.

Resolution chain:
  node_id -> hubitat_matter_devices(our_node_id) -> (hub_ip, hubitat_device_id)
          -> devices[(hub_ip, hubitat_id), is_present] -> canonical row | None
"""

from unittest.mock import MagicMock, patch

import pytest

from services import matter_mapping


pytestmark = pytest.mark.service


def _resp(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    return r


def _patch_get(handler):
    return patch("services.matter_mapping.requests.get", side_effect=handler)


HMD_ROW = {
    "hub_ip": "<LAN_IP>",
    "hubitat_device_id": "3871",      # CURRENT admin id (not the stale 660)
    "device_name": "Light Terrace North",
    "unique_id": "M-abc",
}
DEV_ROW = {
    "id": 170, "label": "Light Terrace North",
    "hub_ip": "<LAN_IP>", "hubitat_id": "3871", "is_present": True,
}


def _handler(hmd=HMD_ROW, dev=DEV_ROW):
    def h(url, params=None, timeout=None):
        if "hubitat_matter_devices" in url:
            return _resp([hmd] if hmd else [])
        if "/devices" in url:
            return _resp([dev] if dev else [])
        return _resp([])
    return h


def test_resolves_to_current_canonical_device():
    with _patch_get(_handler()):
        d = matter_mapping.resolve_node_to_device(2)
    assert d["id"] == 170
    assert d["hubitat_id"] == "3871"


def test_uses_hub_ip_and_hubitat_id_filters_exactly():
    """The devices query must filter on BOTH hub_ip and hubitat_id from the
    hubitat_matter_devices row (composite key), so dual-hub duplicates can't
    cross-match."""
    seen = {}

    def h(url, params=None, timeout=None):
        if "hubitat_matter_devices" in url:
            return _resp([HMD_ROW])
        if "/devices" in url:
            seen.update(params or {})
            return _resp([DEV_ROW])
        return _resp([])

    with _patch_get(h):
        matter_mapping.resolve_node_to_device(2)
    assert seen.get("hub_ip") == "eq.<LAN_IP>"
    assert seen.get("hubitat_id") == "eq.3871"
    assert seen.get("is_present") == "eq.true"


def test_node_not_commissioned_returns_none():
    with _patch_get(_handler(hmd=None)):
        assert matter_mapping.resolve_node_to_device(999) is None


def test_device_removed_returns_none_stale():
    # node exists in discovery, but no present canonical device matches → stale
    with _patch_get(_handler(dev=None)):
        assert matter_mapping.resolve_node_to_device(2) is None


def test_none_node_id_returns_none():
    # No HTTP call should even be attempted.
    with _patch_get(_handler()) as g:
        assert matter_mapping.resolve_node_to_device(None) is None
        g.assert_not_called()


def test_hmd_missing_hub_or_id_returns_none():
    bad = {"hub_ip": "<LAN_IP>", "hubitat_device_id": None}
    with _patch_get(_handler(hmd=bad)):
        assert matter_mapping.resolve_node_to_device(2) is None


# ---------------------------------------------------------------------------
# Reverse resolver: canonical device id -> Matter node (Phase 2)
# ---------------------------------------------------------------------------


def _rev_handler(dev_row=None, hmd_row=None, dmm_row=None):
    """side_effect for resolve_device_to_node's three GETs."""
    def h(url, params=None, timeout=None):
        if "/devices" in url and "hubitat_matter_devices" not in url:
            return _resp([dev_row] if dev_row else [])
        if "hubitat_matter_devices" in url:
            return _resp([hmd_row] if hmd_row else [])
        if "device_matter_map" in url:
            return _resp([dmm_row] if dmm_row else [])
        return _resp([])
    return h


def test_reverse_resolves_canonical_to_node():
    dev = {"hub_ip": "<LAN_IP>", "hubitat_id": "2959"}
    hmd = {"our_node_id": 98, "is_online": True,
           "unique_id": "U-piano", "device_name": "Light piano"}
    with _patch_get(_rev_handler(dev, hmd, {"matter_endpoint_id": 1})):
        n = matter_mapping.resolve_device_to_node(290)
    assert n["node_id"] == 98
    assert n["endpoint_id"] == 1
    assert n["is_online"] is True


def test_reverse_device_not_commissioned_returns_none():
    dev = {"hub_ip": "<LAN_IP>", "hubitat_id": "71"}
    with _patch_get(_rev_handler(dev, hmd_row=None)):
        assert matter_mapping.resolve_device_to_node(71) is None


def test_reverse_unknown_device_returns_none():
    with _patch_get(_rev_handler(dev_row=None)):
        assert matter_mapping.resolve_device_to_node(99999) is None


def test_reverse_none_id_returns_none():
    with _patch_get(_rev_handler()) as g:
        assert matter_mapping.resolve_device_to_node(None) is None
        g.assert_not_called()


def test_reverse_endpoint_defaults_to_1_without_map_row():
    dev = {"hub_ip": "<LAN_IP>", "hubitat_id": "3871"}
    hmd = {"our_node_id": 2, "is_online": True,
           "unique_id": "U-terr", "device_name": "Light Terrace North"}
    with _patch_get(_rev_handler(dev, hmd, dmm_row=None)):
        n = matter_mapping.resolve_device_to_node(170)
    assert n["endpoint_id"] == 1
