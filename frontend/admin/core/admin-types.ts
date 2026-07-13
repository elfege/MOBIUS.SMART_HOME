/**
 * admin/core/admin-types.ts — backend-contract shapes the ADMIN app consumes.
 *
 * These mirror the live endpoints exactly (verified against the running app,
 * 2026-07-13): GET /api/instances returns app_instances rows via PostgREST;
 * GET /api/app-types returns the app_types catalog. Admin-only shapes live
 * here, NOT in frontend/shared/types.ts, so the tiles app is never coupled to
 * them (parallel-agent methodology: each app owns its endpoint shapes).
 */

/** One automation instance — a row of app_instances as GET /api/instances
 *  returns it. `settings`/`device_selections`/`memoization_state` are the raw
 *  JSONB payloads; the proto renders summary state only and treats them as
 *  opaque (the settings editor consumes them in a later milestone). */
export interface InstanceRow {
  id: number;
  instance_uuid: string;
  app_type_id: number;
  label: string;
  is_enabled: boolean;
  is_paused: boolean;
  pause_reason: string | null;
  pause_expires_at: string | null;
  error_count: number;
  last_error: string | null;
  last_activity_at: string | null;
  created_at: string;
  updated_at: string;
  settings: Record<string, unknown>;
  device_selections: Record<string, unknown>;
  memoization_state: Record<string, unknown> | null;
}

/** One app blueprint — a row of app_types as GET /api/app-types returns it.
 *  `settings_schema` is the JSON-schema blob; opaque to the proto. */
export interface AppType {
  id: number;
  type_name: string;
  display_name: string;
  description: string | null;
  version: string | null;
  settings_schema: Record<string, unknown>;
}

/** The instance lifecycle state the UI presents. Derived, in priority order:
 *  disabled (is_enabled=false) > paused (is_paused) > running. `error_count`
 *  is surfaced separately — an instance can be running AND erroring. */
export type InstanceState = 'running' | 'paused' | 'disabled';

/** Derive the presented lifecycle state from a row (single source of the
 *  priority rule above — components must not re-derive it). */
export function instanceState(row: InstanceRow): InstanceState {
  if (!row.is_enabled) return 'disabled';
  if (row.is_paused) return 'paused';
  return 'running';
}
