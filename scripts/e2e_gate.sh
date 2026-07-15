#!/usr/bin/env bash
# ============================================================================
# scripts/e2e_gate.sh — the implementation/E2E gate for merge-to-main.
#
# Mirrors MOBIUS.NVR's e2e_gate shape (the CICD.2 reference impl), adapted to
# this project's CICD.3 posture: the gating tier here runs READ-ONLY against
# the REAL running app — "we test against the real thing. Mock only if no
# other choice." There is no ephemeral stack lifecycle in this script (unlike
# NVR's nvr_test stack): tests/implementation drives the live app and asserts
# it matches the DB/schema truth without actuating anything.
#
# STAGES:
#   1. Reachability fail-fast — a down app must FAIL the gate loudly, not let
#      every test "pass" by skipping (pin 91 / R2: no skip-to-hide).
#   2. Implementation suite: pytest tests/implementation -m "not health"
#      (health reporters are observational — a light being offline must not
#      block a merge).
#   3. Playwright E2E — STUB until tests/e2e lands (P2). The stage announces
#      itself and is a no-op today; when tests/e2e exists it runs automatically.
#
# WIRING (mirrors NVR's options):
#   (a) Manual pre-merge:  ./scripts/e2e_gate.sh && <merge>
#   (b) CI required check: .github/workflows/tests.yml job
#       "Implementation E2E (self-hosted)" runs this on pull_request; mark it
#       required in branch protection ONLY after the self-hosted runner is live
#       (see docs/ci_branch_protection_checklist.md).
#
# USAGE:
#   ./scripts/e2e_gate.sh
#   SMARTHOME_IMPL_BASE_URL=http://localhost:6001 ./scripts/e2e_gate.sh   # ETT stack (P2)
#   E2E_PYTEST_ARGS="tests/implementation/test_impl_rules.py" ./scripts/e2e_gate.sh
#
# Exit code: non-zero on ANY failure — a red gate must block the merge.
# ============================================================================
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

BASE_URL="${SMARTHOME_IMPL_BASE_URL:-http://localhost:5001}"
PYTEST="${PYTEST:-./venv/bin/pytest}"
PYTEST_ARGS="${E2E_PYTEST_ARGS:-tests/implementation}"

[ -x "$PYTEST" ] || PYTEST="$(command -v pytest)" || {
    echo "[e2e-gate] FATAL: pytest not found (no ./venv/bin/pytest, none on PATH)." >&2
    echo "[e2e-gate] Install: venv/bin/pip install -r requirements-test.txt" >&2
    exit 2
}

# ── Stage 1: reachability fail-fast ────────────────────────────────────────
# The implementation conftest skips politely when the app is down; in a GATE
# that silence would read as green. Fail loudly instead.
echo "[e2e-gate] target app: $BASE_URL"
if ! curl -fsS --max-time 5 "$BASE_URL/api/health" >/dev/null 2>&1 \
   && ! curl -fsS --max-time 5 "$BASE_URL/" >/dev/null 2>&1; then
    echo "[e2e-gate] FATAL: app unreachable at $BASE_URL — refusing to 'pass' by skipping." >&2
    echo "[e2e-gate] Start it (./start.sh) or point SMARTHOME_IMPL_BASE_URL at a running app." >&2
    exit 1
fi

# ── Stage 2: implementation suite (read-only, gating) ──────────────────────
echo "[e2e-gate] stage 2: implementation suite (-m 'not health') ..."
SMARTHOME_IMPL_BASE_URL="$BASE_URL" "$PYTEST" $PYTEST_ARGS -m "not health" --tb=short

# ── Stage 3: Playwright E2E (stub until tests/e2e lands — P2) ──────────────
if [ -d tests/e2e ]; then
    echo "[e2e-gate] stage 3: Playwright E2E (tests/e2e) ..."
    SMARTHOME_IMPL_BASE_URL="$BASE_URL" "$PYTEST" tests/e2e --tb=short
else
    echo "[e2e-gate] stage 3: tests/e2e not present yet (P2) — Playwright stage is a stub. NOT a failure."
fi

echo "[e2e-gate] ✔ gate green."
