#!/bin/bash
# =============================================================================
# start.sh - Start 0_SMART_HOME containers
#
# ALL configuration comes from AWS Secrets Manager:
#   - HUBITAT secret: Hub tokens, IPs, app numbers
#   - SMARTHOME secret: Ports, DB config, API token, server IP
#
# NO .env file. No hardcoded credentials. No file-based secrets.
# =============================================================================

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
SCRIPT_R_PATH=$(realpath "${BASH_SOURCE[0]}")
SCRIPT_DIR="${SCRIPT_R_PATH%${SCRIPT_NAME}}"
cd "$SCRIPT_DIR" &>/dev/null || true

# Source helper scripts
. ~/.env.colors 2>/dev/null || true
. ~/logger.sh --no-exec &>/dev/null || true
. ~/.bash_utils &>/dev/null || {
	echo -e "${RED:-}ERROR: Failed to source ~/.bash_utils - required for AWS secrets${NC:-}"
	exit 1
}

echo "=========================================="
echo "  0_SMART_HOME - Startup"
echo "=========================================="
echo ""

# Stop existing 0_SMART_HOME containers if running (NOT webhook-dispatcher)
if docker ps --format '{{.Names}}' | grep -q '^smarthome-app$'; then
	echo "Stopping existing 0_SMART_HOME containers..."
	docker compose down --remove-orphans 2>/dev/null || true
fi

# -------------------------------------------------------------------------
# Pull ALL configuration from AWS Secrets Manager
# pull_aws_secrets exports every key-value pair as env vars
# Profile 1 = personal AWS account
# -------------------------------------------------------------------------
echo ""
echo "Fetching configuration from AWS Secrets Manager..."
set -a

# Application config: ports, DB creds, API token, server IP
pull_aws_secrets SMARTHOME 1

if [ -z "$POSTGRES_PASSWORD" ]; then
	echo -e "${RED:-}ERROR: SMARTHOME secret missing or incomplete (no POSTGRES_PASSWORD)${NC:-}"
	echo "Create it with: push_secret_to_aws SMARTHOME POSTGRES_PASSWORD <pw> ... 1"
	exit 1
fi
echo -e "${GREEN:-}OK: SMARTHOME config loaded${NC:-}"

# Hubitat hub tokens and connection info (shared across projects)
pull_aws_secrets HUBITAT 1

if [ -z "$HUBITAT_API_TOKEN_4" ]; then
	echo -e "${RED:-}ERROR: HUBITAT_API_TOKEN_4 (primary hub) not found in AWS${NC:-}"
	exit 1
fi
echo -e "${GREEN:-}OK: Hubitat tokens loaded${NC:-}"

# -------------------------------------------------------------------------
# Map AWS variable names → app-standardized names
# AWS uses numbered hubs (HUBITAT_*_1 through _4) matching personal config.
# The app uses generic names so it doesn't depend on hub numbering.
# -------------------------------------------------------------------------
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

# Derived vars
export WEBHOOK_TARGETS="${WEBHOOK_TARGETS:-http://smarthome-app:${APP_INTERNAL_PORT:-5000}/api/webhook/event,http://host.docker.internal:80/api/webhook/event}"

set +a

# Display loaded configuration (tokens truncated)
echo ""
echo "Configuration:"
echo "  APP_EXTERNAL_PORT:        ${APP_EXTERNAL_PORT}"
echo "  APP_INTERNAL_PORT:        ${APP_INTERNAL_PORT}"
echo "  NGINX_HTTPS_PORT:         ${NGINX_HTTPS_PORT}"
echo "  POSTGRES_PORT:            ${POSTGRES_EXTERNAL_PORT} -> ${POSTGRES_INTERNAL_PORT}"
echo "  POSTGREST_PORT:           ${POSTGREST_EXTERNAL_PORT} -> ${POSTGREST_INTERNAL_PORT}"
echo "  WEBHOOK_PORT:             ${WEBHOOK_PORT}"
echo "  MATTER_PORT:              ${MATTER_PORT:-5580}"
echo "  SERVER_IP:                ${SERVER_IP}"
echo "  HUBITAT_HUB_IP_MAIN:      ${HUBITAT_HUB_IP_MAIN}"
echo "  HUBITAT_API_NUMBER_MAIN:   ${HUBITAT_API_NUMBER_MAIN}"
echo "  APP_API_TOKEN:            ${APP_API_TOKEN:0:4}... (hidden)"
echo "  POSTGRES_PASSWORD:        **** (hidden)"
echo "  HUBITAT_API_TOKEN_MAIN:    ${HUBITAT_API_TOKEN_MAIN:0:4}... (hidden)"

