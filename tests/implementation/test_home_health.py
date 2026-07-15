"""
HOME-HEALTH reporters (Tier 1, read-only) — marked `health`, NON-gating.

These surface real problems with the operator's LIVE home that are not code
defects: a device wired into an automation that the app cannot currently resolve
(is_present=false), a hub that has stopped syncing, etc. They FAIL loudly and
actionably (per R2, is-not=FAIL — never a silent skip), but they are marked
`health` so a CI merge gate can run `-m "not health"`: a light being offline must
not block a code merge, yet the operator must still see it.

Run just these:  venv/bin/pytest tests/implementation -m health
"""
import pytest

from tests.implementation import checks

pytestmark = [pytest.mark.implementation, pytest.mark.observational, pytest.mark.health]


def test_no_automation_references_absent_devices(client, app_types, present_device_ids):
    """Every device wired into an automation is currently present (resolvable by
    the app). A miss means that automation cannot actuate that device RIGHT NOW —
    the operator should confirm the device was intentionally removed, or fix the
    sync/selection."""
    data = client.get_json("/api/instances")
    instances = data if isinstance(data, list) else data.get("instances", data)

    findings = []
    for i in instances:
        absent = checks.absent_selected_devices(i, present_device_ids)
        if absent:
            type_name = app_types["by_id"].get(i.get("app_type_id"))
            findings.append(
                f"instance {i.get('id')} ({i.get('label')!r}, {type_name}) wired to "
                f"currently-absent device id(s): {absent}"
            )

    assert not findings, (
        "Automations reference devices the app cannot currently resolve "
        "(is_present=false). These automations cannot control those devices right "
        "now — confirm the device was intentionally removed, else fix the "
        "sync/selection:\n  " + "\n  ".join(findings)
    )
