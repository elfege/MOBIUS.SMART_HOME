#!/bin/bash
# ╔══════════════════════════════════════════════════════════════════════════════════════╗
# ║  start.sh                                                                            ║
# ║                                                                                      ║
# ║  Bring up the MOBIUS.SMART_HOME stack: source helpers, fetch secrets, generate TLS,  ║
# ║  start containers, run a dual (direct + nginx HTTPS) health probe.                   ║
# ║                                                                                      ║
# ║      ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐              ║
# ║      │ source helpers   │──▶│ wait AWS / load  │──▶│ pull SMARTHOME + │              ║
# ║      │ + start_utils.sh │   │   .env (mode)    │   │      HUBITAT     │              ║
# ║      └──────────────────┘   └──────────────────┘   └────────┬─────────┘              ║
# ║                                                             ▼                        ║
# ║      ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐              ║
# ║      │ TV token cascade │   │ map AWS HUB_4    │   │ TLS certs + net  │              ║
# ║      │ state>.env>AWS   │   │ → HUB_MAIN, _1-3 │   │ (smarthome-net)  │              ║
# ║      └────────┬─────────┘   └────────┬─────────┘   └────────┬─────────┘              ║
# ║               └────────────────┬─────┴────────────────┬─────┘                        ║
# ║                                ▼                      ▼                              ║
# ║                        ┌──────────────────┐   ┌──────────────────┐                   ║
# ║                        │ webhook share    │──▶│ docker compose   │                   ║
# ║                        │ skip if running  │   │ up -d  +  probe  │                   ║
# ║                        └──────────────────┘   └──────────────────┘                   ║
# ║                                                                                      ║
# ║  FLAGS:                                                                              ║
# ║    --help, -h    Show usage and exit                                                 ║
# ║                                                                                      ║
# ║  CANONICAL EXCEPTIONS (documented):                                                  ║
# ║    S.2.1  (source_global_env) — replaced by repo-local start_utils.sh + .env.colors  ║
# ║           + logger.sh fallbacks, so the script runs on hosts without a personal      ║
# ║           shell config (the whole reason this lib exists).                           ║
# ║    S.2.3  (PAUSE_FILE / --force) — not applicable: this is a Docker stack manager,   ║
# ║           not a cron-driven sync script.                                             ║
# ║    S.2.10 (simple_logger) — replaced by colour-aware echo so output works without    ║
# ║           the host logger; structured logging is the container's job.                ║
# ╚══════════════════════════════════════════════════════════════════════════════════════╝

[[ -t 1 ]] && clear

deactivate &>/dev/null || true

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
SCRIPT_R_PATH=$(realpath "${BASH_SOURCE[0]}")
SCRIPT_DIR="${SCRIPT_R_PATH%${SCRIPT_NAME}}"
builtin cd "$SCRIPT_DIR" &>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
# --test : bring up the ETT TEST STACK and return (canonical ETT; NVR ref impl).
#
# SAME compose file as prod, but: project name `smarthome_test` (own volumes ->
# fresh empty DB), .env.test (dummy tokens, unroutable hub IPs, +1000 ports,
# EVENTSOCKET_ENABLED=false, container prefix), and ONLY the four core services
# (smart-home postgres postgrest nginx) — no matter-server, no autoheal, no
# webhook-dispatcher. Layered so the test stack can NEVER act on the live house.
# No AWS pull: .env.test is self-sufficient (nothing in it is secret).
# ─────────────────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--test" ]]; then
	set -uo pipefail
	if [[ ! -f .env.test ]]; then
		echo "ERROR: .env.test not found at $(pwd).env.test" >&2
		exit 1
	fi
	echo "Bringing up smarthome_test stack (same compose file, .env.test overrides,"
	echo "container prefix smarthome_test_, ports +1000, four core services only)..."
	docker compose -p smarthome_test --env-file .env.test up -d --wait \
		smart-home postgres postgrest nginx
	# Migrations: psql/02-apply-migrations.sh only runs at postgres VOLUME
	# CREATION; a REUSED test volume would silently miss newer migrations, so
	# reconcile through the one runner on every --test start (idempotent).
	bash scripts/apply_db_migrations.sh smarthome_test_smarthome-postgres \
		smarthome_api smarthome || true
	echo ""
	docker compose -p smarthome_test --env-file .env.test ps \
		--format "table {{.Service}}\t{{.Status}}\t{{.Ports}}"
	echo ""
	echo "Test app:   http://localhost:6001   (nginx: http 9082 / https 9445)"
	echo "Run tests:  ./test.sh    Stop: ./stop.sh --test"
	exit 0
