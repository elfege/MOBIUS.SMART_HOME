#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# publish_to_public_mirror.sh
#
# Publishes the PROD branch (main) of this PRIVATE dev repo
# (MOBIUS.SMART_HOME-dev) to the PUBLIC mirror (MOBIUS.SMART_HOME),
# with operator-private / sensitive paths FILTERED OUT of the published
# history — BSL-1.1 licensed.
#
# DESIGN (differs from NVR's in-place scrub on purpose):
#   - Works on a TEMP CLONE of `main`. The live dev repo is NEVER rewritten
#     or force-pushed. dev keeps its full history + all feature branches;
#     only the filtered `main` snapshot-with-history goes public.
#   - Uses `git filter-repo --invert-paths` so the excluded paths are absent
#     from EVERY commit of the published history (not just the tip) — a plain
#     mirror or .gitignore would still leak already-committed files.
#   - Publishes `main` + version tags only (public = prod). Feature/WIP
#     branches stay private.
#
# SAFETY:
#   - DRY-RUN by default. Pass --force to actually push to the public repo.
#   - Public push is a PUBLIC DISCLOSURE. Patent note: this project has a
#     provisional (docs/patent/, itself excluded); BSL restricts USE, it does
#     NOT undo disclosure. Confirm the patent posture before --force.
#   - Verifies the LICENSE survives and every excluded path is gone before
#     pushing.
#
# Requires: git-filter-repo, gh (for repo existence check), a clean `main`.
# ----------------------------------------------------------------------------
set -euo pipefail

PUBLIC_URL="https://github.com/elfege/MOBIUS.SMART_HOME.git"
BRANCH="main"
DRY_RUN=1
[[ "${1:-}" == "--force" ]] && DRY_RUN=0

# Paths stripped from the public history. Most (docs/patent, docs/plans) are
# already untracked, so filter-repo no-ops them — listed anyway so a future
# accidental commit can't leak them. README_*.md / docs/history / docs/*.html
# ARE tracked and WILL be removed from the published history.
EXCLUDE_PATHS=(
    docs/README_handoff.md
    docs/README_project_history.md
    docs/README_daily_standup_pitch.md
    docs/history
    docs/patent
    docs/plans
    docs/teachings
    backups
    chat.md
)
EXCLUDE_GLOBS=( 'docs/*.md' )   # catch other operator markdown under docs/

REPO_ROOT="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)"
cd "$REPO_ROOT"

echo "=============================================================="
echo " publish_to_public_mirror  ($([ "$DRY_RUN" -eq 1 ] && echo DRY-RUN || echo '!!! LIVE PUSH !!!'))"
echo " source : $(git remote get-url origin)  branch=$BRANCH"
echo " target : $PUBLIC_URL"
echo " license: BSL-1.1 (LICENSE in tree)"
echo " exclude: ${EXCLUDE_PATHS[*]} ${EXCLUDE_GLOBS[*]}"
echo "=============================================================="

# LICENSE must exist on main (it travels into the public tree).
git show "$BRANCH:LICENSE" >/dev/null 2>&1 || { echo "ERROR: LICENSE not committed on $BRANCH — add it first."; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "[1/5] Temp clone of $BRANCH (live dev repo untouched) → $TMP/pub"
git clone --quiet --branch "$BRANCH" --single-branch --no-local "$REPO_ROOT" "$TMP/pub"
cd "$TMP/pub"

echo "[2/5] Filtering sensitive paths from the published history"
FILTER_ARGS=()
for p in "${EXCLUDE_PATHS[@]}"; do FILTER_ARGS+=(--path "$p"); done
for g in "${EXCLUDE_GLOBS[@]}"; do FILTER_ARGS+=(--path-glob "$g"); done
git filter-repo --invert-paths "${FILTER_ARGS[@]}" --force

echo "[3/5] Verifying filtered tree"
for p in "${EXCLUDE_PATHS[@]}"; do
    if git ls-tree -r "$BRANCH" --name-only | grep -qF "$p"; then
        echo "  !! $p STILL PRESENT — aborting"; exit 1
    fi
done
git ls-tree -r "$BRANCH" --name-only | grep -q '^LICENSE$' || { echo "  !! LICENSE missing from filtered tree — aborting"; exit 1; }
echo "  ok: excluded paths gone, LICENSE present ($(git ls-tree -r "$BRANCH" --name-only | wc -l) files)"

echo "[4/5] Push to public"
git remote add public "$PUBLIC_URL"
if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  [dry-run] would: git push public --force $BRANCH:$BRANCH && git push public --force --tags"
    echo "  [dry-run] re-run with --force to publish. PUBLIC DISCLOSURE — confirm patent posture first."
else
    git push public --force "$BRANCH:$BRANCH"
    git push public --force --tags
    echo "  pushed filtered $BRANCH + tags to $PUBLIC_URL"
fi

echo "[5/5] Done ($([ "$DRY_RUN" -eq 1 ] && echo 'dry-run — nothing pushed' || echo 'published'))."
