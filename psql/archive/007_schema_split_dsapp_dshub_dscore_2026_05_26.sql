-- Migration 007: split the single `public` schema into storage schemas
--   dshub  — hub substrate (devices, events, hub connectivity)
--   dsapp  — automation/consumer logic
--   dscore — cross-cutting system config
-- and expose all tables as 1:1 auto-updatable VIEWS in a single PostgREST-
-- facing schema `api` (Option B: ~0 application call-site churn).
--
-- Date: 2026-05-26.  Branch: db_schema_split_dsapp_dshub_dscore_05_26_2026_a.
-- Plan: docs/plans/postgres_schema_split_dsapp_dshub_dscore_2026_05_26.md
--
-- IDEMPOTENT + TRANSACTIONAL: re-running is a no-op (tables already moved →
-- `ALTER TABLE` guarded by an existence check; views/grants are CREATE OR
-- REPLACE / GRANT). `SET SCHEMA` is catalog-only (instant on 800k-row tables).
--
-- Cutover note: `upsert_device` is invoked via PostgREST `/rpc/upsert_device`,
-- so it MUST live in the exposed schema `api` (not dshub). Its body keeps
-- bare `devices` refs, resolved via `SET search_path = dshub, public`.

BEGIN;

CREATE SCHEMA IF NOT EXISTS dshub;
CREATE SCHEMA IF NOT EXISTS dsapp;
CREATE SCHEMA IF NOT EXISTS dscore;
CREATE SCHEMA IF NOT EXISTS api;

-- ---------------------------------------------------------------------------
-- 1. Move tables out of public into their storage schema (idempotent).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
  hub  text[] := ARRAY['devices','device_cache','device_hub_mapping',
                       'device_matter_map','hubitat_matter_devices','hub_config',
                       'hub_health','raw_events','event_log','location_modes',
                       'mode_change_log'];
  app  text[] := ARRAY['app_types','app_type_settings','app_instances',
                       'device_subscriptions','event_routings','device_commands',
                       'instance_state_log','instance_setting_exceptions',
                       'scheduled_jobs'];
  core text[] := ARRAY['system_settings','encrypted_secrets','system_boot_log'];
  t text;
BEGIN
  FOREACH t IN ARRAY hub LOOP
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='public' AND c.relname=t AND c.relkind='r') THEN
      EXECUTE format('ALTER TABLE public.%I SET SCHEMA dshub', t);
    END IF;
  END LOOP;
  FOREACH t IN ARRAY app LOOP
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='public' AND c.relname=t AND c.relkind='r') THEN
      EXECUTE format('ALTER TABLE public.%I SET SCHEMA dsapp', t);
    END IF;
  END LOOP;
  FOREACH t IN ARRAY core LOOP
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='public' AND c.relname=t AND c.relkind='r') THEN
      EXECUTE format('ALTER TABLE public.%I SET SCHEMA dscore', t);
    END IF;
  END LOOP;
END $$;

-- Move owned sequences alongside their tables (keeps the schema tidy; column
-- defaults reference the sequence by OID so they follow it automatically).
DO $$
DECLARE rec record;
BEGIN
  FOR rec IN
    SELECT s.relname AS seq, n.nspname AS tbl_schema
    FROM pg_class s
    JOIN pg_depend d  ON d.objid = s.oid AND d.deptype = 'a'
    JOIN pg_class tbl ON tbl.oid = d.refobjid
    JOIN pg_namespace n ON n.oid = tbl.relnamespace
    WHERE s.relkind = 'S'
      AND n.nspname IN ('dshub','dsapp','dscore')
      AND s.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname='public')
  LOOP
    EXECUTE format('ALTER SEQUENCE public.%I SET SCHEMA %I', rec.seq, rec.tbl_schema);
  END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Relocate upsert_device into the exposed `api` schema (PostgREST RPC).
