#!/bin/bash
# ╔══════════════════════════════════════════════════════════════════════════════════════╗
# ║  scripts/publish_public_mirror.sh                                                    ║
# ║                                                                                      ║
# ║  Build a cleansed (filter-repo'd) view of the private repo and push it to the        ║
# ║  PUBLIC mirror. Default: fast-forward only — refuses to rewrite already-published    ║
# ║  portfolio history. Use --rewrite-portfolio-history to force a deliberate rewrite    ║
# ║  (e.g. after a filter-rule change or a one-time historical scrub).                   ║
# ║                                                                                      ║
# ║  Canonical: ~/0_MOBIUS.TILES/docs/plans/dual_repo_canonical_runbook_and_pitfalls_    ║
# ║             for_tiles_and_nvr_2026_05_29.md (private; consult on dev machine)        ║
# ║                                                                                      ║
# ║      ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐              ║
# ║      │ clone live repo  │──▶│ git filter-repo  │──▶│ leak gate        │              ║
# ║      │ into /tmp/build  │   │ strip + redact   │   │ (abort on leak)  │              ║
# ║      └──────────────────┘   └──────────────────┘   └────────┬─────────┘              ║
# ║                                                             ▼                        ║
# ║                                                     ┌──────────────────┐             ║
# ║                                                     │ FF-only push to  │             ║
# ║                                                     │ public/main      │             ║
# ║                                                     └──────────────────┘             ║
# ║                                                                                      ║
# ║  FLAGS:                                                                              ║
# ║    [<branch>]                       branch to publish (default: main)                ║
# ║    --rewrite-portfolio-history      ⚠ force-rewrite the public mirror (operator     ║
# ║                                       confirms; never automated)                     ║
# ║    --no-confirm                     skip the interactive confirmation (for scripted  ║
# ║                                       operator-only invocations)                     ║
# ║    --help, -h                       show usage and exit                              ║
# ║                                                                                      ║
# ║  CANONICAL EXCEPTIONS (documented):                                                  ║
# ║    S.2.1  source_global_env replaced by S.2.18 portable helper sourcing — script     ║
# ║           ships to public clones that have no personal shell config.                 ║
# ║    S.2.3  PAUSE_FILE — not applicable to a one-shot publish script.                  ║
# ║    S.2.6  No bg-PID tracking — script runs serially, no children kept after exit.    ║
# ║           Traps still install for the temp-dir cleanup invariant.                    ║
# ║    S.2.10 simple_logger — colour-aware echo for host-independence.                   ║
# ╚══════════════════════════════════════════════════════════════════════════════════════╝

[[ -t 1 ]] && clear

# Propagate failures: push errors must NOT be silently absorbed (otherwise the
# cleanup trap fires with exit 0 and the build dir is removed before the
# operator can inspect why a push failed). Canonical pitfall PITFALL_5.
set -e

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
SCRIPT_R_PATH=$(realpath "${BASH_SOURCE[0]}")
SCRIPT_DIR="${SCRIPT_R_PATH%${SCRIPT_NAME}}"

# Repo root is the parent of scripts/. builtin cd bypasses any cd wrapper (S.2.17).
REPO_ROOT="$(builtin cd "${SCRIPT_DIR}/.." && pwd)"

# Color + logger helpers: home copy preferred, in-repo copy fallback, tolerated absent (S.2.18).
. ~/.env.colors 2>/dev/null || . "${REPO_ROOT}/.env.colors" 2>/dev/null || true
. ~/logger.sh --no-exec &>/dev/null || . "${REPO_ROOT}/logger.sh" --no-exec &>/dev/null || true

########################################################################-########################################################################
SMARTHOME_PUBLISH__ARGS=("$@")                            #
SMARTHOME_PUBLISH__BRANCH="main"                          # default branch to publish (CLI positional override)
SMARTHOME_PUBLISH__REWRITE=false                          # --rewrite-portfolio-history flag
SMARTHOME_PUBLISH__NO_CONFIRM=false                       # --no-confirm flag
SMARTHOME_PUBLISH__PUBLIC_URL="https://github.com/elfege/MOBIUS.SMART_HOME.git"  # the cleansed mirror's URL
SMARTHOME_PUBLISH__BUILD=""                               # mktemp dir; set by smarthome_publish__prepare_build
SMARTHOME_PUBLISH__REWRITE_CONFIRM_PHRASE="rewrite portfolio history"  # exact phrase operator must type
########################################################################-########################################################################

