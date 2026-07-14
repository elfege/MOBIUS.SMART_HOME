#!/bin/bash
# =============================================================================
# stop.sh - Stop 0_MOBIUS.SMART_HOME containers
# =============================================================================

# Get script directory
SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
SCRIPT_R_PATH=$(realpath "${BASH_SOURCE[0]}")
SCRIPT_DIR="${SCRIPT_R_PATH%${SCRIPT_NAME}}"

cd "$SCRIPT_DIR" &>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
# --test          : stop the ETT test stack (volumes KEPT — faster next start).
# --test --fresh  : stop it AND drop its project-scoped volumes so the next
#                   `start.sh --test` initdb's a pristine database. Wiping is
#                   always an explicit choice, never a side effect.
# ─────────────────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--test" ]]; then
	if [[ "${2:-}" == "--fresh" ]]; then
		echo "Stopping smarthome_test stack AND dropping its volumes (fresh next start)..."
		docker compose -p smarthome_test --env-file .env.test down -v
	else
		echo "Stopping smarthome_test stack (volumes kept; use --fresh to drop)..."
		docker compose -p smarthome_test --env-file .env.test down
	fi
	exit 0
fi

# Source colors
. ~/.env.colors 2>/dev/null || true

echo "=========================================="
echo "  0_MOBIUS.SMART_HOME - Stopping"
echo "=========================================="
echo ""

# Stop containers
docker compose down

echo ""
echo -e "${GREEN:-}OK: Containers stopped${NC:-}"
