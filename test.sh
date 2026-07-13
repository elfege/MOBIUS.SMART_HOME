#!/usr/bin/env bash
# =============================================================================
# test.sh — the test-suite launcher for MOBIUS.SMART_HOME (canonical ETT).
#
# WHY THIS EXISTS: until 2026-07-13 this repo had 33 test files and NO way to run
# them — the project venv had been created under the pre-rename path
# (/home/elfege/0_SMART_HOME/venv) and was dead, and there was no
# requirements-test.txt, so pytest was not even installed. Pin 91's "32 tests,
# zero ever run" was not a CI gap; nothing could execute them at all. This script
# + requirements-test.txt is the fix.
#
# USAGE:
#   ./test.sh                 interactive menu
#   ./test.sh --unit          unit suite (no stack needed — postgrest is mocked)
#   ./test.sh --integration   integration suite (needs the ETT test stack)
#   ./test.sh --all           everything
#   ./test.sh --stack-up      bring the isolated test stack up   (start.sh --test)
#   ./test.sh --stack-down    tear it down                       (stop.sh --test)
#   ./test.sh --status        show both stacks                   (status.sh)
#   ./test.sh --custom <path> run a specific file / node id
#
# The test stack is a SEPARATE compose project (smarthome_test) with its own
# volumes, +1000 ports, dummy hub tokens and unroutable hub IPs — it cannot
# touch the live house. See .env.test.
# =============================================================================
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[38;5;1m'
    BLUE=$'\033[38;5;4m'; NC=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; BLUE=""; NC=""
fi

PYTEST="$REPO_ROOT/venv/bin/pytest"
if [[ ! -x "$PYTEST" ]]; then
    echo "${RED}✗ venv has no pytest.${NC}"
    echo "  Fix:  python3 -m venv venv && venv/bin/pip install -r requirements.txt -r requirements-test.txt"
    exit 1
fi

run_unit()        { "$PYTEST" tests/unit tests/test_panel_resolver.py "$@"; }
run_integration() { "$PYTEST" tests/integration "$@"; }
run_all()         { "$PYTEST" tests "$@"; }

case "${1:-menu}" in
    --unit)         shift; run_unit "$@" ;;
    --integration)  shift; run_integration "$@" ;;
    --all)          shift; run_all "$@" ;;
    --custom)       shift; "$PYTEST" "$@" ;;
    --stack-up)     exec ./start.sh --test ;;
    --stack-down)   shift; exec ./stop.sh --test "$@" ;;
    --status)       exec ./status.sh ;;
    menu)
        echo "${BOLD}MOBIUS.SMART_HOME — tests${NC}"
        echo "  ${BLUE}1${NC}) unit          ${DIM}(no stack; postgrest mocked)${NC}"
        echo "  ${BLUE}2${NC}) integration   ${DIM}(needs test stack)${NC}"
        echo "  ${BLUE}3${NC}) all"
        echo "  ${BLUE}4${NC}) stack up      ${DIM}(start.sh --test)${NC}"
        echo "  ${BLUE}5${NC}) stack down    ${DIM}(stop.sh --test)${NC}"
        echo "  ${BLUE}6${NC}) status"
        read -rp "> " choice
        case "$choice" in
            1) run_unit ;;
            2) run_integration ;;
            3) run_all ;;
            4) ./start.sh --test ;;
            5) ./stop.sh --test ;;
            6) ./status.sh ;;
            *) echo "nothing to do" ;;
        esac
        ;;
    *) echo "usage: $0 [--unit|--integration|--all|--custom <path>|--stack-up|--stack-down|--status]" >&2; exit 2 ;;
esac
