#!/bin/bash

# script name: logger.sh | hard linked to ~/0_SCRIPTS/logger.sh

set -a

ARGS=("$@")
NO_EXEC=false
FORCE=false

if [[ "${ARGS[*]}" == *--no-exec* ]]; then
	NO_EXEC=true
	ARGS=("${ARGS[@]/--no-exec/}")
fi
if [[ "${ARGS[*]}" == *--force* ]]; then
	FORCE=true
	ARGS=("${ARGS[@]/--force/}")
fi

# prevent circular sourcing (no exception)
if ! $FORCE && [[ -n "$LOGGER_SCRIPT_ALREADY_SOURCED" ]]; then
	echo -e "${BG_RED}${BLACK}WARNING: logger.sh is already sourced. Skipping re-source to prevent circular sourcing issues. If you are seeing this message, it means a script is trying to source logger.sh multiple times, which can lead to unexpected behavior. Please check your scripts for multiple sourcing of logger.sh and remove any redundant sources.${NC}"
	return 0
fi

if ! $NO_EXEC; then
	# restore fds if descriptors exist
	if [[ -e /dev/fd/3 ]] && [[ -e /dev/fd/4 ]]; then
		echo "Restoring original stdout and stderr..."
		exec 1>&3 2>&4
		# Close saved file descriptors
		exec 3>&- 4>&-
	fi
	# sourcer.sh should already be sourced. Either by .bashrc (TMC) or by ohvd_install.sh (Field PC Installation)
	if ! declare -f source_env >/dev/null; then
		. sourcer.sh --no-exec
	fi

	declare -a file_names_to_source=(
		.env
		.env.colors
		install_utils.sh --no-exec
		install_variables.sh --no-exec
	)
	if [[ "${BASH_SOURCE[1]}" == *sourcer.sh ]]; then
		echo -e "skipping source_env call because ${BASH_SOURCE[0]} is being sourced by ${BASH_SOURCE[1]}" | tee -a "${HOME}/log.log"
	else
		set -o allexport
		source_env "${file_names_to_source[@]}"
		set +o allexport
	fi
	if declare -f ensure_directories_and_files >/dev/null; then
		ensure_directories_and_files || exit 1
	fi
fi

############################-############################
#                      	 LOGGERS
############################-############################
log() {
	############################-#################################
	#        NEVER CALL ANY UTILITY FROM THIS FUNCTION!
	#     or ensure it doesn't call this function itself
	############################-#################################
	# Function parameters

	# local color="$1" # First argument - either color code or message
	# local message="" # Initialize as empty, will be set properly based on arguments
	local args=("$@")
	local message
	local timestamp
	local stripped_message
	local prefix
	local source_basename
	local source_function
	local logger_basename
	local LOGGER_NAME

	if is_display "${args[*]}"; then
		if display_line "${args[*]}"; then
			return 0
		fi
	fi

	export LOG_FILE="$HOME/log.log"

	source_basename=$(basename "${BASH_SOURCE[1]}")

	source_function="${FUNCNAME[1]}"

	logger_basename=$(basename "${BASH_SOURCE[0]}")

	if declare -f strip_name >/dev/null; then
		LOGGER_NAME="$(strip_name "${BASH_SOURCE[0]}")"
	else
		LOGGER_NAME="${BASH_SOURCE[0]}"
	fi
	LOGGER_NAME="${LOGGER_NAME^^}"

	timestamp=$(date +%D" "%H:%M:%S)

	prefix=" LOG | ${timestamp} | ${source_basename} | ${source_function}"

	message="${CYAN}[$prefix]${NC}: ${args[*]} ${NC}" # add color codes for better visualization in debug mode.
	message_term_debug_mode="${message}"
	message_term_oneliner="${args[*]}"

	# echo "message to strip: $message"
	stripped_message_for_log_file="$(strip_ansi "${message}")" # remove any ANSI color code passed into args.

	# non-debug mode is super messy for now. Keep it this way.
	# for some reason formatting and colors are all messed up
	DEBUG=1

	# Debug mode handling
	if [[ "${DEBUG}" -eq 1 ]]; then
		# Full debug output with timestamps, colors, etc. to terminal
		echo -e "${message}"
	else
		# single line output in non-debug mode
		# stripped from any ANSI codes for faster readability.
		echo -en "\r\033[K ${message_term_oneliner} $NC"
	fi

	error_output=$(write_to_logs "$stripped_message_for_log_file") || {
		echo -e "\033[31;5;1m write_to_logs failed. error_output: $error_output \033[0m"
	}

	# tee naturally prints in both fd and file.
	# Ensure file is written & silence its tty output.
	# echo "${stripped_message_for_log_file}" | tee -a "${LOG_FILE}" >/dev/null

	return 0
}
simple_logger() {
	if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
		printf "%b\n" "${BOLD:-}simple_logger${NC:-} — structured log output to terminal and log files"
		printf "\n"
		printf "%b\n" "${BOLD:-}USAGE:${NC:-}"
		printf "  simple_logger [ERROR|WARNING|INFO] [MINIMAL] \"message\" [\"extra\"...]\n"
		printf "\n"
		printf "%b\n" "${BOLD:-}LEVEL FLAGS (optional, combinable):${NC:-}"
		printf "  ${GREEN:-}ERROR${NC:-}    Prepends \$ERROR emoji, always prints regardless of SIMPLE_LOGGER_DEBUG\n"
		printf "  ${GREEN:-}WARNING${NC:-}  Prepends \$WARNING emoji, always prints\n"
		printf "  ${GREEN:-}INFO${NC:-}     Prepends \$INFO emoji, always prints\n"
		printf "  (none)   Only prints if SIMPLE_LOGGER_DEBUG=true\n"
		printf "\n"
		printf "%b\n" "${BOLD:-}FORMAT FLAGS (optional):${NC:-}"
		printf "  ${GREEN:-}MINIMAL${NC:-}  Short format — message only, no timestamp or caller prefix\n"
		printf "  (none)   Full format — ${CYAN:-}[date][script | func]${NC:-} prefix prepended\n"
		printf "\n"
		printf "%b\n" "${BOLD:-}OUTPUT:${NC:-}"
		printf "  Logs to both \${CRON_LOGS} and \${LOG_FILE} via tee -a\n"
		printf "  Requires: \$LOG_FILE, \$CRON_LOGS (from .env.colors or defaults)\n"
		printf "\n"
		printf "%b\n" "${BOLD:-}EXAMPLES:${NC:-}"
		printf "  simple_logger INFO MINIMAL \"\${GREEN}Task complete\${NC}\"\n"
		printf "  simple_logger ERROR \"\${RED}rsync failed: exit \$exit_code\${NC}\"\n"
		printf "  simple_logger WARNING \"Retrying connection...\"\n"
		printf "  simple_logger \"DEBUG-only message\"  # only prints if SIMPLE_LOGGER_DEBUG=true\n"
		printf "\n"
		printf "%b\n" "${BOLD:-}ENVIRONMENT:${NC:-}"
		printf "  ${CYAN:-}SIMPLE_LOGGER_DEBUG${NC:-}  Set to ${CYAN:-}true${NC:-} to print bare messages (default: false)\n"
		return 0
	fi

	local ARGS=("$@")
	local new_args=()
	local _ERROR=false
	local _WARNING=false
	local _INFO=false
	local _MINIMAL=false
	: ${SIMPLE_LOGGER_DEBUG:=false}

	if [[ "${ARGS[*]}" == *ERROR* ]]; then
		_ERROR=true
		ARGS=("${ARGS[@]/ERROR/}") # Remove ERROR from ARGS to prevent it from printed in the message
	fi
	if [[ "${ARGS[*]}" == *WARNING* ]]; then
		_WARNING=true
		ARGS=("${ARGS[@]/WARNING/}") # Remove WARNING from ARGS to prevent it from printed in the message
	fi
	if [[ "${ARGS[*]}" == *INFO* ]]; then
		_INFO=true
		ARGS=("${ARGS[@]/INFO/}") # Remove INFO from ARGS to prevent it from being printed in the message
	fi

	if [[ "${ARGS[*]}" == *MINIMAL* ]]; then
		_MINIMAL=true
		ARGS=("${ARGS[@]/MINIMAL/}") # Remove MINIMAL from ARGS to prevent it from being printed in the message
	fi

	if $_ERROR || $_WARNING || $_INFO || $_MINIMAL || $SIMPLE_LOGGER_DEBUG; then
		if $_ERROR; then
			new_args+=("$ERROR")
		fi
		if $_WARNING; then
			new_args+=("$WARNING")
		fi
		if $_INFO; then
			new_args+=("$INFO")
		fi
		new_args+=("${ARGS[@]}")
		{
			if $_MINIMAL; then
				echo -e "${new_args[@]}${NC}"
			else
				echo -e "[$(date)][$(basename "${BASH_SOURCE[1]:-'terminal'}") | ${FUNCNAME[1]}] ${new_args[@]} ${NC}"
				echo ""
			fi
		} | tee -a "${CRON_LOGS}" | tee -a "${LOG_FILE}"
	fi
}
write_to_logs() {

	local msg=("$@")
	echo "${msg[*]}" | tee -a "${LOG_FILE}" >/dev/null
	local exit_code=$?
	if [[ "$exit_code" -ne 0 ]]; then
		log_error "Failed to write to log file: ${LOG_FILE}${NC}"
		log_error "${NC}Origin: ${ACCENT_YELLOW} ${BASH_SOURCE[1]} in ${FUNCNAME[1]}"
		return 1
	fi
	return 0
}

