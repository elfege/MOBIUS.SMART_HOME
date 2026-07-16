# tests/e2e — Playwright browser E2E (Stage 3 of `scripts/e2e_gate.sh`)

Drives the **real running app** in a real browser (CICD.3 / R2). Tier-1 here is
**observational** (navigate + read-only asserts, no actuation). Runs via the
existing gate — no separate runner:

    ./scripts/e2e_gate.sh                 # runs tests/e2e automatically once present
    pytest tests/e2e                      # directly
    SMARTHOME_IMPL_BASE_URL=http://localhost:6001 pytest tests/e2e   # ETT stack

**Provisioning (self-hosted runner, one-time):**
    venv/bin/pip install -r requirements-test.txt
    venv/bin/playwright install chromium

App DOWN → the tier FAILS (no green-by-skipping). Browser not provisioned →
the tier SKIPS with a loud reason (env gap, not a product bug). See conftest.py.
