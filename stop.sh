#!/bin/bash
# =============================================================================
# stop.sh - Stop 0_SMART_HOME containers
# =============================================================================

# Get script directory
SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
SCRIPT_R_PATH=$(realpath "${BASH_SOURCE[0]}")
SCRIPT_DIR="${SCRIPT_R_PATH%${SCRIPT_NAME}}"

cd "$SCRIPT_DIR" &>/dev/null || true

# Source colors
. ~/.env.colors 2>/dev/null || true

echo "=========================================="
echo "  0_SMART_HOME - Stopping"
echo "=========================================="
echo ""

# Stop containers
docker compose down

echo ""
echo -e "${GREEN:-}OK: Containers stopped${NC:-}"
