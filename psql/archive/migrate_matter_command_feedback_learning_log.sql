-- ============================================================================
-- Matter command feedback — LEARNING LOG (operator directive 2026-07-11):
--   "Implement new learning capability (separate table) so we have a log of
--    when it worked, didn't work — TOTALLY USER-INPUT BASED."
--   + "idea: modal requires visual confirmation."
--
-- Every Matter test command attempt is INSERTed at send time (with what the
-- API/matter-server *claimed*), then the operator's visual verdict is patched
-- in from the modal ("It worked" / "It didn't"). Divergence between
-- api_success and operator_verdict is the learning signal — and `controller`
-- lets us compare worked-rates across the python-matter-server -> matterjs
-- migration.
--
-- POLICY COMPLIANCE (audit 2026-07-11, MSG-609):
--   P5: NO foreign keys, NO CASCADE — standalone append-only log; rows carry
--       denormalized identity (node_id, label) so they survive device removal.
--   P7: append-only lifecycle logging (only the verdict trio is ever updated).
-- Additive + idempotent, mirrors migrate_matter_removal_flow.sql.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS dshub.matter_command_feedback (
    id               BIGSERIAL PRIMARY KEY,
    node_id          INTEGER NOT NULL,
    endpoint_id      INTEGER NOT NULL DEFAULT 1,
    device_label     VARCHAR(255),
    command          VARCHAR(50) NOT NULL,          -- 'on' | 'off' | future: setLevel...
    controller       VARCHAR(60) NOT NULL DEFAULT 'python-matter-server',
    api_success      BOOLEAN NOT NULL,              -- what the backend/matter-server CLAIMED
    api_detail       TEXT,                          -- error detail / trace summary if any
    sent_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    operator_verdict VARCHAR(20) NOT NULL DEFAULT 'unverified'
                     CHECK (operator_verdict IN ('worked', 'failed', 'unverified')),
    verdict_at       TIMESTAMPTZ,
    verdict_by       VARCHAR(80),                   -- 'operator' (modal) | future: agent handle
    notes            TEXT
);
CREATE INDEX IF NOT EXISTS idx_matter_cmd_fb_node    ON dshub.matter_command_feedback(node_id, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_matter_cmd_fb_verdict ON dshub.matter_command_feedback(operator_verdict);
CREATE INDEX IF NOT EXISTS idx_matter_cmd_fb_ts      ON dshub.matter_command_feedback(sent_at);

-- Expose via the api schema (PostgREST). Reload PostgREST schema cache after.
CREATE OR REPLACE VIEW api.matter_command_feedback AS SELECT * FROM dshub.matter_command_feedback;
GRANT SELECT, INSERT, UPDATE, DELETE ON api.matter_command_feedback TO smarthome_api, smarthome_anon;
GRANT USAGE, SELECT ON SEQUENCE dshub.matter_command_feedback_id_seq TO smarthome_api, smarthome_anon;

COMMIT;
