"""
Implementation-testing harness — the REAL thing, not logic.

Operator directive (2026-07-14): "no mock. We must test the real thing" /
"this is implementation testing, not logic testing." The unit/service tiers
(tests/unit, tests/service) are the fast LOGIC inner-loop: they mock the I/O
boundary and prove pure decisions. Valuable, but they are NOT what proves the
deployed system works. THIS suite is: it drives the REAL running app over HTTP,
against the REAL app_instances, and asserts the actual behavior of the actual
implementation.

TWO TIERS (Architect ruling on MSG-1042, R1/R2 reconciliation)
==============================================================
  TIER 1 — OBSERVATIONAL (this file's default): runs against the REAL LIVE app
    and REAL instances, but MUTATES NOTHING. It is safe on the live home because
    the `client` fixture below PHYSICALLY REFUSES any mutating HTTP verb. This is
    the primary R2 vehicle: "test the app AS IT IS," on the real container.
  TIER 2 — MUTATING (opt-in, marker `mutating`): drives commands / writes / pairing.
    NEVER runs against live — only against the ETT stack seeded from a real
    snapshot. A Tier-2 test must set the base URL to the ETT app and use the
    `mutating_client` fixture. (Tier-2 suites live alongside, guarded by marker.)

R1 — HIERARCHICAL ORCHESTRATOR
==============================
"each app has its OWN internal E2E, and a GLOBAL E2E calls each per instance."
Mechanism: a per-app test module declares `APP_TYPE = "<type_name>"`. Any test in
it that takes an `instance` parameter is parametrized by `pytest_generate_tests`
below over EVERY real instance of that type. The global suite (health, roster,
matter) plus `test_orchestrator_coverage.py` (asserts every real instance is
claimed by exactly one app module — "no transgression") make up the GLOBAL E2E.

is-not = FAIL (R2): if the real app is unreachable, this is a FAILURE, not a
skip. A green suite that skipped because the thing under test was down is the
exact lie pin 91 was about.
"""
import os
from typing import Any, Dict, List, Optional

import pytest
import requests

# Default = the REAL live FastAPI app (direct, not nginx-fronted). Override with
# SMARTHOME_IMPL_BASE_URL to point Tier-2 at the ETT app (e.g. :6001) or to test
# through nginx (:8443). Kept as the app port so these test the APP as-is.
DEFAULT_BASE_URL = os.environ.get("SMARTHOME_IMPL_BASE_URL", "http://localhost:5001")

# Verbs that change state. Tier 1 must never emit one against the live home.
_MUTATING_VERBS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class LiveMutationError(RuntimeError):
    """Raised when a Tier-1 (observational) test attempts a state-changing verb.

    This is a hard structural guardrail, not a lint: it makes it *impossible* for
    an observational implementation test to actuate the operator's real home
    (turn a switch, open a pairing window, write a setting). If you genuinely need
    to mutate, that is a Tier-2 test on the ETT stack with `mutating_client`."""


class ReadOnlyClient:
    """A thin requests wrapper locked to safe verbs (GET/HEAD/OPTIONS).

    Any mutating verb raises LiveMutationError BEFORE a request leaves the
    machine — so pointing this at the live app can never change it."""

    def __init__(self, base_url: str, timeout: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._s = requests.Session()

    def _url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.base_url}{path}"

    def request(self, method: str, path: str, **kw) -> requests.Response:
        if method.upper() in _MUTATING_VERBS:
            raise LiveMutationError(
                f"Tier-1 observational test attempted {method.upper()} {path}. "
                "Observational tests run against the LIVE home and MUST be "
                "read-only. Move mutation to a Tier-2 test on the ETT stack."
            )
        kw.setdefault("timeout", self.timeout)
        return self._s.request(method.upper(), self._url(path), **kw)

    def get(self, path: str, **kw) -> requests.Response:
        return self.request("GET", path, **kw)

    def get_json(self, path: str, **kw) -> Any:
        r = self.get(path, **kw)
        assert r.status_code == 200, (
            f"GET {path} -> {r.status_code} (expected 200). The REAL app must "
            f"serve this. Body: {r.text[:300]}"
        )
        return r.json()


def _fetch_instances(client: ReadOnlyClient) -> List[Dict[str, Any]]:
    data = client.get_json("/api/instances")
    return data if isinstance(data, list) else data.get("instances", data)


def _fetch_app_types(client: ReadOnlyClient) -> List[Dict[str, Any]]:
    data = client.get_json("/api/app-types")
    return data if isinstance(data, list) else data.get("app_types", data)