fi

# Color + logger helpers: home copy preferred, in-repo copy as fallback, tolerated absent.
. ~/.env.colors 2>/dev/null || . "${SCRIPT_DIR}.env.colors" 2>/dev/null || true
. ~/logger.sh --no-exec &>/dev/null || . "${SCRIPT_DIR}logger.sh" --no-exec &>/dev/null || true

# Required: repo-local startup library (AWS-secret pull + .env-only mode + color fallbacks).
# Sets START_UTILS__PROJECT so the library reads SMARTHOME_-prefixed env vars
# via bash indirection — the same library file is shared (via ~/start_utils.sh
# + pre-commit rsync) across every MOBIUS project, each setting its own prefix.
export START_UTILS__PROJECT="SMARTHOME"
. "${SCRIPT_DIR}start_utils.sh" || {
	echo -e "${RED}✗ Failed to source ${SCRIPT_DIR}start_utils.sh — required by start.sh${NC}"
	exit 1
}

########################################################################-########################################################################
SMARTHOME_START__ARGS=("$@")                                                                                                                     #
:                                                                                                                                                #
SMARTHOME_START__LOG_FILE="${LOG_FILE:-$HOME/0_LOGS/log.log}"                                                                                    #
SMARTHOME_START__AWS_WAIT_URL="https://sts.amazonaws.com"                                                                                        #
:                                                                                                                                                #
SMARTHOME_START__CERT_DIR="${SCRIPT_DIR}nginx/certs"                                                                                              #
SMARTHOME_START__CERT_FULL="${SMARTHOME_START__CERT_DIR}/fullchain.pem"                                                                          #
SMARTHOME_START__CERT_KEY="${SMARTHOME_START__CERT_DIR}/privkey.pem"                                                                             #
:                                                                                                                                                #
SMARTHOME_START__NETWORK_NAME="smarthome_smarthome-net"                                                                                          #
SMARTHOME_START__TILES_NETWORK_NAME="tiles_tiles-net"                                                                                            #
SMARTHOME_START__APP_CONTAINER="smarthome-app"                                                                                                    #
SMARTHOME_START__DISPATCHER_CONTAINER="webhook-dispatcher"                                                                                       #
:                                                                                                                                                #
SMARTHOME_START__TV_STATE_FILE="${SCRIPT_DIR}state/samsung_tv_token.txt"                                                                         #
SMARTHOME_START__TV_ENV_FILE="${SCRIPT_DIR}.env"                                                                                                 #
########################################################################-########################################################################

########################################################################-########################################################################
#                                                                INITIALIZATION                                                                  #
########################################################################-########################################################################
safe_exit() {
	# Exit cleanly whether the script is sourced or executed. Use in place of bare
	# `exit` so a sourced invocation returns to the caller's shell instead of killing it.
	local exit_code=${1:-$?}
	if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
		exit "$exit_code"
	else
		return "$exit_code"
	fi
}