########################################################################-########################################################################
#                                                                INITIALIZATION                                                                  #
########################################################################-########################################################################
safe_exit() {
	# Exit cleanly whether the script is sourced or executed. `exit` from a sourced
	# script kills the caller's shell; `return` returns from the source call.
	local exit_code=${1:-$?}
	if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
		exit "$exit_code"
	else
		return "$exit_code"
	fi
}

smarthome_publish__show_help() {
	# Print usage and exit zero. Called from parse_args before any heavy lifting,
	# so --help stays cheap regardless of state.
	echo ""
	echo -e "${BOLD:-}${CYAN:-}Usage:${NC:-} $0 [<branch>] [--rewrite-portfolio-history] [--no-confirm] [--help|-h]"
	echo ""
	echo -e "  Build a cleansed (filter-repo'd) view of the private repo and push it to the"
	echo -e "  PUBLIC mirror at ${SMARTHOME_PUBLISH__PUBLIC_URL}."
	echo ""
	echo -e "${BOLD:-}Default mode (safe):${NC:-}"
	echo -e "  Fast-forward only. Aborts loudly if the push would rewrite already-published"
	echo -e "  portfolio history (which happens when the filter rules change or when this is"
	echo -e "  the first cleansed publish over a previously raw-pushed mirror)."
	echo ""
	echo -e "${BOLD:-}Options:${NC:-}"
	echo -e "  ${CYAN:-}<branch>${NC:-}                          Branch to publish. Default: main."
	echo -e "  ${CYAN:-}--rewrite-portfolio-history${NC:-}       ⚠ Force-rewrite the public mirror. Required"
	echo -e "                                       after filter-rule changes or for the one-time"
	echo -e "                                       historical scrub. Prompts the operator to type"
	echo -e "                                       the exact phrase '${SMARTHOME_PUBLISH__REWRITE_CONFIRM_PHRASE}'"
	echo -e "                                       to confirm. NEVER call this from CI or a hook."
	echo -e "  ${CYAN:-}--no-confirm${NC:-}                      Skip the interactive confirmation. Operator-only,"
	echo -e "                                       for scripted invocations. Combine with"
	echo -e "                                       --rewrite-portfolio-history at your own risk."
	echo -e "  ${CYAN:-}--help${NC:-}, ${CYAN:-}-h${NC:-}                        Show this message and exit."
	echo ""
	echo -e "${BOLD:-}Examples:${NC:-}"
	echo -e "  ${GREEN:-}$0${NC:-}                                  # publish main (default), fast-forward only"
	echo -e "  ${GREEN:-}$0 release-2026${NC:-}                     # publish a release branch, fast-forward only"
	echo -e "  ${GREEN:-}$0 main --rewrite-portfolio-history${NC:-} # one-time historical scrub of the public mirror"
	echo ""
	safe_exit 0
}

smarthome_publish__parse_args() {
	# Walk SMARTHOME_PUBLISH__ARGS once. --help short-circuits via show_help. The first
	# non-flag positional is treated as the branch name; subsequent positionals are
	# rejected (no compound publishes).
	local a saw_branch=false
	for a in "${SMARTHOME_PUBLISH__ARGS[@]}"; do
		case "$a" in
		--rewrite-portfolio-history) SMARTHOME_PUBLISH__REWRITE=true ;;
		--no-confirm) SMARTHOME_PUBLISH__NO_CONFIRM=true ;;
		--help | -h) smarthome_publish__show_help ;;
		--*)
			echo -e "${RED:-}✗ unknown flag: $a${NC:-}" >&2
			safe_exit 2
			;;
		*)
			if $saw_branch; then
				echo -e "${RED:-}✗ unexpected positional: $a (branch already set to '${SMARTHOME_PUBLISH__BRANCH}')${NC:-}" >&2
				safe_exit 2
			fi
			SMARTHOME_PUBLISH__BRANCH="$a"
			saw_branch=true
			;;
		esac
	done
}

smarthome_publish__verify_repo() {
	# Confirm we're sitting on a git repo. publish must NEVER be run outside one —
	# `git clone "$SRC"` below would fail loudly anyway, but bail early with a
	# clearer message.
	if ! git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
		echo -e "${RED:-}✗ $REPO_ROOT is not a git checkout${NC:-}" >&2
		safe_exit 1
	fi
}

