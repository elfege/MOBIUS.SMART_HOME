"""
Regression coverage for services.hubitat_admin_client.to_maker_shape().

Why this file exists
--------------------
On 2026-05-17, the admin-API verify-poll path silently broke because the
inline shape-converter in device_commander.py and reconcile_poll.py was
reading `raw['currentStates']` as a top-level list. The actual
`/device/fullJson/<id>` response nests state at `raw['device']['currentStates']`
as a *dict* keyed by attribute name. Every command verification returned
`actual=None` despite the underlying Hubitat send succeeding. Five-line
unit test would have caught it.

That conversion is now centralized in `to_maker_shape()`. This file is
the regression net for the shape semantics so that bug cannot return.
"""

import pytest

from services.hubitat_admin_client import to_maker_shape


pytestmark = pytest.mark.unit


# Real shape captured from /device/fullJson/30 against home_1 on 2026-05-17.
# Trimmed to what the conversion actually consumes.
REAL_ADMIN_RESPONSE = {
    "device": {
        "id": 30,
        "label": "Lights String Lights",
        "displayName": "Light",
        "name": "Lights String Lights",
        "currentStates": {
            "switch": {
                "name": "switch",
                "value": "on",
                "stringValue": "on",
                "dataType": "ENUM",
                "deviceId": 30,
                "date": "2026-05-17T22:25:41+0000",
            },
            "level": {
                "name": "level",
                "value": "100",
                "stringValue": "100",
                "dataType": "NUMBER",
                "deviceId": 30,
                "date": "2026-05-17T22:25:41+0000",
            },
        },
    },
}


class TestToMakerShape:
    def test_extracts_switch_from_nested_dict(self):
        shaped = to_maker_shape(REAL_ADMIN_RESPONSE)
        assert shaped is not None
        attrs = {a["name"]: a["currentValue"] for a in shaped["attributes"]}
        assert attrs["switch"] == "on"
        assert attrs["level"] == "100"

    def test_preserves_id_and_label(self):
        shaped = to_maker_shape(REAL_ADMIN_RESPONSE)
        assert shaped["id"] == "30"
        assert shaped["label"] == "Lights String Lights"

    def test_label_falls_back_through_displayName_then_name(self):
        for missing, expected in [
            (("label",), "Light"),                  # falls to displayName
            (("label", "displayName"), "Lights String Lights"),  # falls to name
        ]:
            raw = {"device": {**REAL_ADMIN_RESPONSE["device"]}}
            for k in missing:
                raw["device"].pop(k, None)
            assert to_maker_shape(raw)["label"] == expected

    def test_handles_list_shape_defensively(self):
        # Some firmware variants may emit currentStates as a list.
        raw = {
            "device": {
                "id": 99, "label": "X",
                "currentStates": [
                    {"name": "switch", "value": "off"},
                    {"name": "level", "value": "0"},
                ],
            },
        }
        shaped = to_maker_shape(raw)
        attrs = {a["name"]: a["currentValue"] for a in shaped["attributes"]}
        assert attrs == {"switch": "off", "level": "0"}

    def test_empty_currentStates_yields_empty_attributes(self):
        raw = {"device": {"id": 1, "label": "x", "currentStates": {}}}
        shaped = to_maker_shape(raw)
        assert shaped["attributes"] == []

    def test_none_and_non_dict_return_none(self):
        assert to_maker_shape(None) is None
        assert to_maker_shape("not-a-dict") is None
        assert to_maker_shape(42) is None

    def test_missing_device_key_does_not_crash(self):
        # If admin returns a payload without 'device' (e.g., login-redirect
        # HTML mis-parsed), we want None-ish output, not a KeyError.
        shaped = to_maker_shape({"some": "thing"})
        assert shaped["id"] == ""
        assert shaped["attributes"] == []

    def test_extract_attribute_roundtrip(self):
        # Verify the downstream consumer in device_commander recognizes
        # the Maker-shape output. This is the actual call site that broke.
        from services.device_commander import extract_attribute
        shaped = to_maker_shape(REAL_ADMIN_RESPONSE)
        assert extract_attribute(shaped, "switch") == "on"
        assert extract_attribute(shaped, "level") == "100"
        assert extract_attribute(shaped, "doesnt_exist") is None