smarthome_start__show_help() {
	# Print usage and exit zero. --help / -h are the only recognized flags.
	echo ""
	echo -e "${BOLD:-}${CYAN}Usage:${NC} $0 [--help|-h]"
	echo ""
	echo -e "  Bring up the MOBIUS.SMART_HOME Docker stack. Pulls secrets from AWS by default;"
	echo -e "  set ${CYAN}SMARTHOME_USE_AWS_SECRETS=false${NC} in .env for AWS-free deployment."
	echo ""
	echo -e "${BOLD:-}Options:${NC}"
	echo -e "  ${CYAN}--help${NC}, ${CYAN}-h${NC}   Show this message and exit"
	echo ""
	safe_exit 0
}

smarthome_start__parse_args() {
	# Tiny flag handler — only --help is exposed; unknown flags are ignored to keep
	# back-compat with any external invocations that pass extra context.
	local a
	for a in "${SMARTHOME_START__ARGS[@]}"; do
		case "$a" in
			--help | -h) smarthome_start__show_help ;;
		esac
	done
}

smarthome_start__cleanup() {
	# Trapped on EXIT/ERR/INT/TERM/TSTP. Disables further traps to prevent re-entry,
	# logs exit status, and propagates the code via safe_exit so sourced invocations
	# return to the caller instead of killing the shell.
	local exit_code=${1:-$?}
	trap - EXIT INT TERM TSTP ERR
	if [ "$exit_code" -ne 0 ]; then
		echo -e "${RED}✗ start.sh exited non-zero (${exit_code})${NC}" >&2
	fi
	safe_exit "$exit_code"
}
trap 'smarthome_start__cleanup $?' EXIT INT TERM TSTP ERR
########################################################################-########################################################################

########################################################################-########################################################################
#                                                                ENVIRONMENT                                                                     #
########################################################################-########################################################################
smarthome_start__wait_for_aws() {
	# Block until sts.amazonaws.com is reachable, logging every 5s. Skipped entirely
	# in .env-only mode so the stack starts fully offline. Post-power-loss guard.
	mkdir -p "$(dirname "$SMARTHOME_START__LOG_FILE")"
	start_utils__use_aws || return 0
	if curl -sf --max-time 5 "$SMARTHOME_START__AWS_WAIT_URL" -o /dev/null 2>&1; then
		echo -e "${GREEN:-\033[0;32m}[$(date '+%H:%M:%S')] AWS connectivity confirmed — proceeding${NC:-\033[0m}"
		echo "[$(date '+%H:%M:%S')] AWS connectivity confirmed" >> "$SMARTHOME_START__LOG_FILE"
		return 0
	fi
	local msg="[$(date '+%H:%M:%S')] Waiting for AWS (${SMARTHOME_START__AWS_WAIT_URL}) — retrying every 5s"
	echo -e "${FLASH_ACCENT_YELLOW:-\033[5;33m}${msg}${NC:-\033[0m}"
	echo "$msg" >> "$SMARTHOME_START__LOG_FILE"
	until curl -sf --max-time 5 "$SMARTHOME_START__AWS_WAIT_URL" -o /dev/null 2>&1; do
		msg="[$(date '+%H:%M:%S')] Still waiting for AWS — retrying in 5s"
		echo -e "${FLASH_ACCENT_YELLOW:-\033[5;33m}${msg}${NC:-\033[0m}"
		echo "$msg" >> "$SMARTHOME_START__LOG_FILE"
		sleep 5
	done
	echo -e "${GREEN:-\033[0;32m}[$(date '+%H:%M:%S')] AWS connectivity confirmed — proceeding${NC:-\033[0m}"
	echo "[$(date '+%H:%M:%S')] AWS connectivity confirmed" >> "$SMARTHOME_START__LOG_FILE"
}

smarthome_start__ensure_deps() {
	# Install host packages required before container start: openssl for TLS cert
	# generation. jq is ensured lazily by start_utils__ensure_jq when needed.
	if ! command -v openssl &>/dev/null; then
		echo "Installing openssl..."
		sudo apt-get update -qq && sudo apt-get install -y openssl
	fi
}

