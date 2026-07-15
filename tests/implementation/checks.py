"""
Shared per-instance OBSERVATIONAL checks (Tier 1) for the R1 per-app suites.

Every per-app module (`test_impl_<app>.py`) declares `APP_TYPE` and calls these
against each of its REAL instances (parametrized by conftest). They assert the
actual behavior of the actual deployed instance — never a mock — and mutate
nothing, so they are safe against the live home.
"""
from typing import Any, Dict, Iterable, List


def iter_selected_device_ids(device_selections: Dict[str, Any]) -> Iterable[str]:
    """Flatten every device id referenced in an instance's device_selections
    (a dict of category -> list of id strings, or a bare id)."""
    if not isinstance(device_selections, dict):
        return
    for v in device_selections.values():
        if isinstance(v, list):
            for x in v:
                if x is not None:
                    yield str(x)
        elif v is not None:
            yield str(v)


def assert_status_reflects_instance(client, instance: Dict[str, Any]) -> None:
    """The real /status endpoint agrees with the roster row for this instance —
    same id, same pause state. A divergence is exactly the class of lie the
    2026-07-13 12:04 incident was about (UI/state disagreeing with truth)."""
    iid = instance["id"]
    r = client.get(f"/api/instances/{iid}/status")
    assert r.status_code == 200, f"/api/instances/{iid}/status -> {r.status_code}"
    s = r.json()
    assert s.get("id") == iid, f"instance {iid}: /status reports id {s.get('id')}"
    assert s.get("is_paused") == instance.get("is_paused"), (
        f"instance {iid} ({instance.get('label')!r}): /status is_paused="
        f"{s.get('is_paused')} disagrees with roster is_paused={instance.get('is_paused')}"
    )


def assert_runtime_alive(client, instance: Dict[str, Any]) -> None:
    """The instance's runtime is actually up and answering."""
    iid = instance["id"]
    r = client.get(f"/api/instances/{iid}/runtime-status")
    assert r.status_code == 200, f"/api/instances/{iid}/runtime-status -> {r.status_code}"


def assert_selection_well_formed(instance: Dict[str, Any]) -> None:
    """CODE-CORRECTNESS (gating): the instance's device_selections is a well-formed
    map with at least one selected device whose ids are all numeric strings. An
    instance wired to nothing is misconfigured regardless of hub state — this is a
    pure config check and does NOT depend on any device being present right now
    (so a transiently-offline hub can never make it flap)."""
    sel = instance.get("device_selections") or {}
    assert isinstance(sel, dict), (
        f"instance {instance['id']}: device_selections is not a map: {type(sel).__name__}"
    )
    ids = list(iter_selected_device_ids(sel))
    assert ids, (
        f"instance {instance['id']} ({instance.get('label')!r}) selects NO devices at all"
    )
    bad = [x for x in ids if not str(x).isdigit()]
    assert not bad, (
        f"instance {instance['id']} has non-numeric device id(s) in its selection: {bad}"
    )


def absent_selected_devices(instance: Dict[str, Any], present_ids: set) -> List[str]:
    """HOME-HEALTH (non-gating): the selected device ids that the app cannot
    currently resolve (is_present=false — the app 404s them). NOT config rot: the
    devices exist but are absent from the latest hub sync, so these automations
    cannot actuate them right now. Returned for the health reporter to surface."""
    return sorted({d for d in iter_selected_device_ids(instance.get("device_selections") or {})
                   if d not in present_ids})


def assert_no_persistent_error(instance: Dict[str, Any], max_errors: int = 50) -> None:
    """The instance is not stuck in a runaway error loop on the live app."""
    ec = instance.get("error_count") or 0
    assert ec <= max_errors, (
        f"instance {instance['id']} ({instance.get('label')!r}) has error_count={ec}, "
        f"last_error={instance.get('last_error')!r} — a persistent live failure."
    )
