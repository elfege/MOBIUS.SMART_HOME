"""
tests/e2e — Playwright browser E2E, the Stage-3 tier of scripts/e2e_gate.sh.

POSTURE (CICD.3 / R2): we drive the REAL running app in a real browser and
assert what it actually renders. Tier-1 here is OBSERVATIONAL — navigation and
read-only assertions only; nothing is actuated (no toggling a real light from a
gate run). Tier-2 (mutating journeys) lands later behind a marker + the ETT
stack, never against the live home.

TWO DISTINCT "not-ready" cases, kept honest and separate:
  * APP DOWN  -> FAIL. If the app under test is unreachable, that is the exact
    "green-by-skipping" lie R2 forbids (mirrors tests/implementation/conftest).
  * BROWSER NOT PROVISIONED -> SKIP with a loud reason. Playwright's browser
    binary is installed by the self-hosted runner setup (`playwright install
    chromium`), an ENVIRONMENT step, not a product truth. Until it exists the
    tier skips cleanly so it never reds the gate before the runner is ready.

Base URL: SMARTHOME_IMPL_BASE_URL (same knob the gate + implementation tier use;
default http://localhost:5001 = the live app; :6001 = the ETT stack).
"""

import os
import urllib.request

import pytest

BASE_URL = os.environ.get("SMARTHOME_IMPL_BASE_URL", "http://localhost:5001")


@pytest.fixture(scope="session")
def base_url() -> str:
    """The app under test. If it is DOWN, fail loudly (R2: no skip-to-hide)."""
    try:
        urllib.request.urlopen(f"{BASE_URL}/", timeout=5)
    except Exception as e:  # noqa: BLE001
        pytest.fail(
            f"app unreachable at {BASE_URL} ({e}) — a browser E2E run must not "
            f"'pass' by skipping. Start it (./start.sh) or set "
            f"SMARTHOME_IMPL_BASE_URL.")
    return BASE_URL


@pytest.fixture(scope="session")
def _browser():
    """A chromium instance, or SKIP the whole tier if the browser binary is not
    installed yet (self-hosted-runner provisioning step — env gap, not a bug)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed (venv/bin/pip install -r requirements-test.txt)")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001 — browser not provisioned
            pytest.skip(f"chromium not provisioned ({e}); run `playwright install chromium`")
        yield browser
        browser.close()


@pytest.fixture
def page(_browser, base_url):
    """A fresh page per test. Read-only by convention in Tier-1 — assert what the
    real app renders; do not actuate devices/automations from a gate run."""
    context = _browser.new_context(base_url=base_url)
    pg = context.new_page()
    yield pg
    context.close()
