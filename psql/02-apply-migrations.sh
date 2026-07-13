#!/bin/bash
# =============================================================================
# psql/02-apply-migrations.sh — migration runner for a FRESH Postgres init.
# =============================================================================
# Postgres runs everything in /docker-entrypoint-initdb.d/ in alphabetical order,
# but ONLY on the first initialization of an empty data directory. This script is
# that path: it applies psql/migrations/*.sql in numeric order to build the schema
# from zero.
#
# For an ALREADY-initialized database (the normal case), start.sh re-applies the
# same files idempotently against the running container — see start.sh.
#
# WHY THIS EXISTS (2026-07-13): until now there was NO runner at all. The schema
# was created by ~93 DDL statements executed as PYTHON STRINGS from app.py at every
# boot (schema-as-code, unversioned — forbidden by canonical SQL.1), and the .sql
# files in psql/ were dead documentation that nothing ever ran. Worse, they did not
# work: init-db.sql died after 3 tables on a fresh database. See psql/archive/README.md.
#
# Adapted from the reference implementation: MOBIUS.NVR psql/02-apply-migrations.sh.
# =============================================================================
set -e

MIGRATIONS_DIR="/docker-entrypoint-initdb.d/migrations"

if [[ ! -d "$MIGRATIONS_DIR" ]]; then
    echo "[init] No migrations directory at $MIGRATIONS_DIR — skipping."
    exit 0
fi

echo "[init] Applying migrations from $MIGRATIONS_DIR ..."
applied=0
failed=0
applied_names=()
for mig in $(ls "$MIGRATIONS_DIR"/*.sql 2>/dev/null | sort); do
    name="$(basename "$mig")"
    echo "[init]   -> $name"
    # ON_ERROR_STOP keeps each FILE atomic. Unlike the live path below, a failure
    # on a FRESH init is a genuine bug (the chain is supposed to build from zero,
    # and that is now verified), so we surface it loudly rather than swallow it —
    # the old app.py loop caught every exception and logged a mere warning, which
    # is precisely how the schema drifted unnoticed for months.
    if psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
            -f "$mig" >/dev/null 2>&1; then
        applied=$((applied + 1))
        applied_names+=("$name")
    else
        echo "[init]      !! FAILED — re-running to show the error:"
        psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
             -f "$mig" 2>&1 | grep -i error | head -3 | sed 's/^/[init]      /'
        failed=$((failed + 1))
    fi
done

# Record EVERY applied file in the ledger. This MUST happen after the loop: 001
# is what CREATES dscore.schema_migrations, so 000 (which runs before it) cannot be
# recorded inline. Missing that would leave 000 unrecorded, and start.sh's runner
# would then try to re-apply a pg_dump baseline against a populated database and
# fail. (Caught by the fresh-build test, 2026-07-13.)
for name in "${applied_names[@]:-}"; do
    [[ -n "$name" ]] || continue
    psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -q -c \
        "INSERT INTO dscore.schema_migrations (filename) VALUES ('$name') ON CONFLICT DO NOTHING;" \
        >/dev/null 2>&1 || true
done

echo "[init] Migrations done: $applied applied, $failed failed."
[[ $failed -gt 0 ]] && exit 1
exit 0