smarthome_publish__prepare_build() {
	# Create the mktemp build dir under /tmp. Setting SMARTHOME_PUBLISH__BUILD here so
	# the trap installed by set_traps can clean it up on any exit path.
	SMARTHOME_PUBLISH__BUILD="$(mktemp -d /tmp/smarthome_public_build.XXXX)"
	echo -e "${BOLD:-}→ build dir:${NC:-} ${SMARTHOME_PUBLISH__BUILD}"
}

smarthome_publish__cleanup() {
	# Single exit path. Trapped on EXIT/TERM/ERR (with caller-line context) and
	# on INT/TSTP. Removes the build dir to keep /tmp tidy. Does NOT remove on
	# failure — operator may want to inspect the rewritten history.
	local exit_code=${1:-$?}
	local lineno=${2:-}
	local command=${3:-}

	trap - EXIT INT TSTP TERM ERR

	if [[ -n "$SMARTHOME_PUBLISH__BUILD" && -d "$SMARTHOME_PUBLISH__BUILD" ]]; then
		if [[ "$exit_code" -ne 0 ]]; then
			echo -e "${YELLOW:-}⚠ leaving build dir for inspection: ${SMARTHOME_PUBLISH__BUILD}${NC:-}" >&2
		else
			rm -rf "$SMARTHOME_PUBLISH__BUILD"
		fi
	fi

	if [[ "$exit_code" -ne 0 && -n "$lineno" ]]; then
		echo -e "${RED:-}✗ exit ${exit_code} at line ${lineno}: ${command}${NC:-}" >&2
	fi

	safe_exit "$exit_code"
}

smarthome_publish__set_traps() {
	# Capture $LINENO at trap-fire time (it's always 1 inside an EXIT trap body,
	# so the trap string itself must read it eagerly).
	trap 'lineno=$LINENO; smarthome_publish__cleanup "$?" "$lineno" "$BASH_COMMAND"' EXIT TERM ERR
	trap 'smarthome_publish__cleanup 1 "USER INTERRUPT" "$BASH_COMMAND"' INT TSTP
}
########################################################################-########################################################################

########################################################################-########################################################################
#                                                              FILTER & VERIFY                                                                   #
########################################################################-########################################################################
smarthome_publish__clone_into_build() {
	# Clone the live repo into the temp build dir then immediately detach origin.
	# Detaching means any stray `git push` inside the build dir can't accidentally
	# reach the PRIVATE remote — we only re-attach a `public` remote at push time.
	echo -e "${BOLD:-}→ cloning${NC:-} ${REPO_ROOT} @ ${SMARTHOME_PUBLISH__BRANCH} → ${SMARTHOME_PUBLISH__BUILD}"
	git clone -q "$REPO_ROOT" "$SMARTHOME_PUBLISH__BUILD"
	builtin cd "$SMARTHOME_PUBLISH__BUILD"
	git checkout -q "$SMARTHOME_PUBLISH__BRANCH"
	git remote remove origin 2>/dev/null || true
}

smarthome_publish__write_filter_rules() {
	# Write the redaction map (regex → replacement) to a tempfile that filter-repo
	# consumes via --replace-text. Kept in /tmp (separate from the build dir) so
	# the cleanup trap doesn't lose it before filter-repo reads it.
	# Format: `regex:PATTERN==>REPLACEMENT` (filter-repo's --replace-text syntax).
	# Real LAN subnet 192.168.10.x → <LAN_IP>; common-example 192.168.1.x is left
	# alone (often appears in docs as a generic placeholder).
	local rt
	rt="$(mktemp /tmp/smarthome_public_build.replace.XXXX)"
	printf 'regex:192\\.168\\.10\\.[0-9]{1,3}==><LAN_IP>\n' > "$rt"
	echo "$rt"
}

