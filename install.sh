#!/bin/bash
# ╔══════════════════════════════════════════════════════════════════════════════════════╗
# ║  install.sh                                                                          ║
# ║                                                                                      ║
# ║  Turnkey installer for MOBIUS.SMART_HOME. Designed for `curl … | bash` and for      ║
# ║  direct execution. Bootstraps a clone of the public repo into ~/__MOBIUS.INSTALL/    ║
# ║  and hands off to deploy.sh; if the install dir already exists, just delegates       ║
# ║  (forwards flags).                                                                   ║
# ║                                                                                      ║
# ║      ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐              ║
# ║      │ check deps       │──▶│ clone (if absent)│──▶│ exec deploy.sh   │              ║
# ║      │ docker/git/curl  │   │ ~/__MOBIUS.INST. │   │ "$@" (forward)   │              ║
# ║      └──────────────────┘   └──────────────────┘   └──────────────────┘              ║
# ║                                                                                      ║
# ║  FLAGS:                                                                              ║
# ║    --update              Forwarded to deploy.sh (git pull before build)              ║
# ║    --prune | --no-cache  Forwarded to deploy.sh                                      ║
# ║    --help | -h           Show usage and exit                                         ║
# ║                                                                                      ║
# ║  USAGE:                                                                              ║
# ║    curl -fsSL https://raw.githubusercontent.com/elfege/MOBIUS.SMART_HOME/main/install.sh \║
# ║      | bash                                                                          ║
# ║                              — or, after a manual clone —                            ║
# ║    ./install.sh                                                                      ║
# ║                                                                                      ║
# ║  CANONICAL EXCEPTIONS (documented):                                                  ║
# ║    S.2.1  source_global_env — bootstrap context has no sibling files; helpers are    ║
# ║           inlined here (colour fallbacks, log helper) until after the clone, after   ║
# ║           which deploy.sh / start_utils.sh take over.                                ║
# ║    S.2.3  PAUSE_FILE — not relevant to a one-shot installer.                         ║
# ║    S.2.10 simple_logger — colour-aware echo for host-independence.                   ║
# ╚══════════════════════════════════════════════════════════════════════════════════════╝

[[ -t 1 ]] && clear

set -u   # treat unset vars as errors (catches typos in the bootstrap path)

########################################################################-########################################################################
SMARTHOME_INSTALL__ARGS=("$@")                                                                                                                   #
SMARTHOME_INSTALL__REPO_URL="https://github.com/elfege/MOBIUS.SMART_HOME.git"                                                                     #
SMARTHOME_INSTALL__CLONE_DIR="${HOME}/__MOBIUS.INSTALL"                                                                                           #
SMARTHOME_INSTALL__BRANCH="${SMARTHOME_INSTALL_BRANCH:-main}"                                                                                     #
:                                                                                                                                                #
# Inline colour fallbacks: under `curl | bash` no sibling .env.colors exists yet.                                                                 #
: "${RED:=$'\033[0;31m'}"                                                                                                                        #
: "${GREEN:=$'\033[0;32m'}"                                                                                                                      #
: "${YELLOW:=$'\033[1;33m'}"                                                                                                                     #
: "${CYAN:=$'\033[0;36m'}"                                                                                                                       #
: "${BOLD:=$'\033[1m'}"                                                                                                                          #
: "${NC:=$'\033[0m'}"                                                                                                                            #
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

smarthome_install__show_help() {
	# Print usage and exit zero. Flags are documented; everything else forwards.
	echo ""
	echo -e "${BOLD}${CYAN}Usage:${NC} $0 [--update] [--prune] [--no-cache] [--help|-h]"
	echo ""
	echo -e "  Install or refresh MOBIUS.SMART_HOME into ${CYAN}${SMARTHOME_INSTALL__CLONE_DIR}${NC}."
	echo ""
	echo -e "${BOLD}Options:${NC}"
	echo -e "  ${CYAN}--update${NC}      Pull the latest source before rebuilding (forwarded to deploy.sh)"
	echo -e "  ${CYAN}--prune${NC}       Prune Docker resources before build (forwarded)"
	echo -e "  ${CYAN}--no-cache${NC}    Full rebuild, ignoring layer cache (forwarded)"
	echo -e "  ${CYAN}--help${NC}, ${CYAN}-h${NC}    Show this message and exit"
	echo ""
	echo -e "${BOLD}One-liner:${NC}"
	echo -e "  ${GREEN}curl -fsSL https://raw.githubusercontent.com/elfege/MOBIUS.SMART_HOME/main/install.sh | bash${NC}"
	echo ""
	safe_exit 0
}

