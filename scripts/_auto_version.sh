#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
# _auto_version.sh — main-branch version-tagging discipline
#
# Idempotent helper invoked from post-merge AND post-commit hooks.
# Runs ONLY when HEAD is on `main`. On any other branch it exits 0
# silently so feature branches aren't tagged.
#
# Source of truth: ./.tag (gitignored, untracked, one-line version).
# Mirror in git: annotated tag `v<version>` at the commit that took
# ./.tag to that value.
#
# Behaviour on a main-branch commit/merge:
#   1. Find latest `v*.*.*` git tag.
#   2. Scan commits between that tag and HEAD. Infer bump level:
#        - major  → any commit subject contains `!:` or body has `BREAKING CHANGE`
#        - minor  → any commit subject starts with `feat:` / `feat(...)`, OR
#                   any new `psql/*.sql` was added since the tag
#        - patch  → anything else
#   3. expected = bump(latest_tag, level).
#   4. If ./.tag < expected → overwrite ./.tag with expected. If the
#      operator pre-edited ./.tag to a HIGHER value, that wins (the
#      script never downgrades). Manual override path.
#   5. Create annotated tag `v$(cat ./.tag)` at HEAD if missing.
#
# Operator manual override:
#   - Edit ./.tag to e.g. `2.0.0` before merging. The hook will respect
#     it (still tags HEAD as v2.0.0) because expected (e.g. v1.0.1)
#     is lower and the script never downgrades ./.tag.
#
# First-run seed:
#   - When no `v*.*.*` tag exists yet, the script writes ./.tag = 3.3.11
#     (if missing) and tags HEAD as v3.3.11. The 3.3.11 anchor matches
#     the existing tag history on this branch (set on main 2026-05-26)
#     and is the same seed value used across the MOBIUS portfolio for
#     consistency. Subsequent merges follow the normal bump flow.
# ─────────────────────────────────────────────────────────────────────

set -e

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
TAG_FILE="$REPO_ROOT/.tag"
SEED_VERSION="3.3.11"

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
[ "$branch" = "main" ] || exit 0

# --- helpers ---------------------------------------------------------

parse_version() {
    # "v1.0.0" or "1.0.0" → "1 0 0"
    echo "$1" | sed 's/^v//' | awk -F. '{print $1, $2, $3}'
}

bump_version() {
    # bump_version <version> <level: major|minor|patch>
    local v="$1" level="$2" maj min pat
    read -r maj min pat <<< "$(parse_version "$v")"
    case "$level" in
        major) echo "$((maj+1)).0.0" ;;
        minor) echo "$maj.$((min+1)).0" ;;
        patch) echo "$maj.$min.$((pat+1))" ;;
    esac
}

semver_gt() {
    # 0 (true) if $1 > $2; 1 otherwise.
    local a_maj a_min a_pat b_maj b_min b_pat
    read -r a_maj a_min a_pat <<< "$(parse_version "$1")"
    read -r b_maj b_min b_pat <<< "$(parse_version "$2")"
    [ "$a_maj" -gt "$b_maj" ] && return 0
    [ "$a_maj" -lt "$b_maj" ] && return 1
    [ "$a_min" -gt "$b_min" ] && return 0
    [ "$a_min" -lt "$b_min" ] && return 1
    [ "$a_pat" -gt "$b_pat" ] && return 0
    return 1
}

# --- main logic ------------------------------------------------------

latest_tag="$(git tag -l 'v*.*.*' | sort -V | tail -1)"
latest_version="${latest_tag#v}"

# First-run seed path: no version tag exists yet anywhere.
if [ -z "$latest_version" ]; then
    if [ ! -f "$TAG_FILE" ]; then
        echo "$SEED_VERSION" > "$TAG_FILE"
    fi
    current="$(tr -d '[:space:]' < "$TAG_FILE")"
    [ -z "$current" ] && { echo "$SEED_VERSION" > "$TAG_FILE"; current="$SEED_VERSION"; }
    if ! git rev-parse "v$current" >/dev/null 2>&1; then
        git tag -a "v$current" -m "Version $current (initial seed)"
        echo "[auto-version] seeded ./.tag=$current and tagged HEAD as v$current"
    fi
    exit 0
fi

# Commit messages and added files since latest tag.
msgs="$(git log "$latest_tag..HEAD" --format='%s%n%b' 2>/dev/null || true)"
added_files="$(git log "$latest_tag..HEAD" --diff-filter=A --name-only --format='' 2>/dev/null || true)"

level="patch"
if printf '%s\n' "$msgs" | grep -qE '(^|[^-])!:|BREAKING CHANGE'; then
    level="major"
elif printf '%s\n' "$msgs" | grep -qE '^feat(\(|:)'; then
    level="minor"
elif printf '%s\n' "$added_files" | grep -qE '^psql/.*\.sql$'; then
    level="minor"
fi

expected="$(bump_version "$latest_version" "$level")"

if [ ! -f "$TAG_FILE" ]; then
    echo "$latest_version" > "$TAG_FILE"
fi
current="$(tr -d '[:space:]' < "$TAG_FILE")"
[ -z "$current" ] && current="$latest_version"

# Forward-only update: write expected only if it's higher than current.
if semver_gt "$expected" "$current"; then
    echo "$expected" > "$TAG_FILE"
    echo "[auto-version] ./.tag: $current -> $expected (inferred bump: $level)"
    current="$expected"
fi

# Tag HEAD if no tag at this version yet.
if ! git rev-parse "v$current" >/dev/null 2>&1; then
    git tag -a "v$current" -m "Version $current"
    echo "[auto-version] tagged HEAD as v$current"
fi
