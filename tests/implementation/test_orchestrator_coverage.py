"""
R1 ORCHESTRATOR COVERAGE — the "global E2E" guarantee: no real instance goes
untested. The operator's directive was explicit: "each app has its OWN internal
E2E, and a GLOBAL E2E calls each per instance. No transgression."

This test IS that guarantee. It discovers every per-app implementation suite
(`test_impl_<app>.py` declaring APP_TYPE), enumerates every REAL app_instance,
and FAILS if any live instance's app type has no suite claiming it — i.e. an
automation running in the operator's home with zero implementation coverage.

This is the hierarchical-orchestrator invariant, checked, not assumed.
"""
import importlib
import pkgutil
from pathlib import Path

import pytest

import tests.implementation as impl_pkg

pytestmark = [pytest.mark.implementation, pytest.mark.observational]


def _registered_app_types() -> dict:
    """Map app_type -> module name for every per-app suite that declares APP_TYPE."""
    registered = {}
    pkg_dir = Path(impl_pkg.__file__).parent
    for mod in pkgutil.iter_modules([str(pkg_dir)]):
        if not mod.name.startswith("test_impl_"):
            continue
        m = importlib.import_module(f"tests.implementation.{mod.name}")
        app_type = getattr(m, "APP_TYPE", None)
        if app_type:
            assert app_type not in registered, (
                f"app type {app_type!r} claimed by two suites: "
                f"{registered[app_type]} and {mod.name} — each type owns exactly one suite"
            )
            registered[app_type] = mod.name
    return registered


def test_every_live_instance_has_an_implementation_suite(client, app_types):
    """No transgression: every real instance's app type has a per-app suite."""
    registered = _registered_app_types()
    assert registered, "no per-app implementation suites discovered — orchestrator is empty"

    data = client.get_json("/api/instances")
    instances = data if isinstance(data, list) else data.get("instances", data)

    uncovered = []
    for i in instances:
        type_name = app_types["by_id"].get(i.get("app_type_id"))
        if type_name not in registered:
            uncovered.append((i.get("id"), i.get("label"), type_name))

    assert not uncovered, (
        "LIVE instances with NO implementation suite (R1 transgression — an "
        "automation running in the home with zero implementation coverage):\n  "
        + "\n  ".join(f"instance {iid} ({label!r}) type={t}" for iid, label, t in uncovered)
        + f"\nRegistered suites: {sorted(registered)}. Add a test_impl_<app>.py with "
        "APP_TYPE for each missing type."
    )


def test_registered_suites_map_to_real_app_types(app_types):
    """Guard against a suite declaring a typo APP_TYPE that matches no real type
    (it would silently cover nothing)."""
    registered = _registered_app_types()
    unknown = [t for t in registered if t not in app_types["by_name"]]
    assert not unknown, (
        f"per-app suites declare APP_TYPE(s) that are not real app types: {unknown}. "
        f"Known types: {sorted(app_types['by_name'])}"
    )
