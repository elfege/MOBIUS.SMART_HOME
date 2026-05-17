"""
Strict policy enforcement: a setting key MUST live at exactly one
configurable layer per app type.

If a key is exposed in an app's settings_schema (instance-level UI) AND
also stored in app_type_settings for the same app, the cascade is
nondeterministic and global writes will be silently ignored by instances
that already carry the same key in their JSONB.

This test runs against the live registry+DB so it catches violations
introduced by either side (code or DB).
"""

import os

import pytest
import requests


@pytest.mark.integration
class TestCascadeDisjoint:
    def test_no_key_appears_at_both_instance_and_app_type_layers(
        self, live_postgrest_url,
    ):
        # 1. Get every registered app_type with its settings_schema
        r = requests.get(
            f"{live_postgrest_url}/app_types",
            params={"select": "id,type_name,settings_schema"},
            timeout=5,
        )
        assert r.status_code == 200
        app_types = r.json()

        # 2. For each app_type, get the keys exposed at instance level
        #    (the JSON schema's `properties` object).
        #    Plus the keys stored at app_type_settings layer.
        violations = []
        for at in app_types:
            schema = at.get("settings_schema") or {}
            props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
            instance_keys = set(props.keys())

            # app_type_settings rows for this type
            r = requests.get(
                f"{live_postgrest_url}/app_type_settings",
                params={
                    "app_type_id": f"eq.{at['id']}",
                    "select": "key",
                },
                timeout=5,
            )
            global_keys = {row["key"] for row in r.json()}

            overlap = instance_keys & global_keys
            # Exclude keys that begin with __test__ (test artifacts)
            overlap = {k for k in overlap if not k.startswith("__test__")}
            if overlap:
                violations.append((at["type_name"], overlap))

        assert violations == [], (
            f"Cascade-disjoint policy violated. Each key listed below appears "
            f"in both the app's instance-UI settings_schema AND in "
            f"app_type_settings — that makes the cascade nondeterministic. "
            f"Pick one layer per key.\n"
            f"Violations: {violations}\n"
            f"See docs/plans/comprehensive_settings_and_ui_overhaul_2026_05_17.md "
            f"§2 STRICT POLICY."
        )
