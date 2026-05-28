#!/bin/bash
# ╔══════════════════════════════════════════════════════════════════════════════════════╗
# ║  start_utils.sh                                                                      ║
# ║                                                                                      ║
# ║  Self-contained startup helpers sourced by start.sh and deploy.sh. Provides the      ║
# ║  AWS-Secrets-Manager pull (or .env-only fallback) plus minimal color fallbacks,      ║
# ║  with zero dependency on any host-side shell config.                                 ║
# ║                                                                                      ║
# ║     ┌──────────────────────────────┐                                                 ║
# ║     │ pull_aws_secrets NAME ...    │                                                 ║
# ║     └──────────────┬───────────────┘                                                 ║
# ║                    │                                                                 ║
# ║                    ▼                                                                 ║
# ║            ┌───────────────┐                                                         ║
# ║            │ AWS enabled?  │  no   ┌───────────────────────────┐                     ║
# ║            │  (.env flag)  │──────▶│ load .env, return         │                     ║
# ║            └───────┬───────┘       └───────────────────────────┘                     ║
# ║                    │ yes                                                             ║
# ║                    ▼                                                                 ║
# ║            ┌───────────────┐                                                         ║
# ║            │ resolve AWS   │   env / .env / interactive prompt                       ║
# ║            │ profile name  │   (prompt persists answer to .env)                      ║
# ║            └───────┬───────┘                                                         ║
# ║                    ▼                                                                 ║
# ║            ┌───────────────┐                                                         ║
# ║            │ AWS auth      │   sts get-caller-identity, sso login if needed          ║
# ║            └───────┬───────┘                                                         ║
# ║                    ▼                                                                 ║
# ║            ┌───────────────┐                                                         ║
# ║            │ fetch secret  │   aws secretsmanager get-secret-value | jq              ║
# ║            │ export keys   │   each KEY=VALUE → exported into the env                ║
# ║            └───────────────┘                                                         ║
# ║                                                                                      ║
# ║  CONTROL:                                                                            ║
# ║    SMARTHOME_USE_AWS_SECRETS  true  → pull from AWS Secrets Manager (default)        ║
# ║                                false → .env-only mode (skip AWS entirely)            ║
# ║    SMARTHOME_AWS_PROFILE      AWS CLI profile holding the secrets (prompt if unset)  ║
# ║    SMARTHOME_ENV_FILE         Override path to the .env file (default: alongside)    ║
# ║                                                                                      ║
# ║  EXPORTS:  pull_aws_secrets NAME [NAME ...]                                          ║
# ║                                                                                      ║
# ║  NOTE: This file deliberately does NOT use the canonical source_global_env           ║
# ║        bootstrap. Its purpose is to make start.sh / deploy.sh portable across        ║
# ║        machines that do not have a personal shell config, so it must remain          ║
# ║        self-contained. (The canonical S.2.1 rule is documented-exception here.)      ║
# ╚══════════════════════════════════════════════════════════════════════════════════════╝

# Idempotent source guard — re-sourcing is a no-op.
[ -n "${_SMARTHOME_START_UTILS_SOURCED:-}" ] && return 0 2>/dev/null || true
_SMARTHOME_START_UTILS_SOURCED=1

########################################################################-########################################################################
#                                                                  VARIABLES                                                                     #
########################################################################-########################################################################
START_UTILS__DIR="$(builtin cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"                                                             # location of this file
:                                                                                                                                                #
# Color fallbacks: leave existing definitions intact, otherwise provide ANSI defaults                                                            #
# so all messaging below renders without an external color file.                                                                                 #
: "${RED:=$'\033[0;31m'}"                                                                                                                        #
: "${GREEN:=$'\033[0;32m'}"                                                                                                                      #
: "${YELLOW:=$'\033[1;33m'}"                                                                                                                     #
: "${CYAN:=$'\033[0;36m'}"                                                                                                                       #
: "${NC:=$'\033[0m'}"                                                                                                                            #
########################################################################-########################################################################

########################################################################-########################################################################
#                                                                ENVIRONMENT                                                                     #
########################################################################-########################################################################
start_utils__envfile() {
	# Resolve the path of the project .env this lib should read.
	# Honors SMARTHOME_ENV_FILE; defaults to the .env next to this script.
	printf '%s' "${SMARTHOME_ENV_FILE:-${START_UTILS__DIR}/.env}"
}

