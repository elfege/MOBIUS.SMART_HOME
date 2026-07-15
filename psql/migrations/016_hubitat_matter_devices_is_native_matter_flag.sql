-- =============================================================================
-- Migration 016 — is_native_matter flag on hubitat_matter_devices
-- =============================================================================
-- WHY (operator MSG-1035, field C): the Matter UI needs to distinguish devices
-- we commissioned DIRECTLY into Mobius's own Matter controller via factory
-- code / QR — where WE are the primary admin — from devices merely ADOPTED by
-- Hubitat's Matter bridge and re-discovered by us. The two look identical in the
-- node list but mean very different things for decommission / fabric ownership.
--
-- Operator note at request time: "none exist yet." So the column is a DB-backed,
-- forward-compatible contract that defaults FALSE (correct today — every row is
-- Hubitat-adopted) and is flipped TRUE by the NATIVE COMMISSIONING path when it
-- lands (assistant's Matter RN sub-plan, P3a). Deriving it from "node has no
-- Hubitat match" was rejected: that heuristic conflates a native device with an
-- ORPHANED mapping, and a wrong ownership signal on the decommission button is
-- dangerous. A real column, written by the code that actually knows, is the only
-- honest source (house rule: per-field state = a DB row, never a guessed flag).
--
-- Surfaced by GET /api/matter/hubitat-devices (as is_native_matter) and enriched
-- onto GET /api/matter/nodes (as _is_native_matter). The forward-compatible UI
-- lights up its .native-matter styling automatically once a row reads true.
--
-- Idempotent + additive: ADD COLUMN IF NOT EXISTS with a constant DEFAULT is a
-- fast, non-rewriting change on PG 11+ and safe to re-run.
-- =============================================================================

-- The runner applies each file as-is with no ambient search_path, so (like
-- migration 010) we set it here — hubitat_matter_devices lives in dshub.
SET search_path = dshub, dsapp, dscore, public;

ALTER TABLE hubitat_matter_devices
    ADD COLUMN IF NOT EXISTS is_native_matter BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN hubitat_matter_devices.is_native_matter IS
    'TRUE = commissioned directly into Mobius''s Matter controller (we are the '
    'primary admin), FALSE = Hubitat-adopted and re-discovered. Set TRUE only by '
    'the native commissioning path. Operator MSG-1035 field C, migration 016.';