smarthome_install__parse_args() {
	# Short-circuit on --help; everything else flows through to deploy.sh.
	local a
	for a in "${SMARTHOME_INSTALL__ARGS[@]}"; do
		case "$a" in
			--help | -h) smarthome_install__show_help ;;
		esac
	done
}
########################################################################-########################################################################

########################################################################-########################################################################
#                                                                DEPENDENCIES                                                                    #
########################################################################-########################################################################
smarthome_install__missing() {
	# Print missing-dependency names to stdout, one per line.
	local cmd; for cmd in "$@"; do command -v "$cmd" >/dev/null 2>&1 || echo "$cmd"; done
}

smarthome_install__ensure_deps() {
	# Install any missing host tools. apt-get on Debian-family, dnf on RHEL-family;
	# anything else falls through with a manual-install message. Uses sudo, so the
	# user gets prompted for a password unless passwordless sudo is configured.
	#
	# Note: the `docker compose` v2 plugin is a SEPARATE package from the engine
	# (`docker.io` ships only `docker`, not `compose`). We probe via
	# `docker compose version` and install `docker-compose-v2` if the plugin is
	# missing — otherwise the build phase blows up with "unknown flag: --no-cache".
	local missing
	missing="$(smarthome_install__missing docker git curl)"
	if ! docker compose version >/dev/null 2>&1; then
		missing="${missing} compose-plugin"
	fi
	missing="${missing# }"
	if [ -z "$missing" ]; then return 0; fi
	echo -e "${YELLOW}Missing tools — installing:${NC} $(printf '%s ' $missing)"
	if command -v apt-get >/dev/null 2>&1; then
		local pkgs="" m
		for m in $missing; do
			case "$m" in
				docker)         pkgs+=" docker.io" ;;
				compose-plugin) pkgs+=" docker-compose-v2" ;;
				*)              pkgs+=" $m" ;;
			esac
		done
		sudo apt-get update -qq || true
		sudo apt-get install -y $pkgs || {
			echo -e "${RED}✗ apt-get install failed${NC}"; safe_exit 1; }
	elif command -v dnf >/dev/null 2>&1; then
		local pkgs="" m
		for m in $missing; do
			case "$m" in
				compose-plugin) pkgs+=" docker-compose-plugin" ;;
				*)              pkgs+=" $m" ;;
			esac
		done
		sudo dnf install -y $pkgs || {
			echo -e "${RED}✗ dnf install failed${NC}"; safe_exit 1; }
	else
		echo -e "${RED}✗ No supported package manager (apt-get / dnf) found.${NC}"
		echo -e "  Please install manually:  ${CYAN}$missing${NC}"
		echo -e "  Docker: https://docs.docker.com/engine/install/"
		safe_exit 1
	fi
	echo -e "${GREEN}✓ Installed:${NC} $(printf '%s ' $missing)"
}

smarthome_install__ensure_docker_running() {
	# `docker ps` is the cheapest "is the daemon up + I can talk to it" probe.
	# Failure path attempts, in order: start the daemon, add the user to the
	# 'docker' group, then re-exec install.sh under `sg docker -c` so the new
	# group membership becomes active *without* requiring a logout (group
	# changes never propagate to the parent shell — sg / newgrp spawn a child
	# shell where they do).
	if docker ps >/dev/null 2>&1; then return 0; fi
	if command -v systemctl >/dev/null 2>&1; then
		echo -e "${CYAN}Starting docker daemon...${NC}"
		sudo systemctl enable --now docker >/dev/null 2>&1 \
			|| sudo systemctl start docker || true
		sleep 2
		if docker ps >/dev/null 2>&1; then
			echo -e "${GREEN}✓ Docker daemon is running${NC}"
			return 0
		fi
	fi
	# Add to docker group if missing.
	if ! id -nG "$USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
		echo -e "${YELLOW}Adding ${USER} to the 'docker' group...${NC}"
		sudo usermod -aG docker "$USER" || true
	fi
	# Verify the group actually grants access (sg spawns a sub-shell with it
	# active without touching the parent's session) — if so, re-exec install.sh
	# under sg so every subsequent step runs with docker access too.
	if command -v sg >/dev/null 2>&1 && sg docker -c 'docker ps >/dev/null 2>&1'; then
		echo -e "${GREEN}✓ Docker accessible via sg; re-executing install.sh in a docker-group shell${NC}"
		local arg args_quoted=""
		for arg in "${SMARTHOME_INSTALL__ARGS[@]}"; do
			args_quoted+="$(printf '%q ' "$arg")"
		done
		exec sg docker -c "exec $(printf '%q' "$0") $args_quoted"
	fi
	echo -e "${RED}✗ Docker installed but unreachable, even after daemon start + group add.${NC}"
	echo -e "  Inspect:  ${CYAN}sudo systemctl status docker${NC} ${CYAN}journalctl -u docker --no-pager -n 50${NC}"
	safe_exit 1
}
########################################################################-########################################################################