smarthome_publish__run_filter_repo() {
	# Run git filter-repo. The --filename-callback strips entire paths from ALL
	# history; the --replace-text rules redact content inside surviving files.
	# Per canonical §3.2 strip list, adapted for SMART_HOME:
	#   directories  : docs/plans/  docs/history/  docs/teachings/
	#                  docs/weekly_summaries/  docs/patent/  backups/
	#                  DOCS/  nginx/certs/  .hubitat/  _SYNCAPP/  postgres_data/
	#   files        : docs/README_handoff.md  docs/README_project_history.md
	#                  docs/README_port_mappings.md  docs/README_daily_standup_pitch.md
	#                  docs/README_investigation_lighting_reliability_*.md
	#                  CLAUDE.md  chat.md  claude_rules.md
	#                  app_structure.txt  claude_rules.txt  file_history.txt  tree.txt
	#   patterns     : *.pem  *.key  *.crt  *.p12  *.pfx  .env  credentials.json
	#                  *.sqlite  *.db
	# SMART_HOME-specific additions vs the TILES set:
	#   - docs/patent/  (patent disclosure — never publish)
	#   - backups/      (operator local backups)
	#   - docs/README_daily_standup_pitch.md  (work-project standup; not in repo today but defensive)
	#   - CLAUDE.md     (operator instructions w/ hub IPs, AWS profile, token names)
	# The .hubitat/_SYNCAPP/postgres_data entries are defensive — not tracked
	# today, but TILES learned the hard way that zombie-tracked operator paths
	# can survive .gitignore (PITFALL_8 / MSG-300 of the canonical).
	local rt
	rt="$(smarthome_publish__write_filter_rules)"
	echo -e "${BOLD:-}→ filter-repo (strip private paths + redact LAN subnet)${NC:-}"
	git filter-repo --force --prune-empty never --replace-text "$rt" --filename-callback '
strip_dirs = (
    b"docs/plans/",
    b"docs/history/",
    b"docs/teachings/",
    b"docs/weekly_summaries/",
    b"docs/patent/",
    b"backups/",
    b"DOCS/",
    b"nginx/certs/",
    b".hubitat/",
    b"_SYNCAPP/",
    b"postgres_data/",
    # GitHub Actions workflows are dev-side only. autotag.yml + publish-
    # public-mirror.yml both ran on the PUBLIC mirror after publish, where
    # autotag tried to bump tags independently of dev (divergence risk) and
    # publish tried to re-publish-from-public-to-public (no deploy key
    # available; the runs failed loudly on every push). Stripping the
    # whole .github/workflows/ tree from public history makes the public
    # mirror a pure static target — all CI lives on the dev side.
    b".github/workflows/",
)
strip_files = {
    b"docs/README_handoff.md",
    b"docs/README_project_history.md",
    b"docs/README_port_mappings.md",
    b"docs/README_daily_standup_pitch.md",
    b"CLAUDE.md",
    b"chat.md",
    b"claude_rules.md",
    b"app_structure.txt",
    b"claude_rules.txt",
    b"file_history.txt",
    b"tree.txt",
}
strip_prefixes = (
    b"docs/README_investigation_",
)
strip_suffixes = (b".pem", b".key", b".crt", b".p12", b".pfx", b".sqlite", b".db")
strip_exact = {b".env", b"credentials.json"}

if filename in strip_files: return None
if filename in strip_exact: return None
for d in strip_dirs:
    if filename.startswith(d): return None
for p in strip_prefixes:
    if filename.startswith(p): return None
for s in strip_suffixes:
    if filename.endswith(s): return None
return filename
'
	rm -f "$rt"
}

smarthome_publish__leak_gate() {
	# Refuse to push if any flagged path survived the filter. This is the last
	# automated check before the public network reaches the public repo —
	# treat any leak found here as a P0.
	echo -e "${BOLD:-}→ leak gate${NC:-}"

	local leaks ip_leaks host_leaks
	leaks="$(git ls-files | grep -E '\.(pem|key|crt|p12|pfx|sqlite|db)$|^(docs/(plans|history|teachings|weekly_summaries|patent)/|backups/|DOCS/|nginx/certs/|\.hubitat/|_SYNCAPP/|postgres_data/|chat\.md$|claude_rules\.md$|CLAUDE\.md$|app_structure\.txt$|claude_rules\.txt$|file_history\.txt$|tree\.txt$|docs/README_(handoff|project_history|port_mappings|daily_standup_pitch|investigation_)|credentials\.json$|\.env$)' || true)"
	if [[ -n "$leaks" ]]; then
		echo -e "${RED:-}✗ ABORT — flagged path(s) survived the filter:${NC:-}" >&2
		echo "$leaks" >&2
		safe_exit 1
	fi

	ip_leaks="$(git grep -lE '192\.168\.10\.[0-9]' -- . 2>/dev/null || true)"
	if [[ -n "$ip_leaks" ]]; then
		echo -e "${RED:-}✗ ABORT — real LAN IPs survived redaction in:${NC:-}" >&2
		echo "$ip_leaks" >&2
		safe_exit 1
	fi

	# Host-name redaction defense: operator's machine names should not appear in
	# public content. dellserver/office/app1/rog/hvtmc are personal hostnames.
	# Allow them inside CHANGELOG / commit logs — those are historical and stripping
	# would require rewriting commit messages, out of scope here. Only fail if they
	# appear in tracked CONTENT.
	host_leaks="$(git grep -lE '\b(dellserver|hvtmc[a-z]*)\b' -- . 2>/dev/null || true)"
	if [[ -n "$host_leaks" ]]; then
		echo -e "${YELLOW:-}⚠ host names found in tracked content (review before push):${NC:-}" >&2
		echo "$host_leaks" >&2
	fi

	local commit_count file_count
	commit_count=$(git rev-list --count HEAD)
	file_count=$(git ls-files | wc -l)
	echo -e "${GREEN:-}✓ clean:${NC:-} ${commit_count} commits, ${file_count} files"
}
########################################################################-########################################################################

