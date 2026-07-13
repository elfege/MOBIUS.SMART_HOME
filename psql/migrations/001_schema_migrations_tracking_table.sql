-- =============================================================================
-- 001_schema_migrations_tracking_table.sql
-- =============================================================================
-- The ledger: which migration files have been applied to THIS database.
--
-- WHY (2026-07-13): before this, nothing tracked applied migrations. The runner
-- had to either (a) re-run every file on every start and rely on all of them being
-- idempotent, or (b) swallow "already exists" errors — which is how a genuinely
-- broken migration hides. NVR's runner does (b) and even comments that it cannot
-- distinguish "already present" from "actually broken".
--
-- A ledger removes the guesswork: a file is applied EXACTLY ONCE, and a real
-- failure is loud because it cannot be mistaken for a re-run.
--
-- It also lets us BASELINE: an existing database (which already has the schema)
-- records 000_baseline as applied WITHOUT executing it — because pg_dump output is
-- deliberately NOT idempotent (bare CREATE TABLE) and must only ever build a
-- virgin database.
--
-- Lives in dscore (system/meta), per the dshub/dsapp/dscore split.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS dscore;

CREATE TABLE IF NOT EXISTS dscore.schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    baselined   BOOLEAN NOT NULL DEFAULT false   -- true = recorded, never executed
);

COMMENT ON TABLE dscore.schema_migrations IS
    'Applied-migration ledger. baselined=true means the file was recorded as '
    'already-present on a pre-existing database and deliberately NOT executed.';