########################################################################-########################################################################
#                                                                BOOTSTRAP                                                                       #
########################################################################-########################################################################
smarthome_install__clone_or_reuse() {
	# Fresh install → git clone into the install dir. Existing checkout → leave it
	# in place; deploy.sh handles --update if asked.
	if [ -d "${SMARTHOME_INSTALL__CLONE_DIR}/.git" ]; then
		echo -e "${GREEN}✓ Existing install at ${SMARTHOME_INSTALL__CLONE_DIR}${NC}"
		return 0
	fi
	if [ -e "$SMARTHOME_INSTALL__CLONE_DIR" ]; then
		echo -e "${RED}${SMARTHOME_INSTALL__CLONE_DIR} exists but is not a git checkout — refusing to overwrite${NC}"
		safe_exit 1
	fi
	echo -e "${CYAN}Cloning ${SMARTHOME_INSTALL__REPO_URL} (branch ${SMARTHOME_INSTALL__BRANCH}) → ${SMARTHOME_INSTALL__CLONE_DIR}${NC}"
	git clone --branch "$SMARTHOME_INSTALL__BRANCH" --depth 50 \
		"$SMARTHOME_INSTALL__REPO_URL" "$SMARTHOME_INSTALL__CLONE_DIR" || {
		echo -e "${RED}✗ git clone failed${NC}"; safe_exit 1; }
	echo -e "${GREEN}✓ Cloned${NC}"
}