########################################################################-########################################################################
#                                                                  PUSH                                                                          #
########################################################################-########################################################################
smarthome_publish__attach_public_remote() {
	# Add the public remote inside the build dir. The live repo is NEVER asked
	# to know about `public` — that boundary is what keeps a wrong-remote slip
	# (e.g. an absent-minded `git push public main` from the live working tree)
	# impossible at the file-system level.
	git remote add public "$SMARTHOME_PUBLISH__PUBLIC_URL"
}

smarthome_publish__is_fast_forward() {
	# Compare the new HEAD against the current public/main. Returns:
	#   0  → fast-forward (or public/main missing — first publish counts as FF)
	#   1  → divergence (the rewrite case — caller must check the flag)
	# Method: fetch the public ref into a local tracking ref, then check whether
	# the OLD ref is an ancestor of the NEW ref. If it is, push is a fast-forward.
	local new_head old_head
	new_head="$(git rev-parse HEAD)"

	if ! git ls-remote --exit-code public "refs/heads/${SMARTHOME_PUBLISH__BRANCH}" >/dev/null 2>&1; then
		echo -e "${YELLOW:-}⚠ public/${SMARTHOME_PUBLISH__BRANCH} does not exist yet — first publish, treating as fast-forward${NC:-}"
		return 0
	fi

	git fetch -q public "${SMARTHOME_PUBLISH__BRANCH}:refs/remotes/public/${SMARTHOME_PUBLISH__BRANCH}" 2>/dev/null || true
	old_head="$(git rev-parse "refs/remotes/public/${SMARTHOME_PUBLISH__BRANCH}" 2>/dev/null || true)"

	if [[ -z "$old_head" ]]; then
		echo -e "${YELLOW:-}⚠ couldn't resolve public/${SMARTHOME_PUBLISH__BRANCH} locally after fetch — treating as fast-forward${NC:-}"
		return 0
	fi

	if git merge-base --is-ancestor "$old_head" "$new_head"; then
		return 0
	fi
	return 1
}

smarthome_publish__confirm_rewrite() {
	# Belt-and-braces confirmation for --rewrite-portfolio-history. Skipped only
	# under --no-confirm (operator-scripted invocations). Asks the operator to
	# type the exact phrase so a stray newline or Y/N habit can't approve a
	# destructive rewrite. Prompt uses /dev/tty so it survives `&>/log` redirection.
	$SMARTHOME_PUBLISH__NO_CONFIRM && return 0
	echo ""
	echo -e "${BOLD:-}${RED:-}⚠ ABOUT TO REWRITE PUBLIC PORTFOLIO HISTORY ⚠${NC:-}"
	echo -e "  target: ${SMARTHOME_PUBLISH__PUBLIC_URL} :: ${SMARTHOME_PUBLISH__BRANCH}"
	echo -e "  effect: every commit SHA on the public ${SMARTHOME_PUBLISH__BRANCH} will change."
	echo -e "          external links to specific public commits will break."
	echo -e "          this is appropriate for filter-rule changes and one-time scrubs;"
	echo -e "          it is NOT appropriate for routine publishes."
	echo ""
	echo -e "  to confirm, type the exact phrase below and press ENTER:"
	echo -e "    ${CYAN:-}${SMARTHOME_PUBLISH__REWRITE_CONFIRM_PHRASE}${NC:-}"
	echo ""
	local typed=""
	read -r -p "> " typed </dev/tty 2>/dev/tty || true
	if [[ "$typed" != "$SMARTHOME_PUBLISH__REWRITE_CONFIRM_PHRASE" ]]; then
		echo -e "${RED:-}✗ confirmation phrase did not match — aborting${NC:-}" >&2
		safe_exit 1
	fi
}

