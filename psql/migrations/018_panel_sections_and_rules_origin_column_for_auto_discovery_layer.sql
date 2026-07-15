-- =============================================================================
-- Migration 018 — `origin` on panel_sections + panel_section_rules
-- =============================================================================
-- Foundation for the automatic room-discovery sectionizer (assistant-3 design,
-- Architect ratified 2026-07-14: docs/plans/automatic_room_discovery_...md §4).
--
-- Discovery must distinguish ITS OWN rows (regenerable, replaced on every
-- re-run) from operator-authored ones (sacred). `origin='auto'` marks the
-- disposable auto-layer; re-runs EXPLICITLY delete only those rows (no ON
-- DELETE CASCADE anywhere, per the 2026-07-11 hard policy) and re-insert.
-- Everything existing today is operator/seed material, hence the DEFAULT.
-- panel_device_affinities deliberately gets NO origin column: discovery is
-- forbidden from writing affinities at all (design invariant, enforced by the
-- absence of any code path).

ALTER TABLE dsapp.panel_sections
    ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'operator'
        CHECK (origin IN ('operator', 'auto'));

ALTER TABLE dsapp.panel_section_rules
    ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'operator'
        CHECK (origin IN ('operator', 'auto'));

-- Re-runs delete by origin; keep that scan cheap.
CREATE INDEX IF NOT EXISTS idx_panel_section_rules_origin
    ON dsapp.panel_section_rules (origin);
