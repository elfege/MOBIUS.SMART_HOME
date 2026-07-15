"""
Unit tests for the sibling failover chain (services/command_failover +
DeviceCommander._failover_chain).

The ordering logic is pure — tested directly. The chain itself is tested with
monkeypatched sibling fetch + admin client fakes, asserting the RULED policy:
sequential (never concurrent), primary-hub sibling first, one tight attempt
each, overall-timeout respected, and a win mutates the CommandResult so the
operator sees SUCCESS instead of the home_2 None-storm's false failure.
"""
from unittest.mock import patch

import pytest

from services.command_failover import order_targets


def row(id_, hubitat_id, hub_ip, hub_name, enabled=True, primary=False, hub_id=1):
    return {"id": id_, "hubitat_id": hubitat_id, "hub_id": hub_id,
            "hub_config": {"hub_ip": hub_ip, "hub_name": hub_name,
                           "is_enabled": enabled, "is_primary": primary}}


class TestOrderTargets:
    def test_primary_hub_first_then_hub_id(self):
        rows = [row(3, "30", "10.0.0.3", "home_3", hub_id=3),
                row(1, "10", "10.0.0.1", "home_1", primary=True, hub_id=1),
                row(2, "20", "10.0.0.2", "home_2", hub_id=2)]
        t = order_targets(rows)
        assert [x["hub_name"] for x in t] == ["home_1", "home_2", "home_3"]

    def test_excluded_hub_and_disabled_hub_dropped(self):
        rows = [row(1, "10", "10.0.0.1", "home_1", primary=True),
                row(2, "20", "10.0.0.2", "home_2", enabled=False),
                row(3, "30", "10.0.0.3", "home_3")]
        t = order_targets(rows, exclude_hub_ip="10.0.0.1")
        assert [x["hub_name"] for x in t] == ["home_3"]

    def test_one_target_per_hub(self):
        rows = [row(1, "10", "10.0.0.1", "home_1"),
                row(2, "11", "10.0.0.1", "home_1")]
        assert len(order_targets(rows)) == 1

    def test_rows_without_routing_dropped(self):
        assert order_targets([{"id": 1, "hubitat_id": None, "hub_config": {}}]) == []
        assert order_targets([]) == []

    def test_helper_keys_stripped(self):
        t = order_targets([row(1, "10", "10.0.0.1", "home_1")])
        assert set(t[0].keys()) == {"id", "hubitat_id", "hub_ip", "hub_name"}


class _FakeAdmin:
    """Admin client fake: deaf on home_2 (send ok, never actuates), good on
    home_1 (actuates immediately) — the exact None-storm shape."""
    def __init__(self, hub_name):
        self.hub_name = hub_name
        self.sent = []

    def send_command(self, device_id, command, arg=None):
        self.sent.append((device_id, command))
        return True

    def get_device(self, device_id):
        # /device/fullJson shape: state nests under 'device'.'currentStates'
        # (to_maker_shape converts it to the Maker attributes list).
        if self.hub_name == "home_1":
            return {"device": {"currentStates": {"switch": {"value": "off"}}}}
        return {"device": {"currentStates": {}}}  # deaf copy: no state ever


