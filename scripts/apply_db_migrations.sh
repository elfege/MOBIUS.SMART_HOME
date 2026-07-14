#!/bin/bash
# =============================================================================
# scripts/apply_db_migrations.sh — the ONE migration runner (canonical SQL.1)
# =============================================================================
# Applies psql/migrations/*.sql in numeric order, EXACTLY ONCE each, tracked in
# dscore.schema_migrations. Used by start.sh (live database). The fresh-Postgres
# path (psql/02-apply-migrations.sh) does the same thing at initdb time.
#
# HISTORY (why this exists): until 2026-07-13 MOBIUS.SMART_HOME had NO runner.
# ~93 DDL statements ran as PYTHON STRINGS from app.py at every boot
# (schema-as-code, unversioned — forbidden by SQL.1), every failure was swallowed
# as a logger.warning, and the .sql files in psql/ were dead documentation that
# nothing executed. They also did not work: init-db.sql died after 3 tables on a
# virgin database. See psql/archive/README.md.
#
# BASELINING: 000_baseline is a pg_dump of the live schema and is deliberately NOT
# idempotent (bare CREATE TABLE) — it may only ever build a VIRGIN database. On a
# database that already has the schema, it is RECORDED as applied and skipped.
# This is the standard baseline pattern, and it is exactly where server-intercom
# got burned ("baseline ONLY what the live DB actually has").
#
# Usage: apply_db_migrations.sh <postgres_container> <user> <db> [migrations_dir]
# =============================================================================
set -uo pipefail

CONTAINER="${1:-smarthome-postgres}"
PGUSER="${2:-smarthome_api}"
PGDB="${3:-smarthome}"
MIG_DIR="${4:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/psql/migrations}"

RED=$'\033[38;5;1m'; GRN=$'\033[38;5;2m'; DIM=$'\033[2m'; NC=$'\033[0m'

psql_q() { docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDB" -tAq "$@"; }

[[ -d "$MIG_DIR" ]] || { echo "no migrations dir at $MIG_DIR"; exit 0; }

# Wait for Postgres to actually accept connections.
for _ in $(seq 1 30); do
    docker exec "$CONTAINER" pg_isready -U "$PGUSER" -d "$PGDB" >/dev/null 2>&1 && break
    sleep 2
done
if ! docker exec "$CONTAINER" pg_isready -U "$PGUSER" -d "$PGDB" >/dev/null 2>&1; then
    echo "${RED}✗ Postgres ($CONTAINER) not ready — migrations SKIPPED${NC}"; exit 1
fi

# 1) The ledger must exist before we can consult it.
LEDGER="$MIG_DIR/001_schema_migrations_tracking_table.sql"
[[ -f "$LEDGER" ]] && docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -U "$PGUSER" -d "$PGDB" -q < "$LEDGER" >/dev/null 2>&1

# 2) BASELINE an existing database: if the ledger is empty but the schema is
#    already here, record 000 as applied WITHOUT running it (pg_dump output would
#    fail on bare CREATE TABLE against existing tables).
applied_count="$(psql_q -c "SELECT COUNT(*) FROM dscore.schema_migrations;" 2>/dev/null || echo 0)"
schema_present="$(psql_q -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='dshub' AND table_name='devices';" 2>/dev/null || echo 0)"
if [[ "${applied_count:-0}" -eq 0 && "${schema_present:-0}" -gt 0 ]]; then
    for _b in "$MIG_DIR"/000_*.sql; do
        [[ -f "$_b" ]] || continue
        psql_q -c "INSERT INTO dscore.schema_migrations (filename, baselined) VALUES ('$(basename "$_b")', true) ON CONFLICT DO NOTHING;" >/dev/null 2>&1
        echo "${DIM}  · $(basename "$_b") — BASELINED (schema already present; not executed)${NC}"
    done
fi

# 3) Apply anything not yet in the ledger, in order.
ok=0; skip=0; bad=0
for mig in $(ls "$MIG_DIR"/*.sql 2>/dev/null | sort); do
    name="$(basename "$mig")"
    seen="$(psql_q -c "SELECT COUNT(*) FROM dscore.schema_migrations WHERE filename='$name';" 2>/dev/null || echo 0)"
    if [[ "${seen:-0}" -gt 0 ]]; then
        skip=$((skip+1)); continue
    fi
    # -1: each migration is ONE transaction — a failure leaves NOTHING behind.
    # Without it, psql commits statement-by-statement, so a failed file was
    # half-applied and every later attempt (or the error re-run below, now
    # removed) collided with its own debris ("schema api already exists" while
    # the REAL first error was dscore — CI run 1's misleading failure).
    # Stderr is captured from the ONE attempt; the old path RE-EXECUTED the
    # migration just to print the error — a double-apply that masked the true
    # failure behind its own side effects.
    # Success = EXIT CODE, never stderr-emptiness: NOTICEs ("schema exists,
    # skipping") also land on stderr and must not fail a good migration.
    err="$(docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -1 -U "$PGUSER" -d "$PGDB" -q < "$mig" 2>&1 >/dev/null)"; rc=$?
    if [[ $rc -eq 0 ]]; then
        psql_q -c "INSERT INTO dscore.schema_migrations (filename) VALUES ('$name') ON CONFLICT DO NOTHING;" >/dev/null 2>&1
        echo "${GRN}  ✓ $name${NC}"; ok=$((ok+1))
    else
        # Loud. The old app.py path logged failures as warnings — that is precisely
        # how the schema drifted for months without anyone noticing.
        echo "${RED}  ✗ $name FAILED${NC}"
        printf '%s\n' "$err" | grep -i error | head -3 | sed 's/^/      /'
        bad=$((bad+1))
    fi
done

if [[ $bad -gt 0 ]]; then
    echo "${RED}✗ Migrations: $ok applied, $skip already-applied, $bad FAILED${NC}"; exit 1
fi
echo "${GRN}✓ Migrations: $ok applied, $skip already-applied${NC}"