smarthome_install__seed_env_if_absent() {
	# First-install only — existing .env is left untouched so re-running install.sh
	# is idempotent (returning users keep their config). On first install we ASK
	# ONCE whether the user wants AWS Secrets Manager or .env-only, with timeout
	# default to .env-only (the safer assumption for an arbitrary host). The
	# answer is persisted to .env, so every subsequent run skips this entirely.
	#
	# When there's no terminal (curl-pipe-bash with no /dev/tty, automated
	# provisioner, etc.), we silently use the .env-only default rather than hang.
	#
	# MOBIUS.SMART_HOME-specific secrets seeded in .env-only mode:
	#   POSTGRES_PASSWORD      database creds
	#   APP_API_TOKEN          bearer token for the FastAPI app
	# Hubitat tokens are NOT auto-generated — they come from each user's hub
	# Maker API config and must be set manually in .env after first install.
	local envf="${SMARTHOME_INSTALL__CLONE_DIR}/.env"
	if [ -f "$envf" ]; then
		echo -e "${GREEN}✓ Existing .env at ${envf} — leaving in place${NC}"
		return 0
	fi
	local use_aws="no"
	if [ "${SMARTHOME_USE_AWS_SECRETS:-}" = "true" ]; then
		# Pre-set via env: skip the prompt entirely, honor caller's choice.
		use_aws="yes"
	elif : >/dev/tty 2>/dev/null; then
		echo "" >/dev/tty
		echo -e "${BOLD}MOBIUS.SMART_HOME needs configuration:${NC}" >/dev/tty
		echo -e "  ${CYAN}AWS Secrets Manager${NC} — pulls POSTGRES_PASSWORD / APP_API_TOKEN / hub" >/dev/tty
		echo -e "    tokens from named AWS secrets (SMARTHOME + HUBITAT). Requires aws CLI" >/dev/tty
		echo -e "    configured." >/dev/tty
		echo -e "  ${CYAN}.env-only${NC} (default) — generates random app secrets locally; hub tokens" >/dev/tty
		echo -e "    still need manual entry in .env after install." >/dev/tty
		echo "" >/dev/tty
		local answer=""
		read -t 30 -r -p "Use AWS Secrets Manager? (yes/no, 30s timeout = no): " answer </dev/tty 2>/dev/tty || true
		[[ "$answer" =~ ^(yes|YES|y|Y)$ ]] && use_aws="yes"
	fi
	if [ "$use_aws" = "yes" ]; then
		cat > "$envf" <<-ENV
			# Generated by install.sh (AWS Secrets Manager mode)
			# Required before deploy.sh: set SMARTHOME_AWS_PROFILE (the AWS CLI
			# profile holding the SMARTHOME and HUBITAT secrets).
			# start.sh will pull SMARTHOME (POSTGRES_PASSWORD, APP_API_TOKEN,
			# ports, SERVER_IP) and HUBITAT (HUBITAT_API_TOKEN_4 and friends).
			SMARTHOME_USE_AWS_SECRETS=true
			SMARTHOME_AWS_PROFILE=${SMARTHOME_AWS_PROFILE:-}
		ENV
		chmod 600 "$envf"
		echo -e "${GREEN}✓ AWS-mode .env stub:${NC} ${envf}"
	else
		local pg tok
		pg="$(openssl rand -hex 32 2>/dev/null || head -c 64 /dev/urandom | base64 | tr -d '/+=' | head -c 64)"
		tok="$(openssl rand -hex 32 2>/dev/null || head -c 64 /dev/urandom | base64 | tr -d '/+=' | head -c 64)"
		cat > "$envf" <<-ENV
			# Generated by install.sh (.env-only mode, no AWS, unattended-safe defaults)
			SMARTHOME_USE_AWS_SECRETS=false
			POSTGRES_PASSWORD=${pg}
			APP_API_TOKEN=${tok}

			# Hubitat credentials — REQUIRED for actual operation. Replace these
			# placeholders with your hub's Maker API token + IP + app number.
			# Maker API is opt-in fallback; the admin-API path runs by default.
			HUBITAT_API_TOKEN_MAIN=
			HUBITAT_HUB_IP_MAIN=
			HUBITAT_API_NUMBER_MAIN=

			# Service ports (override only if these collide with other stacks).
			APP_EXTERNAL_PORT=5001
			NGINX_HTTPS_PORT=8445
			POSTGRES_EXTERNAL_PORT=5433
			POSTGREST_EXTERNAL_PORT=3002
			WEBHOOK_PORT=5050
			MATTER_PORT=5580
			SERVER_IP=127.0.0.1
		ENV
		chmod 600 "$envf"
		echo -e "${GREEN}✓ .env seeded (.env-only mode, random app secrets):${NC} ${envf}"
		echo -e "  ${YELLOW}NEXT STEPS:${NC} edit ${envf} to add your Hubitat hub IP + token before"
		echo -e "  the stack can talk to the hub. SERVER_IP also needs your LAN-facing IP."
	fi
}

smarthome_install__handoff() {
	# Hand off to deploy.sh inside the install dir.
	builtin cd "$SMARTHOME_INSTALL__CLONE_DIR" || { echo -e "${RED}✗ cd failed${NC}"; safe_exit 1; }
	if [ ! -x ./deploy.sh ]; then
		echo -e "${RED}✗ deploy.sh missing or non-executable in $(pwd)${NC}"
		safe_exit 1
	fi
	local fwd=("${SMARTHOME_INSTALL__ARGS[@]}")
	echo -e "${CYAN}Handing off to deploy.sh ${fwd[*]}${NC}"
	exec ./deploy.sh "${fwd[@]}"
}
########################################################################-########################################################################

########################################################################-########################################################################
#                                                                  EXECUTION                                                                     #
########################################################################-########################################################################
smarthome_install__run() {
	# Top-level orchestrator.
	smarthome_install__parse_args
	echo "=========================================="
	echo "  MOBIUS.SMART_HOME — Installer"
	echo "=========================================="
	smarthome_install__ensure_deps
	smarthome_install__ensure_docker_running
	smarthome_install__clone_or_reuse
	smarthome_install__seed_env_if_absent
	smarthome_install__handoff
}
########################################################################-########################################################################

smarthome_install__run