############################-############################
#                      	 HELPERS
############################-############################
cleanup_logging() {
	# Cleanup function to restore original file descriptors
	# Only attempt cleanup if descriptors exist
	if [[ -e /dev/fd/3 ]]; then
		# Restore original stdout
		exec 1>&3
		# Close saved file descriptor 3
		exec 3>&-
	fi
	if [[ -e /dev/fd/4 ]]; then
		# Restore original stderr
		exec 2>&4
		# Close saved file descriptor 4
		exec 4>&-
	fi
}
strip_ansi() {
	local input="$1"
	local output
	output=$(echo "$input" | sed -E "${ANSI_STRIP_SED_COMMAND}" | sed -E 's/^\\(\s*?)//' | sed -E 's/^\\//' | sed -E 's/\](\s*?)\\/]/')
	if [[ "$output" == "" ]]; then
		echo "$input"
	else
		echo "$output"
	fi

}
repeat_print() {
	# Usage: repeat_print <char> [iterations] [color_code]
	local content=${1:-}
	local iterations=${2:-$(tput cols)}
	local color_code=${3:-}
	local reset_code="\033[0m"

	[[ -t 1 ]] || return 0 # must only run in interactive mode.

	for i in $(seq 1 "$iterations"); do
		printf "%b%s%b" "$color_code" "$content" "$reset_code"
	done
}