# --------------------------------------------------------------------------- #
# Session-scoped fixtures: the REAL app is the fixture. If it is down, FAIL.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def base_url() -> str:
    return DEFAULT_BASE_URL


@pytest.fixture(scope="session")
def client(base_url) -> ReadOnlyClient:
    """Read-only client against the REAL app. Reachability is asserted here, so
    an unreachable app FAILS the suite (is-not=FAIL) rather than skipping it."""
    c = ReadOnlyClient(base_url)
    try:
        r = c.get("/api/app-types")
    except requests.RequestException as e:
        pytest.fail(
            f"REAL app unreachable at {base_url} ({e}). Implementation tests "
            "assert against the running system; a down app is a FAILURE, not a "
            "skip. Start it (./start.sh) or set SMARTHOME_IMPL_BASE_URL."
        )
    assert r.status_code == 200, f"app liveness probe /api/app-types -> {r.status_code}"
    return c


@pytest.fixture(scope="session")
def app_types(client) -> Dict[str, Any]:
    rows = _fetch_app_types(client)
    by_id = {a["id"]: a.get("type_name") for a in rows}
    by_name = {a.get("type_name"): a["id"] for a in rows}
    return {"rows": rows, "by_id": by_id, "by_name": by_name}


@pytest.fixture(scope="session")
def real_instances(client) -> List[Dict[str, Any]]:
    return _fetch_instances(client)


@pytest.fixture(scope="session")
def present_device_ids(client) -> set:
    """The set of canonical device ids (as str) present on the REAL app right now.
    Used by per-app suites to assert an instance's device_selections don't point
    at a device that has been pruned from its hub (a real, silent breakage)."""
    data = client.get_json("/api/devices")
    devices = data if isinstance(data, list) else data.get("devices", data)
    return {str(d.get("id")) for d in devices}


@pytest.fixture
def instance(request) -> Dict[str, Any]:
    """The single real instance this test iteration is bound to (parametrized by
    pytest_generate_tests over the app module's APP_TYPE). Direct use without
    parametrization is a collection error surfaced below."""
    return request.param


# --------------------------------------------------------------------------- #
# R1 orchestrator: parametrize per-app `instance` tests over REAL instances.
# --------------------------------------------------------------------------- #
def _impl_cache(config) -> Dict[str, Any]:
    """Fetch the real instances + app-type map ONCE per session, cached on the
    pytest config so every module's generate hook is a dict lookup, not an HTTP
    round-trip."""
    cache = getattr(config, "_impl_inst_cache", None)
    if cache is None:
        c = ReadOnlyClient(DEFAULT_BASE_URL)
        cache = {"insts": _fetch_instances(c),
                 "types": {a["id"]: a.get("type_name") for a in _fetch_app_types(c)}}
        config._impl_inst_cache = cache
    return cache


def instances_of(config, type_name: str) -> List[Dict[str, Any]]:
    cache = _impl_cache(config)
    return [i for i in cache["insts"]
            if cache["types"].get(i.get("app_type_id")) == type_name]


def pytest_generate_tests(metafunc):
    """If a test wants an `instance` and its module declares APP_TYPE, run it once
    per REAL instance of that type — the "per app suite, per instance" of R1.

    An app type with zero live instances yields an empty parameter set: pytest
    reports it as an explicit, VISIBLE 'no instances' skip (not a hidden pass) —
    the coverage test asserts the converse (no instance left unclaimed)."""
    if "instance" not in metafunc.fixturenames:
        return
    app_type = getattr(metafunc.module, "APP_TYPE", None)
    if app_type is None:
        return  # non-app module using `instance` directly — leave to explicit param
    insts = instances_of(metafunc.config, app_type)
    ids = [f"inst{i.get('id')}:{(i.get('label') or '').replace(' ', '_')[:24]}"
           for i in insts]
    metafunc.parametrize("instance", insts, ids=ids)


def pytest_configure(config):
    config.addinivalue_line("markers",
                            "implementation: real running app + real instances (not mocked)")
    config.addinivalue_line("markers",
                            "observational: Tier 1 — read-only against the LIVE app; safe")
    config.addinivalue_line("markers",
                            "mutating: Tier 2 — mutates; ETT stack + real snapshot ONLY, never live")
    config.addinivalue_line("markers",
                            "health: live-home health finding (real, actionable) — NON-gating for "
                            "code merges; run/exclude with -m health / -m 'not health'")
