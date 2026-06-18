"""
Coverage for AML's _turn_on_switch() — including the 2026-06-17 fix that
re-levels an already-on device when the cached level differs from the
cascade target.

Reference bug: instance 8 'Motion Hallway' in WatchingTV mode. modeDimLevels
had WatchingTV=10, but the light was already on at Day brightness (100).
Motion arrived → master() → _control_lights('on') → _turn_on_switch found
device.switch=='on' and short-circuited, never calling setLevel. The new
behavior keeps the skip path for matched-level / non-dimming cases, and
adds a 'relevel when on but level mismatched' branch.
"""

from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest


pytestmark = pytest.mark.service


def _make_result(*, success=True, verified=True, error=None,
                 expected="on", actual="on", retries=0, elapsed=10.0):
    res = MagicMock()
    res.success = success
    res.verified = verified
    res.error = error
    res.expected_state = expected
    res.actual_state = actual
    res.retries_used = retries
    res.elapsed_ms = elapsed
    return res


def _make_instance(*, useDim=True, dim_target=50, has_SwitchLevel=True,
                   manual_memo=None):
    """Minimal harness with the real _turn_on_switch bound."""
    from apps.advanced_motion_lighting.light_control.switch_commands import (
        SwitchCommandsMixin,
    )
    from apps.advanced_motion_lighting.light_control.color_and_dim import (
        ColorAndDimMixin,
    )

    inst = MagicMock()
    inst._turn_on_switch = (
        SwitchCommandsMixin._turn_on_switch.__get__(inst)
    )
    inst._device_has_capability = (
        ColorAndDimMixin._device_has_capability.__get__(inst)
    )

    inst.logger = MagicMock()
    inst._memoization = {
        'switch_state': {},
        'dim_level': dict(manual_memo) if manual_memo else {},
        'color_state': {},
    }
    inst._get_current_dim_level = MagicMock(return_value=dim_target)

    settings = {'useDim': useDim, 'useColor': False}
    inst.get_setting = MagicMock(side_effect=lambda k, default=None:
                                 settings.get(k, default))
    inst.send_command = MagicMock(return_value=_make_result())

    # Patch the capability list directly via a fake device passed by the
    # test; _device_has_capability reads device['capabilities'].
    inst._has_SwitchLevel_for_test = has_SwitchLevel
    return inst


def _device(*, switch="on", level=None, capabilities=None):
    return {
        'attributes': {
            'switch': switch,
            **({'level': level} if level is not None else {}),
        },
        'capabilities': capabilities or ['Switch'],
    }


# ---------------------------------------------------------------------------
# OFF → ON path (target_level used in initial setLevel)
# ---------------------------------------------------------------------------


def test_off_to_on_with_useDim_sends_setLevel_with_target():
    inst = _make_instance(useDim=True, dim_target=42)
    device = _device(switch="off", capabilities=['Switch', 'SwitchLevel'])

    changed = inst._turn_on_switch("d1", "Hallway", device)

    assert changed is True
    inst.send_command.assert_called_once_with("d1", 'setLevel', [42])
    assert inst._memoization['switch_state']['Hallway'] == {
        'state': 'on', 'source': 'app'
    }
    assert inst._memoization['dim_level']['Hallway'] == {
        'level': 42, 'source': 'app'
    }


def test_off_to_on_without_useDim_sends_on():
    inst = _make_instance(useDim=False)
    device = _device(switch="off")

    changed = inst._turn_on_switch("d1", "Hallway", device)

    assert changed is True
    inst.send_command.assert_called_once_with("d1", 'on')
    assert 'Hallway' not in inst._memoization['dim_level']


# ---------------------------------------------------------------------------
# ALREADY-ON re-level path (the 2026-06-17 fix)
# ---------------------------------------------------------------------------