smarthome_start__load_local_env() {
	# Source the project .env for non-secret local overrides (ports, Samsung TV
	# app name, SERVER_IP). Safe to call before pull_aws_secrets — AWS values
	# subsequently exported take precedence. Idempotent across re-runs.
	if [ -f "${SCRIPT_DIR}.env" ]; then
		set -a; . "${SCRIPT_DIR}.env"; set +a
		echo -e "${GREEN}✓ .env loaded${NC}"
	fi
}

smarthome_start__stop_existing() {
	# If a previous stack is running, bring it down before starting the new one
	# to avoid name/port conflicts. Targets the app container specifically so
	# the shared webhook-dispatcher (also used by TILES) is left untouched.
	docker ps --format '{{.Names}}' | grep -q "^${SMARTHOME_START__APP_CONTAINER}$" || return 0
	echo "Stopping existing SMART_HOME stack..."
	docker compose down --remove-orphans 2>/dev/null || true
}
########################################################################-########################################################################

########################################################################-########################################################################
#                                                                  SECRETS                                                                       #
########################################################################-########################################################################
smarthome_start__fetch_app_config() {
	# Pull the SMARTHOME secret (Postgres password, ports, API token, SERVER_IP).
	# pull_aws_secrets is a no-op (loads .env) in .env-only mode; the required-key
	# check below works in both modes.
	echo "Fetching app configuration..."
	pull_aws_secrets SMARTHOME
	if [ -z "$POSTGRES_PASSWORD" ]; then
		echo -e "${RED}✗ SMARTHOME secret missing or incomplete (no POSTGRES_PASSWORD)${NC}"
		echo -e "${RED}  Create with:  push_secret_to_aws SMARTHOME POSTGRES_PASSWORD <pw> ...${NC}"
		echo -e "${RED}  Or in .env-only mode, set POSTGRES_PASSWORD in .env${NC}"
		safe_exit 1
	fi
	echo -e "${GREEN}✓ SMARTHOME config loaded${NC}"
}

smarthome_start__fetch_hub_tokens() {
	# Pull the HUBITAT secret (shared across SMART_HOME + TILES projects). Exports
	# HUBITAT_API_TOKEN_1..4 / HUBITAT_HUB_IP_1..4 / HUBITAT_API_NUMBER_1..4.
	# Hub 4 is the primary in this deployment; absence is fatal.
	echo "Fetching Hubitat hub tokens..."
	pull_aws_secrets HUBITAT
	if [ -z "$HUBITAT_API_TOKEN_4" ]; then
		echo -e "${RED}✗ HUBITAT_API_TOKEN_4 (primary hub) not found${NC}"
		safe_exit 1
	fi
	echo -e "${GREEN}✓ Hubitat tokens loaded${NC}"
}

smarthome_start__map_hub_names() {
	# Translate AWS-numbered hub variables → app-standardized names so the app
	# doesn't depend on hub-numbering conventions. HUB_4 is primary by site
	# convention; HUB_1/2/3 are the other LAN hubs (token+IP+app number).
	export HUBITAT_API_TOKEN_MAIN="${HUBITAT_API_TOKEN_4}"
	export HUBITAT_HUB_IP_MAIN="${HUBITAT_HUB_IP_4:-<LAN_IP>}"
	export HUBITAT_API_NUMBER_MAIN="${HUBITAT_API_NUMBER_4:-268}"
	export HUBITAT_API_TOKEN_OTHER_HUB_1="${HUBITAT_API_TOKEN_1:-}"
	export HUBITAT_HUB_IP_OTHER_HUB_1="${HUBITAT_HUB_IP_1:-}"
	export HUBITAT_API_NUMBER_OTHER_HUB_1="${HUBITAT_API_NUMBER_1:-}"
	export HUBITAT_API_TOKEN_OTHER_HUB_2="${HUBITAT_API_TOKEN_2:-}"
	export HUBITAT_HUB_IP_OTHER_HUB_2="${HUBITAT_HUB_IP_2:-}"
	export HUBITAT_API_NUMBER_OTHER_HUB_2="${HUBITAT_API_NUMBER_2:-}"
	export HUBITAT_API_TOKEN_OTHER_HUB_3="${HUBITAT_API_TOKEN_3:-}"
	export HUBITAT_HUB_IP_OTHER_HUB_3="${HUBITAT_HUB_IP_3:-}"
	export HUBITAT_API_NUMBER_OTHER_HUB_3="${HUBITAT_API_NUMBER_3:-}"
}

