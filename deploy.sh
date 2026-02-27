#!/bin/bash
# =============================================================================
# deploy.sh - Rebuild and deploy 0_MOBIUS.SMART_HOME
#
# Usage:
#   ./deploy.sh              # Rebuild + start (prompts for prune/no-cache)
#   ./deploy.sh --prune      # Prune first, skip prompt
#   ./deploy.sh --no-cache   # No-cache build, skip prompt
#   ./deploy.sh --prune --no-cache  # Both, no prompts
#
# =============================================================================

SCRIPT_DIR="$(builtin cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Source helper scripts
. ~/.env.colors 2>/dev/null || true
. ~/logger.sh --no-exec &>/dev/null || true

# Parse flags
do_prune=false
do_nocache=false
for arg in "$@"; do
	case $arg in
	--prune) do_prune=true ;;
	--no-cache) do_nocache=true ;;
	esac
done

echo "=== 0_MOBIUS.SMART_HOME Deploy ==="
echo ""

# Prune: flag or prompt
if ! $do_prune; then
	prune_answer="no"
	read -t 3 -p "Prune orphans, dangling images, and networks? (yes/no): " prune_answer || true
	[[ "$prune_answer" == "yes" || "$prune_answer" == "YES" ]] && do_prune=true
fi
if $do_prune; then
	echo "Cleaning up Docker resources..."
	docker compose down --rmi all --remove-orphans 2>/dev/null || true
	docker volume prune -f
	docker builder prune -af 2>/dev/null || true
fi

# No-cache: flag or prompt (defaults to yes on timeout)
if ! $do_nocache; then
	nocache_answer=""
	read -t 10 -p "No-cache build? (type 'no' to skip, ENTER/timeout = yes): " nocache_answer || true
	[[ "$nocache_answer" == "no" || "$nocache_answer" == "NO" ]] || do_nocache=true
fi

# Build images
if $do_nocache; then
	echo "Building with --no-cache (full rebuild, no layer cache)..."
	docker compose build --no-cache
else
	echo "Building images (cached)..."
	docker compose build
fi

echo ""
echo "Starting stack..."

# Source start.sh for environment setup (AWS secrets, certs, then docker compose up)
source ./start.sh