# -------------------------------------------------------------------------
# SSL certificates (self-signed, for HTTPS via nginx)
# -------------------------------------------------------------------------
CERT_DIR="$SCRIPT_DIR/nginx/certs"
if [ ! -f "$CERT_DIR/fullchain.pem" ] || [ ! -f "$CERT_DIR/privkey.pem" ]; then
	echo ""
	echo "Generating self-signed SSL certificates..."
	mkdir -p "$CERT_DIR"
	openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
		-keyout "$CERT_DIR/privkey.pem" \
		-out "$CERT_DIR/fullchain.pem" \
		-subj "/C=US/ST=State/L=City/O=SmartHome/CN=${SERVER_IP}" \
		-addext "subjectAltName=IP:${SERVER_IP},DNS:localhost,DNS:<HOST>" \
		2>/dev/null
	chmod 600 "$CERT_DIR/privkey.pem"
	echo -e "${GREEN:-}OK: SSL certificates generated${NC:-}"
else
	echo ""
	echo -e "${GREEN:-}OK: SSL certificates exist${NC:-}"
fi

# Ensure nginx html directory exists (for error pages)
mkdir -p "$SCRIPT_DIR/nginx/html"

# -------------------------------------------------------------------------
# Start containers
# -------------------------------------------------------------------------
echo ""

# Check if webhook-dispatcher is already running (from 0_TILES or elsewhere)
if docker ps --format '{{.Names}}' | grep -q '^webhook-dispatcher$'; then
	echo -e "${CYAN:-}INFO: webhook-dispatcher already running (from another project). Skipping.${NC:-}"
	echo "Starting 0_SMART_HOME services only (smart-home, postgres, postgrest, nginx)..."
	docker compose up -d --no-deps smart-home postgres postgrest nginx
else
	echo "Starting all containers (including webhook-dispatcher)..."
	docker compose up -d
fi

# Wait for containers to start
echo ""
echo "Waiting for containers to start..."
sleep 5

# Check container status
if docker ps --format '{{.Names}}' | grep -q '^smarthome-app$'; then
	echo -e "${GREEN:-}OK: Containers are running!${NC:-}"
	echo ""
	echo "Access 0_SMART_HOME UI at:"
	echo "  - https://${SERVER_IP}:${NGINX_HTTPS_PORT}/"
	echo "  - http://${SERVER_IP}:${APP_EXTERNAL_PORT}/  (direct, no nginx)"
	echo "  - https://${SERVER_IP}:${NGINX_HTTPS_PORT}/docs  (OpenAPI docs)"
	echo ""
	echo "Webhook dispatcher:"
	echo "  - http://${SERVER_IP}:${WEBHOOK_PORT}/api/webhook/event"
	echo ""
	echo "Useful commands:"
	echo "  View logs:        docker compose logs -f smart-home"
	echo "  Dispatcher logs:  docker compose logs -f webhook-dispatcher"
	echo "  Stop containers:  ./stop.sh"
	echo "  Rebuild:          ./deploy.sh"
	echo ""

	# Health checks (direct + nginx HTTPS)
	sleep 5
	if curl -s "http://localhost:${APP_EXTERNAL_PORT}/api/health" >/dev/null 2>&1; then
		echo -e "${GREEN:-}OK: Health check passed (direct)${NC:-}"
	else
		echo -e "${YELLOW:-}WARNING: Health check pending (direct) - app may still be starting${NC:-}"
	fi
	if curl -sk "https://localhost:${NGINX_HTTPS_PORT}/api/health" >/dev/null 2>&1; then
		echo -e "${GREEN:-}OK: Health check passed (nginx HTTPS)${NC:-}"
	else
		echo -e "${YELLOW:-}WARNING: Health check pending (nginx HTTPS) - may still be starting${NC:-}"
	fi
else
	echo -e "${RED:-}ERROR: Container failed to start${NC:-}"
	echo "Check logs with: docker compose logs"
	exit 1
fi