smarthome_start__resolve_tv_token() {
	# Samsung TV token cascade: state file (container-written on every token
	# refresh) > .env (manual override) > AWS secret (anything pulled earlier).
	# A missing token is non-fatal — the TV will require re-pairing on first
	# connect, which the driver handles.
	local file_tok env_tok
	if [ -f "$SMARTHOME_START__TV_STATE_FILE" ]; then
		file_tok="$(cat "$SMARTHOME_START__TV_STATE_FILE" | tr -d '[:space:]')"
		if [ -n "$file_tok" ]; then
			export SAMSUNG_TV_TOKEN="$file_tok"
			echo -e "${GREEN}✓ Samsung TV token loaded from state file${NC}"
			return 0
		fi
	fi
	if [ -z "${SAMSUNG_TV_TOKEN:-}" ] && [ -f "$SMARTHOME_START__TV_ENV_FILE" ]; then
		env_tok="$(grep '^SAMSUNG_TV_TOKEN=' "$SMARTHOME_START__TV_ENV_FILE" | cut -d= -f2 | tr -d '[:space:]')"
		if [ -n "$env_tok" ]; then
			export SAMSUNG_TV_TOKEN="$env_tok"
			echo -e "${GREEN}✓ Samsung TV token loaded from .env${NC}"
			return 0
		fi
	fi
	if [ -z "${SAMSUNG_TV_TOKEN:-}" ]; then
		echo -e "${YELLOW}⚠ No Samsung TV token found — TV will require pairing on first connect${NC}"
	fi
}

smarthome_start__configure_runtime() {
	# Defaults for external-facing service ports + the webhook target list.
	# Each value honors an existing environment override so deployments can pin
	# ports. The Hubitat side configures a SINGLE endpoint (the dispatcher) and
	# it fans out from there.
	#
	# P0 of the TILES decommission (2026-07-12): the `tiles-app` fan-out target
	# was DEAD — TILES removed its /api/webhook/event intake in May 2026, so the
	# dispatcher had been POSTing every Hubitat event into a black hole. Dropped.
	# (The TILES-network connect below is now moot too; it goes at P5 retirement.)
	export WEBHOOK_TARGETS="${WEBHOOK_TARGETS:-http://${SMARTHOME_START__APP_CONTAINER}:${APP_INTERNAL_PORT:-5000}/api/webhook/event}"
}

smarthome_start__load_environment() {
	# Wrap all env-mutating phases under `set -a` so every assigned variable is
	# auto-exported to `docker compose up`. pull_aws_secrets exports explicitly
	# too, but the wrapper keeps additive vars (port defaults, hub mapping)
	# clean.
	set -a
	smarthome_start__load_local_env
	smarthome_start__fetch_app_config
	smarthome_start__fetch_hub_tokens
	smarthome_start__map_hub_names
	smarthome_start__resolve_tv_token
	smarthome_start__configure_runtime
	set +a
}