--    Body unchanged; bare `devices` resolves via search_path.
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS public.upsert_device(text,text,text,text,text,text,jsonb,jsonb);
CREATE OR REPLACE FUNCTION api.upsert_device(
    p_hub_ip       TEXT,
    p_hubitat_id   TEXT,
    p_name         TEXT,
    p_label        TEXT,
    p_device_type  TEXT,
    p_protocol     TEXT,
    p_capabilities JSONB,
    p_attributes   JSONB
) RETURNS TABLE(device_id BIGINT, action TEXT)
LANGUAGE plpgsql
SET search_path = dshub, public
AS $$
DECLARE
    v_existing_id         BIGINT;
    v_existing_hub_ip     TEXT;
    v_existing_hubitat_id TEXT;
BEGIN
    SELECT d.id, d.hub_ip, d.hubitat_id
      INTO v_existing_id, v_existing_hub_ip, v_existing_hubitat_id
      FROM devices d
     WHERE d.label = p_label
     LIMIT 1;

    IF v_existing_id IS NULL THEN
        INSERT INTO devices
            (hub_ip, hubitat_id, name, label, device_type, protocol,
             capabilities, attributes, last_synced_at)
        VALUES
            (p_hub_ip, p_hubitat_id, p_name, p_label, p_device_type,
             p_protocol, p_capabilities, p_attributes, NOW())
        RETURNING id INTO v_existing_id;
        device_id := v_existing_id; action := 'INSERT'; RETURN NEXT;
    ELSIF v_existing_hub_ip = p_hub_ip AND v_existing_hubitat_id = p_hubitat_id THEN
        UPDATE devices SET
            name           = p_name,
            device_type    = p_device_type,
            protocol       = p_protocol,
            capabilities   = p_capabilities,
            attributes     = p_attributes,
            last_synced_at = NOW()
        WHERE id = v_existing_id;
        device_id := v_existing_id; action := 'UPDATE'; RETURN NEXT;
    ELSE
        device_id := v_existing_id; action := 'SKIP_MESH'; RETURN NEXT;
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- 3. Expose every storage table as a 1:1 view in `api` (the only schema
--    PostgREST serves). Application call sites (`/devices`, `/event_log`, …)
--    are unchanged.
-- ---------------------------------------------------------------------------
DO $$
DECLARE rec record;
BEGIN
  FOR rec IN
    SELECT n.nspname AS sch, c.relname AS t
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname IN ('dshub','dsapp','dscore') AND c.relkind='r'
  LOOP
    EXECUTE format('CREATE OR REPLACE VIEW api.%I AS SELECT * FROM %I.%I',
                   rec.t, rec.sch, rec.t);
  END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 4. Grants. Views are SECURITY INVOKER, so smarthome_anon needs privileges on
--    BOTH the api views AND the underlying storage tables (mirrors the prior
--    blanket public grant — Option B keeps privilege parity, separation is at
--    the storage-organization level).
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA dshub, dsapp, dscore, api TO smarthome_anon;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA dshub  TO smarthome_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA dsapp  TO smarthome_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA dscore TO smarthome_anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA api    TO smarthome_anon; -- views

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA dshub  TO smarthome_anon;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA dsapp  TO smarthome_anon;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA dscore TO smarthome_anon;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO smarthome_anon; -- safety: any seq left behind

GRANT EXECUTE ON FUNCTION api.upsert_device(text,text,text,text,text,text,jsonb,jsonb) TO smarthome_anon;

-- Future tables/sequences in these schemas inherit the grants.
ALTER DEFAULT PRIVILEGES IN SCHEMA dshub  GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO smarthome_anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA dsapp  GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO smarthome_anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA dscore GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO smarthome_anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA api    GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO smarthome_anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA dshub  GRANT USAGE,SELECT ON SEQUENCES TO smarthome_anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA dsapp  GRANT USAGE,SELECT ON SEQUENCES TO smarthome_anon;
ALTER DEFAULT PRIVILEGES IN SCHEMA dscore GRANT USAGE,SELECT ON SEQUENCES TO smarthome_anon;

-- PostgREST: pick up the new exposed schema + relations.
NOTIFY pgrst, 'reload schema';

COMMIT;