############################-############################
#                  COSMETIC DISPLAYS
############################-############################
is_display() {
	local args=("$@")
	local pattern1="#{3,}"
	local pattern2="\*{3,}"
	local pattern3="\s{6,}"

	[[ -t 1 ]] || return 0 # must only run in interactive mode.
	if echo "${args[*]}" | grep -q "$TO_TTY"; then
		return 0
	fi
	if [[ "${args[*]}" =~ $pattern1 ]]; then
		return 0
	fi
	if [[ "${args[*]}" =~ $pattern2 ]]; then
		return 0
	fi
	if [[ "${args[*]}" =~ $pattern3 ]]; then
		return 0
	fi
	return 1
}
display_line() {
	local args=("$@")
	# args=${args[@]/${TO_TTY}//}
	# args=${args[@]////}
	optimized_block "${args[@]}"
}
display_block() {
	optimized_block "$@"
}
optimized_block() {
	: "
        Wrapper function that safely executes optimized_block_exec without interfering with stdin.

        Problem Statement:
        The optimized_block_exec function uses a while-read loop with a here-string (<<<) to process content.
        This redirection temporarily hijacks stdin from the parent script. Additionally, the function calls
        Python subprocesses which can inherit and potentially corrupt the stdin file descriptor.
        
        When executed inline, this causes subsequent read commands in the parent script to hang or fail,
        because stdin is left in an inconsistent state after the function completes.

        Solution:
        By running optimized_block_exec as a background subprocess with:
        1. Output redirected to /dev/tty (bypassing stdout/stderr inheritance)
        2. Disowning the process to prevent signal propagation
        3. Waiting for completion before continuing
        
        We ensure that any stdin manipulation happens in an isolated process space that cannot
        affect the parent script's stdin. This allows normal read operations to work immediately
        after this function returns.

        A fixed 4-second sleep ensures terminal rendering completes before continuing.
        This delay is necessary because terminal rendering is asynchronous and continues
        after the process exits.

        Arguments:
            '\$@' - All arguments are passed through to optimized_block_exec
        
        Returns:
            Exit status of the background process
    "
	[[ -t 1 ]] || return 0 # must only run in interactive mode.

	printf '\n\n'

	local block_pid
	set +m
	{
		optimized_block_exec "$@" >/dev/tty </dev/null &
	} 2>/dev/null # hide the [1] PID print which goes to stderr.
	block_pid=$!
	disown "$block_pid" 2>/dev/null
	set -m

	# Wait for terminal to finish rendering
	# can't use 'wait' on disowned pid.
	# wait on a disowned PID can fail with wait: 'pid NNN is not a child of this shell'
	while kill -0 "$block_pid" &>/dev/null; do
		sleep 0.001
	done
}
optimized_block_exec() {
	: "
        Renders content in a visually appealing box with proper width calculations and Unicode border characters.

        This function creates a terminal UI element that:
        1. Calculates the display width of text (accounting for Unicode/wide characters)
        2. Centers content within a box with proper padding
        3. Handles ANSI color codes correctly when calculating widths

        WARNING - Stdin Interference:
        This function uses 'while IFS= read -r line; do ... done <<< \"\${content}\"' which temporarily
        redirects stdin to read from the here-string. The function also invokes Python subprocesses
        via get_display_width() which can inherit and modify stdin state.

        These operations can leave stdin in an undefined state, causing subsequent read commands
        in the calling script to hang or behave unexpectedly. This is why this function should
        ALWAYS be called through the optimized_block() wrapper, never directly.

        Technical Details:
        - Uses Python's wcwidth module for accurate character width calculations
        - Falls back to simple character counting if Python/wcwidth unavailable
        - Strips ANSI escape sequences before calculating display widths
        - Handles empty lines and ensures proper box formatting

        Arguments:
            $1 - The content to display
            $2 - Explicit text/foreground color (optional, extracted from content or DEFAULT_TEXT_COLOR)
            $3 - Explicit background color (optional, extracted from content or DEFAULT_BG_COLOR)
            $4 - Maximum width (default: 300, auto-adjusts to terminal width)
        
        Side Effects:
            - Temporarily redirects stdin
            - Spawns Python subprocesses
            - Writes directly to terminal
    "
	[[ -t 1 ]] || return 0           # must only run in interactive mode.
	local content="$1"               #
	local explicit_fg="${2:-}"       #
	local explicit_bg="${3:-}"       #
	local width=${4:-300}            #
	local reset="${NC:-\033[0m}"     #
	:                                #
	:                                # Determine fg: explicit $2 > extracted from content > default
	local fg                         #
	if [[ -n "$explicit_fg" ]]; then #
		fg="$explicit_fg"               #
	else                             #
		local extracted_fg              #
		extracted_fg=$(printf '%b' "$content" | grep -oP '\x1b\[38;5;[0-9]+m' | head -1)
		if [[ -n "$extracted_fg" ]]; then #
			fg="$extracted_fg"               #
		else                              #
			fg="${DEFAULT_TEXT_COLOR:-}"     #
		fi                                #
	fi                                 #
	:                                  #
	:                                  # Determine bg: explicit $3 > extracted from content > default
	local bg                           #
	if [[ -n "$explicit_bg" ]]; then   #
		bg="$explicit_bg"                 #
	else                               #
		local extracted_bg                #
		extracted_bg=$(printf '%b' "$content" | grep -oP '\x1b\[48;5;[0-9]+m' | head -1)
		if [[ -n "$extracted_bg" ]]; then #
			bg="$extracted_bg"               #
		else                              #
			bg="${DEFAULT_BG_COLOR:-}"       #
		fi                                #
	fi                                 #

	get_display_width() {
		local str="$1"
		if command -v python3 >/dev/null && python3 -c "import wcwidth" 2>/dev/null; then
			# Explicitly close stdin for the Python process
			python3 -c "import sys; from wcwidth import wcswidth; print(wcswidth(sys.argv[1]))" "$str" </dev/null
		else
			echo "${#str}" # Fallback: naive char count
		fi
	}
	:                                                          # Get terminal width (fallback if tput fails)
	local term_width                                           #
	term_width=$(tput cols 2>/dev/null || echo "$width")       #
	[[ $width -gt $term_width ]] && width=$term_width          #
	:                                                          #
	:                                                          # Borders and padding lines
	local h_border=$(printf '═%.0s' $(seq 1 $((width - 2))))   #
	local top_border="╔${h_border}╗"                           #
	local bottom_border="╚${h_border}╝"                        #
	local inner_fill=$(printf ' %.0s' $(seq 1 $((width - 2)))) #
	local empty_line="║${bg}${inner_fill}${reset}║"            #
	:                                                          #
	strip_ansi() {                                             # Strip ANSI escapes
		sed -E 's/\x1B\[[0-9;]*[mK]//g'                           #
	}                                                          #
	:                                                          #
	printf "%b\n" "$top_border"                                # Render top
	printf "%b\n" "$empty_line"                                # Top margin (bg-filled)
	:                                                          #
	while IFS= read -r line; do                                # Line-by-line rendering
		if [[ -z "$line" ]]; then
			printf "%b\n" "$empty_line"
			continue
		fi

		local clean_line
		clean_line=$(echo -e "$line" | strip_ansi)
		local visible_len
		visible_len=$(get_display_width "$clean_line")
		local max_content=$((width - 4))

		# ANSI-aware word-wrap: preserves color codes across wrapped lines
		local wrapped
		if [[ $visible_len -le $max_content ]]; then
			wrapped="$line"
		else
			# ANSI-aware word-wrap delegated to external helper (whitespace-safe, linter-friendly)
			local -a python_wrap=(python3 "$HOME/0_SCRIPTS/0_UTILITIES_AND_HELPERS/python_helpers/ansi_wrap.py" "$(printf '%b' "$line")" "$max_content")
			wrapped=$("${python_wrap[@]}" </dev/null)
		fi

		while IFS= read -r wrap_line; do
			local wrap_clean
			wrap_clean=$(printf '%b' "$wrap_line" | strip_ansi)
			local wrap_vis
			wrap_vis=$(get_display_width "$wrap_clean")
			local total_padding=$((width - wrap_vis - 4))
			[[ $total_padding -lt 0 ]] && total_padding=0
			local left_padding=$((total_padding / 2))
			local right_padding=$((total_padding - left_padding))

			printf "%b" "║${bg} "
			printf "%${left_padding}s" ""
			printf "%b" "${fg}${wrap_line}"
			printf "%b" "${bg}"
			printf "%${right_padding}s" ""
			printf "%b\n" " ${reset}║"
		done <<<"$wrapped"
	done <<<"${content}" #$'\n'

	printf "%b\n" "$empty_line"
	printf "%b\n" "$bottom_border"

}
print_title() {

	[[ -t 1 ]] || return 0 # must only run in interactive mode.

	local bg_color="${1:-}" # Optional background color
	local fg_color="${2:-}" # Optional foreground color
	local message="${3:-}"  # The message to display
	local term_width

	term_width=$(tput cols) # Terminal width for formatting

	# Skip empty messages
	if [[ -z "$message" ]]; then
		return 0
	fi

	# Step 1: Strip ANSI to calculate visible message length
	stripped_message=$(strip_ansi "$message")
	message_length=${#stripped_message}

	# Step 2: Calculate padding
	total_padding=$((term_width - message_length - 2))
	left_padding=$((total_padding / 2))
	right_space=$((term_width - left_padding - message_length - 2))

	# Step 3: Print content line with proper color logic
	printf "%s" "║" # Left border

	# Apply background color if provided
	if [[ -n "$bg_color" ]]; then
		printf "%b" "$bg_color"
	fi

	printf "%*s" "$left_padding" "" # Left padding

	# Apply foreground color if provided
	if [[ -n "$fg_color" ]]; then
		printf "%b" "$fg_color"
	fi

	printf "%s" "$stripped_message" # Message

	# Reset foreground to background if both colors provided
	if [[ -n "$fg_color" && -n "$bg_color" ]]; then
		printf "%b" "$bg_color"
	fi

	printf "%*s" "$right_space" "" # Right padding

	# Reset all colors
	printf "%b" "${NC}"

	printf "%s\n" "║" # Right border

	return 0
}
get_rainbow_color() {
	[[ -t 1 ]] || return 0 # must only run in interactive mode.
	# to modify to return next
	for ((r = 0; r < 255; r += 4)); do
		printf "\033[48;2;${r};0;$((255 - r))m "
	done
	printf "\033[0m\n"
}

############################-############################
# 				    GLOWING PROGRESS BAR
############################-############################
glow_print() {
	# Repeats a single sign/arrow with wave-like multicolor animation.
	# Runs as background process — call stop_glow to terminate.
	# Usage: glow_print [sign] [length_pct] [prefix] [suffix] [delay]
	#   sign       : char to repeat (default: →). Direction inferred from sign.
	#                1-char: → ← > < -   repeats at full count
	#                2-char: -> <- => <= << >>   repeats at count/2
	#                3+ char: keeps only first char
	#   length_pct : repeated pattern width as % of terminal cols (default: 25)
	#   prefix     : static text printed before the glow animation
	#   suffix     : static text printed after the glow animation
	#   delay      : seconds between frames (default: 0.04)

	[[ -t 1 ]] || return 0 # must only run in interactive mode.

	if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
		printf "%b\n" "${BOLD:-}glow_print${NC:-} — animated wave-color repeater"
		printf "\n"
		printf "%b\n" "${BOLD:-}USAGE:${NC:-}"
		printf "  glow_print [sign] [length_pct] [prefix] [suffix] [delay]\n"
		printf "\n"
		printf "%b\n" "${BOLD:-}ARGUMENTS:${NC:-}"
		printf "  ${GREEN:-}sign${NC:-}        Char to repeat (default: →). Direction inferred from sign.\n"
		printf "              1-char:  ${CYAN:-}→ ← > < -${NC:-}     repeats at full count\n"
		printf "              2-char:  ${CYAN:-}-> <- => <= << >>${NC:-}  repeats at count/2\n"
		printf "              3+ char: keeps only first char\n"
		printf "  ${GREEN:-}length_pct${NC:-}  Width as %% of terminal cols (default: ${CYAN:-}25${NC:-})\n"
		printf "  ${GREEN:-}prefix${NC:-}      Static text before the animation (optional)\n"
		printf "  ${GREEN:-}suffix${NC:-}      Static text after the animation (optional)\n"
		printf "  ${GREEN:-}delay${NC:-}       Seconds between frames (default: ${CYAN:-}0.04${NC:-})\n"
		printf "\n"
		printf "%b\n" "${BOLD:-}CONTROL:${NC:-}"
		printf "  ${CYAN:-}stop_glow${NC:-}             Kill all glows, clear line\n"
		printf "  ${CYAN:-}stop_glow --no-clear${NC:-}  Kill all glows, keep output\n"
		printf "\n"
		printf "%b\n" "${BOLD:-}EXAMPLES:${NC:-}"
		printf "  glow_print \"→\"                              # default\n"
		printf "  glow_print \"->\" 30 \"syncing \" \" done\"       # with prefix/suffix\n"
		printf "  glow_print \"←\" 50 \"\" \"\" 0.02               # fast, 50%% width\n"
		return 0
	fi

	# Pre-parse named args; remaining positionals are left for the assignments below
	local row=""
	local -a _gp_positionals=()
	for _gp_arg in "$@"; do
		case "$_gp_arg" in
		--row=*) row="${_gp_arg#--row=}" ;;
		--*) : ;; # unknown named args — ignore
		*) _gp_positionals+=("$_gp_arg") ;;
		esac
	done
	set -- "${_gp_positionals[@]+"${_gp_positionals[@]}"}"

	local sign="${1:-→}"
	local length_pct="${2:-25}"
	local prefix="${3:-}"
	local suffix="${4:-}"
	local delay="${5:-0.04}"

	# Direction detection: default right, flip for left-pointing signs
	local direction="right"
	case "$sign" in
	"←" | "<" | "<-" | "<=" | "<<") direction="left" ;;
	esac

	# Normalize sign to a single display char; handle 2-char width
	local char="$sign"
	local char_width=1
	local sign_len=${#sign}
	if ((sign_len == 2)); then
		# Recognized 2-char patterns: each repetition occupies 2 columns
		char_width=2
	elif ((sign_len > 2)); then
		# More than 2 chars: keep only first
		char="${sign:0:1}"
	fi

	# Capture terminal width from parent shell (subshell tput may return wrong value)
	local cols=$(tput cols 2>/dev/null || echo 80)

	# Auto-truncate prefix/suffix so total line never exceeds terminal width.
	# Layout: prefix + " " + glow + " " + suffix + " " + status(~25)
	# Reserve: 2 spaces (around glow) + 26 (status+space) + minimum 4 glow chars
	local max_label_budget=$((cols - 32))
	local prefix_len=0 suffix_len=0
	[[ -n "$prefix" ]] && prefix_len=${#prefix}
	[[ -n "$suffix" ]] && suffix_len=${#suffix}
	local total_label=$((prefix_len + suffix_len))
	if ((total_label > max_label_budget && max_label_budget > 0)); then
		# Split budget proportionally, truncate with "…"
		local half=$((max_label_budget / 2))
		if ((prefix_len > half)); then
			prefix="${prefix:0:$((half - 1))}…"
			prefix_len=${#prefix}
		fi
		local suffix_budget=$((max_label_budget - prefix_len))
		if ((suffix_len > suffix_budget)); then
			suffix="${suffix:0:$((suffix_budget - 1))}…"
			suffix_len=${#suffix}
		fi
	fi

	# Subtract prefix/suffix visible widths from glow region
	# -2 for the blanks between prefix/glow and glow/suffix
	local iterations=$(((cols * length_pct / 100 - prefix_len - suffix_len - 2) / char_width))
	((iterations < 1)) && iterations=1

	local glow_pid_file="/tmp/glowpids"
	export GLOW_INJECTOR="${GLOW_INJECTOR:-/tmp/glow_status.txt}"

	tput civis 2>/dev/null # Hide cursor

	(
		trap 'tput cnorm 2>/dev/null; exit 0' INT TERM ERR EXIT

		tput civis 2>/dev/null # Hide cursor in subshell too

		# Rainbow palette — 30 hues for smooth gradient (256-color)
		# red → orange → yellow → green → cyan → blue → violet → magenta → back
		local -a palette=(
			196 202 208 214 220 226 # red → yellow
			190 154 118 82 46       # yellow → green
			47 48 49 50 51          # green → cyan
			45 39 33 27 21          # cyan → blue
			57 93 129 165 201       # blue → magenta
			200 199 198 197         # magenta → red
		)
		local plen=${#palette[@]}
		local reset_code="\033[0m"
		local frame=0
		# Wave direction: negative step = colors scroll rightward on screen
		local step=-2
		[[ "$direction" == "left" ]] && step=2

		local previous_status=""
		local current_status=""

		while true; do
			# Check for injected status updates (same pattern as start_spinner)
			if [[ -s "$GLOW_INJECTOR" ]]; then
				local new_status="$(<"$GLOW_INJECTOR")"
				if [[ "$new_status" != "$previous_status" ]]; then
					previous_status="$new_status"

					# Strip \r from rsync output, extract percentage (e.g., "59%")
					current_status="$(echo "${new_status}" | tr -d '\r' | grep -oP '\d+%' | tail -1)"
					: >"$GLOW_INJECTOR"
				fi
			fi

			# Buffer entire frame, single printf to avoid partial-render tearing
			local line=""
			for i in $(seq 1 "$iterations"); do
				local color_idx=$((((i * 3 + frame * step) % plen + plen) % plen))
				line+="\033[38;5;${palette[$color_idx]}m${char}${reset_code}"
			done
			if [[ -n "$row" ]]; then
				# Absolute positioning: save cursor, jump to row, clear line, print, restore
				printf "\033[s\033[%d;1H\033[K%s %b %s %s\033[u" "$row" "$prefix" "$line" "$suffix" "$current_status" >&2
			else
				printf "\r\033[K%s %b %s %s" "$prefix" "$line" "$suffix" "$current_status" >&2
			fi

			frame=$(((frame + 1) % plen))
			sleep "$delay"
		done

	) &
	local new_pid=$!
	disown "$new_pid" # Detach only this PID, not unrelated jobs

	# Load existing PIDs array from file, append new PID, write back
	local -a glow_pids=()
	if [[ -f "$glow_pid_file" ]]; then
		# shellcheck disable=SC1090
		source "$glow_pid_file"
	fi
	glow_pids+=("$new_pid")
	echo "glow_pids=(${glow_pids[*]})" >"$glow_pid_file"

	trap 'stop_glow --no-clear' INT
}
stop_glow() {
	local no_clear="${1:-}"
	local glow_pid_file="/tmp/glowpids"

	tput cnorm 2>/dev/null

	if [[ -f "$glow_pid_file" ]]; then
		local -a glow_pids=()
		# shellcheck disable=SC1090
		source "$glow_pid_file"

		for pid in "${glow_pids[@]}"; do
			kill "$pid" &>/dev/null
			# Poll until dead (can't wait on disowned process)
			local attempts=0
			while kill -0 "$pid" 2>/dev/null && ((attempts++ < 50)); do
				sleep 0.02
			done
		done

		rm -f "$glow_pid_file"
		rm -f "${GLOW_INJECTOR:-/tmp/glow_status.txt}"
		trap - INT
	fi

	if [[ -z "$no_clear" ]]; then
		# Clear the glow line and move cursor back
		printf "\r\033[K"
	fi
}

############################-############################
# 					   PROGRESS BAR
############################-############################
start_progress() {
	# Renders a progress bar driven by file-based input (rsync --info=progress2 or iteration counts).
	# Reads from PROGRESS_INJECTOR file. Runs as background process.
	# Accepts named arguments only (no positional args).

	[[ -t 1 ]] || return 0 # must only run in interactive mode.

	# NOTE: do NOT install signal traps in the caller's shell here — that's the
	# caller's responsibility. Installing traps on INT/TERM/TSTP/ERR/EXIT in the
	# caller leaks state that kills the parent script (e.g. sync_linux_homes.sh)
	# when the progress bar's own internal processes signal up.

	if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
		printf "%b\n" "${BOLD:-}start_progress${NC:-} — background progress bar with file-based IPC"
		printf "\n"
		printf "%b\n" "${BOLD:-}USAGE:${NC:-}"
		printf "  start_progress --injector=/tmp/myfile.txt [OPTIONS]\n"
		printf "\n"
		printf "%b\n" "${BOLD:-}REQUIRED:${NC:-}"
		printf "  ${GREEN:-}--injector=PATH${NC:-}      File that drives progress updates (${CYAN:-}REQUIRED${NC:-})\n"
		printf "                        Aliases: ${CYAN:-}--inject=, -i=${NC:-}\n"
		printf "\n"
		printf "%b\n" "${BOLD:-}OPTIONS:${NC:-}"
		printf "  ${GREEN:-}--length=N${NC:-}           Bar width as %% of terminal cols (default: ${CYAN:-}80${NC:-})\n"
		printf "  ${GREEN:-}--prefix=TEXT${NC:-}         Static text before the bar\n"
		printf "  ${GREEN:-}--suffix=TEXT${NC:-}         Static text after the bar\n"
		printf "  ${GREEN:-}--delay=N${NC:-}             Refresh interval in seconds (default: ${CYAN:-}0.1${NC:-})\n"
		printf "  ${GREEN:-}--total=N${NC:-}             Total iterations for %% calculation (default: ${CYAN:-}100${NC:-})\n"
		printf "  ${GREEN:-}--fill=COLOR${NC:-}          Fill color: var name (${CYAN:-}BG_BLUE${NC:-}) or ANSI code\n"
		printf "                        Auto-converts fg (38;5;N) to bg (48;5;N)\n"
		printf "  ${GREEN:-}--empty_bg=COLOR${NC:-}      Empty bar background (default: dark grey 236)\n"
		printf "\n"
		printf "%b\n" "${BOLD:-}INPUT MODES:${NC:-}  (caller writes to the --injector file)"
		printf "  ${CYAN:-}rsync:${NC:-}       rsync --info=progress2 ... >> \"\$injector\"\n"
		printf "               Parses last NN%% from rsync output (handles \\\\r).\n"
		printf "  ${CYAN:-}iteration:${NC:-}   echo \"--iteration=42 desc\" > \"\$injector\"\n"
		printf "               Calculates %%: iteration * 100 / total.\n"
		printf "  ${CYAN:-}auto-advance:${NC:-} If injector is empty, self-increments by loop iteration.\n"
		printf "\n"
		printf "%b\n" "${BOLD:-}LIFECYCLE:${NC:-}"
		printf "  1. Caller creates injector file and calls start_progress\n"
		printf "  2. Background process (compute_progress) reads injector, draws bar on stderr\n"
		printf "  3. When compute_progress finishes (iteration >= total), stop_progress runs\n"
		printf "  4. stop_progress kills background PIDs, draws final frame, cleans up files\n"
		printf "\n"
		printf "%b\n" "${BOLD:-}INTERNALS:${NC:-}"
		printf "  PID file:     unique per instance (${CYAN:-}/tmp/progresspids_<uuid>${NC:-})\n"
		printf "  Final frame:  ${CYAN:-}/tmp/progress_final_frame${NC:-} (flock-protected)\n"
		printf "  Final bar:    set_final_bar writes escaped array to final frame file\n"
		printf "\n"
		printf "%b\n" "${BOLD:-}RELATED FUNCTIONS:${NC:-}"
		printf "  ${CYAN:-}stop_progress${NC:-}  [--injector=PATH] [--pid_file=PATH]\n"
		printf "                 Kill bar, source final frame, clean up files.\n"
		printf "  ${CYAN:-}set_final_bar${NC:-}  [--prefix=] [--bar=] [--pct=] [--suffix=] [--size=]\n"
		printf "                 Write final frame to ${CYAN:-}/tmp/progress_final_frame${NC:-}.\n"
		printf "\n"
		printf "%b\n" "${BOLD:-}EXAMPLES:${NC:-}"
		printf "  ${CYAN:-}# Rsync with progress${NC:-}\n"
		printf "  start_progress --injector=/tmp/rsync_prog.txt --prefix=\"uploading\" --fill=BG_BLUE\n"
		printf "  rsync -aH --info=progress2 src/ dst/ >> /tmp/rsync_prog.txt\n"
		printf "  stop_progress --injector=/tmp/rsync_prog.txt\n"
		printf "\n"
		printf "  ${CYAN:-}# Iteration-based${NC:-}\n"
		printf "  start_progress --injector=/tmp/iter.txt --total=500 --prefix=\"processing\" --delay=0.05\n"
		printf "  for i in {1..500}; do echo \"--iteration=\$i\" > /tmp/iter.txt; do_work; done\n"
		printf "  stop_progress --injector=/tmp/iter.txt\n"
		return 0
	fi

	# stop_progress &>/dev/null </dev/null

	local session_id="$(uuidgen | tr -d '-')"                           # set unique session/instance id.
	:                                                                   # for clean instances parallelization
	:                                                                   #
	export progress_pids_array_name="PROGRESS_PIDS_ARRAY_${session_id}" #
	export progress_pid_file="/tmp/progresspids_${session_id}"          #
	# TEMP_FINAL_FRAME_FILE is derived per-caller AFTER arg parsing,    #
	# so it follows progress_pid_file when the caller passes --pid_file #
	# (see the `export TEMP_FINAL_FRAME_FILE=...` block below).         #
	:                                                            #
	declare -ag "$progress_pids_array_name=()"                   #
	local -n _pids_ref="$progress_pids_array_name"               #
	:                                                            #
	:                                                            #
	local this_injector                                          # unique injector per instance. REQUIRED as --injector param
	local length_pct=80                                          #
	local is_rsync=false                                         #
	local prefix=""                                              #
	local suffix=""                                              #
	local delay="0.1"                                            #
	local total_iterations=100                                   #
	local noclear=false                                          #
	local bar=""                                                 #
	local pid                                                    #
	local row=""                                                 # absolute terminal row for parallel bars; empty = use \r in-place
	export fill_bg="\033[48;5;34m"                               # green background (256-color 34 = strong green)
	export empty_bg="\033[48;5;236m"                             # dark grey background
	export progress_stop_file="/tmp/progress_stop_${session_id}" # simpler stop even handling than struggling with bg trap control
	local max_text_width=30                                      # max width for prefix and suffix combined (to prevent overflow on narrow terminals)
	local cols=$(tput cols 2>/dev/null || echo 80)               # capture terminal width from parent shell (subshell tput may return wrong value)
	local max_label_budget=$((cols - 10))                        # reserve: 2 spaces + bar + " 100%"

	for arg in "$@"; do
		case "$arg" in
		--this_injector=* | --injector=* | -i=* | --inject=*)
			this_injector="${arg#*=}"
			;;
		--length_pct=* | -l=* | --length=* | -L=*)
			length_pct="${arg#*=}"
			;;
		--prefix=* | -p=*)
			prefix="${arg#*=}"
			;;
		--suffix=* | -s=*)
			suffix="${arg#*=}"
			;;
		--delay=* | -d=*)
			delay="${arg#*=}"
			;;
		--total_iterations=* | --total=* | -t=*)
			total_iterations="${arg#*=}"
			;;
		--fill_bg=* | --fill_color=* | --fill=* | -f=* | --bg=*)
			fill_bg="${arg#*=}"
			if [[ ! "$fill_bg" == *\[* ]]; then
				fill_bg="${!fill_bg}"
			fi
			fill_bg="${fill_bg//38;5;/48;5;}"
			;;
		--empty_bg=* | --empty_color=* | -e=*)
			empty_bg="${arg#*=}"
			;;
		--is_rsync=* | --sync=* | --rsync=*)
			is_rsync="${arg#*=}"
			;;
		--stop_file=*)
			export progress_stop_file="${arg#*=}"
			;;
		--progress_pid_file=* | --pid_file=*)
			export progress_pid_file="${arg#*=}"
			;;
		--progress_pids_array_name=* | --pid_array=*)
			export progress_pids_array_name="${arg#*=}"
			;;
		--row=* | -r=*)
			row="${arg#*=}"
			;;
		*) ;;
		esac
	done

	[[ -z "${this_injector}" ]] && {
		echo -e "$RED" "Missing injector file. Must be set by caller"
		return 1
	}
	[[ -f "${this_injector}" ]] || touch "$this_injector"
	[[ -f "$progress_stop_file" ]] || touch "$progress_stop_file"
	set_stop_progress --state="false" --stop_file="$progress_stop_file"

	# Per-caller final frame file: derive from the (now final) progress_pid_file path.
	# Parallel bars each own their own frame file — no more clobbering on the shared
	# /tmp/progress_final_frame that caused "rog 100%" to render on office's row.
	export TEMP_FINAL_FRAME_FILE="${progress_pid_file//progresspids/progress_final_frame}"
	touch "${TEMP_FINAL_FRAME_FILE}"
	: >"${this_injector}"
	touch "${progress_pid_file}"

	prefix=${prefix:0:$((max_text_width / 2))}              # cap at half of max_text_width
	suffix=${suffix:0:$((max_text_width / 2))}              # cap at half of max_text_width
	printf -v prefix "%-$((max_text_width / 2))s" "$prefix" # left-pad to half of max_text_width — right-align with "%15s"
	printf -v suffix "%-$((max_text_width / 2))s" "$suffix" # left-pad to half of max_text_width — left-align with "%-15s"

	# Auto-truncate prefix/suffix (same logic as glow_print)
	local prefix_len=0 suffix_len=0
	[[ -n "$prefix" ]] && prefix_len=${#prefix}
	[[ -n "$suffix" ]] && suffix_len=${#suffix}
	local total_label=$((prefix_len + suffix_len))
	if ((total_label > max_label_budget && max_label_budget > 0)); then
		local half=$((max_label_budget / 2))
		if ((prefix_len > half)); then
			prefix="${prefix:0:$((half - 1))}…"
			prefix_len=${#prefix}
		fi
		local suffix_budget=$((max_label_budget - prefix_len))
		if ((suffix_len > suffix_budget)); then
			suffix="${suffix:0:$((suffix_budget - 1))}…"
			suffix_len=${#suffix}
		fi
	fi

	# Bar width: total available minus prefix, suffix, spaces, and " 100%" label (6 chars)
	local bar_width=$((cols * length_pct / 100 - prefix_len - suffix_len - 6))
	((bar_width < 4)) && bar_width=4

	tput civis 2>/dev/null # hide cursor during rendering
	set +m                 # disable job control so traps can fire

	local compute_pid=$(
		set +m # disable job control so traps can fire
		(
			set +m # disable job control so traps can fire

			trap 'tput cnorm 2>/dev/null; trap - ERR TERM TSTP INT; set_stop_progress --state=true --stop_file="$progress_stop_file" --caller=trap' ERR TERM TSTP INT
			compute_progress \
				--prefix="$prefix" \
				--suffix="$suffix" \
				--total_iterations="$total_iterations" \
				--bar_w="$bar_width" \
				--delay="$delay" \
				--rsync="$is_rsync" \
				--injector="$this_injector" \
				--stop_file="$progress_stop_file" \
				--progress_pid_file="$this_pidfile" \
				--progress_pids_array_name="$progress_pids_array_name" \
				--row="$row"

		) >/dev/null </dev/null &
		p=$!
		disown "$p"
		register_pid "${progress_pids_array_name}" "${progress_pid_file}" "$p"

		while kill -0 $p &>/dev/null; do
			sleep 0.0001
		done

	) >/dev/null </dev/null

	# echo >&2
	# echo "end of start_progress reached" >&2

	# stop_progress "prefix" "$bar" "$pc_label" "suffix" --injector="$this_injector" --pid_file="${progress_pid_file}"

}
stop_progress() {
	# Stop a single start_progress bar: kill its compute_progress subshell,
	# draw its final frame, clean up its state files.
	#
	# --reason=clean|error controls which final frame gets drawn:
	#
	#   clean (default): write "100%" to --injector → compute_progress's main
	#     loop sees 100% on its next poll → exits the while loop naturally →
	#     calls set_final_bar "$prefix" "$bar" 100 "$suffix" with the REAL state →
	#     -z "$bar" is false → draws cyan SUCCESS frame.
	#
	#   error: skip the injector trick, send SIGTERM straight to compute_progress →
	#     its outer trap fires → writes STOP_PROGRESS=true to the stop file →
	#     compute_progress's loop body takes the failure branch → draws red ⚠️❌.
	#
	# Callers in phase-end cleanup paths should pass --reason=clean --injector=PATH.
	# Callers in error paths (cleanup trap, error handler) should pass --reason=error.
	#
	# All state vars are declared LOCAL so multiple stop_progress calls for
	# different bars don't leak state into each other.

	local args=("$@")
	local prefix=${1:-""}
	local bar=${2:-}
	local pct_label=${3:-100}
	local suffix=${4:-$CHEKED}
	local this_injector=""
	local progress_pid_file=""
	local progress_stop_file=""
	local progress_pids_array_name=""
	local frame_file=""
	local reason="clean" # clean=cyan success frame; error=red failure frame

	for arg in "${args[@]}"; do
		case "$arg" in
		--this_injector=* | --injector=* | -i=* | --inject=*)
			this_injector="${arg#*=}"
			;;
		--progress_pid_file=* | --pid_file=*)
			progress_pid_file="${arg#*=}"
			;;
		--bar=*)
			bar="${arg#*=}"
			;;
		--prefix=* | -p=*)
			prefix="${arg#*=}"
			;;
		--suffix=* | -s=*)
			suffix="${arg#*=}"
			;;
		--stop_file=*)
			progress_stop_file="${arg#*=}"
			;;
		--progress_pids_array_name=* | --pid_array=*)
			progress_pids_array_name="${arg#*=}"
			;;
		--reason=*)
			reason="${arg#*=}"
			;;
		*) ;;
		esac
	done

	if [[ -z "${progress_pid_file}" ]]; then
		echo -e "$BG_RED" "MISSING PID FILE IN stop_progress. Ensure to pass it when calling start_progress from a script" "$NC" >&2
		return 1
	fi

	# Source the pid file (populates the array named by --progress_pids_array_name).
	# Guard with -f — absent file means nothing to kill, nothing to clean up.
	if [[ -f "$progress_pid_file" ]]; then
		. "$progress_pid_file" &>/dev/null || true
	else
		return 0
	fi

	# Stop compute_progress — method depends on reason:
	if [[ "$reason" == "clean" && -n "$this_injector" && -f "$this_injector" ]]; then
		# CLEAN STOP: signal completion via the injector. Writing "100%" makes
		# compute_progress's next poll see percent=100, exit its while loop
		# naturally, and call set_final_bar with the current state → cyan success.
		echo "100%" >>"$this_injector" 2>/dev/null || true
		# Give compute_progress one poll cycle to notice (default delay is 0.1s,
		# but it may be mid-sleep when we wrote; 0.2s covers both).
		sleep 0.2
	fi

	# Bind nameref AFTER parsing, and only if the caller supplied an array name.
	# For clean stops this is a no-op safety sweep (compute_progress should have
	# exited naturally above). For error stops this is the primary kill path.
	if [[ -n "$progress_pids_array_name" ]]; then
		local -n _ref_array="$progress_pids_array_name"
		local _pid
		if [[ "$reason" == "error" ]]; then
			# ERROR STOP: SIGTERM first → compute_progress's trap fires → loop
			# body takes failure branch → red ⚠️❌ final frame. Then SIGKILL
			# any stragglers that ignored SIGTERM.
			for _pid in "${_ref_array[@]}"; do
				[[ "$_pid" -eq 0 ]] && continue
				kill -TERM "$_pid" 2>/dev/null || true
			done
			sleep 0.15
		fi
		# SIGKILL any still-alive stragglers (both clean and error paths).
		for _pid in "${_ref_array[@]}"; do
			[[ "$_pid" -eq 0 ]] && continue
			kill -0 "$_pid" 2>/dev/null && kill -9 "$_pid" 2>/dev/null || true
		done
	fi

	# Per-caller final frame file (see set_final_bar / start_progress).
	# Derived from the pid file path so every bar owns its own frame, and the
	# race where multiple bars clobbered a single /tmp/progress_final_frame file
	# (drawing the wrong bar's final state over the wrong row) can't happen.
	frame_file="${progress_pid_file//progresspids/progress_final_frame}"
	if [[ -f "$frame_file" ]]; then
		. "$frame_file" >/dev/null 2>&1 || true
		[[ -n "${final[*]:-}" ]] && "${final[@]}" >&2
	fi

	# Clean up state files
	[[ -n "$this_injector" ]] && : >"$this_injector"
	[[ -n "$progress_pid_file" && -f "$progress_pid_file" ]] && {
		rm -rf "${progress_stop_file}" 2>/dev/null || true
		rm -rf "${progress_pid_file}" 2>/dev/null || true
		rm -rf "${frame_file}" 2>/dev/null || true
	}

	set -m >&1 >&2     # re-enable job control
	tput cnorm >&1 >&2 # restore cursor
}
compute_progress() {

	local args=("$@")

	local STOP_PROGRESS=false

	local is_rsync
	local percent=0
	local previous_raw=""
	local overflow_count=0
	local parsed
	local iteration=1
	local bar
	local pct_label
	local raw
	local parts=()
	local original_suffix="$suffix"
	local row=""

	for arg in "$@"; do
		case "$arg" in
		--this_injector=* | --injector=* | -i=* | --inject=*)
			this_injector="${arg#*=}"
			;;
		--bar_width=* | --bar_w=*)
			bar_width="${arg#*=}"
			;;
		--prefix=*)
			prefix="${arg#*=}"
			;;
		--suffix=*)
			suffix="${arg#*=}"
			;;
		--delay=* | -d=*)
			delay="${arg#*=}"
			;;
		--injector=* | -s=*)
			injector="${arg#*=}"
			;;
		--is_rsync=* | --sync=* | --rsync=*)
			is_rsync="${arg#*=}"
			;;
		--stop_file=*)
			progress_stop_file="${arg#*=}"
			;;
		--row=* | -r=*)
			row="${arg#*=}"
			;;
		*) ;;
		esac
	done

	# for ((iteration = 0; iteration <= total_iterations; iteration++)); do
	while ((percent < 100)); do
		# Read rsync progress from status file

		if [[ -f "$this_injector" ]]; then
			# local raw=$(tr -d '\r\n' <"$this_injector")
			raw=$(tail -c 200 "$this_injector" | tr '\r' '\n' | grep -oP '\d+%' | tail -1)

			if [[ "$raw" != "$previous_raw" ]]; then

				parts=($raw)
				for part in "${parts[@]}"; do
					case "$part" in
					--iteration=*)
						iteration="${part#*=}"
						raw="${raw/--iteration=/}" # Remove iteration info from raw to prevent parsing issues
						;;
					*) ;;
					esac
				done

				previous_raw="$raw"

				if [[ "$raw" =~ ([0-9]+)% ]]; then
					parsed="${BASH_REMATCH[1]}"
					if ((parsed >= 0 && parsed <= 100)); then
						percent=$parsed
					fi

					# new_prefix="${raw%%%?\s*}" # Text before first percentage (if any)
					# if [[ -n "$new_prefix" ]]; then
					# 	prefix="${new_prefix#%?\s*}" # Remove any trailing % and whitespace from prefix
					# fi
					new_suffix="${raw##*%?\s*}" # Text after last percentage (if any)
					if [[ -n "$new_suffix" ]]; then
						suffix="$new_suffix"
					fi
					[[ "$raw" =~ --prefix=([^[:space:]]+) ]] && prefix="${BASH_REMATCH[1]}"
					[[ "$raw" =~ --suffix=([^[:space:]]+) ]] && suffix="${BASH_REMATCH[1]}"

				else
					parsed=0
				fi

			fi
		fi

		# Calculate filled vs unfilled
		local filled=$((bar_width * percent / 100))
		((filled > bar_width)) && filled=$bar_width
		local unfilled=$((bar_width - filled))

		bar=""
		if ((filled > 0)); then
			bar+="${fill_bg}"
			printf -v spaces "%${filled}s" ""
			bar+="${spaces}${NC}"
		fi
		if ((unfilled > 0)); then
			bar+="${empty_bg}"
			printf -v spaces "%${unfilled}s" ""
			bar+="${spaces}${NC}"
		fi

		# Enter staging mode if 0.
		if ((percent == 0)); then
			staging_mode=true
		else
			staging_mode=false
		fi

		# Build display suffix: prepend "Staging files..." while in staging mode.
		if $staging_mode; then
			suffix="Staging files... ${original_suffix}"
		else
			suffix="$original_suffix"
		fi

		# echo >&2
		# echo "this_injector: $this_injector" >&2
		# echo "percent: $percent" >&2
		# echo "is_rsync: $is_rsync" >&2

		# echo -e "$BG_RED" "((percent >= 99))" >&2
		if $is_rsync && ((parsed == 0)); then
			percent=0
		elif ((parsed == 0)); then
			iteration=$((iteration + 1))
			percent=$((iteration * 100 / total_iterations))
		fi

		# echo "sourcing progress_stop_file: $progress_stop_file" >&2
		#
		# echo "" >&2
		# echo -e "$ACCENT_YELLOW" "STOP_PROGRESS: $STOP_PROGRESS" "$NC" >&2
		# echo "" >&2

		print_bar "$prefix" "$bar" "$percent" "$suffix" "$row"

		[[ -f "$progress_stop_file" ]] && {

			. "$progress_stop_file"

			# cat "$progress_stop_file" >&2

			# echo "STOP_PROGRESS: $STOP_PROGRESS" >&2
			[[ -z "$STOP_PROGRESS" ]] && continue

			$STOP_PROGRESS && {
				# Draw the final (failure) frame and exit. The outer stop_progress
				# call from the caller will replay this frame via the per-caller
				# final-frame file, so we don't recurse into stop_progress here —
				# doing so was the cause of the "MISSING PID FILE" warning because
				# the recursive call didn't pass --pid_file (it couldn't know the
				# caller's per-host path from inside this subshell).
				set_final_bar --final_error_length=50 --row="$row"
				exit 1 # this func always runs in a background shell.
			}
		}

		sleep "$delay"

	done

	set_final_bar "$prefix" "$bar" "$percent" "$suffix" --row="$row"

	: >"$progress_pid_file"

}
set_stop_progress() {
	local args=("$@")
	local state="${1:-true}"
	local stop_file="${2:-$progress_stop_file}"
	local caller="${3:-${FUNCNAME[1]}}"
	for arg in "${args[@]}"; do
		case "$arg" in
		--state=*)
			state="${arg#*=}"
			;;
		--stop_file=*)
			stop_file="${arg#*=}"
			;;
		--caller=*)
			caller="${arg#*=}"
			;;
		*) ;;
		esac
	done

	# echo "set_stop_progress called from ${FUNCNAME[1]:-'unknown'} => $caller" | tee -a "$LOG_FILE"

	echo "STOP_PROGRESS=$state" >"${stop_file}"

	(
		# echo "progress_stop_file: $progress_stop_file" >&2
		[[ -z "$progress_stop_file" ]] && {
			echo "progress_stop_file is not set! cleaning up..." >&2
			readarray -t prog < <(find /tmp/ -type f -name 'progress_stop_*' 2>/dev/null)
			for p in "${prog[@]}"; do
				echo "setting $p to true" >&2
				echo "STOP_PROGRESS=true" >"$p"

				sleep 1 # time for value to propagate to all currently running progress bars

				rm -f $p &>dev/null && echo "$p removed" >&2 || echo "$p not removed" >&2

			done
		}
	) >/dev/null </dev/null &
	disown

}
set_final_bar() {

	local args=("$@")
	# Positional args ONLY if they don't look like flags. A call like
	#   set_final_bar --final_error_length=50 --row="$row"
	# would otherwise set prefix="--final_error_length=50", bar="--row=5",
	# hijacking the display with flag text. Skip any positional that starts
	# with "--" and let the argparse loop below handle it as a named arg.
	local prefix="" bar="" percent=50 suffix="${CHEKED}"
	[[ "${1:-}" != --* ]] && prefix="${1:-""}"
	[[ "${2:-}" != --* ]] && bar="${2:-}"
	[[ "${3:-}" != --* ]] && percent="${3:-50}"
	[[ "${4:-}" != --* ]] && suffix="${4:-$CHEKED}"
	# final_bar_length and final_error_length are NAMED args only — do NOT
	# read from $5. Earlier versions read both from $5 AND the callers started
	# passing `--row="$row"` as the 5th positional, which made $5 the literal
	# string "--row=" → final_bar_length="--row=" → printf "%--row=s" crashed
	# with "invalid format character r" and every error cascade-advanced the
	# cursor, stacking progress bars one per line.
	local final_bar_length=100
	local final_error_length=30
	local row="" # absolute terminal row for parallel bars; empty = use \r in-place

	for arg in "${args[@]}"; do
		case "$arg" in
		--prefix=*)
			prefix="${arg#*=}"
			;;
		--bar=*)
			bar="${arg#*=}"
			;;
		--pct=* | --pct_label=*)
			pct_label="${arg#*=}"
			;;
		--suffix=*)
			suffix="${arg#*=}"
			;;
		--size=* | --final_bar_length=*)
			final_bar_length="${arg#*=}"
			;;
		--final_error_length=*)
			final_error_length="${arg#*=}"
			;;
		--row=* | -r=*)
			row="${arg#*=}"
			;;
		*) ;;
		esac
	done

	# Respect the inherited TEMP_FINAL_FRAME_FILE if start_progress set one
	# (per-caller path derived from progress_pid_file — see start_progress).
	# Only apply the shared default if nothing was set by the caller.
	: "${TEMP_FINAL_FRAME_FILE:=/tmp/progress_final_frame}"
	[[ -f "${TEMP_FINAL_FRAME_FILE}" ]] || touch "${TEMP_FINAL_FRAME_FILE}"

	# trap - INT EXIT TERM TSTP

	if [[ "${prefix,,}" == "failed" || "$suffix" == *"${FAILED}"* || -z "$bar" ]]; then

		# failure or interruption, red with default length
		bar+="${BG_RED}"
		printf -v spaces "%${final_error_length}s"
		bar+="${spaces}${NC}"
		pct_label="000"
		prefix="${WARNING}"
		suffix="${FAILED}"

	else
		# successful operation, just set length to desired value
		bar="${BG_CYAN}"
		printf -v spaces "%${final_bar_length}s"
		bar+="${spaces}${NC}"
	fi

	printf -v pct_label "%3d%%" "$percent" # printf -v assigns formatted string to variable
	if [[ -n "$row" ]]; then
		# Final frame for parallel bars: positioned at absolute row, doesn't disturb prompt.
		final=(printf "\033[s\033[%d;1H\033[K%s %b %s %b\033[u" "$row" "$prefix" "$bar" "$pct_label" "$suffix")
	else
		final=(printf "\r\033[K%s %b %s %b" "$prefix" "$bar" "$pct_label" "$suffix")
	fi

	#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~#
	# printf '%q ' escapes each array element 		 #
	# so that ;, \033[, and other special characters #
	# survive the round-trip through file → source.  #
	#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~#
	# Per-caller lock file derived from the frame file path so concurrent bars
	# serialize only against themselves, not each other.
	local frame_lock="${TEMP_FINAL_FRAME_FILE}.lock"
	(
		flock -n 9 || return
		printf 'declare -ag final=(%s)\n' "$(printf '%q ' "${final[@]}")" >"${TEMP_FINAL_FRAME_FILE}" #
	) 9>"$frame_lock"

	export TEMP_FINAL_FRAME_FILE

	# trap - EXIT INT ERR TERM TSTP

	# printf "\r\033[K" >&2

}
print_bar() {

	local args=("$@")
	local prefix=${1:-""}
	local bar=${2:-}
	local percent=${3:-50}
	local suffix=${4}
	local row=${5:-} # absolute terminal row; empty = render in-place with \r

	# Format percentage with padding
	# %3d = right-aligned, 3-char-wide integer.
	# %% = literal %. So we get  0%,  42%, 100% — always same width, no jumping.

	printf -v pct_label "%3d%%" "$percent" # printf -v assigns formatted string to variable

	################################# PRINT #############################

	if [[ -n "$row" ]]; then
		# Save cursor, move to absolute row, clear line, draw, restore cursor.
		# Lets parallel bars coexist without overwriting each other or the prompt.
		printf "\033[s\033[%d;1H\033[K%s %b %s %b\033[u" "$row" "$prefix" "$bar" "$pct_label" "$suffix" >&2
	else
		printf "\r\033[K%s %b %s %b" "$prefix" "$bar" "$pct_label" "$suffix" >&2
	fi
}