smarthome_start__print_loaded_config() {
	# Friendly summary of what landed in the env. Tokens truncated for the log;
	# passwords masked entirely.
	echo ""
	echo "Configuration:"
	echo "  APP_EXTERNAL_PORT:        ${APP_EXTERNAL_PORT:-5001}"
	echo "  APP_INTERNAL_PORT:        ${APP_INTERNAL_PORT:-5000}"
	echo "  NGINX_HTTPS_PORT:         ${NGINX_HTTPS_PORT:-8445}"
	echo "  POSTGRES_PORT:            ${POSTGRES_EXTERNAL_PORT:-5433} -> ${POSTGRES_INTERNAL_PORT:-5432}"
	echo "  POSTGREST_PORT:           ${POSTGREST_EXTERNAL_PORT:-3002} -> ${POSTGREST_INTERNAL_PORT:-3001}"
	echo "  WEBHOOK_PORT:             ${WEBHOOK_PORT:-5050}"
	echo "  MATTER_PORT:              ${MATTER_PORT:-5580}"
	echo "  SERVER_IP:                ${SERVER_IP}"
	echo "  HUBITAT_HUB_IP_MAIN:      ${HUBITAT_HUB_IP_MAIN}"
	echo "  HUBITAT_API_NUMBER_MAIN:  ${HUBITAT_API_NUMBER_MAIN}"
	echo "  APP_API_TOKEN:            ${APP_API_TOKEN:0:4}... (hidden)"
	echo "  POSTGRES_PASSWORD:        **** (hidden)"
	echo "  HUBITAT_API_TOKEN_MAIN:   ${HUBITAT_API_TOKEN_MAIN:0:4}... (hidden)"
}
########################################################################-########################################################################

########################################################################-########################################################################
#                                                                  NETWORK & TLS                                                                 #
########################################################################-########################################################################
smarthome_start__gen_ssl_certs() {
	# Self-signed TLS cert for nginx, generated only when absent. CN + SAN bind
	# to SERVER_IP so https://${SERVER_IP} works without warnings under mkcert;
	# raw https://ip works always (with the usual self-signed warning).
	if [ -f "$SMARTHOME_START__CERT_FULL" ] && [ -f "$SMARTHOME_START__CERT_KEY" ]; then
		echo -e "${GREEN}✓ TLS certificates already exist${NC}"
		return 0
	fi
	echo "Generating self-signed TLS certificates..."
	mkdir -p "$SMARTHOME_START__CERT_DIR"
	openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
		-keyout "$SMARTHOME_START__CERT_KEY" \
		-out "$SMARTHOME_START__CERT_FULL" \
		-subj "/C=US/ST=State/L=City/O=SmartHome/CN=${SERVER_IP}" \
		-addext "subjectAltName=IP:${SERVER_IP},DNS:localhost,DNS:<HOST>" \
		2>/dev/null
	chmod 600 "$SMARTHOME_START__CERT_KEY"
	mkdir -p "${SCRIPT_DIR}nginx/html"
	echo -e "${GREEN}✓ TLS certificates generated${NC}"
}

smarthome_start__ensure_network() {
	# Compose marks the network external so the proxy (or other stacks) can
	# attach without "Resource in use" errors on restart. Create it up-front
	# if it doesn't exist yet.
	docker network inspect "$SMARTHOME_START__NETWORK_NAME" &>/dev/null || \
		docker network create "$SMARTHOME_START__NETWORK_NAME" >/dev/null
}
########################################################################-########################################################################

