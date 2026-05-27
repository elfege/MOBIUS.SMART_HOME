-- Migration 008: device dedup by (hub_ip,hubitat_id) + linkedDevice/primary rule
--
-- Replaces the fragile `UNIQUE(label)` + SKIP_MESH-by-label dedup with identity
-- on (hub_ip, hubitat_id). The native-vs-mirror split and the same-label
-- primary-hub-wins decision are computed in the classifier (it can see all hubs
-- at once); this function just upserts one row by its true identity and stores
-- the classifier's verdict (is_name_duplicate).
--
-- Also fixes a latent bug: the old upsert_device INSERT never set hub_id (NOT
-- NULL), so every NEW-device insert silently failed — only pre-existing rows
-- (UPDATE branch) ever persisted. New signature takes p_hub_id.
--
-- Date 2026-05-26. Branch device_to_hubs_classifier_admin_api_is_primary_linkeddevice_dedup_05_26_2026_a.

BEGIN;

-- 1. devices: retire label-uniqueness as the dedup mechanism; add the
--    name-duplicate flag (a native that lost a same-label conflict to the
--    primary hub — hidden from the picker, kept as a failover candidate).
ALTER TABLE dshub.devices DROP CONSTRAINT IF EXISTS devices_label_key;
ALTER TABLE dshub.devices ADD COLUMN IF NOT EXISTS is_name_duplicate BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS idx_devices_label ON dshub.devices(label);  -- non-unique, lookup speed
-- (UNIQUE(hub_ip,hubitat_id) already exists and is now the sole identity key.)

-- 2. upsert_device: identity = (hub_ip, hubitat_id); sets hub_id + is_name_duplicate.
--    No SKIP_MESH branch — mirrors are filtered upstream by linkedDevice and
--    never reach here as canonical rows.
DROP FUNCTION IF EXISTS api.upsert_device(text,text,text,text,text,text,jsonb,jsonb);
DROP FUNCTION IF EXISTS api.upsert_device(text,text,integer,text,text,text,text,jsonb,jsonb,boolean);
CREATE OR REPLACE FUNCTION api.upsert_device(
    p_hub_ip            TEXT,
    p_hubitat_id        TEXT,
    p_hub_id            INTEGER,
    p_name              TEXT,
    p_label             TEXT,
    p_device_type       TEXT,
    p_protocol          TEXT,
    p_capabilities      JSONB,
    p_attributes        JSONB,
    p_is_name_duplicate BOOLEAN
) RETURNS TABLE(device_id BIGINT, action TEXT)
LANGUAGE plpgsql
SET search_path = dshub, public
AS $$
DECLARE
    v_id BIGINT;
BEGIN
    SELECT id INTO v_id FROM devices
     WHERE hub_ip = p_hub_ip AND hubitat_id = p_hubitat_id;

    IF v_id IS NULL THEN
        INSERT INTO devices
            (hub_ip, hubitat_id, hub_id, name, label, device_type, protocol,
             capabilities, attributes, is_name_duplicate, last_synced_at)
        VALUES
            (p_hub_ip, p_hubitat_id, p_hub_id, p_name, p_label, p_device_type,
             p_protocol, p_capabilities, p_attributes, p_is_name_duplicate, NOW())
        RETURNING id INTO v_id;
        device_id := v_id; action := 'INSERT'; RETURN NEXT;
    ELSE
        UPDATE devices SET
            hub_id            = p_hub_id,
            name              = p_name,
            label             = p_label,
            device_type       = p_device_type,
            protocol          = p_protocol,
            capabilities      = p_capabilities,
            attributes        = p_attributes,
            is_name_duplicate = p_is_name_duplicate,
            last_synced_at    = NOW()
        WHERE id = v_id;
        device_id := v_id; action := 'UPDATE'; RETURN NEXT;
    END IF;
END;
$$;
GRANT EXECUTE ON FUNCTION api.upsert_device(text,text,integer,text,text,text,text,jsonb,jsonb,boolean) TO smarthome_anon;

-- 3. Refresh the exposed view so the new is_name_duplicate column is visible
--    through PostgREST (SELECT * views don't pick up new columns automatically).
CREATE OR REPLACE VIEW api.devices AS SELECT * FROM dshub.devices;

NOTIFY pgrst, 'reload schema';
COMMIT;