############################-############################
#              global logging policies
############################-############################
init_logging() {

	return

	# Create log file if it doesn't exist
	LOG_FILE="${HOME}/log.log"

	# initialize var in .env to an empty value
	set_global_var CURRENT_OUTPUT_MODE ""

	# Set initial output mode based on DEBUG flag
	if [[ "$DEBUG" -eq 1 ]]; then
		redirect_to_both
	else
		redirect_to_file_only
	fi
}
redirect_to_both() {
	# Redirect output to both terminal and log file

	return

	CURRENT_OUTPUT_MODE=$(get_value_from_env CURRENT_OUTPUT_MODE) || {
		local status=$?
		log_error "'redirect_to_both' function failed."
		exit 1
	}

	# Only change redirection if we're not already in "both" mode
	if [[ "$CURRENT_OUTPUT_MODE" != "both" ]]; then
		# Restore original stdout/stderr
		exec 1>&3 2>&4

		# Set up tee to write to both terminal and log file
		exec 1> >(tee -a "$LOG_FILE") 2> >(tee -a "$LOG_FILE" >&2)

		CURRENT_OUTPUT_MODE="both"
		# log_message "Output redirected to both terminal and log file"
	fi

	set_global_var CURRENT_OUTPUT_MODE "${CURRENT_OUTPUT_MODE}"
}
redirect_to_file_only() {
	# Redirect output to log file only

	return

	CURRENT_OUTPUT_MODE=$(get_value_from_env CURRENT_OUTPUT_MODE)
	# Only change redirection if we're not already in "file_only" mode
	if [[ "$CURRENT_OUTPUT_MODE" != "file_only" ]]; then
		if [[ "$DEBUG" -eq 1 ]]; then
			redirect_to_both # In debug mode, always show output
		else
			# Restore original stdout/stderr first
			exec 1>&3 2>&4

			# Redirect stdout and stderr to log file
			exec 1>>"$LOG_FILE" 2>>"$LOG_FILE"

			CURRENT_OUTPUT_MODE="file_only"
		fi
	fi
	set_global_var CURRENT_OUTPUT_MODE "${CURRENT_OUTPUT_MODE}"
}
restore_interactive_terminal() {
	# Restore interactive terminal (alias for redirect_to_both)
	redirect_to_both
}
redirect_stdout_to_file() {
	# Alias for consistency with existing code
	redirect_to_file_only
}

if ! $NO_EXEC; then
	log "LOGGER SOURCED"
fi

set +a

export LOGGER_SCRIPT_ALREADY_SOURCED=true

echo "$(basename "${BASH_SOURCE[0]}") loaded"