########################################################################-########################################################################
#                                                                  EXECUTION                                                                     #
########################################################################-########################################################################
smarthome_start__start_stack() {
	# Install/verify the host-side UI restart + matter-service watcher and its
	# tmpfs trigger dir BEFORE compose up (so the dir exists for the bind mount).
	# Non-fatal — never blocks bringing the stack up.
	smarthome_start__install_restart_watcher

	# The webhook-dispatcher is a shared container across MOBIUS.SMART_HOME and
	# MOBIUS.TILES — Docker only runs one. Whichever project starts first owns
	# it; subsequent starts skip the service to avoid name collision.
	if docker ps --format '{{.Names}}' | grep -q "^${SMARTHOME_START__DISPATCHER_CONTAINER}$"; then
		echo "webhook-dispatcher already running (shared container) — starting the rest"
		docker compose up -d smart-home postgres postgrest nginx matter-server
	else
		echo "Starting all containers (including webhook-dispatcher)..."
		docker compose up -d
	fi

	# Cross-stack DNS: connect the dispatcher to TILES's network so `tiles-app`
	# resolves when fan-out targets it. Best-effort — silently skipped if TILES
	# isn't running.
	docker network connect "$SMARTHOME_START__TILES_NETWORK_NAME" "$SMARTHOME_START__DISPATCHER_CONTAINER" 2>/dev/null || true

	echo "Waiting for containers to settle..."
	sleep 5

	if ! docker ps --format '{{.Names}}' | grep -q "^${SMARTHOME_START__APP_CONTAINER}$"; then
		echo -e "${RED}✗ ${SMARTHOME_START__APP_CONTAINER} failed to start${NC}"
		echo "  Inspect: docker compose logs"
		safe_exit 1
	fi
	echo -e "${GREEN}✓ Containers are running${NC}"

	smarthome_start__apply_migrations
}

# =============================================================================
# DB MIGRATIONS — the LIVE-database path (canonical SQL.1, 2026-07-13)
# =============================================================================
# psql/02-apply-migrations.sh only runs when Postgres initializes an EMPTY data
# directory. An already-initialized database (the normal case) would therefore
# never see a new migration — so we apply them here, on every start, against the
# running container.
#
# WHY THIS FUNCTION EXISTS: until 2026-07-13 there was NO runner anywhere. The
# schema was created by ~93 DDL statements executed as PYTHON STRINGS inside
# app.py at every boot (schema-as-code, unversioned — forbidden by SQL.1), while
# the .sql files in psql/ were dead documentation that nothing ever executed.
# app.py's loop also swallowed every exception as a mere warning, so a broken
# migration could never fail loudly. Both are fixed: app.py carries zero DDL, and
# failures below are REPORTED.
#
# Migrations are idempotent (IF NOT EXISTS / OR REPLACE / re-GRANT), so re-applying
# the whole chain on every start is a cheap no-op. Verified 2026-07-13: the chain
# (000 baseline -> 010 -> 011) builds a virgin database that matches live exactly.
# =============================================================================
smarthome_start__apply_migrations() {
	local runner="${SCRIPT_DIR}scripts/apply_db_migrations.sh"
	[[ -x "$runner" ]] || { echo -e "${RED}✗ missing $runner${NC}"; return 1; }

	echo ""
	echo "Applying DB migrations..."
	"$runner" \
		"${SMARTHOME_START__POSTGRES_CONTAINER:-smarthome-postgres}" \
		"${POSTGRES_USER:-smarthome_api}" \
		"${POSTGRES_DB:-smarthome}" \
		"${SCRIPT_DIR}psql/migrations"
}

smarthome_start__health_check() {
	# Probe both the direct app port and the nginx HTTPS endpoint. Both are
	# best-effort — a "pending" message means the container is still warming
	# up (Postgres init, schema migrations), not necessarily broken.
	sleep 5
	if curl -s "http://localhost:${APP_EXTERNAL_PORT:-5001}/api/health" >/dev/null 2>&1; then
		echo -e "${GREEN}✓ Health check passed (direct app)${NC}"
	else
		echo -e "${YELLOW}⚠ Direct health check pending — app may still be starting${NC}"
	fi
	if curl -sk "https://localhost:${NGINX_HTTPS_PORT:-8445}/api/health" >/dev/null 2>&1; then
		echo -e "${GREEN}✓ Health check passed (nginx HTTPS)${NC}"
	else
		echo -e "${YELLOW}⚠ Nginx HTTPS health check pending — may still be starting${NC}"
	fi
}

