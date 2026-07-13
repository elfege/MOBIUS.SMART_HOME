#!/usr/bin/env bash
# =============================================================================
# status.sh — show BOTH stacks at a glance (canonical ETT).
#
#   ./status.sh          both stacks
#   ./status.sh --test   only the isolated test stack (smarthome_test)
#   ./status.sh --live   only the live stack
#
# The live stack runs the operator's actual house. The test stack is a separate
# compose project with its own volumes and +1000 ports and CANNOT reach the real
# hubs (dummy tokens, unroutable IPs — see .env.test). Showing them side by side
# is the point: it should be obvious, always, which one you are about to touch.
# =============================================================================
set -uo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[38;5;1m'; BLUE=$'\033[38;5;4m'; NC=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; BLUE=""; NC=""
fi

show_live() {
    echo "${BOLD}${RED}LIVE stack${NC} ${DIM}(the operator's house — restarts affect his lights/phone)${NC}"
    docker compose ps --format "table {{.Service}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null \
        || echo "  (not running)"
    local h
    h=$(docker inspect smarthome-app --format '{{.State.Health.Status}}' 2>/dev/null || echo "-")
    echo "  app health: ${h}"
    echo
}

show_test() {
    echo "${BOLD}${BLUE}TEST stack${NC} ${DIM}(smarthome_test — isolated volumes, +1000 ports, dummy hubs)${NC}"
    if [[ -f .env.test ]]; then
        docker compose -p smarthome_test --env-file .env.test ps \
            --format "table {{.Service}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null \
            | grep -v '^$' || true
        if ! docker ps --format '{{.Names}}' | grep -q '^smarthome_test_'; then
            echo "  (not running — bring it up with ./start.sh --test)"
        fi
    else
        echo "  (.env.test missing)"
    fi
    echo
}

case "${1:-both}" in
    --test) show_test ;;
    --live) show_live ;;
    both)   show_live; show_test ;;
    *) echo "usage: $0 [--test|--live]" >&2; exit 2 ;;
esac
