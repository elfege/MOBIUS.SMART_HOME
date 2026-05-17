"""
resolve_expected_state(command, args) maps a Hubitat command name to the
(attribute, expected_value) pair that the verification poll will check.

Commands not in the mapping return None → verification is skipped.
{arg0} placeholders get substituted from args.
"""

import pytest

from models.command import resolve_expected_state


@pytest.mark.unit
class TestResolveExpectedState:
    def test_on_command(self):
        result = resolve_expected_state("on")
        assert result == {"attribute": "switch", "expected": "on"}

    def test_off_command(self):
        result = resolve_expected_state("off")
        assert result == {"attribute": "switch", "expected": "off"}

    def test_setlevel_implies_switch_on(self):
        # setLevel doesn't have a level-equals-N verification — it just
        # expects the switch to have turned on as a side effect.
        result = resolve_expected_state("setLevel", [75])
        assert result == {"attribute": "switch", "expected": "on"}

    def test_setlevel_zero_still_returns_on(self):
        # Even setLevel(0) returns expected switch=on by current mapping.
        # Documenting current behavior — if changed, this test should change too.
        result = resolve_expected_state("setLevel", [0])
        assert result == {"attribute": "switch", "expected": "on"}

    def test_setcolortemperature_substitutes_arg0(self):
        result = resolve_expected_state("setColorTemperature", [2700])
        assert result == {"attribute": "colorTemperature", "expected": "2700"}

    def test_setcolortemperature_arg0_substitution_uses_str(self):
        # arg0 is coerced to str (PostgREST returns currentValue as str)
        result = resolve_expected_state("setColorTemperature", [4000])
        assert result["expected"] == "4000"
        assert isinstance(result["expected"], str)

    def test_unknown_command_returns_none(self):
        assert resolve_expected_state("setColor", [{"hue": 50}]) is None

    def test_unknown_command_with_no_args_returns_none(self):
        assert resolve_expected_state("refresh") is None

    def test_no_args_for_command_needing_arg0_falls_through(self):
        # If setColorTemperature is called without args, the {arg0} placeholder
        # stays in the expected value (caller's bug — verification will fail).
        result = resolve_expected_state("setColorTemperature", None)
        assert result == {
            "attribute": "colorTemperature",
            "expected": "{arg0}",
        }

    def test_empty_args_list_falls_through_like_no_args(self):
        result = resolve_expected_state("setColorTemperature", [])
        assert result["expected"] == "{arg0}"
