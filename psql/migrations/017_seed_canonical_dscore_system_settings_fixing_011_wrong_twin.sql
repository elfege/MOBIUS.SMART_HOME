-- =============================================================================
-- Migration 017 — seed the CANONICAL system_settings table (dscore), fixing 011
-- =============================================================================
-- CI run 2 (2026-07-14) proved a from-scratch database serves ZERO system
-- settings through PostgREST. Root cause: system_settings exists as TWO twin
-- tables — dscore.system_settings (the CANONICAL one: the api view reads it,
-- the UI writes it, live rows updated through 2026-07-12) and a STALE
-- dshub.system_settings (untouched since 2026-06-19). Migration 011's
-- unqualified INSERT resolved via its dshub-first search_path into the WRONG
-- (stale) twin, and its matter_primary_enabled line was even explicitly
-- qualified dshub. On the live database this was invisible — dscore was
-- already populated by years of app-boot seeding. On a virgin build, dscore
-- stayed EMPTY and every settings read through the API returned nothing.
--
-- This migration re-issues the FULL canonical seed set INTO dscore, explicitly
-- qualified. ON CONFLICT DO NOTHING => provable no-op on the live database
-- (all keys present) and the missing system floor on fresh installs.
-- The stale dshub twin is deliberately NOT dropped here (additive-only per
-- house data policy); its retirement is a separate, explicit decision.
-- =============================================================================

INSERT INTO dscore.system_settings (key, value, value_type, description, ui_exposed, requires_restart) VALUES
          ('motion_timeout_floor_seconds', '60', 'int',
           'Minimum no-motion timeout in seconds. AML/Fan clamp computed timeouts to this floor unless the instance has bypassTimeoutFloor=true.',
           TRUE, FALSE),
          ('reconcile_interval_secs', '60', 'int', 'Normal reconcile-poll cadence.', TRUE, FALSE),
          ('reconcile_aggressive_secs', '10', 'int', 'Aggressive reconcile cadence after recent hub WS failure.', TRUE, FALSE),
          ('reconcile_aggressive_window_secs', '300', 'int', 'How recently a hub WS failure must have occurred to engage aggressive reconcile.', TRUE, FALSE),
          ('eventsocket_watchdog_secs', '120', 'int', 'Recycle WS connection if no events arrive within this window.', TRUE, FALSE),
          ('device_cmd_verify_retries', '3', 'int', 'Polls per command-send attempt to verify state.', TRUE, FALSE),
          ('device_cmd_verify_delay', '1.0', 'float', 'Seconds between verify polls.', TRUE, FALSE),
          ('device_cmd_operation_retries', '2', 'int', 'Full send+verify cycles before giving up.', TRUE, FALSE),
          ('aml_init_master_delay_seconds', '5', 'int', 'AML initialize() schedules its first master() run after this many seconds. Short delay lets in-flight motion events arrive first.', TRUE, FALSE),
          ('aml_periodic_eval_interval_seconds', '60', 'int', 'Defensive: every AML instance runs master() at this cadence regardless of events. Minimum 10s.', TRUE, FALSE),
          ('timezone', 'America/New_York', 'string', 'IANA timezone name. Hub-derived: refreshed hourly from /location/list/data on every enabled hub. UI editing is advisory — the next refresh cycle overrides. DB stays in UTC; this is applied to the app container at boot for log timestamps.', TRUE, FALSE),
          ('hub_tz_inconsistency', 'false', 'bool', 'Set to TRUE by the hub-TZ refresher when enabled hubs report disagreeing time zones. Dashboard surfaces this as a warning so the user can fix the outlier from the Hubitat UI.', TRUE, FALSE),
          ('hub_tz_breakdown', '{}', 'string', 'JSON object {hub_name: tz_or_status} from the most recent hub-TZ refresh. Populated by services.hub_tz_resolver. Values are Windows-style TZ strings, "unreachable", or "unmapped:<tz>".', TRUE, FALSE),
          ('colorblind_mode', 'false', 'bool', 'Use a colorblind-safe (Okabe-Ito) palette in charts and accent colors. Designed for protanopia / deuteranopia / tritanopia.', TRUE, FALSE),
          ('eventsocket_enabled', 'true', 'bool', 'Master switch for Hubitat eventsocket WS intake. Requires app restart.', TRUE, TRUE),
          ('reconcile_poll_enabled', 'true', 'bool', 'Reconcile poll on/off. Requires app restart.', TRUE, TRUE),
          ('device_commands_logging', 'true', 'bool', 'Two-phase device_commands logging. Requires app restart.', TRUE, TRUE),
          ('webhook_intake_enabled', 'false', 'bool', 'Legacy webhook intake — rollback escape hatch.', TRUE, TRUE),
          ('maker_api_enabled', 'false', 'bool', 'When TRUE: reconcile poll + commands + verify use Maker API (legacy path). When FALSE (default 2026-05-17): all three use the Hubitat admin API directly — bypasses Maker entirely. Toggle on /hubs page. Eventsocket WS handles inbound events regardless.', TRUE, FALSE)
ON CONFLICT (key) DO NOTHING;

INSERT INTO dscore.system_settings (key, value, value_type, description, ui_exposed, requires_restart)
VALUES ('matter_primary_enabled', 'false', 'bool', 'When TRUE: commands for devices commissioned to our matter-server (and online) go DIRECTLY over Matter (faster + independent of Hubitat''s Matter bridge); Hubitat is the fallback. Default false.', true, false)
ON CONFLICT (key) DO NOTHING;