def test_already_on_mismatched_level_triggers_relevel():
    """The bug fix: device is on at level 100, target says 10, setLevel
    must fire to drop the brightness."""
    inst = _make_instance(useDim=True, dim_target=10)
    device = _device(switch="on", level=100,
                     capabilities=['Switch', 'SwitchLevel'])

    changed = inst._turn_on_switch("d1", "Hallway", device)

    assert changed is True
    inst.send_command.assert_called_once_with("d1", 'setLevel', [10])
    assert inst._memoization['dim_level']['Hallway'] == {
        'level': 10, 'source': 'app'
    }
    # The skip log line must NOT appear — re-level path was taken.
    skip_calls = [c for c in inst.logger.debug.call_args_list
                  if 'Skip ON' in str(c)]
    assert not skip_calls


def test_already_on_matched_level_skips_no_command():
    """No command should fire when device is on and level already matches."""
    inst = _make_instance(useDim=True, dim_target=50)
    device = _device(switch="on", level=50,
                     capabilities=['Switch', 'SwitchLevel'])

    changed = inst._turn_on_switch("d1", "Hallway", device)

    assert changed is False
    inst.send_command.assert_not_called()
    assert 'Hallway' not in inst._memoization['dim_level']


def test_already_on_useDim_off_skips_even_if_level_mismatched():
    """useDim off → the re-level branch must not trigger; preserve the
    original skip semantics."""
    inst = _make_instance(useDim=False)
    device = _device(switch="on", level=100,
                     capabilities=['Switch', 'SwitchLevel'])

    changed = inst._turn_on_switch("d1", "Hallway", device)

    assert changed is False
    inst.send_command.assert_not_called()


def test_already_on_no_SwitchLevel_cap_skips_even_with_useDim():
    """Device lacks SwitchLevel capability — can't setLevel anyway, skip."""
    inst = _make_instance(useDim=True, dim_target=10)
    device = _device(switch="on", level=None, capabilities=['Switch'])

    changed = inst._turn_on_switch("d1", "Hallway", device)

    assert changed is False
    inst.send_command.assert_not_called()


def test_already_on_relevel_failed_command_returns_false_no_memo():
    """setLevel failed at send → no memo update, return False."""
    inst = _make_instance(useDim=True, dim_target=10)
    inst.send_command.return_value = _make_result(
        success=False, error="HTTP 500", verified=False
    )
    device = _device(switch="on", level=100,
                     capabilities=['Switch', 'SwitchLevel'])

    changed = inst._turn_on_switch("d1", "Hallway", device)

    assert changed is False
    assert 'Hallway' not in inst._memoization['dim_level']


def test_already_on_relevel_unverified_command_returns_false_no_memo():
    """setLevel sent but verifier couldn't confirm — DO NOT update memo
    (preserves override-detection accuracy)."""
    inst = _make_instance(useDim=True, dim_target=10)
    inst.send_command.return_value = _make_result(
        success=True, verified=False, actual="55", expected="10",
        retries=3, elapsed=4000.0,
    )
    device = _device(switch="on", level=100,
                     capabilities=['Switch', 'SwitchLevel'])

    changed = inst._turn_on_switch("d1", "Hallway", device)

    assert changed is False
    assert 'Hallway' not in inst._memoization['dim_level']


def test_already_on_unparseable_level_attribute_still_relevels():
    """If device reports level as 'unknown' or non-numeric, treat current
    as None → mismatch with any int target → re-level."""
    inst = _make_instance(useDim=True, dim_target=10)
    device = _device(switch="on", level="unknown",
                     capabilities=['Switch', 'SwitchLevel'])

    changed = inst._turn_on_switch("d1", "Hallway", device)

    assert changed is True
    inst.send_command.assert_called_once_with("d1", 'setLevel', [10])


def test_already_on_with_no_level_attribute_relevels():
    """Missing level attribute (None) → mismatch with target → re-level."""
    inst = _make_instance(useDim=True, dim_target=10)
    device = _device(switch="on", level=None,
                     capabilities=['Switch', 'SwitchLevel'])

    changed = inst._turn_on_switch("d1", "Hallway", device)

    assert changed is True
    inst.send_command.assert_called_once_with("d1", 'setLevel', [10])