smarthome_publish__push_branch() {
	# Push the cleansed branch to public. In FF-only mode, plain `git push`.
	# In --rewrite mode, plain `--force` — --force-with-lease requires a
	# remote-tracking ref to compare against, which we don't fetch in rewrite
	# mode (the FF check that would have fetched is skipped). The lease check
	# is moot here anyway: rewrite mode IS the deliberate-rewrite operator
	# action, and the typed-phrase confirmation in confirm_rewrite() is
	# already the operator's "yes I mean it" signal. (Canonical PITFALL_4.)
	if $SMARTHOME_PUBLISH__REWRITE; then
		echo -e "${BOLD:-}${YELLOW:-}→ force push (rewrite mode)${NC:-}"
		git push --force public "${SMARTHOME_PUBLISH__BRANCH}"
	else
		echo -e "${BOLD:-}→ fast-forward push${NC:-}"
		git push public "${SMARTHOME_PUBLISH__BRANCH}"
	fi
}

smarthome_publish__push_tags() {
	# Tags: in FF mode, add-only (regular tag push). In rewrite mode, --force
	# allows tag MOVES (rare; happens when filter-repo'd tags shift because
	# their target commit SHA changed). The leak gate already verified no
	# unwanted content shipped, so moving tags is safe.
	if $SMARTHOME_PUBLISH__REWRITE; then
		echo -e "${BOLD:-}${YELLOW:-}→ force tag push (rewrite mode)${NC:-}"
		git push --force public --tags
	else
		echo -e "${BOLD:-}→ tag push (add-only)${NC:-}"
		git push public --tags
	fi
}

smarthome_publish__verify_ff_or_abort() {
	# In default (FF-only) mode, refuse to proceed if the push would rewrite
	# history. Loud, actionable, names the operator-only escape hatch.
	if $SMARTHOME_PUBLISH__REWRITE; then
		return 0
	fi
	if smarthome_publish__is_fast_forward; then
		return 0
	fi
	echo "" >&2
	echo -e "${RED:-}✗ ABORT — fast-forward push to public/${SMARTHOME_PUBLISH__BRANCH} rejected.${NC:-}" >&2
	echo -e "${RED:-}  this push would REWRITE already-published portfolio history${NC:-}" >&2
	echo -e "${RED:-}  (filter rules likely changed since the last publish).${NC:-}" >&2
	echo "" >&2
	echo -e "  To accept the rewrite, re-run with:" >&2
	echo -e "    ${CYAN:-}$0 ${SMARTHOME_PUBLISH__BRANCH} --rewrite-portfolio-history${NC:-}" >&2
	echo "" >&2
	echo -e "  NEVER call --rewrite-portfolio-history from CI or a hook." >&2
	echo -e "  See ~/0_MOBIUS.TILES/docs/plans/dual_repo_canonical_runbook_and_pitfalls_for_tiles_and_nvr_2026_05_29.md §2." >&2
	safe_exit 1
}
########################################################################-########################################################################

########################################################################-########################################################################
#                                                                  EXECUTION                                                                     #
########################################################################-########################################################################
smarthome_publish__run() {
	# Top-level orchestrator. Strict order:
	#   parse_args -> verify_repo -> prepare_build -> set_traps
	#   clone -> filter_repo -> leak_gate
	#   attach_public_remote -> verify_ff_or_abort -> confirm_rewrite
	#   push_branch -> push_tags
	# The confirm prompt fires AFTER the FF check so the operator only sees it
	# when a rewrite is genuinely about to happen.
	smarthome_publish__parse_args
	smarthome_publish__verify_repo
	smarthome_publish__prepare_build
	smarthome_publish__set_traps

	smarthome_publish__clone_into_build
	smarthome_publish__run_filter_repo
	smarthome_publish__leak_gate

	smarthome_publish__attach_public_remote
	smarthome_publish__verify_ff_or_abort
	$SMARTHOME_PUBLISH__REWRITE && smarthome_publish__confirm_rewrite

	smarthome_publish__push_branch
	smarthome_publish__push_tags

	echo -e "${GREEN:-}✓ published to ${SMARTHOME_PUBLISH__PUBLIC_URL} :: ${SMARTHOME_PUBLISH__BRANCH}${NC:-}"
}
########################################################################-########################################################################

smarthome_publish__run