start_utils__dotenv_get() {
	# Read a single KEY from the project .env without sourcing the whole file.
	# Strips inline #comments, surrounding quotes, and whitespace. Prints the
	# value, or nothing if the key is absent or blank.
	local key="$1" envf; envf="$(start_utils__envfile)"
	[ -f "$envf" ] || return 0
	awk -F= -v k="$key" '
		$0 ~ "^[[:space:]]*"k"[[:space:]]*=" {
			v=$2; sub(/#.*/,"",v); gsub(/[[:space:]"'\'']/,"",v)
			if(v!="") print v
		}' "$envf" | tail -1
}

start_utils__use_aws() {
	# Return 0 if AWS Secrets Manager should be the source of truth, 1 if .env-only.
	# Controlled by SMARTHOME_USE_AWS_SECRETS (env or .env). Default true.
	local v="${SMARTHOME_USE_AWS_SECRETS:-$(start_utils__dotenv_get SMARTHOME_USE_AWS_SECRETS)}"
	case "$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]')" in
		false | 0 | no | off) return 1 ;;
		*)                    return 0 ;;
	esac
}

start_utils__load_dotenv() {
	# Source every KEY=VALUE from the project .env into the environment, exported.
	# Used in .env-only mode to deliver config without AWS. Idempotent across calls.
	local envf; envf="$(start_utils__envfile)"
	if [ ! -f "$envf" ]; then
		echo -e "${YELLOW}⚠ .env-only mode but no ${envf} found${NC}" >&2
		return 1
	fi
	[ -n "${_START_UTILS_DOTENV_LOADED:-}" ] && return 0
	set -a; . "$envf"; set +a
	_START_UTILS_DOTENV_LOADED=1
	echo -e "${GREEN}✓ Loaded config from ${envf} (.env-only mode, AWS skipped)${NC}"
}

start_utils__resolve_profile() {
	# Resolve the AWS CLI profile name in precedence order:
	#   1. SMARTHOME_AWS_PROFILE / AWS_PROFILE already exported
	#   2. SMARTHOME_AWS_PROFILE in the project .env
	# Prints the resolved name, or nothing if unset anywhere.
	[ -n "${SMARTHOME_AWS_PROFILE:-}" ] && { printf '%s' "$SMARTHOME_AWS_PROFILE"; return 0; }
	[ -n "${AWS_PROFILE:-}" ]           && { printf '%s' "$AWS_PROFILE";           return 0; }
	start_utils__dotenv_get SMARTHOME_AWS_PROFILE
}

start_utils__prompt_profile() {
	# Interactively ask for an AWS profile name when none is configured, then
	# persist the answer to .env so subsequent (including non-interactive) runs
	# read it without prompting. Fails fast with instructions when no TTY is
	# attached (e.g. systemd unit), rather than hanging on read.
	local envf; envf="$(start_utils__envfile)"
	if [ ! -t 0 ]; then
		echo -e "${RED}SMARTHOME_AWS_PROFILE is not set and no terminal is attached.${NC}" >&2
		echo -e "${RED}Set it once:  echo 'SMARTHOME_AWS_PROFILE=<name>' >> ${envf}${NC}" >&2
		return 1
	fi
	echo -e "${YELLOW}AWS profile not configured (SMARTHOME_AWS_PROFILE).${NC}" >&2
	local avail; avail="$(aws configure list-profiles 2>/dev/null | paste -sd', ' -)"
	[ -n "$avail" ] && echo -e "  Available AWS profiles: ${CYAN}${avail}${NC}" >&2
	local p=""
	read -r -p "Enter the AWS profile to pull SMART_HOME secrets with: " p
	[ -z "$p" ] && { echo -e "${RED}No profile entered — aborting.${NC}" >&2; return 1; }
	touch "$envf"
	if grep -qE '^[[:space:]]*SMARTHOME_AWS_PROFILE=' "$envf"; then
		sed -i "s|^[[:space:]]*SMARTHOME_AWS_PROFILE=.*|SMARTHOME_AWS_PROFILE=${p}|" "$envf"
	else
		printf 'SMARTHOME_AWS_PROFILE=%s\n' "$p" >> "$envf"
	fi
	echo -e "${GREEN}Saved SMARTHOME_AWS_PROFILE=${p} to ${envf}${NC}" >&2
	printf '%s' "$p"
}
########################################################################-########################################################################

