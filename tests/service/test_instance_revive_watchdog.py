"""
Coverage for instance_manager.revive_dead_instances() — the dead-instance
watchdog added 2026-06-19.

Closes the gap that left instance 5 (Motion Kitchen) stopped-and-dead for
~7h after an abandoned edit. Rules under test:
  - in DB, not paused, not running, no recent stop → REVIVE
  - paused                                         → SKIP (intentional off)
  - running                                        → SKIP
  - recently stopped, within grace window          → SKIP (active edit)
  - recently stopped, past grace window            → REVIVE (abandoned edit)
"""

import time
from unittest.mock import MagicMock

import pytest

from services.instance_manager import InstanceManager


pytestmark = pytest.mark.service


def _mgr(instances, running=(), recently_stopped=None):
    """Build an InstanceManager with the DB + registry mocked."""
    m = InstanceManager.__new__(InstanceManager)
    m.logger = MagicMock()
    m._running_instances = {iid: MagicMock() for iid in running}
    m._recently_stopped = dict(recently_stopped or {})
    m.get_all_instances = MagicMock(return_value=instances)
    m._start_from_db = MagicMock(return_value=True)
    return m


def _inst(iid, paused=False, label=None):
    return {"id": iid, "is_paused": paused, "label": label or f"inst{iid}"}


def test_dead_instance_is_revived():
    m = _mgr([_inst(5)])
    out = m.revive_dead_instances()
    assert out["revived"] == [5]
    m._start_from_db.assert_called_once_with(5)


def test_paused_instance_is_not_revived():
    m = _mgr([_inst(5, paused=True)])
    out = m.revive_dead_instances()
    assert out["revived"] == []
    assert out["skipped_paused"] == 1
    m._start_from_db.assert_not_called()


def test_running_instance_is_not_revived():
    m = _mgr([_inst(5)], running=(5,))
    out = m.revive_dead_instances()
    assert out["revived"] == []
    m._start_from_db.assert_not_called()


def test_recently_stopped_within_grace_is_skipped():
    m = _mgr([_inst(5)], recently_stopped={5: time.monotonic()})
    out = m.revive_dead_instances(grace_seconds=900)
    assert out["revived"] == []
    assert out["skipped_grace"] == 1
    m._start_from_db.assert_not_called()


def test_recently_stopped_past_grace_is_revived():
    # Stop stamped well beyond the grace window → abandoned edit → revive.
    m = _mgr([_inst(5)], recently_stopped={5: time.monotonic() - 10_000})
    out = m.revive_dead_instances(grace_seconds=900)
    assert out["revived"] == [5]
    m._start_from_db.assert_called_once_with(5)


def test_mixed_fleet():
    m = _mgr(
        [_inst(1), _inst(2, paused=True), _inst(3), _inst(4)],
        running=(3,),
        recently_stopped={4: time.monotonic()},   # mid-edit
    )
    out = m.revive_dead_instances(grace_seconds=900)
    assert out["revived"] == [1]          # 1 dead, 2 paused, 3 running, 4 grace
    assert out["skipped_paused"] == 1
    assert out["skipped_grace"] == 1


def test_list_failure_returns_error_not_raise():
    m = _mgr([])
    m.get_all_instances = MagicMock(side_effect=RuntimeError("postgrest down"))
    out = m.revive_dead_instances()
    assert out["status"] == "error"
