#!/bin/bash
# ╔══════════════════════════════════════════════════════════════════════════════════════╗
# ║  deploy.sh                                                                           ║
# ║                                                                                      ║
# ║  Rebuild the MOBIUS.SMART_HOME Docker image and start the stack. Skip the rebuild    ║
# ║  by running start.sh directly.                                                       ║
# ║                                                                                      ║
# ║      ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐              ║
# ║      │ parse flags      │──▶│ prune (opt.)     │──▶│ docker compose   │              ║
# ║      │ --prune/--no-... │   │ + cleanup images │   │ build [--no-...] │              ║
# ║      └──────────────────┘   └──────────────────┘   └────────┬─────────┘              ║
# ║                                                             ▼                        ║
# ║                                                     ┌──────────────────┐             ║
# ║                                                     │ ./start.sh       │             ║
# ║                                                     └──────────────────┘             ║
# ║                                                                                      ║
# ║  FLAGS:                                                                              ║
# ║    --prune       Prune Docker system before build (skip the prompt)                  ║
# ║    --no-cache    Build with --no-cache (skip the prompt)                             ║
# ║    --help, -h    Show usage and exit                                                 ║
# ║                                                                                      ║
# ║  CANONICAL EXCEPTIONS (documented):                                                  ║
# ║    S.2.1  source_global_env replaced by repo-local start_utils.sh — see start.sh     ║
# ║           header for rationale.                                                      ║
# ║    S.2.3  PAUSE_FILE — not applicable to a Docker build script.                      ║
# ║    S.2.10 simple_logger — colour-aware echo for host-independence.                   ║
# ╚══════════════════════════════════════════════════════════════════════════════════════╝

[[ -t 1 ]] && clear

deactivate &>/dev/null || true

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
SCRIPT_R_PATH=$(realpath "${BASH_SOURCE[0]}")
SCRIPT_DIR="${SCRIPT_R_PATH%${SCRIPT_NAME}}"
builtin cd "$SCRIPT_DIR" &>/dev/null || true

sudo chown -R "$USER":"$USER" ./ &>/dev/null || true

# Color + logger helpers: home copy preferred, in-repo copy as fallback, tolerated absent.
. ~/.env.colors 2>/dev/null || . "${SCRIPT_DIR}.env.colors" 2>/dev/null || true
. ~/logger.sh --no-exec &>/dev/null || . "${SCRIPT_DIR}logger.sh" --no-exec &>/dev/null || true
. /etc/profile.d/custom-env.sh --no-exec &>/dev/null || true
. "${SCRIPT_DIR}start_utils.sh" || true   # repo-local startup library

########################################################################-########################################################################
SMARTHOME_DEPLOY__ARGS=("$@")                                                                                                                    #
SMARTHOME_DEPLOY__DO_PRUNE=false                                                                                                                 #
SMARTHOME_DEPLOY__DO_NOCACHE=false                                                                                                               #
SMARTHOME_DEPLOY__IMAGE_NAME="smarthome-smart-home"                                                                                              #
########################################################################-########################################################################

########################################################################-########################################################################
#                                                                INITIALIZATION                                                                  #
########################################################################-########################################################################
safe_exit() {
	# Exit cleanly whether the script is sourced or executed.
	local exit_code=${1:-$?}
	if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
		exit "$exit_code"
	else
		return "$exit_code"
	fi
}

smarthome_deploy__show_help() {
	# Print usage and exit zero.
	echo ""
	echo -e "${BOLD:-}${CYAN}Usage:${NC} $0 [--prune] [--no-cache] [--help|-h]"
	echo ""
	echo -e "  Rebuild the MOBIUS.SMART_HOME image and start the stack."
	echo ""
	echo -e "${BOLD:-}Options:${NC}"
	echo -e "  ${CYAN}--prune${NC}       Prune Docker resources first (skip prompt)"
	echo -e "  ${CYAN}--no-cache${NC}    Build with --no-cache (skip prompt; default on prompt timeout)"
	echo -e "  ${CYAN}--help${NC}, ${CYAN}-h${NC}    Show this message and exit"
	echo ""
	safe_exit 0
}

smarthome_deploy__parse_args() {
	# Recognize the build flags; --help short-circuits.
	local a
	for a in "${SMARTHOME_DEPLOY__ARGS[@]}"; do
		case "$a" in
			--prune)     SMARTHOME_DEPLOY__DO_PRUNE=true ;;
			--no-cache)  SMARTHOME_DEPLOY__DO_NOCACHE=true ;;
			--help | -h) smarthome_deploy__show_help ;;
		esac
	done
}