########################################################################-########################################################################
#                                                              AUTHENTICATION                                                                    #
########################################################################-########################################################################
start_utils__ensure_jq() {
	# Ensure jq is installed (required to parse Secrets Manager JSON payloads).
	# Attempts an apt install on Debian-family hosts.
	command -v jq &>/dev/null && return 0
	echo -e "${YELLOW}Installing jq (required to parse AWS secrets)...${NC}"
	sudo apt-get update -qq && sudo apt-get install -y jq
}

start_utils__aws_auth() {
	# Verify the given AWS profile has a usable session. On failure, attempt
	# an `aws sso login` when the profile is SSO-backed (interactive). Returns
	# 0 on a successful sts get-caller-identity, 1 otherwise.
	local profile="$1"
	if aws sts get-caller-identity --profile "$profile" &>/dev/null; then
		return 0
	fi
	if aws configure get sso_session  --profile "$profile" &>/dev/null \
		|| aws configure get sso_start_url --profile "$profile" &>/dev/null; then
		echo -e "${CYAN}AWS session for '${profile}' is invalid — running 'aws sso login'...${NC}"
		aws sso login --profile "$profile" >/dev/null 2>&1 || true
		aws sts get-caller-identity --profile "$profile" &>/dev/null && return 0
	fi
	return 1
}
########################################################################-########################################################################

########################################################################-########################################################################
#                                                                SECRETS API                                                                     #
########################################################################-########################################################################
pull_aws_secrets() {
	# Public entry point. Fetch one or more AWS Secrets Manager secrets by name
	# and export every JSON key/value pair into the calling shell's environment.
	#
	# Usage: pull_aws_secrets NAME [NAME ...]
	#
	# In .env-only mode (SMARTHOME_USE_AWS_SECRETS=false) this short-circuits to
	# sourcing the project .env — no AWS call, no profile prompt — so the same
	# call site works in both deployment modes without conditionals upstream.
	#
	# A bare trailing digit in args is accepted for back-compat with older
	# call sites (it is ignored; profile is resolved from .env).
	if [ "$#" -eq 0 ] || [ "$1" = "--help" ]; then
		echo "Usage: pull_aws_secrets NAME [NAME ...]"
		echo "  Exports every key of each AWS Secrets Manager secret into the env."
		echo "  Profile is read from SMARTHOME_AWS_PROFILE (env / .env); prompts if unset."
		echo "  SMARTHOME_USE_AWS_SECRETS=false routes to .env-only mode (no AWS)."
		return 0
	fi

	if ! start_utils__use_aws; then
		start_utils__load_dotenv
		return $?
	fi

	local secret_names=() arg
	for arg in "$@"; do
		case "$arg" in
			[0-9]) : ;;                       # legacy positional profile digit — ignored
			*)     secret_names+=("$arg") ;;
		esac
	done
	if [ "${#secret_names[@]}" -eq 0 ]; then
		echo -e "${RED}pull_aws_secrets: no secret name given${NC}" >&2
		return 1
	fi

	local profile; profile="$(start_utils__resolve_profile)"
	if [ -z "$profile" ]; then
		profile="$(start_utils__prompt_profile)" || return 1
	fi
	export AWS_PROFILE="$profile"

	start_utils__ensure_jq
	if ! start_utils__aws_auth "$profile"; then
		echo -e "${RED}pull_aws_secrets: AWS auth failed for profile '${profile}'${NC}" >&2
		return 1
	fi

	local name json rc=0
	for name in "${secret_names[@]}"; do
		echo "Querying AWS secret: ${name} (profile ${profile})..."
		json="$(aws secretsmanager get-secret-value \
			--profile "$profile" --secret-id "$name" \
			--query SecretString --output text 2>/dev/null)"
		if [ -z "$json" ]; then
			echo -e "${YELLOW}⚠ secret '${name}' returned nothing (missing or no access)${NC}" >&2
			rc=1
			continue
		fi
		local k v
		while IFS='=' read -r k v; do
			[ -n "$k" ] && export "$k=$v"
		done < <(printf '%s' "$json" | jq -r 'to_entries[] | "\(.key)=\(.value)"')
	done
	return $rc
}
########################################################################-########################################################################