smarthome_start__print_access_info() {
	# Friendly summary of how to reach the running stack and the most useful
	# commands for follow-up.
	echo ""
	echo "Access MOBIUS.SMART_HOME at:"
	echo "  - https://${SERVER_IP}:${NGINX_HTTPS_PORT:-8445}/"
	echo "  - http://${SERVER_IP}:${APP_EXTERNAL_PORT:-5001}/         (direct, no nginx)"
	echo "  - https://${SERVER_IP}:${NGINX_HTTPS_PORT:-8445}/docs     (OpenAPI docs)"
	echo ""
	echo "Webhook dispatcher:"
	echo "  - http://${SERVER_IP}:${WEBHOOK_PORT:-5050}/api/webhook/event"
	echo ""
	echo "Useful commands:"
	echo "  View logs:        docker compose logs -f smart-home"
	echo "  Dispatcher logs:  docker compose logs -f webhook-dispatcher"
	echo "  Stop containers:  ./stop.sh"
	echo "  Rebuild:          ./deploy.sh"
	echo ""
}

smarthome_start__install_restart_watcher() {
    # UI Restart button (canonical STANDARD RESTART.1-4, mirrors NVR + TILES):
    # ensure the tmpfs trigger dir exists (mounted into the app container by
    # docker-compose) and install the host-side watcher systemd unit. NON-FATAL:
    # a watcher hiccup must never block bringing the stack up.
    local unit="smarthome-restart-watcher.service"
    local script="${SCRIPT_DIR}scripts/smarthome-restart-watcher.sh"
    mkdir -p /dev/shm/smarthome-restart 2>/dev/null || true
    # World-writable + sticky (like /tmp): the host watcher (User=elfege) AND
    # the container app (non-root appuser) both write the trigger file. Without
    # this, docker's bind mount creates the dir root-owned and the watcher hits
    # "Permission denied" (2026-07-09).
    chmod 1777 /dev/shm/smarthome-restart 2>/dev/null || true
    if [[ ! -f "$script" ]]; then
        echo -e "${YELLOW}⚠ ${script} not found — UI restart button disabled${NC}"
        return 0
    fi
    chmod +x "$script" 2>/dev/null || true
    if systemctl is-active --quiet "$unit" 2>/dev/null; then
        echo -e "${GREEN}✓ ${unit} already running${NC}"
        return 0
    fi
    if ! sudo tee "/etc/systemd/system/${unit}" >/dev/null <<UNIT
[Unit]
Description=MOBIUS.SMART_HOME UI restart watcher (runs start.sh on trigger)
After=network.target docker.service

[Service]
Type=simple
User=${USER}
ExecStart=${script}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
    then
        echo -e "${YELLOW}⚠ could not write ${unit} — UI restart button disabled${NC}"
        return 0
    fi
    sudo systemctl daemon-reload 2>/dev/null || true
    sudo systemctl enable "$unit" 2>/dev/null || true
    sudo systemctl start "$unit" 2>/dev/null || true
    if systemctl is-active --quiet "$unit" 2>/dev/null; then
        echo -e "${GREEN}✓ ${unit} installed + running (UI restart button enabled)${NC}"
    else
        echo -e "${YELLOW}⚠ ${unit} not active — check: journalctl -u ${unit}${NC}"
    fi
}

smarthome_start__run() {
	# Top-level orchestrator. Each phase is self-contained and fail-loud; the
	# trap handles partial-failure status reporting.
	smarthome_start__parse_args
	smarthome_start__wait_for_aws
	echo "=========================================="
	echo "  MOBIUS.SMART_HOME — Startup"
	echo "=========================================="
	smarthome_start__ensure_deps
	smarthome_start__stop_existing
	smarthome_start__load_environment
	smarthome_start__print_loaded_config
	smarthome_start__gen_ssl_certs
	smarthome_start__ensure_network
	smarthome_start__start_stack
	smarthome_start__health_check
	smarthome_start__print_access_info
}
########################################################################-########################################################################

smarthome_start__run