class TestFailoverChain:
    @pytest.fixture
    def commander(self):
        from services.device_commander import DeviceCommander
        c = DeviceCommander.__new__(DeviceCommander)  # no network in __init__
        c.verify_retries = 3
        c.verify_delay = 0.0
        c.operation_retries = 2
        c.operation_delay = 0.0
        c.command_timeout = 30.0
        c._status_lock = __import__("threading").Lock()
        c._device_status = {}
        c._verify_fail_counts = {}
        c._verify_backoff_until = {}
        return c

    def _result(self):
        from services.device_commander import CommandResult
        return CommandResult(device_id="200", device_name="TV POWER",
                             command="off", args=None)

    def test_win_on_primary_sibling_mutates_result(self, commander):
        import time
        fakes = {}

        def fake_get_client(ip, name):
            return fakes.setdefault(name, _FakeAdmin(name))

        targets_rows = [row(261, "2954", "10.0.0.1", "home_1", primary=True)]
        res = self._result()
        with patch("services.command_failover.fetch_sibling_rows",
                   return_value=targets_rows), \
             patch("services.device_to_hubs_classifier.get_device_by_canonical_id",
                   return_value={"label": "TV POWER"}), \
             patch("services.hubitat_admin_client.get_client", fake_get_client), \
             patch.object(type(commander), "_update_cache_after_verify",
                          lambda self, *a, **k: None):
            ok = commander._failover_chain(
                "200", "off", None, verify=True,
                expected={"attribute": "switch", "expected": "off"},
                result=res, exclude_hub_ip="10.0.0.2",
                device_name="TV POWER", start_time=time.monotonic(),
                use_admin=True)
        assert ok is True
        assert res.verified is True and res.success is True
        assert res.actual_state == "off" and res.error is None
        assert fakes["home_1"].sent == [(2954, "off")]

    def test_no_siblings_returns_false_untouched(self, commander):
        import time
        res = self._result()
        with patch("services.command_failover.fetch_sibling_rows", return_value=[]), \
             patch("services.device_to_hubs_classifier.get_device_by_canonical_id",
                   return_value={"label": "TV POWER"}):
            ok = commander._failover_chain(
                "200", "off", None, True,
                {"attribute": "switch", "expected": "off"}, res,
                exclude_hub_ip=None, device_name="TV POWER",
                start_time=time.monotonic(), use_admin=True)
        assert ok is False and res.verified is False

    def test_overall_timeout_stops_chain_before_sending(self, commander):
        import time
        fakes = {}

        def fake_get_client(ip, name):
            return fakes.setdefault(name, _FakeAdmin(name))

        res = self._result()
        with patch("services.command_failover.fetch_sibling_rows",
                   return_value=[row(261, "2954", "10.0.0.1", "home_1")]), \
             patch("services.device_to_hubs_classifier.get_device_by_canonical_id",
                   return_value={"label": "TV POWER"}), \
             patch("services.hubitat_admin_client.get_client", fake_get_client):
            ok = commander._failover_chain(
                "200", "off", None, True,
                {"attribute": "switch", "expected": "off"}, res,
                exclude_hub_ip=None, device_name="TV POWER",
                start_time=time.monotonic() - 999,  # already past timeout
                use_admin=True)
        assert ok is False
        assert fakes == {}  # NOTHING was sent — timeout respected up front

    def test_deaf_sibling_then_good_sibling_sequential(self, commander):
        import time
        fakes = {}
        calls_order = []

        def fake_get_client(ip, name):
            calls_order.append(name)
            return fakes.setdefault(name, _FakeAdmin(name))

        rows = [row(314, "3975", "10.0.0.2", "home_2", hub_id=2),
                row(261, "2954", "10.0.0.1", "home_1", primary=True, hub_id=1)]
        res = self._result()
        with patch("services.command_failover.fetch_sibling_rows", return_value=rows), \
             patch("services.device_to_hubs_classifier.get_device_by_canonical_id",
                   return_value={"label": "TV POWER"}), \
             patch("services.hubitat_admin_client.get_client", fake_get_client), \
             patch.object(type(commander), "_update_cache_after_verify",
                          lambda self, *a, **k: None):
            ok = commander._failover_chain(
                "200", "off", None, True,
                {"attribute": "switch", "expected": "off"}, res,
                exclude_hub_ip=None, device_name="TV POWER",
                start_time=time.monotonic(), use_admin=True)
        assert ok is True
        # primary-first ordering: home_1 tried FIRST and won -> home_2 never touched
        assert calls_order == ["home_1"]


class _DeafButAcceptingAdmin(_FakeAdmin):
    """Send accepted, readback NEVER returns state — the .70 None-storm shape."""
    def get_device(self, device_id):
        return {"device": {"currentStates": {}}}


class _ContradictingAdmin(_FakeAdmin):
    """Send accepted, readback returns a REAL wrong value (device stayed on)."""
    def get_device(self, device_id):
        return {"device": {"currentStates": {"switch": {"value": "on"}}}}


class TestTrichotomy:
    """MSG-1155: VERIFIED / CONTRADICTED->failover / INDETERMINATE->stop.
    A None readback must never cause another physical actuation."""

    def _commander(self):
        from services.device_commander import DeviceCommander
        c = DeviceCommander.__new__(DeviceCommander)
        c.verify_retries = 2
        c.verify_delay = 0.0
        c.operation_retries = 1
        c.operation_delay = 0.0
        c.command_timeout = 30.0
        c._status_lock = __import__("threading").Lock()
        c._device_status = {}
        return c

    def _run_chain(self, commander, rows, admin_cls):
        import time
        from services.device_commander import CommandResult
        fakes = {}

        def fake_get_client(ip, name):
            return fakes.setdefault(name, admin_cls(name))

        res = CommandResult(device_id="200", device_name="X", command="off", args=None)
        with patch("services.command_failover.fetch_sibling_rows", return_value=rows), \
             patch("services.device_to_hubs_classifier.get_device_by_canonical_id",
                   return_value={"label": "X"}), \
             patch("services.hubitat_admin_client.get_client", fake_get_client), \
             patch.object(type(commander), "_update_cache_after_verify",
                          lambda self, *a, **k: None):
            ok = commander._failover_chain(
                "200", "off", None, True,
                {"attribute": "switch", "expected": "off"}, res,
                exclude_hub_ip=None, device_name="X",
                start_time=time.monotonic(), use_admin=True)
        return ok, res, fakes

    def test_indeterminate_sibling_stops_chain_no_third_actuation(self):
        rows = [row(1, "10", "10.0.0.3", "home_3", hub_id=3),
                row(2, "20", "10.0.0.4", "home_4", hub_id=4)]
        ok, res, fakes = self._run_chain(self._commander(), rows, _DeafButAcceptingAdmin)
        assert ok is True                      # chain STOPPED honestly
        assert res.success is True and res.verified is False
        assert "indeterminate" in (res.error or "")
        assert set(fakes) == {"home_3"}        # second sibling NEVER actuated

    def test_contradicted_sibling_moves_to_next(self):
        rows = [row(1, "10", "10.0.0.3", "home_3", hub_id=3),
                row(2, "20", "10.0.0.4", "home_4", hub_id=4)]
        ok, res, fakes = self._run_chain(self._commander(), rows, _ContradictingAdmin)
        assert ok is False                     # both contradicted -> honest failure
        assert set(fakes) == {"home_3", "home_4"}  # chain DID walk on real wrong values