smarthome_deploy__verify_files() {
	# Verify the build inputs the docker engine needs are present in cwd.
	if [ ! -f Dockerfile ]; then
		echo -e "${RED}✗ Dockerfile not found in $(pwd)${NC}"
		safe_exit 1
	fi
	if [ ! -f docker-compose.yml ]; then
		echo -e "${RED}✗ docker-compose.yml not found in $(pwd)${NC}"
		safe_exit 1
	fi
}
########################################################################-########################################################################

########################################################################-########################################################################
#                                                                  PROMPTS                                                                       #
########################################################################-########################################################################
smarthome_deploy__maybe_prompt_prune() {
	# If --prune wasn't passed, ask; default to "no" on a 10s timeout so an
	# unattended run never deletes anything by accident.
	$SMARTHOME_DEPLOY__DO_PRUNE && return 0
	local answer="no"
	read -t 10 -r -p "Prune Docker system? (yes/no, 10s timeout = no): " answer || true
	[[ "$answer" == "yes" || "$answer" == "YES" ]] && SMARTHOME_DEPLOY__DO_PRUNE=true
}

smarthome_deploy__maybe_prompt_nocache() {
	# If --no-cache wasn't passed, ask; default to "yes" on timeout so an
	# unattended run produces a clean image (correctness over build time).
	$SMARTHOME_DEPLOY__DO_NOCACHE && return 0
	local answer=""
	read -t 10 -r -p "No-cache build? (type 'no' to skip, ENTER/timeout = yes): " answer || true
	[[ "$answer" == "no" || "$answer" == "NO" ]] || SMARTHOME_DEPLOY__DO_NOCACHE=true
}
########################################################################-########################################################################

########################################################################-########################################################################
#                                                              BUILD & DEPLOY                                                                    #
########################################################################-########################################################################
smarthome_deploy__cleanup_containers() {
	# Bring the previous stack down before rebuilding to avoid name/port conflicts.
	# --remove-orphans clears the dispatcher only if SMART_HOME owns it; an instance
	# launched by TILES is in a different compose project and is left intact.
	echo "Stopping previous stack..."
	docker compose down --remove-orphans &>/dev/null || true
	echo -e "${GREEN}✓ Containers removed${NC}"
}

smarthome_deploy__remove_old_image() {
	# Drop the previously-built application image so the rebuild produces a fresh
	# layer set even when the cache would otherwise reuse it.
	docker rmi "$SMARTHOME_DEPLOY__IMAGE_NAME" &>/dev/null || true
	echo -e "${GREEN}✓ Old image removed${NC}"
}

smarthome_deploy__prune_docker() {
	# System-wide prune (containers, networks, dangling images, build cache).
	$SMARTHOME_DEPLOY__DO_PRUNE || return 0
	echo "Pruning Docker resources..."
	docker system prune -f || true
}

smarthome_deploy__fetch_credentials() {
	# Hub credentials needed if the Dockerfile consumes build args; a no-op in
	# .env-only mode (start_utils.sh routes pull_aws_secrets to .env loading).
	echo "Fetching Hubitat credentials for build..."
	pull_aws_secrets HUBITAT &>/dev/null || true
}

smarthome_deploy__build_image() {
	# Run the build; honor the --no-cache flag.
	if $SMARTHOME_DEPLOY__DO_NOCACHE; then
		echo "Building image (--no-cache, full rebuild)..."
		docker compose build --no-cache
	else
		echo "Building image (cached)..."
		docker compose build
	fi
	if [ $? -ne 0 ]; then
		echo -e "${RED}✗ Docker build failed${NC}"
		safe_exit 1
	fi
	echo -e "${GREEN}✓ Image built${NC}"
}

smarthome_deploy__handoff_to_start() {
	# start.sh handles secrets, certs, network setup, compose up, and health.
	./start.sh
}
########################################################################-########################################################################

########################################################################-########################################################################
#                                                                  EXECUTION                                                                     #
########################################################################-########################################################################
smarthome_deploy__run() {
	# Top-level orchestrator.
	smarthome_deploy__parse_args
	echo "=========================================="
	echo "  MOBIUS.SMART_HOME — Docker Image Build"
	echo "=========================================="
	smarthome_deploy__verify_files
	smarthome_deploy__maybe_prompt_prune
	smarthome_deploy__maybe_prompt_nocache
	smarthome_deploy__cleanup_containers
	smarthome_deploy__remove_old_image
	smarthome_deploy__prune_docker
	smarthome_deploy__fetch_credentials
	smarthome_deploy__build_image
	smarthome_deploy__handoff_to_start
}
########################################################################-########################################################################

smarthome_deploy__run
