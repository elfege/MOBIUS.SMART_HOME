"""
0_MOBIUS.SMART_HOME FastAPI Application

Main entry point for the smart home automation system.
Provides REST API for instance management, device access, and webhook handling.
Serves Jinja2 templates for the web UI.
"""

import os
import asyncio
import logging
import traceback
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, Query, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# Defensive HTTP wrapper: offloads blocking `requests.*` calls onto a worker
# thread so an unresponsive PostgREST / Hubitat hub can't hang the event loop.
# Used by every FastAPI route below that talks to PostgREST. See
# services/http_sync_offload.py for the rationale and the 2026-05-27 incident.
from services.http_sync_offload import aget, apost, apatch, adelete
from services.supervised_tasks import supervised_spawn

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# =============================================================================
# Apply user-configured timezone BEFORE logging is configured, so every log
# line uses the user's local time. Reads system_settings.timezone via a
# direct psycopg2 connection (the resolver isn't initialized yet at this
# point in module load). Falls back to UTC silently if anything fails.
# =============================================================================
def _apply_user_timezone():
    try:
        import time as _t
        import psycopg2  # noqa: WPS433
        conn = psycopg2.connect(
            host=os.environ.get('POSTGRES_HOST', 'postgres'),
            port=os.environ.get('POSTGRES_PORT', '5432'),
            dbname=os.environ.get('POSTGRES_DB', 'smarthome'),
            user=os.environ.get('POSTGRES_USER', 'smarthome_api'),
            password=os.environ.get('POSTGRES_PASSWORD', ''),
            connect_timeout=3,
        )
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT value FROM system_settings WHERE key = %s",
                ('timezone',),
            )
            row = cur.fetchone()
            if row and row[0]:
                os.environ['TZ'] = row[0]
                _t.tzset()
            cur.close()
        finally:
            conn.close()
    except Exception:
        # Boot-time best-effort. If DB isn't reachable yet, app continues
        # in UTC and run_db_migrations will create the row on first run.
        pass


_apply_user_timezone()


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Live-logs UI tap (2026-07-12): ring-buffer handler on the ROOT logger feeds
# the navbar "Logs" modal (GET /api/logs/tail / /api/logs/sources). Installed
# immediately after basicConfig so every subsequent logger is captured.
from services.log_stream import install_ring_log_handler, get_log_handler  # noqa: E402
install_ring_log_handler()


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class CreateInstanceRequest(BaseModel):
    """Request body for creating a new automation instance."""
    app_type: str
    label: str
    device_selections: dict
    settings: dict = {}


class UpdateInstanceRequest(BaseModel):
    """Request body for updating an existing automation instance."""
    label: Optional[str] = None
    device_selections: Optional[dict] = None
    settings: Optional[dict] = None


class PauseInstanceRequest(BaseModel):
    """Request body for pausing an instance.

    Universal pause contract (2026-06-16): the dashboard sends either
    ``duration_minutes`` (legacy / Minutes unit) or ``duration_seconds``
    (Seconds unit for sub-minute pauses). The server converts to minutes
    for the existing instance_manager.pause_instance path. 0 in EITHER
    field means indefinite (no auto-resume).
    """
    duration_minutes: Optional[int] = None
    duration_seconds: Optional[int] = None
    reason: Optional[str] = None


class DeviceCommandRequest(BaseModel):
    """Request body for sending a command to a device."""
    command: str
    args: Optional[list] = None


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def initialize_services():
    """Initialize all services on startup."""
    logger.info("Initializing services...")

    # Initialize app registry
    from apps.app_registry import initialize_registry
    from services.instance_manager import get_instance_manager

    instance_manager = get_instance_manager()
    initialize_registry(instance_manager)

    # Initialize scheduler
    from services.scheduler_service import get_scheduler
    get_scheduler()

    # Load all instances
    instance_manager.initialize_all_instances()

    logger.info("Services initialized")


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------


def run_db_migrations():
    """
    DEPRECATED — intentionally a no-op. Schema is VERSIONED, not application code.

    Until 2026-07-13 this function executed ~93 DDL statements as PYTHON STRINGS
    against the database at every application boot. That is schema-as-code: it is
    unversioned, it cannot be reviewed as a diff, it silently swallowed every
    failure as a `logger.warning` ("DB migration skipped: ..."), and it meant the
    .sql files in psql/ were dead documentation that nothing ever executed.
    Canonical SQL.1 forbids it: "Schema must NOT live as a string executed by your
    app at startup."

    The DDL now lives in psql/migrations/ as numbered, idempotent files, applied by
    start.sh (live database) and psql/02-apply-migrations.sh (fresh Postgres init):

        000_baseline_schema_dumped_from_live_database_2026_07_13.sql
        010_extract_boot_ddl_from_app_py_schema_as_code_removal.sql   <- this function's body
        011_seed_and_backfill_from_app_py_boot.sql

    Schema changes therefore require ./start.sh (or ./deploy.sh), NOT merely a
    `docker restart smarthome-app` — which is the correct separation: application
    code and database schema have different lifecycles.

    Kept as a no-op (rather than deleted) so any stale caller fails loudly in review
    instead of silently resurrecting boot-time DDL.
    """
    logger.info(
        "Schema is managed by psql/migrations/ (canonical SQL.1); "
        "applied by start.sh / 02-apply-migrations.sh — app.py carries no DDL."
    )


def _wait_for_postgrest(timeout_s: float = 120.0, interval_s: float = 2.0) -> bool:
    """
    Block until PostgREST answers, or until timeout.

    On a host reboot the app container can come up BEFORE postgrest is ready; the
    first startup DB call then ReadTimeouts and the app crashes (exit 255)
    instead of waiting — this caused the 2026-07-10 reboot outage (app + nginx +
    matter-server all down, no automations). Retrying here makes startup
    self-heal regardless of container start order. This is more robust than a
    compose `depends_on: service_healthy`, which is NOT honored when the Docker
    daemon restarts containers after a host reboot (only on `compose up`).

    Any status < 500 means PostgREST is up and serving (a 404 on the bare path
    still proves it's answering). Returns True if it became reachable; on timeout
    logs an error and returns False (fail-loud, but let the app try rather than
    hang forever).
    """
    import time as _time
    import requests as _requests
    pg = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    deadline = _time.monotonic() + timeout_s
    attempt = 0
    while _time.monotonic() < deadline:
        attempt += 1
        try:
            r = _requests.get(f"{pg}/", timeout=5)
            if r.status_code < 500:
                logger.info(f"[startup] PostgREST ready after {attempt} attempt(s) (HTTP {r.status_code})")
                return True
        except Exception as e:  # noqa: BLE001 - not-ready yet; retry
            if attempt == 1 or attempt % 5 == 0:
                logger.warning(f"[startup] waiting for PostgREST ({pg}) — attempt {attempt}: {e}")
        _time.sleep(interval_s)
    logger.error(f"[startup] PostgREST not reachable after {timeout_s:.0f}s — continuing (startup may still fail)")
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: initialize services on startup, cleanup on shutdown."""
    # Boot-race guard — wait for PostgREST before ANY DB-dependent init so a
    # reboot (app up before postgrest ready) no longer crashes the app on
    # ReadTimeout. See _wait_for_postgrest for why this beats depends_on.
    _wait_for_postgrest()

    initialize_services()

    # Capture the main asyncio loop so worker-thread code (device_commander's
    # ThreadPoolExecutor) can drive the async Matter client via
    # run_coroutine_threadsafe. Used by the Matter-primary command path.
    try:
        import asyncio as _asyncio
        from services import matter_client as _mc
        _mc.set_event_loop(_asyncio.get_running_loop())
    except Exception as e:
        logger.warning(f"matter_client.set_event_loop failed: {e}")

    # Apply any pending schema migrations
    run_db_migrations()

    # Start Matter discovery background service (scans hubs every 5 min)
    from services.matter_discovery import start_matter_discovery, stop_matter_discovery
    start_matter_discovery(scan_interval=300)

    # Start the Matter self-healing watchdog: its first sweep connects the
    # matter_client at startup (killing the lazy-connection bug where an idle
    # WS drop pinned every command to Hubitat), then keeps the connection alive
    # and re-interviews stale "not available" nodes. See services/matter_watchdog.py.
    from services.matter_watchdog import start_matter_watchdog
    start_matter_watchdog()

    # Stamp our fabric LABEL so devices carry a MEANINGFUL admin name instead of
    # matterjs's "HomeAssistant" default (2026-07-11 operator directive). This is
    # the fabric-descriptor label the device stores + our debug console and the
    # Hubitat Fabric Manager driver display. NOTE: it does NOT change Apple
    # Home's "Matter Test" text — that is keyed on our test VendorID 0xFFF1, not
    # the label. matterjs may reset the default to "HomeAssistant" on its own
    # restart, so we (re)assert it on every app startup. Best-effort.
    async def _set_matter_fabric_label():
        label = os.environ.get("MATTER_FABRIC_LABEL", "MOBIUS.HOME")[:32]
        try:
            from services.matter_client import get_matter_client
            mc = get_matter_client()
            if mc.is_connected or await mc.connect():
                await mc.set_default_fabric_label(label)
                logger.info(f"matter: default fabric label set to '{label}'")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"matter: could not set default fabric label: {e}")
    asyncio.create_task(_set_matter_fabric_label())

    # Start device cache refresh (Matter-first, Maker API fallback)
    from services.device_cache_refresh import start_cache_refresh, stop_cache_refresh
    refresh_interval = int(os.environ.get('DEVICE_CACHE_REFRESH_INTERVAL', '120'))
    start_cache_refresh(refresh_interval=refresh_interval)

    # Start Hubitat eventsocket client (raw WS event stream per hub).
    # Default mode is 'shadow' — connect and log events without routing them,
    # so cutover can compare against the Maker API webhook path before flipping.
    # Set EVENTSOCKET_INTAKE_MODE=primary in env to dispatch through WebhookRouter.
    from services.hubitat_eventsocket_client import (
        start_eventsocket, stop_eventsocket
    )
    await start_eventsocket()

    # Samsung TV multi-instance registry — replaces the env-driven single-
    # tenant client. Spawns one SamsungTVClient per enabled row in
    # dsapp.samsung_tv_instances. On the very first boot after migration 009
    # with no rows yet, runs the env→DB importer so the legacy single TV
    # becomes row id 1 automatically. See:
    #   services/samsung_tv_registry.py
    #   docs/plans/samsung_tv_multi_instance_refactor_per_instance_ip_mac_token_in_database.md
    from services.samsung_tv_registry import (
        start_samsung_tv_registry, stop_samsung_tv_registry,
    )
    await start_samsung_tv_registry()

    # Reconcile poll — safety net for the WS-only intake. Polls /devices/all
    # per hub every 60s (10s in aggressive mode after a recent WS failure),
    # synthesizes events for cache↔hub divergences through WebhookRouter.
    from services.reconcile_poll import (
        start_reconcile_poll, stop_reconcile_poll
    )
    await start_reconcile_poll()

    # DB size-cap auto-prune (2026-05-17). Per-table max bytes; oldest rows
    # dropped + VACUUM ANALYZE every hour. Targets ~100MB total budget across
    # event_log / raw_events / event_routings / device_commands / etc.
    # See services/db_size_cap.py for the policy.
    try:
        from services.db_size_cap import schedule_prune_job, run_prune_pass
        from services.scheduler_service import get_scheduler
        schedule_prune_job(get_scheduler(), interval_seconds=3600)
        # Also run one pass synchronously at boot so we never start over budget.
        await asyncio.to_thread(run_prune_pass)
    except Exception as e:
        logger.warning(f"db_size_cap startup failed (non-fatal): {e}")

    # Run hub classification on startup (populates device_hub_mapping table).
    # Runs in background thread so it doesn't block app readiness.
    # TILES and DeviceCommander depend on this data for native-hub routing.
    import threading
    def _startup_classification():
        try:
            from services.device_to_hubs_classifier import run_classification, invalidate_cache
            logger.info("Running startup hub classification...")
            result = run_classification()
            invalidate_cache()
            total = result.get("total_native", 0) if isinstance(result, dict) else 0
            logger.info(f"Startup hub classification complete: {total} native devices mapped")
        except Exception as e:
            logger.warning(f"Startup hub classification failed (will retry on next POST /api/hub/classify): {e}")

    threading.Thread(target=_startup_classification, name="startup-hub-classify", daemon=True).start()

    # Hub-derived timezone refresh (2026-05-17 user directive).
    # Each hub carries its own location TZ; Mobius queries every enabled
    # hub on a schedule and caches the agreed-upon TZ to system_settings.
    # On disagreement, picks majority, warns, and persists the breakdown
    # so the dashboard can surface which hub to reconfigure.
    #
    # First refresh runs in a background thread so it doesn't block app
    # readiness (network query × N hubs would). Subsequent refreshes run
    # hourly via APScheduler.
    def _refresh_hub_timezone():
        try:
            from services.hub_tz_resolver import (
                resolve_hub_timezone,
                apply_resolved_timezone_to_environment,
                persist_resolved_timezone,
            )
            iana_tz, consistent, per_hub = resolve_hub_timezone()
            persist_resolved_timezone(iana_tz, consistent, per_hub)
            if iana_tz:
                apply_resolved_timezone_to_environment(iana_tz)
                if consistent:
                    logger.info(
                        f"Hub-derived TZ resolved: {iana_tz} (all hubs agree)"
                    )
                else:
                    logger.warning(
                        f"Hub-derived TZ resolved to {iana_tz} but hubs "
                        f"disagree. Breakdown: {per_hub}. Fix the outlier "
                        f"from its Hubitat UI → Settings → Location → "
                        f"Hub Time Zone."
                    )
            else:
                logger.info(
                    "Hub-derived TZ unavailable (no hubs reachable or "
                    "unmapped Windows TZ). Falling back to whatever's in "
                    "system_settings.timezone."
                )
        except Exception as e:
            logger.warning(
                f"Hub TZ refresh failed (non-fatal): {e}", exc_info=True
            )

    threading.Thread(
        target=_refresh_hub_timezone,
        name="startup-hub-tz-refresh",
        daemon=True,
    ).start()
    try:
        from services.scheduler_service import get_scheduler
        get_scheduler()._scheduler.add_job(
            func=_refresh_hub_timezone,
            trigger='interval',
            seconds=3600,
            id='hub_tz_refresh',
            replace_existing=True,
        )
    except Exception as e:
        logger.warning(f"Hub TZ periodic refresh schedule failed: {e}")

    # Hub-derived location-mode polling (2026-05-18).
    # The eventsocket WS doesn't deliver LOCATION frames on Elfege's
    # firmware (zero captured in raw_events across ~2.5k DEVICE frames).
    # Pull `currentMode` from the authoritative hub every 60s and write
    # location_modes + mode_change_log on transitions; route_mode_change
    # fires so running AML instances see on_mode_change() the same way
    # the WS path would have delivered it.
    def _mode_poll_first_pass():
        try:
            from services.mode_poller import run_poll_pass
            result = run_poll_pass()
            logger.info(f"mode_poller startup pass: {result}")
        except Exception as e:
            logger.warning(f"mode_poller startup pass failed: {e}")

    threading.Thread(
        target=_mode_poll_first_pass,
        name="startup-mode-poll",
        daemon=True,
    ).start()
    try:
        from services.mode_poller import schedule_poll_job
        from services.scheduler_service import get_scheduler
        schedule_poll_job(get_scheduler()._scheduler, interval_seconds=60)
    except Exception as e:
        logger.warning(f"mode_poller schedule failed: {e}")

    # Dead-instance watchdog (2026-06-19). Revives app instances whose
    # worker was left stopped (e.g. an abandoned edit — the wizard stops a
    # worker on edit-entry and only restarts it on save/cancel) past the
    # grace window. Closes the gap that left instance 5 (Motion Kitchen)
    # dead ~7h on 2026-06-18 with the kitchen unmanaged. Paused instances
    # are never revived. grace 900s, checked every 300s → an abandoned edit
    # self-heals within ~20 min instead of staying dead indefinitely.
    try:
        from services.scheduler_service import get_scheduler
        from services.instance_manager import get_instance_manager
        _im = get_instance_manager()
        get_scheduler()._scheduler.add_job(
            func=lambda: _im.revive_dead_instances(grace_seconds=900),
            trigger='interval',
            seconds=300,
            id='instance_revive_watchdog',
            replace_existing=True,
        )
        logger.info("instance_revive_watchdog: scheduled every 300s (grace 900s)")
    except Exception as e:
        logger.warning(f"instance_revive_watchdog schedule failed: {e}")

    # Timed-pause expiry reconciler (2026-07-11). The restart-proof guarantee
    # for timed pauses. pause() schedules its auto-resume in an in-memory
    # APScheduler jobstore that is destroyed by any restart/reload with nothing
    # re-arming it, so a restart between a pause and its expiry left the
    # instance paused forever — STP "TV Allowed Time" (inst 13) sat paused ~14h
    # after a restart 10 min before its resume was due, the kids' TV un-gated
    # all night. This reconciler resumes instances whose DURABLE
    # pause_expires_at has elapsed, self-healing within one tick of any restart.
    # Run once now to catch pauses that expired during downtime, then every 60s
    # (bounds over-run to <=60s). Cheap: one indexed query. See
    # services.instance_manager.resume_expired_pauses.
    try:
        from services.scheduler_service import get_scheduler
        from services.instance_manager import get_instance_manager
        get_instance_manager().resume_expired_pauses()  # immediate catch-up on boot
        get_scheduler()._scheduler.add_job(
            func=lambda: get_instance_manager().resume_expired_pauses(),
            trigger='interval',
            seconds=60,
            id='pause_expiry_reconciler',
            replace_existing=True,
        )
        logger.info("pause_expiry_reconciler: scheduled every 60s")
    except Exception as e:
        logger.warning(f"pause_expiry_reconciler schedule failed: {e}")

    # Periodic device reclassification (2026-06-19). Classification used to
    # run ONLY at startup or on a manual refresh (↻), so a device removed
    # from a hub lingered in the registry indefinitely and stayed
    # selectable (operator-reported orphan 'Light piano'). Re-running every
    # 10 min lets the presence-prune (devices.is_present) reflect hub
    # add/remove within minutes. run_classification is sync HTTP across all
    # hubs; APScheduler runs it in a worker thread so the loop is unblocked.
    try:
        from services.scheduler_service import get_scheduler
        from services.device_to_hubs_classifier import (
            run_classification as _run_classification,
        )
        get_scheduler()._scheduler.add_job(
            func=_run_classification,
            trigger='interval',
            seconds=600,
            id='device_reclassify',
            replace_existing=True,
        )
        logger.info("device_reclassify: scheduled every 600s")
    except Exception as e:
        logger.warning(f"device_reclassify schedule failed: {e}")

    # Sonos volume LOCK enforcement (2026-06-22, operator: "persist volume level
    # when other system changes it"). Polls speakers with persist_volume=true and
    # re-asserts their persisted_level over UPnP. Poll-based (not Hubitat-event-
    # based) to avoid fuzzy device-name→speaker mapping. No-op when none locked.
    try:
        from services.scheduler_service import get_scheduler
        from services.sonos import enforce_locked_volumes as _enforce_sonos_vol
        get_scheduler()._scheduler.add_job(
            func=_enforce_sonos_vol,
            trigger='interval',
            seconds=30,
            id='sonos_volume_enforce',
            replace_existing=True,
        )
        logger.info("sonos_volume_enforce: scheduled every 30s")
    except Exception as e:
        logger.warning(f"sonos_volume_enforce schedule failed: {e}")

    # Hubitat admin-API contract-drift watch (2026-05-26).
    # Firmware 2.5.0.143 changed POST /device/runmethod from form-encoded to
    # JSON with no backward compat, silently breaking every command. This
    # watcher polls each hub's firmware version + a harmless runmethod canary
    # and records the result in hub_health (surfaced on the Hubs cards), so the
    # next contract flip announces itself instead of being diagnosed by hand.
    # First pass in a background thread (network × N hubs); 6h recurring.
    def _contract_watch_first_pass():
        try:
            from services.hub_contract_watch import run_watch_pass
            result = run_watch_pass()
            logger.info(f"contract_watch startup pass: {result}")
        except Exception as e:
            logger.warning(f"contract_watch startup pass failed: {e}")
    threading.Thread(
        target=_contract_watch_first_pass,
        name="startup-contract-watch",
        daemon=True,
    ).start()
    try:
        from services.hub_contract_watch import schedule_watch_job
        from services.scheduler_service import get_scheduler
        schedule_watch_job(get_scheduler()._scheduler, interval_seconds=21600)
    except Exception as e:
        logger.warning(f"contract_watch schedule failed: {e}")

    # Start Samsung TV client (WS + HTTP power-poll background tasks).
    # Config is read from env vars (set in docker-compose or start.sh).
    # on_power_change pushes state to all registered Hubitat callbacks via
    # the blueprint's push_state_changes() so Hubitat stays in sync in real-time.
    from services.samsung_tv_client import get_tv_client
    from apps.samsung_tv.blueprint import push_state_changes as _tv_push, _persist_token

    async def _on_tv_state_change(state) -> None:
        """Bridge TV power OR connection state changes → Hubitat LAN push."""
        nonlocal _tv_client
        await _tv_push(_tv_client)

    async def _on_tv_token_save(new_token: str) -> None:
        """Persist a newly-issued TV auth token so it survives container restarts."""
        os.environ["SAMSUNG_TV_TOKEN"] = new_token
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _persist_token, new_token)

    # Token loaded from env var — populated by start.sh from ./state/samsung_tv_token.txt.
    _saved_token = os.environ.get("SAMSUNG_TV_TOKEN", "")

    _tv_client = get_tv_client(
        tv_ip            = os.environ.get("SAMSUNG_TV_IP",   "<LAN_IP>"),
        mac_address      = os.environ.get("SAMSUNG_TV_MAC",  "AABBCCDDEEFF"),
        token            = _saved_token,
        use_ssl          = os.environ.get("SAMSUNG_TV_SSL",  "true").lower() == "true",
        name             = os.environ.get("SAMSUNG_TV_NAME", "living_room_tv"),
        on_power_change  = _on_tv_state_change,
        on_conn_change   = _on_tv_state_change,
        on_token_save    = _on_tv_token_save,
    )
    await _tv_client.start()

    # Phase C defensive layer: in-process event-loop watchdog. Ticks every
    # 2s; /api/health returns 503 if no tick within 10s. Docker healthcheck
    # surfaces that as unhealthy, and the autoheal sidecar restarts the
    # container. This narrows the stall-detection window from "loop fully
    # wedged, no HTTP at all" to "loop degraded for >10s" — well before a
    # stall becomes visible in the UI.
    from services.loop_watchdog import start_watchdog, stop_watchdog
    start_watchdog()

    yield

    stop_watchdog()
    stop_cache_refresh()
    from services.matter_watchdog import stop_matter_watchdog
    stop_matter_watchdog()
    stop_matter_discovery()
    await stop_eventsocket()
    await stop_reconcile_poll()

    # Stop Samsung TV multi-instance registry — tears down every per-row
    # client cleanly. Symmetric with start_samsung_tv_registry above.
    await stop_samsung_tv_registry()

    # Stop the legacy single-tenant Samsung TV client. Coexists with the
    # registry today: the registry handles /samsung-tv/<id>/* routes; the
    # legacy client backs the bare /samsung-tv/* routes until the cutover
    # step (see plan §4.7 step 6). At that point this block disappears.
    await _tv_client.stop()

    logger.info("Shutting down...")


# ---------------------------------------------------------------------------
# Create FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="MOBIUS.HOME",
    description="Hubitat home automation platform with multi-instance support",
    lifespan=lifespan,
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")


# Static-asset cache control (2026-06-22). Without this, browsers heuristically
# cache JS/CSS and operators don't see UI changes until a manual hard-refresh —
# the recurring "I still don't see the new dropdown" problem. `no-cache` forces
# the browser to REVALIDATE every load (StaticFiles sets ETag/Last-Modified, so
# unchanged files still return a cheap 304); changed files are fetched fresh.
# Lightweight interim fix for the versioned-asset TODO (#19).
@app.middleware("http")
async def _revalidate_static_assets(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


# Jinja2 templates
templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# Include routers
# ---------------------------------------------------------------------------

from apps.advanced_motion_lighting.blueprint import router as motion_router  # noqa: E402
app.include_router(motion_router)

from apps.samsung_tv.blueprint import router as samsung_tv_router  # noqa: E402
app.include_router(samsung_tv_router)

# Multi-instance Samsung TV router (mounts at /samsung-tv/<id>/* etc.).
# Coexists with the legacy single-tenant router above; routes don't
# collide because the new ones are all parameterized with {instance_id}.
# See docs/plans/samsung_tv_multi_instance_refactor_per_instance_ip_mac_token_in_database.md
from apps.samsung_tv.instance_router import router as samsung_tv_instance_router  # noqa: E402
app.include_router(samsung_tv_instance_router)

# Matter command feedback — USER-INPUT-BASED learning log (operator directive
# 2026-07-11): every Test ON/OFF attempt is logged, then the modal's visual
# confirmation ("It worked"/"It didn't") is patched onto the row.
# Table: dshub.matter_command_feedback (migrate_matter_command_feedback_learning_log.sql).
from services.matter_command_feedback import router as matter_feedback_router  # noqa: E402
app.include_router(matter_feedback_router)

# PANEL API (TILES absorb, P1 — 2026-07-12): the authenticated surface the
# RN/Expo panel app talks to. MAX-CONVENTIONAL, DEFAULT-DENY auth (operator
# ruling §6b): enrolled per-device tokens (SHA-256 hashed, individually
# revocable) + least-privilege scopes + the trusted-LAN check as a SECOND
# factor — never the gate. This deliberately does NOT reproduce the absorbed
# TILES posture, whose POST /api/device/<id>/command had no auth at all.
# Package: apps/tiles_api/ (tiles-exclusive glue only; shared control logic
# stays in services/ per the fanatic-modularization ruling).
from apps.tiles_api.routes import router as panel_router  # noqa: E402
app.include_router(panel_router)

# MATTER HUB->HUB COPY (2026-07-13, operator-directed; implemented by the
# assistant seat per MSG-1002/1006, landed by Architect): copy — NEVER transfer
# — every eligible Matter device from one hub's fabric onto another's, via
# multi-admin ECM. Strictly sequential end-to-end (a Hubitat pairs ONE device
# at a time), whole run inside the global matter_pairing_lock, every transition
# audited to dshub.matter_hub_ports (migration 015). Routes:
# /api/matter/port-devices{,/status,/preview}.
from services.matter_hub_port.router import router as matter_hub_port_router  # noqa: E402
app.include_router(matter_hub_port_router)

# The ONE global Matter-pairing mutex (a Hubitat/Matter controller pairs a single
# device at a time). Imported at module level because BOTH the bulk paths and the
# single-commission endpoint take it — the single path was unguarded until
# 2026-07-13 and let concurrent commission storms through (MSG-1017).
from services.matter_pairing_lock import (  # noqa: E402
    PairingLockBusy, matter_pairing_lock,
)

# Certificate-installation routes (/install-cert, /api/cert/status). Serves the
# shared MOBIUS local CA so users trust HTTPS once instead of clicking through
# the browser warning each load. Ported from NVR; see services/cert_routes.py.
from services.cert_routes import register_cert_routes  # noqa: E402
register_cert_routes(app, templates)

# Sonos announcement subsystem routes (/api/sonos/clip, /speakers, /announce).
# Clips are served over PLAIN http here (Sonos won't fetch self-signed HTTPS).
# See services/sonos/ — local UPnP control, no Hubitat, no cloud.
from services.sonos.routes import register_sonos_routes  # noqa: E402
register_sonos_routes(app)


# =============================================================================
# Health & Status
# =============================================================================


@app.get("/api/health", tags=["health"])
async def health():
    """
    Liveness probe. Returns 200 only if the event-loop watchdog has
    ticked within the alive threshold (10s by default). Returns 503 when
    the loop is wedged or degraded — Docker healthcheck picks that up
    and the autoheal sidecar restarts the container. See
    services/loop_watchdog.py for the rationale.
    """
    from services.loop_watchdog import (
        is_loop_alive,
        last_tick_age_seconds,
        ALIVE_THRESHOLD_SECS,
    )
    age = last_tick_age_seconds()
    if not is_loop_alive():
        # 503 fails the Docker healthcheck → autoheal restarts. Body is
        # informational; the status code is what the orchestrator reads.
        raise HTTPException(
            status_code=503,
            detail={
                "status": "degraded",
                "reason": "event loop watchdog stale",
                "loop_lag_seconds": age,
                "alive_threshold_seconds": ALIVE_THRESHOLD_SECS,
            },
        )
    return {"status": "ok", "loop_lag_seconds": age}


@app.get("/api/health/breakers", tags=["health"])
async def health_breakers():
    """
    Observable state of every circuit breaker registered in the
    process. Deliberately NOT gated on liveness — a breaker being
    OPEN is degraded operation, not unhealthy: autoheal should not
    restart the container just because (say) the matter-server is
    down. This endpoint exists so the operator can see the breakers'
    state without waiting for an ERROR log to appear.

    Returns:
      {
        "breakers": [
          {
            "name": "hubitat:<LAN_IP>",
            "state": "closed" | "open" | "half_open",
            "failure_count": 0,
            "fail_threshold": 5,
            "reset_timeout_secs": 30.0,
            "fail_window_secs": 60.0,
            "last_failure_reason": "...",
            "secs_until_half_open": null | float,
            ...
          },
          ...
        ],
        "any_open": bool   # convenience for dashboards
      }
    """
    from services.circuit_breaker import all_breakers
    snapshots = [b.snapshot() for b in all_breakers().values()]
    return {
        "breakers": snapshots,
        "any_open": any(s["state"] != "closed" for s in snapshots),
    }


@app.post("/api/health/breakers/{name}/reset", tags=["health"])
async def health_breaker_reset(name: str):
    """
    Manually reset a breaker back to CLOSED (clears the failure
    count). For operator use after fixing the downstream — saves the
    30s cooldown wait. Returns 404 if the breaker doesn't exist (i.e.
    nothing has tried to use it yet under that name).
    """
    from services.circuit_breaker import all_breakers
    breakers = all_breakers()
    breaker = breakers.get(name)
    if breaker is None:
        raise HTTPException(
            status_code=404,
            detail=f"no breaker named '{name}' (try one of: {sorted(breakers)})",
        )
    breaker.reset()
    return {"status": "reset", "snapshot": breaker.snapshot()}


@app.get("/api/status", tags=["health"])
async def status():
    """Detailed status endpoint."""
    from services.instance_manager import get_instance_manager
    from services.hubitat_client import get_default_client

    manager = get_instance_manager()
    instances = manager.get_all_instances()

    # Check Hubitat connectivity
    try:
        client = get_default_client()
        hubitat_connected = client.is_connected()
    except Exception:
        hubitat_connected = False

    return {
        "status": "ok",
        "instances_count": len(instances),
        "running_instances": len(manager._running_instances),
        "hubitat_connected": hubitat_connected,
    }


# =============================================================================
# App Types
# =============================================================================


@app.get("/api/app-types", tags=["app-types"])
async def get_app_types():
    """List available app types."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()
    return manager.get_app_types()


@app.get("/api/app-types/{type_name}/schema", tags=["app-types"])
async def get_app_type_schema(type_name: str):
    """Get settings schema for an app type."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()
    schema = manager.get_app_type_schema(type_name)
    if schema:
        return schema
    raise HTTPException(status_code=404, detail="App type not found")


# =============================================================================
# Instances
# =============================================================================


@app.get("/api/instances", tags=["instances"])
async def get_instances():
    """List all instances."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()
    return manager.get_all_instances()


@app.post("/api/instances", status_code=201, tags=["instances"])
async def create_instance(body: CreateInstanceRequest):
    """Create a new instance."""
    from services.instance_manager import get_instance_manager

    manager = get_instance_manager()
    instance_id = manager.create_instance(
        app_type=body.app_type,
        label=body.label,
        device_selections=body.device_selections,
        settings=body.settings,
    )

    if instance_id:
        return {"id": instance_id, "message": "Instance created"}
    raise HTTPException(status_code=500, detail="Failed to create instance")


@app.get("/api/instances/{instance_id}", tags=["instances"])
async def get_instance(instance_id: int):
    """Get instance details."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()
    instance = manager.get_instance(instance_id)
    if instance:
        return instance
    raise HTTPException(status_code=404, detail="Instance not found")


@app.put("/api/instances/{instance_id}", tags=["instances"])
async def update_instance(instance_id: int, body: UpdateInstanceRequest):
    """Update instance settings."""
    from services.instance_manager import get_instance_manager

    manager = get_instance_manager()
    success = manager.update_instance(
        instance_id,
        label=body.label,
        device_selections=body.device_selections,
        settings=body.settings,
    )

    if success:
        return {"message": "Instance updated"}
    raise HTTPException(status_code=500, detail="Failed to update instance")


@app.delete("/api/instances/{instance_id}", tags=["instances"])
async def delete_instance(instance_id: int):
    """Delete an instance."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    if manager.delete_instance(instance_id):
        return {"message": "Instance deleted"}
    raise HTTPException(status_code=500, detail="Failed to delete instance")


@app.post("/api/instances/{instance_id}/stop", tags=["instances"])
async def stop_instance(instance_id: int):
    """Kill a running instance (e.g. when entering edit mode).

    The instance stays in the DB but is no longer processing events.
    Call POST .../start or PUT to restart it.
    """
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()
    was_running = manager.stop_instance(instance_id)
    return {"message": "Instance stopped", "was_running": was_running}


@app.post("/api/instances/{instance_id}/start", tags=["instances"])
async def start_instance(instance_id: int):
    """Start an instance from its current DB state.

    Used after cancelling an edit (instance was stopped on edit entry).
    """
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()
    # Stop first in case it's somehow still running
    manager.stop_instance(instance_id)
    started = manager._start_from_db(instance_id)
    if started:
        return {"message": "Instance started"}
    raise HTTPException(status_code=500, detail="Failed to start instance")


@app.post("/api/instances/{instance_id}/pause", tags=["instances"])
async def pause_instance(instance_id: int, body: PauseInstanceRequest = PauseInstanceRequest()):
    """Pause an instance.

    Accepts either ``duration_minutes`` (legacy) or ``duration_seconds``
    (preferred for sub-minute pauses). When both are provided,
    ``duration_seconds`` wins; we convert with ceil so 30 seconds doesn't
    round down to 0-minute (= indefinite). 0 in EITHER means indefinite.
    """
    from services.instance_manager import get_instance_manager

    duration_minutes = body.duration_minutes
    if body.duration_seconds is not None:
        if body.duration_seconds == 0:
            duration_minutes = 0
        else:
            # Ceil so 30s -> 1 minute (won't degenerate to indefinite).
            duration_minutes = max(1, (body.duration_seconds + 59) // 60)

    manager = get_instance_manager()
    if manager.pause_instance(instance_id, duration_minutes, body.reason):
        return {"message": "Instance paused"}
    raise HTTPException(status_code=500, detail="Failed to pause instance")


@app.post("/api/instances/{instance_id}/resume", tags=["instances"])
async def resume_instance(instance_id: int):
    """Resume a paused instance."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    if manager.resume_instance(instance_id):
        return {"message": "Instance resumed"}
    raise HTTPException(status_code=500, detail="Failed to resume instance")


@app.get("/api/instances/{instance_id}/status", tags=["instances"])
async def get_instance_status(instance_id: int):
    """Get runtime status of an instance."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    running = manager.get_running_instance(instance_id) is not None

    return {
        "id": instance_id,
        "label": instance.get("label"),
        "is_running": running,
        "is_paused": instance.get("is_paused", False),
        "is_enabled": instance.get("is_enabled", True),
        "last_activity": instance.get("last_activity_at"),
    }


@app.get("/api/instances/{instance_id}/runtime-status", tags=["instances"])
async def get_instance_runtime_status(instance_id: int):
    """
    Live runtime state of a running instance — what the debug-panel
    countdown reads to show "time until next turn-off".

    The off-timer starts when the room goes QUIET (the inactive transition),
    not when motion began — so the countdown is anchored on that transition,
    the SAME anchor master() uses to decide off (via off_timer_status()). While
    motion is active the light is staying on, so there is no countdown.

    Returns a small JSON with:
      - last_motion_time:  ISO UTC of the most recent motion=active event
                           (informational/tooltip only — NOT the anchor)
      - timeout_seconds:   current effective no-motion timeout, after
                           per-mode lookup + system-floor clamp
      - off_anchor_at:     ISO UTC of the inactive transition the countdown
                           runs from; None while motion is active or cold
      - timeout_at:        ISO UTC of when AML will decide off
                           (= off_anchor_at + timeout_seconds); None when
                           motion is active or no transition yet
      - remaining_seconds: float when counting down (off_anchor + timeout -
                           now); None while motion is active (staying on) or
                           when there is no anchor yet (idle)
      - current_mode:      string from location_modes (DB-read, not Maker)
      - is_motion_active:  current verdict (True → staying on, no countdown)
      - is_paused:         persisted pause state
      - is_running:        whether the instance is loaded in-memory

    404 if the instance row doesn't exist. 200 with is_running=false
    and null runtime fields when the instance is stopped.
    """
    from datetime import datetime, timezone
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    running = manager.get_running_instance(instance_id)
    out = {
        "id": instance_id,
        "label": instance.get("label"),
        "is_running": running is not None,
        "is_paused": instance.get("is_paused", False),
        "last_motion_time": None,
        "timeout_seconds": None,
        # User's configured display unit ('seconds' or 'minutes'). The
        # UI formats the countdown accordingly so a 5-min timeout reads
        # "off in 4m 23s" and a 30-sec timeout reads "off in 18s".
        "time_unit": (instance.get("settings") or {}).get("timeUnit", "seconds"),
        "timeout_at": None,
        "remaining_seconds": None,
        "current_mode": None,
        "is_motion_active": None,
    }
    if running is None:
        return out

    # Read in-memory runtime state. These are AML-specific attributes;
    # other app types may not have them — defend with getattr.
    rt = getattr(running, "_runtime", None)
    last_motion = getattr(rt, "last_motion_time", None) if rt else None
    if last_motion is not None:
        # Informational only (tooltip). NOT the countdown anchor — the off
        # timer starts when the room goes quiet, not when motion began.
        out["last_motion_time"] = last_motion.isoformat()

    timeout_seconds = None
    try:
        # _get_timeout_seconds is on the TimeoutMixin (AML); FanAutomation
        # has a different path. Guard so we don't crash on non-AML types.
        if hasattr(running, "_get_timeout_seconds"):
            timeout_seconds = int(running._get_timeout_seconds())
    except Exception as e:
        logger.debug(f"runtime-status: _get_timeout_seconds failed: {e}")

    try:
        if hasattr(running, "_get_current_mode"):
            out["current_mode"] = running._get_current_mode()
    except Exception:
        pass

    # Countdown: anchor on the OFF-timer start (the inactive transition),
    # shared with master()'s off decision via off_timer_status() so the bar
    # can never disagree with reality. While motion is active the light is
    # staying on → no countdown (remaining_seconds=None).
    off_status = None
    try:
        if hasattr(running, "off_timer_status"):
            off_status = running.off_timer_status()
    except Exception as e:
        logger.debug(f"runtime-status: off_timer_status failed: {e}")

    if off_status is not None:
        out["is_motion_active"] = bool(off_status.get("is_active"))
        if timeout_seconds is not None:
            out["timeout_seconds"] = timeout_seconds
        anchor_iso = off_status.get("off_anchor_iso")
        if off_status.get("is_active"):
            # Staying on — no countdown to show.
            out["remaining_seconds"] = None
            out["off_anchor_at"] = None
        elif anchor_iso and timeout_seconds is not None:
            from datetime import timedelta
            try:
                anchor_at = datetime.fromisoformat(
                    anchor_iso.replace("Z", "+00:00")
                )
                timeout_at = anchor_at + timedelta(seconds=timeout_seconds)
                out["off_anchor_at"] = anchor_at.isoformat()
                out["timeout_at"] = timeout_at.isoformat()
                out["remaining_seconds"] = (
                    timeout_at - datetime.now(timezone.utc)
                ).total_seconds()
            except Exception as e:
                logger.debug(
                    f"runtime-status: bad off_anchor {anchor_iso!r}: {e}"
                )
        # else: inactive but no transition yet (cold) — leave remaining None;
        # timeout_seconds above surfaces the configured idle window.
    else:
        # Non-AML app types without off_timer_status (e.g. FanAutomation):
        # fall back to the legacy is_motion_active + last_motion anchoring.
        try:
            if hasattr(running, "_is_motion_active"):
                out["is_motion_active"] = bool(running._is_motion_active())
        except Exception:
            pass
        if last_motion is not None and timeout_seconds is not None:
            from datetime import timedelta
            timeout_at = last_motion + timedelta(seconds=timeout_seconds)
            out["timeout_at"] = timeout_at.isoformat()
            out["timeout_seconds"] = timeout_seconds
            out["remaining_seconds"] = (
                timeout_at - datetime.now(timezone.utc)
            ).total_seconds()
        elif timeout_seconds is not None:
            out["timeout_seconds"] = timeout_seconds

    return out


@app.post("/api/instances/{instance_id}/run", tags=["instances"])
async def run_instance(instance_id: int):
    """
    Run instance: start if stopped, or re-evaluate state if already running.

    When already running, calls master() to evaluate current conditions
    (motion state, timeouts) and control lights accordingly.
    """
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    running = manager.get_running_instance(instance_id)
    if running:
        # Already running — re-evaluate current state via master()
        running.master()
        return {"message": "Instance re-evaluated current state"}

    if manager._start_instance(instance_id, instance):
        return {"message": "Instance started"}
    raise HTTPException(status_code=500, detail="Failed to start instance")


@app.post("/api/instances/{instance_id}/update", tags=["instances"])
async def update_initialize_instance(instance_id: int):
    """
    Reload an instance (stop + start with current config).

    Also resets memoization state so the instance starts fresh
    without stale override records.
    """
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    # Reset memoization on reload (stale memo causes incorrect behavior)
    manager.update_memoization(instance_id, {})

    manager.stop_instance(instance_id)
    manager._rebuild_subscriptions(instance_id)
    manager._start_from_db(instance_id)
    return {"message": "Instance reloaded with memoization reset"}


# =============================================================================
# Devices
# =============================================================================


@app.get("/api/devices", tags=["devices"])
async def get_devices(capability: Optional[str] = Query(None)):
    """
    List devices, optionally filtered by capability.

    Reads from the canonical `devices` table (DB-cached) — NOT a live Maker
    API call. The canonical table is kept fresh by hub_classifier on startup
    and by the reconcile poll. This eliminates the multi-second "Loading
    devices..." wait in the wizard that the per-category live-Maker pattern
    used to cause (one HTTP roundtrip to Hubitat per category × 6 categories).

    For a forced live refresh from Hubitat, see /api/devices/refresh.

    Args:
        capability: Filter by capability (e.g., 'motionSensor', 'switch').
                    PostgREST JSONB containment: capabilities ? capability.
    """
    try:
        pg = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
        params = {
            'select': 'id,hub_ip,hubitat_id,label,name,device_type,'
                      'protocol,capabilities,attributes',
            'order': 'label',
            # Hide devices that have been pruned from their hub. The
            # classifier sets is_present=false when a device stops
            # appearing in the hub pull (2026-06-19). Removed devices must
            # not stay selectable in the wizard.
            'is_present': 'eq.true',
        }
        if capability:
            # PostgREST JSONB array-contains: cs.["value"] for JSONB array
            # of strings. NOT cs.{"value"} — that's PG-array literal syntax
            # and PostgREST rejects it on JSONB columns (PGRST 22P02).
            params['capabilities'] = f'cs.["{capability}"]'
        r = await aget(f"{pg}/devices", params=params, timeout=5)
        r.raise_for_status()
        rows = r.json()
        # Shape compatibility: legacy callers expect each device to have
        # `id` (the integer canonical PK is fine) and a `label`. Already do.
        return rows
    except Exception as e:
        logger.error(f"Failed to get devices: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/devices/by-categories", tags=["devices"])
async def get_devices_by_categories(categories: str = Query(...)):
    """
    Bulk endpoint: returns devices grouped by capability in ONE roundtrip.

    `categories` is a comma-separated list of capability names matching
    what the wizard's device_categories return. Example:
        GET /api/devices/by-categories?categories=motionSensor,switch,contact
        →  {
              "motionSensor": [...],
              "switch":       [...],
              "contact":      [...]
           }

    Internally one PostgREST call fetches all devices, then we group in
    memory. Faster than N round-trips and trivial to add new categories.
    """
    cats = [c.strip() for c in categories.split(',') if c.strip()]
    if not cats:
        return {}
    try:
        pg = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
        r = await aget(
            f"{pg}/devices",
            params={
                'select': 'id,hub_ip,hubitat_id,label,name,device_type,'
                          'protocol,capabilities,attributes',
                'order': 'label',
                # Hide hub-pruned devices (is_present=false), same as the
                # per-capability /devices endpoint above. 2026-06-19.
                'is_present': 'eq.true',
            },
            timeout=5,
        )
        r.raise_for_status()
        all_devices = r.json()
        # Case-insensitive capability matching. Hubitat ships capabilities
        # as PascalCase ("MotionSensor", "Switch") but wizard configs use
        # camelCase ("motionSensor", "switch") in places. The naive `c in
        # caps` would silently return empty for every category. Build a
        # lowercase capability set per device once and compare lowered
        # category names against it.
        out = {c: [] for c in cats}
        cats_lower = {c.lower(): c for c in cats}
        for d in all_devices:
            caps = d.get('capabilities') or []
            caps_lower = {str(c).lower() for c in caps}
            for cat_lower, cat_orig in cats_lower.items():
                if cat_lower in caps_lower:
                    out[cat_orig].append(d)
        return out
    except Exception as e:
        logger.error(f"get_devices_by_categories: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/devices/refresh", tags=["devices"])
async def refresh_devices_from_hubitat(
    device_id: Optional[str] = Query(
        None,
        description="Canonical devices.id OR Hubitat per-hub id. Omit or pass '0' to refresh the whole roster.",
    ),
):
    """
    Force a fresh pull of devices from Hubitat → canonical `devices` table.

    - `device_id` omitted or '0' → full classifier sweep (every device on
      every hub re-ingested). Slow; reconcile poll keeps it fresh anyway.
    - `device_id` non-zero → just that device: fullJson roundtrip refreshes
      capabilities + attributes, label/name/type updated from the roster,
      upserted via the canonical RPC. Accepts EITHER the canonical id (the
      #146 you see in chips) OR the Hubitat per-hub id (e.g. 2781) — looks
      up canonical first, falls back to per-hub.

    Returns the same shape on success in both modes; the single-device mode
    adds a `resolved` block and `caps_count`. Errors surface as
    `{ok: false, reason: ...}` with HTTP 200 (the operator triggered this
    manually; loud-fail with detail rather than a bare 5xx). HTTP 5xx is
    reserved for unhandled exceptions in the orchestrator itself.
    """
    from services.device_to_hubs_classifier import (
        run_classification, invalidate_cache, refresh_single_device,
    )

    # 'All' path: device_id absent, blank, or literal "0".
    if not device_id or str(device_id).strip() in ("", "0"):
        try:
            # run_classification + invalidate_cache do blocking PostgREST
            # roundtrips. Off the loop so a slow classifier doesn't stall
            # other handlers.
            result = await asyncio.to_thread(run_classification)
            await asyncio.to_thread(invalidate_cache)
            return {
                "ok":    True,
                "mode":  "all",
                "total_native": (result or {}).get("total_native", 0),
            }
        except Exception as e:
            logger.error(f"refresh_devices_from_hubitat (all): {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    # Single-device path. refresh_single_device is sync (requests +
    # admin client are sync); offload so the loop keeps spinning.
    try:
        result = await asyncio.to_thread(refresh_single_device, device_id)
        result["mode"] = "single"
        return result
    except Exception as e:
        logger.error(
            f"refresh_devices_from_hubitat (single id={device_id}): {e}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/devices/{device_id}", tags=["devices"])
async def get_device(device_id: str):
    """Get device details."""
    from services.device_to_hubs_classifier import fetch_device_live

    try:
        device = fetch_device_live(device_id)
        if device:
            return device
        raise HTTPException(status_code=404, detail="Device not found")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get device: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/devices/{device_id}/command", tags=["devices"])
async def send_device_command(device_id: str, body: DeviceCommandRequest):
    """
    Send command to a device via the DeviceCommander.

    Uses threaded execution with nested retries and state verification.

    Args:
        device_id: Hubitat device ID
        body: Command name and optional arguments
    """
    from services.device_commander import get_device_commander

    try:
        commander = get_device_commander()
        result = await commander.send_command(
            device_id=device_id,
            command=body.command,
            args=body.args,
            verify=True,
        )

        if result.success:
            return {
                "message": "Command sent",
                "verified": result.verified,
                "status": result.status.value,
                "actual_state": result.actual_state,
                "expected_state": result.expected_state,
                "retries_used": result.retries_used,
                "elapsed_ms": round(result.elapsed_ms, 1),
            }
        raise HTTPException(
            status_code=500,
            detail=f"Command failed: {result.error}"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to send command: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Webhooks (from Hubitat via webhook-dispatcher)
# =============================================================================


@app.post("/api/webhook/event", tags=["webhooks"])
async def handle_event_webhook(request: Request):
    """
    Deprecated — device event intake moved to the Hubitat eventsocket.

    Reason: the Maker API webhook path was fragile — firmware updates and
    re-saving the Maker API app silently de-armed per-device event forwarding,
    producing the 2026-05-16 Living-room failure (canons 55/56 stopped
    delivering events while the hub itself kept firing them). The eventsocket
    bypasses per-device opt-in entirely. See services/hubitat_eventsocket_client.py.

    Set WEBHOOK_INTAKE_ENABLED=true to re-open this endpoint as a temporary
    rollback during a problem with the WS intake.
    """
    if os.environ.get('WEBHOOK_INTAKE_ENABLED', 'false').strip().lower() == 'true':
        from services.webhook_router import get_webhook_router
        try:
            payload = await request.json()
            router = get_webhook_router()
            routed_count = await router.route_event(payload)
            return {"routed_to": routed_count}
        except Exception as e:
            logger.error(f"Webhook event processing failed: {e}", exc_info=True)
            return {"routed_to": 0, "error": str(e)}

    # Closed path — explicit 410 so the dispatcher's failed-POST logs are
    # not misread as a transient error.
    raise HTTPException(
        status_code=410,
        detail=(
            "Webhook event intake is deprecated; events now arrive via the "
            "Hubitat eventsocket. Set WEBHOOK_INTAKE_ENABLED=true to "
            "temporarily re-open this endpoint."
        ),
    )


@app.post("/api/webhook/mode", tags=["webhooks"])
async def handle_mode_webhook(request: Request):
    """Handle mode change webhook from Hubitat."""
    from services.webhook_router import get_webhook_router

    try:
        payload = await request.json()
        logger.info(f"Mode change webhook: {payload}")

        router = get_webhook_router()
        notified = await router.route_mode_change(payload)

        return {"notified": notified}
    except Exception as e:
        logger.error(f"Mode webhook processing failed: {e}", exc_info=True)
        return {"notified": 0, "error": str(e)}


# =============================================================================
# Modes
# =============================================================================


# =============================================================================
# Settings (cascade: system → app-type → instance)
# =============================================================================


@app.get("/api/system_settings", tags=["settings"])
async def list_system_settings(ui_only: bool = Query(True)):
    """
    List system-wide settings. Default: only UI-exposed ones.
    Set ui_only=false to include internal knobs.
    """
    params = {"order": "key"}
    if ui_only:
        params["ui_exposed"] = "eq.true"
    try:
        r = await aget(
            f"{os.environ.get('POSTGREST_URL', 'http://postgrest:3001')}/system_settings",
            params=params,
            timeout=5,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"list_system_settings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/system_settings/{key}", tags=["settings"])
async def get_system_setting(key: str):
    """Get a single system setting by key. Returns coerced value."""
    from services.settings_resolver import get_resolver, _coerce
    resolver = get_resolver()
    # Force a fresh fetch so the caller sees the latest value
    resolver._sys_cache.pop(key, None)
    row = resolver._fetch_system_row(key)
    if row is None:
        raise HTTPException(status_code=404, detail=f"setting {key!r} not found")
    return {
        "key": row["key"],
        "value": _coerce(row["value"], row["value_type"]),
        "value_type": row["value_type"],
    }


class SystemSettingPatch(BaseModel):
    """Body for PATCH /api/system_settings/{key}."""
    value: Any


@app.patch("/api/system_settings/{key}", tags=["settings"])
async def patch_system_setting(key: str, body: SystemSettingPatch):
    """Update a system setting. Type-coerces against the stored value_type."""
    from services.settings_resolver import get_resolver
    resolver = get_resolver()
    ok = resolver.set_system(key, body.value)
    if not ok:
        raise HTTPException(status_code=400, detail=f"could not set {key!r}")
    return {"key": key, "value": body.value}


# =============================================================================
# Device Name Normalizer — global feature with a MANDATORY preview-confirm gate.
# The UI calls /preview (a no-op scan) and shows the proposed renames in a modal;
# only after the user confirms does it call /enable (which also runs one pass so
# the previewed renames apply immediately).
# =============================================================================


@app.get("/api/device-name-normalizer/status", tags=["settings"])
async def device_name_normalizer_status():
    """Current on/off state of the device-name normalizer."""
    from services import device_name_normalizer as dnn
    from services.settings_resolver import get_resolver
    dnn.seed_settings()
    resolver = get_resolver()
    resolver.invalidate_all()
    return {
        "enabled": bool(resolver.get_system(dnn.SETTING_ENABLED, False)),
        "apply": bool(resolver.get_system(dnn.SETTING_APPLY, False)),
    }


@app.get("/api/device-name-normalizer/preview", tags=["settings"])
async def device_name_normalizer_preview():
    """
    NO-OP dry-run: scan device labels and return the renames that WOULD be
    applied, without changing any setting or touching any device. This backs
    the mandatory confirmation modal shown before the feature is enabled.
    """
    from services import device_name_normalizer as dnn
    return dnn.preview()


@app.post("/api/device-name-normalizer/enable", tags=["settings"])
async def device_name_normalizer_enable():
    """
    Turn the feature ON (sets enabled + apply true) and run one pass immediately
    so the previewed renames are applied now. The frontend only calls this after
    the user confirms the preview modal.
    """
    from services import device_name_normalizer as dnn
    from services.settings_resolver import get_resolver
    dnn.seed_settings()
    resolver = get_resolver()
    ok1 = resolver.set_system(dnn.SETTING_ENABLED, True)
    ok2 = resolver.set_system(dnn.SETTING_APPLY, True)
    if not (ok1 and ok2):
        raise HTTPException(status_code=400, detail="could not enable normalizer")
    resolver.invalidate_all()
    dnn.trigger_pass_background()
    return {"enabled": True, "apply": True}


@app.post("/api/device-name-normalizer/disable", tags=["settings"])
async def device_name_normalizer_disable():
    """Turn the feature OFF (sets enabled + apply false). Safe; no preview needed."""
    from services import device_name_normalizer as dnn
    from services.settings_resolver import get_resolver
    dnn.seed_settings()
    resolver = get_resolver()
    resolver.set_system(dnn.SETTING_ENABLED, False)
    resolver.set_system(dnn.SETTING_APPLY, False)
    resolver.invalidate_all()
    return {"enabled": False, "apply": False}


@app.get("/api/app_types/{type_name}/settings", tags=["settings"])
async def list_app_type_settings(type_name: str):
    """
    List per-app-type global settings for the named app type.
    """
    pg = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    # Look up app_type id from name
    r = await aget(f"{pg}/app_types",
                      params={"type_name": f"eq.{type_name}",
                              "select": "id"},
                      timeout=5)
    rows = r.json() if r.status_code == 200 else []
    if not rows:
        raise HTTPException(status_code=404,
                            detail=f"app_type {type_name!r} not found")
    app_type_id = rows[0]["id"]
    r = await aget(f"{pg}/app_type_settings",
                      params={"app_type_id": f"eq.{app_type_id}",
                              "ui_exposed": "eq.true",
                              "order": "key"},
                      timeout=5)
    r.raise_for_status()
    return r.json()


class AppTypeSettingPatch(BaseModel):
    """Body for PATCH /api/app_types/{type_name}/settings/{key}."""
    value: Any


@app.get("/api/instances/{instance_id}/setting-exceptions", tags=["settings"])
async def list_instance_setting_exceptions(instance_id: int):
    """List all per-field exceptions granted to this instance."""
    pg = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    r = await aget(
        f"{pg}/instance_setting_exceptions",
        params={
            "instance_id": f"eq.{instance_id}",
            "select": "id,setting_path,reason,granted_at",
        },
        timeout=5,
    )
    r.raise_for_status()
    return r.json()


class SettingExceptionGrant(BaseModel):
    """Body for POST /api/instances/{id}/setting-exceptions."""
    setting_path: str
    reason: Optional[str] = None


@app.post("/api/instances/{instance_id}/setting-exceptions", tags=["settings"])
async def grant_instance_setting_exception(
    instance_id: int, body: SettingExceptionGrant,
):
    """
    Grant this instance an exception for `setting_path` — bypasses
    system-enforced validation (e.g., motion_timeout_floor_seconds) for that
    field only. Audit record kept (granted_at).
    """
    pg = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    r = await apost(
        f"{pg}/instance_setting_exceptions",
        json={
            "instance_id": instance_id,
            "setting_path": body.setting_path,
            "reason": body.reason,
        },
        headers={
            "Content-Type": "application/json",
            "Prefer": "return=representation,resolution=merge-duplicates",
        },
        timeout=5,
    )
    if r.status_code in (200, 201):
        body_json = r.json()
        return body_json[0] if isinstance(body_json, list) and body_json else body_json
    raise HTTPException(status_code=r.status_code, detail=r.text)


@app.delete(
    "/api/instances/{instance_id}/setting-exceptions/{setting_path:path}",
    tags=["settings"],
)
async def revoke_instance_setting_exception(
    instance_id: int, setting_path: str,
):
    """Revoke a per-field exception. setting_path uses path-style routing so
    nested keys like `modeTimeouts.Night` work without URL encoding."""
    pg = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    r = await adelete(
        f"{pg}/instance_setting_exceptions",
        params={
            "instance_id": f"eq.{instance_id}",
            "setting_path": f"eq.{setting_path}",
        },
        timeout=5,
    )
    if r.status_code not in (200, 204):
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return {"ok": True, "setting_path": setting_path}


@app.patch(
    "/api/app_types/{type_name}/settings/{key}",
    tags=["settings"],
)
async def patch_app_type_setting(
    type_name: str, key: str, body: AppTypeSettingPatch,
):
    """Update a per-app-type setting."""
    pg = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    r = await aget(f"{pg}/app_types",
                      params={"type_name": f"eq.{type_name}",
                              "select": "id"},
                      timeout=5)
    rows = r.json() if r.status_code == 200 else []
    if not rows:
        raise HTTPException(status_code=404,
                            detail=f"app_type {type_name!r} not found")
    app_type_id = rows[0]["id"]
    from services.settings_resolver import get_resolver
    ok = get_resolver().set_app_type(app_type_id, key, body.value)
    if not ok:
        raise HTTPException(status_code=400,
                            detail=f"could not set ({type_name}, {key})")
    return {"app_type": type_name, "key": key, "value": body.value}


# =============================================================================
# Hub Classification (native-hub device routing)
# =============================================================================


@app.post("/api/hub/classify", tags=["hub-classification"])
async def run_hub_classification():
    """
    Run device classification across all configured hubs.

    Queries each hub's Maker API, classifies devices as native vs
    mesh-linked, builds cross-reference routing table, and writes
    to the device_hub_mapping table.

    This enables the DeviceCommander to route commands directly to
    the hub that physically owns each device (bypassing Hub Mesh relay).
    """
    from services.device_to_hubs_classifier import run_classification, invalidate_cache
    import asyncio

    try:
        loop = asyncio.get_running_loop()
        # Run classification in executor to avoid blocking event loop
        # (makes HTTP requests to all 4 hubs)
        result = await loop.run_in_executor(None, run_classification)
        # Invalidate in-memory routing cache so new data is picked up
        invalidate_cache()
        return result
    except Exception as e:
        logger.error(f"Hub classification failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/hub/mapping", tags=["hub-classification"])
async def get_hub_mapping(
    hub_name: Optional[str] = Query(None, description="Filter by native hub name"),
    protocol: Optional[str] = Query(None, description="Filter by protocol"),
):
    """
    Get the current device-to-hub mapping table.

    Returns all classified devices with their native hub, protocol,
    and mirror info. Optionally filter by hub or protocol.
    """
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    params = {}
    if hub_name:
        params["native_hub_name"] = f"eq.{hub_name}"
    if protocol:
        params["protocol"] = f"eq.{protocol}"
    params["order"] = "device_label.asc"

    try:
        resp = req.get(
            f"{postgrest_url}/device_hub_mapping",
            params=params,
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if resp.status_code == 200:
            entries = resp.json()
            return {
                "count": len(entries),
                "entries": entries,
            }
        return {"error": f"PostgREST returned {resp.status_code}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/hub/mapping/stats", tags=["hub-classification"])
async def get_hub_mapping_stats():
    """
    Get summary statistics for the device hub mapping.

    Returns per-hub and per-protocol counts.
    """
    import requests as req
    from collections import defaultdict

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

    try:
        resp = req.get(
            f"{postgrest_url}/device_hub_mapping",
            headers={"Accept": "application/json"},
            timeout=10,
        )
        if resp.status_code != 200:
            return {"error": f"PostgREST returned {resp.status_code}"}

        entries = resp.json()
        hub_counts = defaultdict(int)
        proto_counts = defaultdict(int)
        hub_proto = defaultdict(lambda: defaultdict(int))

        for e in entries:
            hub = e.get("native_hub_name", "unknown")
            proto = e.get("protocol", "unknown")
            hub_counts[hub] += 1
            proto_counts[proto] += 1
            hub_proto[hub][proto] += 1

        return {
            "total": len(entries),
            "by_hub": dict(hub_counts),
            "by_protocol": dict(proto_counts),
            "hub_protocol_matrix": {
                hub: dict(protos) for hub, protos in hub_proto.items()
            },
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Matter Protocol
# =============================================================================


class MatterCommissionRequest(BaseModel):
    """Request body for commissioning a new Matter device."""
    code: str  # QR code string (MT:...) or manual pairing code
    # Setup-code vault (reclaim-as-primary phase, 2026-07-12): when the operator
    # commissions with a FACTORY/label code (not a hub/HomeKit ephemeral ECM
    # code), flag it so we capture it encrypted for later reclaim. Ephemeral ECM
    # codes are worthless to store, so is_factory defaults False (no capture).
    is_factory: bool = False
    unique_id: Optional[str] = None   # discovery handle to key the vault row
    mac: Optional[str] = None
    serial: Optional[str] = None
    device_name: Optional[str] = None


class MatterMapRequest(BaseModel):
    """Request body for mapping a Hubitat device to a Matter node."""
    hubitat_device_id: str
    matter_node_id: int
    matter_endpoint_id: int = 1
    device_name: Optional[str] = None


@app.get("/api/matter/watchdog", tags=["matter"])
async def matter_watchdog_health():
    """
    Matter self-healing watchdog health snapshot — for the UI status panel /
    failure reports. Reports connection state, per-node reachability
    (available vs "not available"), last re-interview attempts, last error and
    last check time. See services/matter_watchdog.py.
    """
    from services.matter_watchdog import get_health
    return get_health()


@app.post("/api/matter/service/{action}", tags=["matter"])
async def matter_service_control(action: str):
    """
    Stop / start / restart the matter-server CONTAINER from the UI.

    The app container has no Docker socket (by design), so — exactly like
    POST /api/restart — we write a trigger to the tmpfs file the host-side
    smarthome-restart-watcher polls; the watcher runs the docker command
    (`docker stop|start|restart smarthome-matter-server`, with a
    `docker compose up -d matter-server` create-fallback for start/restart).

    action ∈ {stop, start, restart}. Returns 503 if the trigger dir isn't
    mounted (watcher not installed yet — run ./start.sh once on the host).
    """
    action = (action or "").lower()
    if action not in ("stop", "start", "restart"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action '{action}'. Use stop, start, or restart.",
        )
    trigger_dir = os.path.dirname(RESTART_TRIGGER_FILE)
    if not os.path.isdir(trigger_dir):
        raise HTTPException(
            status_code=503,
            detail="Restart watcher not available. Run ./start.sh once on the host to install it.",
        )
    logger.info(f"[Matter] service '{action}' requested via UI")

    def _write_trigger():
        import time as _time
        _time.sleep(0.5)
        try:
            with open(RESTART_TRIGGER_FILE, 'w') as f:
                f.write(f'matter:{action} {_time.time()}')
            logger.info(f"[Matter] wrote 'matter:{action}' trigger — host watcher will act")
        except Exception as e:
            logger.error(f"[Matter] failed to write matter trigger: {e}")

    import threading as _threading
    _threading.Thread(target=_write_trigger, daemon=True, name=f'matter-{action}-trigger').start()
    return {
        "success": True,
        "action": action,
        "message": f"matter-server {action} initiated on the host (~10-30s). Refresh status to confirm.",
    }


class MatterRemoveBody(BaseModel):
    """Body for the Matter remove endpoints (optional reason + force flag)."""
    reason: Optional[str] = None
    force: Optional[bool] = False


@app.post("/api/matter/devices/{node_id}/remove", tags=["matter"])
async def matter_remove_device_endpoint(node_id: int, body: Optional[MatterRemoveBody] = None):
    """
    Remove a COMMISSIONED Matter node: decommission from OUR fabric (remove_node)
    + SOFT-delete its registry row (keeps the row + canonical id so a
    same-identity re-add reactivates it) + log to dshub.matter_removals. Use for
    stale/ghost commissions. force=true skips the decommission (DB-only) for
    dead/unreachable nodes. Safe on already-gone nodes.
    """
    from services.matter_removal import remove_matter_device
    reason = (body.reason if body and body.reason else "")
    force = bool(body and body.force)
    return await remove_matter_device(node_id, reason=reason, performed_by="operator", force=force)


@app.post("/api/matter/discovered/{unique_id}/remove", tags=["matter"])
async def matter_remove_discovered_endpoint(unique_id: str, body: Optional[MatterRemoveBody] = None):
    """
    Remove a DISCOVERED device by unique_id (the card key) — works whether or not
    it's commissioned. Decommissions its node if it has one (unless force), then
    SOFT-deletes the row (kept + rediscoverable via a re-scan). force=true = DB-only.
    """
    from services.matter_removal import remove_matter_device_by_uid
    reason = (body.reason if body and body.reason else "")
    force = bool(body and body.force)
    return await remove_matter_device_by_uid(unique_id, reason=reason, performed_by="operator", force=force)


@app.post("/api/matter/discovered/remove-all", tags=["matter"])
async def matter_remove_all_discovered_endpoint(body: Optional[MatterRemoveBody] = None):
    """
    Soft-remove ALL active discovered devices (decommission each unless force).
    Rows are kept + marked removed, so a re-scan brings them all back. Returns
    {total, removed, force}.
    """
    from services.matter_removal import remove_all_discovered
    reason = (body.reason if body and body.reason else "bulk remove")
    force = bool(body and body.force)
    return await remove_all_discovered(force=force, reason=reason, performed_by="operator")


@app.get("/api/matter/status", tags=["matter"])
async def matter_status():
    """
    Get matter-server connection status.

    Returns connection state and server info if connected.
    """
    from services.matter_client import get_matter_client

    client = get_matter_client()
    status = {"connected": client.is_connected, "url": client.url}

    if client.is_connected:
        try:
            info = await client.get_server_info()
            status["server_info"] = info
        except Exception as e:
            status["server_info_error"] = str(e)

    return status


class MatterNodeCommandBody(BaseModel):
    """Body for POST /api/matter/nodes/{node_id}/command — direct Matter test."""
    command: str                       # 'on' | 'off' | 'level'
    endpoint_id: Optional[int] = 1
    level: Optional[int] = None        # 0-100, required when command == 'level'


@app.post("/api/matter/nodes/{node_id}/command", tags=["matter"])
async def matter_node_command(node_id: int, body: MatterNodeCommandBody):
    """
    Command a Matter node DIRECTLY via matter-server (on/off) AND return the
    VERBOSE backend trace so the operator sees exactly what happened — every
    matter-client log line during the invoke, the raw result, and the real error
    (no more opaque "Unknown error").

    Always HTTP 200 with a structured body: {success, detail, result, endpoint,
    trace:[{ts,level,msg}]}. Tests the MATTER device itself (OnOff cluster 6),
    independent of any Hubitat mapping. success=false + detail + trace when the
    invoke is dropped (the current "reads work, invokes fail" bug is visible here).
    """
    from services.matter_client import get_matter_client
    from services.matter_debug import get_diagnostics
    command = (body.command or "").strip().lower()
    if command not in ("on", "off", "level", "setlevel"):
        raise HTTPException(status_code=400, detail="command must be 'on', 'off', or 'level'")
    is_level = command in ("level", "setlevel")
    if is_level and body.level is None:
        raise HTTPException(status_code=400, detail="level (0-100) is required for a level command")
    client = get_matter_client()
    diag = get_diagnostics()
    endpoint = body.endpoint_id if body.endpoint_id is not None else 1
    seq0 = diag.oplog._seq          # op-log position BEFORE the command
    ok, result, detail = False, None, None
    cluster = 8 if is_level else 6  # LevelControl vs OnOff — for the log line only
    logger.info(f"[matter cmd] node={node_id} ep={endpoint} cmd={command} -> invoke cluster {cluster}")
    try:
        if not client.is_connected and not await client.connect():
            detail = "Cannot connect to matter-server (is the container running?)"
        elif is_level:
            lvl = max(0, min(100, int(body.level)))
            result = await client.send_hubitat_command(node_id, endpoint, "setLevel", [lvl])
            ok = result is not None
        else:
            result = await client.send_hubitat_command(node_id, endpoint, command)
            ok = result is not None
            if not ok:
                detail = (f"send returned no result for node {node_id} ep{endpoint} — "
                          f"matter-server gave no ack (untranslatable, or the invoke was "
                          f"dropped). See trace.")
            logger.info(f"[matter cmd] node={node_id} cmd={command} result={result!r} ok={ok}")
    except Exception as e:  # noqa: BLE001 - surface the real error to the UI
        detail = f"{type(e).__name__}: {e}"
        logger.error(f"[matter cmd] node={node_id} cmd={command} FAILED: {detail}")
    trace = diag.op_log(since_seq=seq0, limit=120).get("records", [])
    return {
        "success": ok, "node_id": node_id, "command": command, "endpoint": endpoint,
        "result": result, "detail": detail, "trace": trace,
    }


# ---------------------------------------------------------------------------
# Matter troubleshooting / diagnostics surface (services/matter_debug.py).
# The operator (Matter debug console) and agents (these routes) call the SAME
# MatterDiagnostics class — one source of truth for fabric state + repair.
# ---------------------------------------------------------------------------

@app.get("/api/matter/nodes/{node_id}/fabrics", tags=["matter"])
async def matter_node_fabrics(node_id: int):
    """OperationalCredentials fabric table: count/5, per-fabric index/vendor/
    label, which are OURS/current, and the orphan count."""
    from services.matter_debug import get_diagnostics
    try:
        return await get_diagnostics().read_fabrics(node_id)
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:  # noqa: BLE001
        # A node not present in the current controller (e.g. 0 nodes on the
        # fresh matterjs fabric post-migration, or a stale/old node id) makes
        # the underlying get_node raise a Matter error. Don't 500 the debug
        # console — return a clean, readable result so the actions still render.
        logger.warning(f"read_fabrics(node {node_id}) failed: {e}")
        return {"node_id": node_id, "error": f"{type(e).__name__}: {e}",
                "fabrics": [], "commissioned_fabrics": None, "max_fabrics": 5,
                "our_orphan_count": 0}


@app.get("/api/matter/nodes/{node_id}/diagnostics", tags=["matter"])
async def matter_node_diagnostics(node_id: int):
    """Full per-node dump: fabrics + availability + OnOff/Level state + identity."""
    from services.matter_debug import get_diagnostics
    try:
        return await get_diagnostics().node_diagnostics(node_id)
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:  # noqa: BLE001
        # See matter_node_fabrics: a node absent from the current controller
        # raises rather than returning None. Keep the console usable.
        logger.warning(f"node_diagnostics(node {node_id}) failed: {e}")
        return {"node_id": node_id, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/matter/server/diagnostics", tags=["matter"])
async def matter_server_diagnostics():
    """matter-server health: connection, WS, circuit-breaker, node reachability."""
    from services.matter_debug import get_diagnostics
    return await get_diagnostics().server_diagnostics()


@app.get("/api/matter/debug/log", tags=["matter"])
async def matter_debug_log(since_seq: int = 0, limit: int = 200):
    """Verbose live stream (ring buffer) of matter-client operations for the
    debug console. Poll with the returned last_seq for an incremental tail."""
    from services.matter_debug import get_diagnostics
    return get_diagnostics().op_log(since_seq=since_seq, limit=limit)


_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")
_MATTER_SERVER_LOG = "/matter-logs/matter-server.log"


class FabricLabelBody(BaseModel):
    """Body for POST /api/matter/fabric-label."""
    label: str
    relabel_existing: bool = True   # also UpdateFabricLabel on current nodes


@app.get("/api/matter/fabric-label", tags=["matter"])
async def matter_get_fabric_label():
    """The fabric label matterjs stamps on new commissions (default MOBIUS.HOME).
    Informational: this is the admin name a device STORES for our fabric — shown
    in our debug console + the Hubitat Fabric Manager driver, NOT in Apple Home
    (that reads our VendorID 0xFFF1 → 'Matter Test')."""
    from services.matter_client import get_matter_client
    mc = get_matter_client()
    if not mc.is_connected and not await mc.connect():
        raise HTTPException(status_code=503, detail="matter controller unreachable")
    return {"default_fabric_label": await mc.get_fabric_label(),
            "note": "Apple Home shows 'Matter Test' from our test VendorID 0xFFF1, not this label."}


@app.post("/api/matter/fabric-label", tags=["matter"])
async def matter_set_fabric_label(body: FabricLabelBody):
    """Set the default fabric label (future commissions) and, by default,
    UpdateFabricLabel on every CURRENT node (0x3E cmd 0x09) so already-commissioned
    devices get relabelled too. Max 32 chars (Matter spec)."""
    label = (body.label or "").strip()[:32]
    if not label:
        raise HTTPException(status_code=400, detail="label required (1–32 chars)")
    from services.matter_client import get_matter_client
    mc = get_matter_client()
    if not mc.is_connected and not await mc.connect():
        raise HTTPException(status_code=503, detail="matter controller unreachable")
    await mc.set_default_fabric_label(label)
    relabelled, errors = [], []
    if body.relabel_existing:
        try:
            nodes = await mc.get_nodes() or []
        except Exception as e:  # noqa: BLE001
            nodes = []
            errors.append(f"get_nodes: {e}")
        for n in nodes:
            nid = n.get("node_id") or n.get("nodeId")
            if nid is None:
                continue
            try:
                # UpdateFabricLabel — OperationalCredentials (62) command 0x09.
                await mc.send_command(nid, 0, 62, "UpdateFabricLabel", {"label": label})
                relabelled.append(nid)
            except Exception as e:  # noqa: BLE001
                errors.append({"node": nid, "error": str(e)})
    logger.info(f"matter fabric-label set to '{label}'; relabelled nodes {relabelled}")
    return {"default_fabric_label": label, "relabelled_nodes": relabelled, "errors": errors}


@app.get("/api/matter/server-log", tags=["matter"])
async def matter_server_log(since: int = -1, max_bytes: int = 60000):
    """
    Tail the matter-server (matterjs) CHIP-level log — the SERVER-side detail
    (PASE/CASE, attestation, AddNOC status, error codes) that the client op-log
    can't see. matterjs writes it to a shared volume (LOG_FILE); the app tails
    it read-only. Byte-offset incremental: pass the returned `offset` back as
    `since` for a live stream. since=-1 (default) starts near the tail.

    Returns {available, lines[], offset}. `available:false` until the next full
    ./start.sh wires the shared volume + LOG_FILE.
    """
    import os
    path = _MATTER_SERVER_LOG
    try:
        size = os.path.getsize(path)
    except OSError:
        return {"available": False, "lines": [], "offset": 0}
    if since < 0 or since > size:
        start = max(0, size - max_bytes)   # first poll: last ~max_bytes only
    else:
        start = since
        if size - start > max_bytes:       # don't return a huge backlog at once
            start = size - max_bytes
    try:
        def _read():
            with open(path, "r", errors="replace") as f:
                f.seek(start)
                data = f.read()
                return data, f.tell()
        data, offset = await asyncio.to_thread(_read)
    except Exception as e:  # noqa: BLE001
        return {"available": False, "lines": [], "offset": since, "error": str(e)}
    lines = [_ANSI_RE.sub("", ln).rstrip() for ln in data.splitlines() if ln.strip()]
    return {"available": True, "lines": lines, "offset": offset}


class MatterFabricRemoveBody(BaseModel):
    """Optional reason for a fabric removal."""
    reason: Optional[str] = None


@app.post("/api/matter/nodes/{node_id}/fabrics/{fabric_index}/remove", tags=["matter"])
async def matter_remove_fabric(node_id: int, fabric_index: int,
                               body: Optional[MatterFabricRemoveBody] = None):
    """RemoveFabric by index — frees ONE fabric slot (console clears a specific
    orphaned fabric). Remote command; no device reset."""
    from services.matter_debug import get_diagnostics
    try:
        return await get_diagnostics().remove_fabric(node_id, fabric_index)
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"RemoveFabric failed: {e}")


class MatterDecommissionBody(BaseModel):
    """keep_current=True clears ONLY orphaned OUR fabrics (device stays ours);
    False fully leaves the device."""
    keep_current: Optional[bool] = False


async def _matter_reconcile_decommissioned(node_ids: List[int]) -> Dict[str, int]:
    """
    DB half of a decommission — keep the database honest about the fabric.

    A decommission removes our fabric FROM THE DEVICE, but until 2026-07-12
    nothing cleared the DB side, so hubitat_matter_devices rows kept
    is_commissioned=true + a dead our_node_id and device_matter_map kept rows
    pointing at nodes that no longer exist ("mapping stale — no current
    device" ghosts; Commission All then skipped those devices as 'already
    commissioned'). For every decommissioned node id:
      - hubitat_matter_devices: is_commissioned=false, our_node_id=null
      - device_matter_map: DELETE the link rows (explicit scoped DELETE of
        link records only — no device/table cascade; per the no-CASCADE policy)
    Best-effort per id; returns {"devices_reconciled": n, "mappings_deleted": m}.
    """
    if not node_ids:
        return {"devices_reconciled": 0, "mappings_deleted": 0}
    import requests as req
    pg = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    ids_csv = ",".join(str(i) for i in node_ids)
    devices = mappings = 0
    try:
        r = await asyncio.to_thread(lambda: req.patch(
            f"{pg}/hubitat_matter_devices",
            params={"our_node_id": f"in.({ids_csv})"},
            json={"is_commissioned": False, "our_node_id": None},
            headers={"Content-Type": "application/json", "Prefer": "return=representation"},
            timeout=5))
        devices = len(r.json()) if r.ok else 0
    except Exception as e:  # noqa: BLE001
        logger.warning(f"decommission reconcile: device rows PATCH failed: {e}")
    try:
        r = await asyncio.to_thread(lambda: req.delete(
            f"{pg}/device_matter_map",
            params={"matter_node_id": f"in.({ids_csv})"},
            headers={"Prefer": "return=representation"},
            timeout=5))
        mappings = len(r.json()) if r.ok else 0
    except Exception as e:  # noqa: BLE001
        logger.warning(f"decommission reconcile: mapping rows DELETE failed: {e}")
    if devices or mappings:
        logger.info(f"decommission reconcile: {devices} device row(s) uncommissioned, "
                    f"{mappings} mapping(s) deleted for nodes [{ids_csv}]")
    return {"devices_reconciled": devices, "mappings_deleted": mappings}


@app.post("/api/matter/nodes/{node_id}/decommission", tags=["matter"])
async def matter_decommission_node(node_id: int, body: Optional[MatterDecommissionBody] = None):
    """Remove OUR fabrics from one device (frees the slots we filled). With
    keep_current=false (full leave) the DB is reconciled too: the device row
    goes back to uncommissioned and its mapping rows are deleted, so the UI
    and Commission All see reality instead of a ghost."""
    from services.matter_debug import get_diagnostics
    keep = bool(body and body.keep_current)
    try:
        result = await get_diagnostics().decommission_node(node_id, keep_current=keep)
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not keep:  # fully left the device -> clear DB state for this node
        result["reconciled"] = await _matter_reconcile_decommissioned([node_id])
    return result


@app.post("/api/matter/decommission-all", tags=["matter"])
async def matter_decommission_all(body: Optional[MatterDecommissionBody] = None):
    """Decommission OUR fabrics from EVERY commissioned node (UI warning-gated).
    With keep_current=false the DB is reconciled for every node that was in the
    fabric (rows back to uncommissioned + mappings deleted) — previously the DB
    kept claiming is_commissioned=true after a full decommission, which is why
    'Decommission all' looked like it did nothing and Commission All skipped
    everything."""
    from services.matter_client import get_matter_client
    from services.matter_debug import get_diagnostics
    keep = bool(body and body.keep_current)
    # Snapshot the node ids BEFORE decommissioning (afterwards they're gone).
    node_ids: List[int] = []
    if not keep:
        try:
            client = get_matter_client()
            if client.is_connected or await client.connect():
                nodes = await client.get_nodes()
                node_ids = [int(n.get("node_id")) for n in (nodes or [])
                            if n.get("node_id") is not None]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"decommission-all: pre-snapshot of node ids failed: {e}")
    try:
        result = await get_diagnostics().decommission_all(keep_current=keep)
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not keep and node_ids:
        result["reconciled"] = await _matter_reconcile_decommissioned(node_ids)
    return result


@app.get("/api/matter/nodes", tags=["matter"])
async def matter_nodes():
    """
    List all commissioned Matter nodes.

    Connects to matter-server if not already connected.
    """
    from services.matter_client import get_matter_client

    client = get_matter_client()
    if not client.is_connected:
        connected = await client.connect()
        if not connected:
            raise HTTPException(
                status_code=503,
                detail="Cannot connect to matter-server"
            )

    try:
        nodes = await client.get_nodes()

        # Enrich nodes with friendly names from our discovered devices table.
        # Match by unique_id: Matter Basic Information cluster (40), attr 18 = UniqueID
        # Enrich with friendly names from hubitat_matter_devices.
        # Two lookups: by our_node_id (direct) and by unique_id (fallback).
        import requests as req
        postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
        by_node_id = {}
        by_unique_id = {}
        try:
            disc_resp = req.get(
                f"{postgrest_url}/hubitat_matter_devices",
                headers={"Accept": "application/json"},
                timeout=5
            )
            if disc_resp.ok:
                for d in disc_resp.json():
                    if d.get('our_node_id'):
                        by_node_id[d['our_node_id']] = d
                    by_unique_id[d['unique_id']] = d
        except Exception:
            pass

        for node in nodes:
            node_id = node.get('node_id') or node.get('nodeId')

            # Primary: match by our_node_id (set during commission)
            match = by_node_id.get(node_id)

            # Fallback: match by UniqueID attribute
            if not match:
                attrs = node.get('attributes', {})
                for key, val in attrs.items():
                    if '/40/18' in key and isinstance(val, str) and val in by_unique_id:
                        match = by_unique_id[val]
                        break

            if match:
                # Prefer Hubitat friendly name over Matter product name
                node['_device_name'] = (
                    match.get('maker_api_device_name')
                    or match.get('device_name')
                )
                node['_hubitat_device_id'] = match.get('maker_api_device_id')
                # Enrich for the Matter UI: hub link target + responsiveness
                # signals (matter_discovery refreshes is_online/last_seen_at).
                node['_hub_ip'] = match.get('hub_ip')
                node['_is_online'] = match.get('is_online')
                node['_last_seen_at'] = match.get('last_seen_at')

                # Resolve to the CURRENT canonical device via the exact
                # (hub_ip, hubitat_id) anchor (2026-06-19). This is what the
                # UI's Test ON/OFF and staleness must use — NOT the frozen
                # maker-api id above (the #660 bug). None => stale mapping.
                from services.matter_mapping import resolve_node_to_device
                _canon = resolve_node_to_device(node_id)
                node['_canonical_id'] = _canon.get('id') if _canon else None
                node['_canonical_label'] = _canon.get('label') if _canon else None
                node['_canonical_hubitat_id'] = _canon.get('hubitat_id') if _canon else None
                node['_mapping_stale'] = _canon is None

                # Backfill: if matched by UniqueID but our_node_id not set, update DB
                if not match.get('our_node_id') and node_id:
                    try:
                        req.patch(
                            f"{postgrest_url}/hubitat_matter_devices",
                            params={"unique_id": f"eq.{match['unique_id']}"},
                            json={"our_node_id": node_id},
                            headers={"Content-Type": "application/json"},
                            timeout=5
                        )
                        logger.info(f"Backfilled our_node_id={node_id} for {match['unique_id']}")
                    except Exception:
                        pass

        return nodes
    except Exception as e:
        logger.error(f"Failed to get Matter nodes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/matter/reconcile", tags=["matter"])
async def matter_reconcile():
    """
    Reconcile device_matter_map with commissioned nodes.
    Matches commissioned matter-server nodes to discovered Hubitat devices
    by UniqueID and creates missing mapping entries automatically.
    """
    from services.matter_discovery import get_matter_discovery_service
    service = get_matter_discovery_service()
    reconciled = await service._reconcile_mappings()
    return {"reconciled": reconciled}


def _vault_pg_conn():
    """psycopg2 connection for SERVER-SIDE-ONLY tables (the setup-code vault is
    never exposed via PostgREST). Same env as run_db_migrations()."""
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get('POSTGRES_HOST', 'postgres'),
        port=os.environ.get('POSTGRES_PORT', '5432'),
        dbname=os.environ.get('POSTGRES_DB', 'smarthome'),
        user=os.environ.get('POSTGRES_USER', 'smarthome_api'),
        password=os.environ.get('POSTGRES_PASSWORD', ''),
    )


async def _vault_store_setup_code(code: str, *, unique_id: Optional[str] = None,
                                  mac: Optional[str] = None, serial: Optional[str] = None,
                                  device_name: Optional[str] = None,
                                  is_factory: bool = True,
                                  source: str = "commission") -> bool:
    """
    Encrypt + upsert a device setup code into `matter_device_codes` (best-effort,
    fail-closed). Returns True if stored. No-op when is_factory is False (ephemeral
    ECM codes are worthless to vault) or the vault is unavailable (missing
    `cryptography` lib or `MATTER_CODE_ENC_KEY`). NEVER raises — a capture failure
    must not fail an otherwise-successful commission.
    """
    from services import matter_code_vault as vault
    if not is_factory or not code:
        return False
    if not vault.is_available():
        logger.info("setup-code vault unavailable (no cryptography lib or "
                    "MATTER_CODE_ENC_KEY) — skipping capture; set the key + ./deploy.sh.")
        return False
    enc = vault.encrypt(code)
    if enc is None:
        return False
    ct, nonce = enc
    fp = vault.key_fingerprint()

    def _upsert():
        import psycopg2
        conn = _vault_pg_conn()
        try:
            with conn, conn.cursor() as cur:
                # Prefer upsert on unique_id when we have it; else plain insert.
                if unique_id:
                    cur.execute(
                        """INSERT INTO matter_device_codes
                             (unique_id, mac, serial, device_name, code_ciphertext,
                              nonce, is_factory_code, source, key_fingerprint, updated_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
                           ON CONFLICT (unique_id) WHERE unique_id IS NOT NULL
                           DO UPDATE SET mac=EXCLUDED.mac, serial=EXCLUDED.serial,
                             device_name=EXCLUDED.device_name,
                             code_ciphertext=EXCLUDED.code_ciphertext,
                             nonce=EXCLUDED.nonce, is_factory_code=EXCLUDED.is_factory_code,
                             source=EXCLUDED.source, key_fingerprint=EXCLUDED.key_fingerprint,
                             updated_at=NOW()""",
                        (unique_id, mac, serial, device_name,
                         psycopg2.Binary(ct), psycopg2.Binary(nonce),
                         True, source, fp))
                else:
                    cur.execute(
                        """INSERT INTO matter_device_codes
                             (unique_id, mac, serial, device_name, code_ciphertext,
                              nonce, is_factory_code, source, key_fingerprint)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (unique_id, mac, serial, device_name,
                         psycopg2.Binary(ct), psycopg2.Binary(nonce),
                         True, source, fp))
        finally:
            conn.close()

    try:
        import psycopg2  # noqa: F401 (used inside _upsert via closure import)
        await asyncio.to_thread(_upsert)
        logger.info(f"setup-code vaulted (source={source}, key={fp}) for "
                    f"{device_name or unique_id or mac or 'device'}")
        return True
    except Exception as e:  # noqa: BLE001 — capture must never fail the caller
        logger.warning(f"setup-code vault upsert failed (best-effort): {e}")
        return False


@app.post("/api/matter/commission", tags=["matter"])
async def matter_commission(body: MatterCommissionRequest):
    """
    Commission a new Matter device using a pairing code.

    The code can be a QR code string (MT:...) or a manual numeric
    pairing code. A USB Bluetooth adapter is required on the server
    for BLE-based commissioning of new devices. Devices already
    paired to another controller (e.g., Hubitat) can be commissioned
    via on-network commissioning without BLE.

    Args:
        body: Contains the pairing code
    """
    from services.matter_client import get_matter_client

    # Normalize the pairing code (MSG-670): operators paste manual codes with
    # dashes/spaces ("1275-690-9098"). Strip separators from NUMERIC codes only
    # — QR payloads ("MT:...") use a base-38 charset where '-' is a legal
    # character, so those pass through verbatim. Without this we were relying
    # on the matter-server's parser to forgive formatting.
    code = (body.code or "").strip()
    if not code.upper().startswith("MT:"):
        code = code.replace("-", "").replace(" ", "")

    client = get_matter_client()
    if not client.is_connected:
        connected = await client.connect()
        if not connected:
            raise HTTPException(
                status_code=503,
                detail="Cannot connect to matter-server"
            )

    # IN-FLIGHT GUARD (2026-07-13, from the assistant's diagnosis MSG-1017).
    # This single-commission path had NO guard: the operator clicked Commission
    # again while attempt 1 was still in flight (21:27 accepted while 21:26 was
    # running), producing concurrent commission_with_code storms against the
    # same radio. A Matter controller pairs ONE device at a time, exactly like
    # the bulk path — so this now takes the SAME global mutex Commission All and
    # hub->hub copy use, and refuses with 409 (naming the holder) instead of
    # piling on. Short TTL: a single pairing is minutes, not an hour.
    # NOTE: the bulk worker never calls THIS endpoint (it calls the auto path),
    # so taking the lock here cannot deadlock a bulk run against itself.
    try:
        async with matter_pairing_lock(
            "commission_single", f"code:…{code[-4:]}", ttl_s=300,
        ):
            return await _do_commission_with_code(client, code, body)
    except PairingLockBusy as e:
        raise HTTPException(status_code=409, detail=str(e))


async def _do_commission_with_code(client, code: str, body: "MatterCommissionRequest"):
    """The actual commission + vault + error-translation, run while holding the
    global Matter-pairing mutex (see matter_commission)."""
    try:
        result = await client.commission_with_code(code)
        # Setup-code VAULT (reclaim-as-primary): if the operator flagged this as a
        # FACTORY/label code, capture it encrypted for later reclaim. Best-effort
        # + fail-closed — never let a capture hiccup fail a good commission.
        vaulted = await _vault_store_setup_code(
            code, unique_id=body.unique_id, mac=body.mac, serial=body.serial,
            device_name=body.device_name, is_factory=body.is_factory,
            source="commission")
        return {"message": "Device commissioned", "node": result,
                "factory_code_vaulted": vaulted}
    except Exception as e:
        raw = str(e)
        logger.error(f"Matter commissioning failed: {e}")
        # matter-server only surfaces a generic 'Commission with code failed for
        # node N'; the real CHIP cause (mDNS/discovery timeout, secure-pairing
        # 'Incorrect state', already-commissioned) lives in its log. Translate to
        # the actionable hint the operator actually needs.
        low = raw.lower()
        if "already" in low or "fabric already" in low or "0x7e" in low:
            hint = ("This device appears ALREADY commissioned in our fabric — "
                    "remove it first (Remove on its card), then recommission.")
        elif ("timed out" in low or "timeout" in low or "discovery" in low
                or "mdns" in low or "code failed" in low or "incorrect state" in low):
            hint = ("Couldn't reach the device to pair (discovery/mDNS timeout). "
                    "Usually the Hubitat pairing window EXPIRED — click 'Get Setup Code' "
                    "again and paste + Commission within ~2 minutes, with the device powered "
                    "on and on the LAN.")
            # POST-FAILURE DISCOVERY HINT (2026-07-13, assistant's ask MSG-1017-B).
            # "No commissionable device discovered" is deeply misleading when a
            # device IS sitting there in pairing mode but advertising a DIFFERENT
            # discriminator than the code targets — discovery filters it out and
            # says nothing. That exact mismatch (code short-disc 11 vs device
            # advertising 8) cost the operator an hour tonight. So: look, and say
            # what we actually see. Best-effort — a failing probe must never
            # replace the real error.
            probe = await _discovery_mismatch_hint(code)
            if probe:
                hint = probe + " " + hint
        elif "checksum" in low or "invalid checksum" in low:
            hint = ("The code's checksum is wrong — a typo, a truncated/expired code, or a "
                    "QR string pasted with characters dropped. Re-copy the FULL code.")
        elif "133" in low or "invalid command" in low:
            hint = ("The device rejected the pairing (CHIP 133). Common causes: (1) the "
                    "commissioning window CLOSED before we finished — generate a FRESH "
                    "'Get Setup Code' and Commission within ~2 min; (2) a SECOND Matter "
                    "controller was interfering (e.g. an orphan-purge side-boot — it's now "
                    "stopped); (3) you pasted the device's FACTORY label code on an "
                    "already-paired device — use the hub/HomeKit-generated window code instead.")
        elif "no memory" in low or "sendnoc" in low or "0x0b" in low:
            hint = ("The device's fabric table is FULL (~5 slots). Free one first: clear our "
                    "orphans (Debug console), or the Hubitat Fabric Manager driver, then retry.")
        else:
            hint = "Check the matter-server logs for the CHIP-level cause."
        raise HTTPException(status_code=502, detail=f"{raw} — {hint}")


class FactoryCodeBackfillRequest(BaseModel):
    """Manual label-scan backfill of a device's FACTORY setup code — for devices
    first commissioned by Hubitat/Apple (we never saw their factory code)."""
    code: str
    unique_id: Optional[str] = None
    mac: Optional[str] = None
    serial: Optional[str] = None
    device_name: Optional[str] = None


@app.post("/api/matter/devices/factory-code", tags=["matter"])
async def matter_backfill_factory_code(body: FactoryCodeBackfillRequest):
    """
    Vault a device's FACTORY setup code from a manual label/QR scan (the backfill
    path for devices we didn't first-commission). Requires the vault to be active
    (cryptography lib + MATTER_CODE_ENC_KEY) — 503 if not, so the operator gets a
    clear 'set the key' signal rather than a silent no-op. Needs at least one
    identity (unique_id / mac / serial)."""
    from services import matter_code_vault as vault
    if not vault.is_available():
        raise HTTPException(
            status_code=503,
            detail="Setup-code vault is not active: set MATTER_CODE_ENC_KEY in the "
                   "SMARTHOME secret and ensure the cryptography lib is installed "
                   "(./deploy.sh), then retry.")
    if not (body.unique_id or body.mac or body.serial):
        raise HTTPException(status_code=400,
                            detail="Provide at least one device identity (unique_id, mac, or serial).")
    code = (body.code or "").strip()
    if not code.upper().startswith("MT:"):
        code = code.replace("-", "").replace(" ", "")
    stored = await _vault_store_setup_code(
        code, unique_id=body.unique_id, mac=body.mac, serial=body.serial,
        device_name=body.device_name, is_factory=True, source="manual_backfill")
    if not stored:
        raise HTTPException(status_code=500, detail="Failed to vault the setup code.")
    return {"message": "Factory code vaulted", "device": body.device_name or body.unique_id}


@app.get("/api/matter/vault/status", tags=["matter"])
async def matter_vault_status():
    """
    Setup-code vault status for the UI — WITHOUT ever returning a code or
    ciphertext: {available, key_fingerprint, count, devices:[{unique_id, mac,
    device_name, source, is_factory_code, updated_at}]}. `available:false` means
    the key and/or cryptography lib is missing (capture is inert)."""
    from services import matter_code_vault as vault
    available = vault.is_available()

    def _list():
        import psycopg2
        conn = _vault_pg_conn()
        try:
            with conn, conn.cursor() as cur:
                # NEVER select code_ciphertext/nonce here — metadata only.
                cur.execute(
                    "SELECT unique_id, mac, device_name, source, is_factory_code, "
                    "updated_at FROM matter_device_codes ORDER BY updated_at DESC")
                rows = cur.fetchall()
                return [{"unique_id": r[0], "mac": r[1], "device_name": r[2],
                         "source": r[3], "is_factory_code": r[4],
                         "updated_at": r[5].isoformat() if r[5] else None} for r in rows]
        finally:
            conn.close()

    try:
        devices = await asyncio.to_thread(_list)
    except Exception as e:  # noqa: BLE001 — table may not exist pre-migration
        logger.debug(f"vault status list failed: {e}")
        devices = []
    return {"available": available, "key_fingerprint": vault.key_fingerprint(),
            "count": len(devices), "devices": devices}


@app.get("/api/matter/map", tags=["matter"])
async def matter_mappings():
    """Get all Hubitat-to-Matter device mappings, enriched with the resolved
    CURRENT canonical device (so the table reflects re-pairs / staleness
    instead of the frozen commission-time id). See services.matter_mapping.
    """
    from services.matter_mapping import get_device_matter_map_enriched
    # Sync (blocking PostgREST) — offload so a slow lookup can't hold the loop.
    return await asyncio.to_thread(get_device_matter_map_enriched)


@app.post("/api/matter/map", tags=["matter"])
async def matter_create_mapping(body: MatterMapRequest):
    """
    Map a Hubitat device to a Matter node.

    After mapping, commands sent to this Hubitat device will also be
    sent via the Matter protocol for faster local control.
    """
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    try:
        resp = req.post(
            f"{postgrest_url}/device_matter_map",
            json={
                "hubitat_device_id": body.hubitat_device_id,
                "matter_node_id": body.matter_node_id,
                "matter_endpoint_id": body.matter_endpoint_id,
                "device_name": body.device_name
            },
            headers={
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates"
            },
            timeout=5
        )
        if resp.ok:
            return {"message": "Mapping created"}
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create Matter mapping: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/matter/map/{hubitat_device_id}", tags=["matter"])
async def matter_delete_mapping(hubitat_device_id: str):
    """Remove a Hubitat-to-Matter device mapping."""
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    try:
        resp = req.delete(
            f"{postgrest_url}/device_matter_map",
            params={"hubitat_device_id": f"eq.{hubitat_device_id}"},
            timeout=5
        )
        if resp.ok:
            return {"message": "Mapping deleted"}
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete Matter mapping: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/matter/map", tags=["matter"])
async def matter_delete_all_mappings():
    """Remove ALL Hubitat↔Matter mappings (the mapping section's 'Remove all').
    Rows are plain link records (no device/node state is touched) — rebuild
    them any time with POST /api/matter/map/rebuild."""
    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    r = await adelete(
        f"{postgrest_url}/device_matter_map",
        params={"hubitat_device_id": "not.is.null"},   # PostgREST DELETE needs a filter
        headers={"Prefer": "return=representation"},
        timeout=10,
    )
    if r.status_code not in (200, 204):
        raise HTTPException(status_code=r.status_code, detail=r.text[:300])
    try:
        removed = len(r.json()) if r.text else 0
    except Exception:
        removed = 0
    logger.info(f"matter map: removed ALL {removed} mappings (operator action)")
    return {"removed": removed}


@app.post("/api/matter/map/rebuild", tags=["matter"])
async def matter_rebuild_mappings():
    """Rebuild Hubitat↔Matter mappings from the discovered-devices table:
    every non-removed device that has BOTH a Hubitat device id and a node in
    our fabric (our_node_id) gets an upserted device_matter_map row (endpoint
    1). Devices without a node are skipped — commission first, then rebuild.
    Idempotent (merge-duplicates on the hubitat_device_id key)."""
    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    r = await aget(
        f"{postgrest_url}/hubitat_matter_devices",
        params={"is_removed": "not.is.true",
                "our_node_id": "not.is.null",
                "select": "unique_id,device_name,hubitat_device_id,our_node_id"},
        timeout=10,
    )
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text[:300])
    rows = r.json()
    created, skipped = 0, 0
    for d in rows:
        hid = (d.get("hubitat_device_id") or "").strip()
        if not hid:
            skipped += 1
            continue
        w = await apost(
            f"{postgrest_url}/device_matter_map",
            json={"hubitat_device_id": hid,
                  "matter_node_id": d["our_node_id"],
                  "matter_endpoint_id": 1,
                  "device_name": d.get("device_name") or ""},
            headers={"Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates"},
            timeout=5,
        )
        if w.status_code in (200, 201, 204):
            created += 1
        else:
            skipped += 1
            logger.warning(f"map rebuild: upsert failed for {hid}: {w.status_code}")
    logger.info(f"matter map rebuild: {created} upserted, {skipped} skipped "
                f"(of {len(rows)} node-bearing devices)")
    return {"rebuilt": created, "skipped": skipped,
            "candidates": len(rows),
            "note": "devices without our_node_id are excluded — commission them first, then rebuild"}


_MATTER_HUB_SEL = ("id,hub_name,hub_ip,is_primary,is_enabled,"
                   "maker_api_app_number,maker_api_token_env,hardware_version")


async def _resolve_matter_hubs() -> List[Dict[str, Any]]:
    """
    Resolve the SET of hubs Matter may scan for devices.

    MULTI-SELECT MATTER HUBS (operator directive 2026-07-11, evolving the
    earlier single-hub stopgap now that MAC dedup makes multi-hub safe):
    discovery scans this set; a physical device seen on >1 hub is deduped by
    MAC (EUI-64) at read time so it never shows twice and is never
    double-commissioned. COMMISSIONING is per-device via the device's OWN hub
    (auto), so it is unaffected by how many hubs are selected. Resolution:
      1. system_settings.matter_hub_ids (JSON array of ids) -> those hub rows;
      2. legacy system_settings.matter_hub_id (single id) -> that row;
      3. the is_primary=true hub;
      4. [] (nothing resolvable — callers surface a clear error).
    Ids pointing at no hub row are dropped; selection order is preserved.
    """
    import json
    pg = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    ids: List[int] = []
    try:
        r = await aget(f"{pg}/system_settings",
                       params={"key": "eq.matter_hub_ids", "select": "value"}, timeout=5)
        if r.status_code == 200 and r.json():
            raw = r.json()[0].get("value")
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, list):
                ids = [int(x) for x in parsed if str(x).strip().lstrip("-").isdigit()]
    except Exception as e:
        logger.debug(f"matter_hub_ids read failed: {e}")
    if not ids:  # legacy single-id setting
        try:
            r = await aget(f"{pg}/system_settings",
                           params={"key": "eq.matter_hub_id", "select": "value"}, timeout=5)
            if r.status_code == 200 and r.json():
                raw = str(r.json()[0].get("value") or "").strip()
                if raw.isdigit():
                    ids = [int(raw)]
        except Exception:
            pass
    if ids:
        try:
            r = await aget(f"{pg}/hub_config",
                           params={"id": f"in.({','.join(str(i) for i in ids)})",
                                   "select": _MATTER_HUB_SEL}, timeout=5)
            rows = r.json() if r.status_code == 200 else []
            if rows:
                order = {hid: n for n, hid in enumerate(ids)}
                rows.sort(key=lambda h: order.get(h["id"], 999))
                return rows
        except Exception as e:
            logger.warning(f"matter_hub_ids lookup failed: {e}")
    try:
        r = await aget(f"{pg}/hub_config",
                       params={"is_primary": "eq.true", "select": _MATTER_HUB_SEL, "limit": "1"},
                       timeout=5)
        if r.status_code == 200 and r.json():
            return r.json()
    except Exception as e:
        logger.warning(f"primary hub lookup failed: {e}")
    return []


async def _resolve_matter_hub() -> Optional[Dict[str, Any]]:
    """The primary of the selected Matter-hub set (or the first). Retained for
    single-hub call sites; multi-hub scanning uses _resolve_matter_hubs()."""
    hubs = await _resolve_matter_hubs()
    if not hubs:
        return None
    for h in hubs:
        if h.get("is_primary"):
            return h
    return hubs[0]


def _short_discriminator_from_manual_code(code: str) -> Optional[int]:
    """
    The 4-bit SHORT discriminator an 11-digit manual Matter setup code targets.

    The manual code is NOT one flat integer — it is three base-10 CHUNKS plus a
    Verhoeff check digit (Matter core spec, manual pairing code):

        digit  0     chunk1 = (vid_pid_present << 2) | discriminator[11:10]
        digits 1-5   chunk2 = (discriminator[9:8]  << 14) | passcode[13:0]
        digits 6-9   chunk3 = passcode[26:14]
        digit  10    Verhoeff check digit

    so the SHORT (4-bit) discriminator is the TOP FOUR bits of the 12-bit
    discriminator: (chunk1 & 0x3) << 2 | (chunk2 >> 14) & 0x3.

    Discovery filters candidates by this value; the PASSCODE is the actual
    secret. A device advertising a different discriminator is therefore filtered
    out and reported as "not found" even while it sits in pairing mode — the
    failure this exists to explain. QR payloads (MT:…) carry the FULL 12-bit
    discriminator, so we only decode the numeric form here.

    Verified against the operator's real 2026-07-13 code 25803812418 →
    short discriminator 11, passcode 20341430 (independently hand-decoded from
    the matter.js logs, MSG-1017). Returns None on anything unparseable; never
    raises.
    """
    try:
        raw = (code or "").strip()
        # A QR payload is NOT a manual code. It also contains digits, so a naive
        # digit-strip would happily "decode" it into nonsense — reject explicitly.
        if raw.upper().startswith("MT:"):
            return None
        digits = "".join(ch for ch in raw if ch.isdigit())
        # Manual codes are exactly 11 digits (or 21 with VID/PID appended); the
        # discriminator bits live in the same leading chunks either way.
        if len(digits) not in (11, 21):
            return None
        chunk1 = int(digits[0])
        chunk2 = int(digits[1:6])
        return ((chunk1 & 0x3) << 2) | ((chunk2 >> 14) & 0x3)
    except Exception:
        return None


async def _discovery_mismatch_hint(code: str) -> Optional[str]:
    """
    After a failed commission, run ONE discovery and report what is ACTUALLY on
    the network — specifically whether a device is in pairing mode but
    advertising a discriminator that does not match the code.

    Returns a human sentence, or None when there is nothing useful to add.
    Never raises: this is a diagnostic garnish on an error path.
    """
    try:
        from services.matter_client import get_matter_client
        client = get_matter_client()
        if not client.is_connected and not await client.connect():
            return None
        found = await asyncio.wait_for(client._send_command("discover"), timeout=20)
        found = found if isinstance(found, list) else []
        # Only devices actually in commissioning mode are pairable.
        open_devs = [d for d in found if d.get("commissioning_mode")]
        if not open_devs:
            return ("Probe: NO device is currently advertising commissioning mode on "
                    "the LAN — the device is not in pairing mode (or not on the network).")

        want = _short_discriminator_from_manual_code(code)
        lines = []
        for d in open_devs:
            long_d = d.get("long_discriminator")
            short_d = (int(long_d) >> 8) & 0x0F if long_d is not None else None
            ip = next((a for a in (d.get("addresses") or [])
                       if "." in a and ":" not in a), "?")
            lines.append(f"{ip} advertising discriminator {long_d} (short {short_d})")
            # THE actionable case: something IS pairing-ready, but not this code.
            if want is not None and short_d is not None and short_d != want:
                return (f"Probe: a device IS in pairing mode at {ip}, advertising "
                        f"discriminator {long_d} (short {short_d}) — but your code "
                        f"targets short discriminator {want}. They do not match, so "
                        f"discovery filtered it out. This code is not for that device "
                        f"(or the device's discriminator changed — power-cycle it, and "
                        f"if it still differs, factory-reset it so its printed code "
                        f"matches again).")
        return "Probe: commissionable device(s) seen — " + "; ".join(lines) + "."
    except Exception as e:  # noqa: BLE001 — diagnostics must never mask the real error
        logger.debug(f"discovery-mismatch probe failed (non-fatal): {e}")
        return None


@app.post("/api/matter/discover-mdns", tags=["matter"])
async def matter_discover_mdns():
    """
    Discover commissionable Matter devices DIRECTLY over mDNS (_matterc._udp)
    via the matter controller — NO hub involved. This is the hub-free half of
    discovery (2026-07-11 operator directive: Matter should not rely on a hub).

    Returns live (non-persisted) results: devices currently in commissioning
    mode. Each is enriched with a MAC derived from its IPv6 link-local address
    (EUI-64) and cross-matched against hub-discovered rows by MAC/IP — the
    dedup key hierarchy is MAC → serial → normalized name, so the same physical
    device shows as ONE identity with source chips, never two cards.
    """
    from services.matter_client import get_matter_client
    from services.matter_discovery import mac_from_ipv6_ll
    client = get_matter_client()
    if not client.is_connected and not await client.connect():
        raise HTTPException(status_code=503, detail="matter controller unreachable")
    try:
        found = await asyncio.wait_for(client._send_command("discover"), timeout=45)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"mDNS discover failed: {e}")
    found = found if isinstance(found, list) else []

    # Cross-match against hub-discovered rows (dedup: MAC first, then IP).
    pg = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    hub_rows = []
    try:
        hr = await aget(f"{pg}/hubitat_matter_devices",
                        params={"is_removed": "not.is.true",
                                "select": "unique_id,device_name,hub_name,hub_ip,mac_address,ip_address,our_node_id"},
                        timeout=5)
        hub_rows = hr.json() if hr.status_code == 200 else []
    except Exception:
        pass
    by_mac = {(r.get("mac_address") or "").lower(): r for r in hub_rows if r.get("mac_address")}
    by_ip = {(r.get("ip_address") or "").strip(): r for r in hub_rows if r.get("ip_address")}

    out = []
    for d in found:
        addrs = d.get("addresses") or []
        mac = None
        ipv4 = None
        for a in addrs:
            if a.startswith("fe80::") and not mac:
                mac = mac_from_ipv6_ll(a.split("%")[0])
            if "." in a and ":" not in a:
                ipv4 = a
        match = (by_mac.get((mac or "").lower())
                 or by_ip.get(ipv4 or "")
                 or None)
        out.append({
            "instance_name": d.get("instance_name"),
            "vendor_id": d.get("vendor_id"),
            "product_id": d.get("product_id"),
            "device_name": d.get("device_name") or "",
            "commissioning_mode": d.get("commissioning_mode"),
            "long_discriminator": d.get("long_discriminator"),
            "ipv4": ipv4,
            "mac": mac,
            "addresses": addrs,
            # Same physical device already known via a hub → dedup chip, and
            # commissioning it here ADDS our fabric (multi-admin), it doesn't
            # duplicate the device. Hub-linked rows keep the Hubitat fallback;
            # pure-mDNS devices are Matter-direct only (no fallback path).
            "hub_match": ({"unique_id": match.get("unique_id"),
                           "device_name": match.get("device_name"),
                           "hub_name": match.get("hub_name"),
                           "our_node_id": match.get("our_node_id")}
                          if match else None),
        })
    return {"devices": out, "count": len(out)}


@app.get("/api/matter/hub", tags=["matter"])
async def matter_hub_get():
    """The Matter hub selection: every hub (for the dropdown) + the currently
    resolved single Matter hub (selected, or the main/primary hub by default).

    Each hub carries `hardware_version` (from the hub's /hub/details/json,
    probed + persisted lazily when unknown) and the derived `has_thread_br`:
    C-8 family hubs have a BUILT-IN Thread border router; C-7 and earlier have
    none, so Thread Matter devices cannot commission/route through them (the
    2026-07-11 Hub-1 pairing saga, MSG-682). The UI badges Thread-capable hubs
    so the operator picks the right Matter hub."""
    pg = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    r = await aget(f"{pg}/hub_config",
                   params={"select": "id,hub_name,hub_ip,is_primary,is_enabled,hardware_version",
                           "order": "id"},
                   timeout=5)
    hubs = r.json() if r.status_code == 200 else []
    for h in hubs:
        if not h.get("hardware_version"):
            # Lazy self-heal for new/unknown hubs: one quick probe, persisted.
            try:
                pr = await aget(f"http://{h['hub_ip']}/hub/details/json", timeout=4)
                hv = (pr.json() or {}).get("hardwareVersion") if pr.status_code == 200 else None
                if hv:
                    h["hardware_version"] = hv
                    await apatch(f"{pg}/hub_config",
                                 params={"id": f"eq.{h['id']}"},
                                 json={"hardware_version": hv},
                                 headers={"Content-Type": "application/json"},
                                 timeout=5)
            except Exception as e:
                logger.debug(f"hub {h.get('hub_name')} hardware probe skipped: {e}")
        hv = (h.get("hardware_version") or "").upper()
        h["has_thread_br"] = hv.startswith("C-8")   # C-8 / C-8 Pro
    selected = await _resolve_matter_hubs()
    return {"hubs": hubs, "selected_ids": [h["id"] for h in selected],
            "selected": selected}


class MatterHubSelectBody(BaseModel):
    """Body for POST /api/matter/hub — the SET of hubs Matter may scan.
    Accepts hub_ids (preferred, multi-select) or a single hub_id (back-compat)."""
    hub_ids: Optional[List[int]] = None
    hub_id: Optional[int] = None


@app.post("/api/matter/hub", tags=["matter"])
async def matter_hub_set(body: MatterHubSelectBody):
    """Persist the Matter hub SELECTION SET (system_settings.matter_hub_ids).
    Discovery scans every selected hub; devices are deduped by MAC so one
    physical device on multiple hubs is one card, commissioned once (via its
    own hub, auto). An empty set is rejected — at least one hub is required."""
    ids = body.hub_ids if body.hub_ids is not None else (
        [body.hub_id] if body.hub_id is not None else [])
    ids = sorted({int(i) for i in ids})
    if not ids:
        raise HTTPException(status_code=400, detail="Select at least one hub")
    pg = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    r = await aget(f"{pg}/hub_config",
                   params={"id": f"in.({','.join(str(i) for i in ids)})",
                           "select": "id,hub_name,hub_ip"}, timeout=5)
    rows = r.json() if r.status_code == 200 else []
    found_ids = {h["id"] for h in rows}
    missing = [i for i in ids if i not in found_ids]
    if missing:
        raise HTTPException(status_code=404, detail=f"No hub(s) with id {missing}")
    import json as _json
    w = await apost(
        f"{pg}/system_settings",
        json={"key": "matter_hub_ids", "value": _json.dumps(ids), "value_type": "json",
              "description": "Set of hubs Matter scans (multi-select; dedup by MAC)"},
        headers={"Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates"},
        timeout=5,
    )
    if w.status_code not in (200, 201, 204):
        raise HTTPException(status_code=w.status_code, detail=w.text[:300])
    logger.info(f"Matter hub set updated: {[h['hub_name'] for h in rows]}")
    return {"ok": True, "selected": rows, "selected_ids": ids}


@app.post("/api/matter/discover", tags=["matter"])
async def matter_discover():
    """
    Discover Matter devices from the SELECTED SET of Matter hubs (multi-select;
    default = the main/primary hub). Queries each hub's /hub/matterDetails/json
    and stores results in hubitat_matter_devices (per-hub rows). The same
    physical device on >1 hub is deduped by MAC at READ time (list endpoint),
    so it is never shown twice or double-commissioned.
    """
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

    # The selected set of Matter hubs (multi-select). Scanning multiple hubs is
    # safe because devices are deduped by MAC; commissioning is still per-device
    # via the device's own hub.
    matter_hubs = await _resolve_matter_hubs()
    if not matter_hubs:
        raise HTTPException(
            status_code=503,
            detail="No Matter hub resolvable: select one or more (POST /api/matter/hub) "
                   "or mark a hub as primary in Settings → Hubs.")
    hubs = [{"ip": h["hub_ip"], "name": h["hub_name"]} for h in matter_hubs]

    # First, get all Maker API devices for name matching
    from services.hubitat_client import get_default_client
    maker_devices = []
    try:
        client = get_default_client()
        maker_devices = client.get_all_devices() or []
    except Exception as e:
        logger.warning(f"Could not load Maker API devices for matching: {e}")

    # Build name-lookup index (lowercase name → device)
    maker_by_name = {}
    for d in maker_devices:
        name = (d.get('label') or d.get('name') or '').strip().lower()
        if name:
            maker_by_name[name] = d

    discovered = []
    errors = []

    for hub in hubs:
        try:
            resp = req.get(
                f"http://{hub['ip']}/hub/matterDetails/json",
                timeout=10
            )
            if not resp.ok:
                errors.append(f"{hub['name']} ({hub['ip']}): HTTP {resp.status_code}")
                continue

            data = resp.json()
            if not data.get('enabled'):
                continue

            for device in data.get('devices', []):
                unique_id = device.get('uniqueId', '')
                if not unique_id:
                    continue

                matter_name = (device.get('name') or '').strip()

                # Try to match against Maker API devices by name
                maker_match = None
                match_confidence = 'none'
                name_lower = matter_name.lower()

                # Exact name match ONLY (name.lower()). Fuzzy substring matching
                # was REMOVED (audit R1e / F5): it mis-bound devices — e.g. a
                # Matter "Light" matching Maker "Light Desk"/"Light Bedroom" — and
                # it diverged from services/matter_discovery.py, which is
                # exact-only per the 2026-07-09 directive. Last-writer-wins on the
                # same table meant the two paths fought; both are exact now.
                if name_lower in maker_by_name:
                    maker_match = maker_by_name[name_lower]
                    match_confidence = 'exact'

                # MAC = the cross-hub dedup key (multi-hub scanning). Derived
                # from the device's IPv6 link-local (EUI-64) — burned-in and
                # fabric-independent, so the same physical device on 2 hubs
                # collapses to ONE card at read time. (Serial is a stronger key
                # but needs a per-device fullJson fetch; MAC is free here and
                # reliable for these LAN devices — operator's point.)
                from services.matter_discovery import mac_from_ipv6_ll as _mac
                _ip = device.get('ipAddress', '') or ''
                _mac_addr = _mac(_ip.split('%')[0]) if _ip.startswith('fe80::') else None

                row = {
                    "unique_id": unique_id,
                    "device_name": matter_name,
                    "manufacturer": device.get('manufacturer', ''),
                    "model": device.get('model', ''),
                    "ip_address": _ip,
                    "mac_address": _mac_addr,
                    "is_online": device.get('online', False),
                    "hub_ip": hub['ip'],
                    "hub_name": hub['name'],
                    "hubitat_node_id": device.get('nodeId', 0),
                    "hubitat_device_id": str(device.get('id', '')),
                    "hubitat_dni": device.get('dni', ''),
                    # MANUAL scan resurrects soft-removed rows for the scanned
                    # hub (the documented Remove contract: 're-scan restores').
                    # The periodic timer upsert does NOT write these keys, so it
                    # never resurrects removed rows.
                    "is_removed": False,
                    "removed_at": None,
                }

                if maker_match:
                    row["maker_api_device_id"] = str(maker_match.get('id', ''))
                    row["maker_api_device_name"] = maker_match.get('label') or maker_match.get('name', '')
                    row["match_confidence"] = match_confidence

                discovered.append(row)

                # Upsert into database (dedup by unique_id)
                req.post(
                    f"{postgrest_url}/hubitat_matter_devices",
                    json=row,
                    headers={
                        "Content-Type": "application/json",
                        "Prefer": "resolution=merge-duplicates"
                    },
                    timeout=5
                )

        except Exception as e:
            errors.append(f"{hub['name']} ({hub['ip']}): {str(e)}")

    matched = sum(1 for d in discovered if d.get('match_confidence', 'none') != 'none')
    return {
        "discovered": len(discovered),
        "matched": matched,
        "hubs_scanned": len(hubs),
        "errors": errors
    }


@app.get("/api/matter/hubitat-devices", tags=["matter"])
async def matter_hubitat_devices():
    """Discovered Hubitat Matter devices for the UI cards.

    - Excludes soft-removed rows (is_removed) — Remove/Remove-all actually hide
      cards now (2026-07-11 fix; removal used to write only the frozen
      matter_devices table, so 'remove all removed nothing').
    - hub_name is OVERRIDDEN with the LIVE hub_config name resolved by hub_ip:
      the stored column is frozen at discovery time and survives hub renames
      (rows still said 'other_hub_2' months after the hub became home_2 —
      audit F11). Display always derives from live config (P4).
    - DEDUP by MAC (multi-hub scanning): a physical device seen on >1 hub is
      collapsed to ONE card. The representative is the row on a primary hub
      (else the first), with `also_on_hubs` listing the other hubs — so the UI
      shows one device and commissioning targets one owning hub. Rows with NO
      MAC are never merged (each stays its own card)."""
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    try:
        resp = req.get(
            f"{postgrest_url}/hubitat_matter_devices",
            params={"order": "device_name.asc", "is_removed": "not.is.true"},
            headers={"Accept": "application/json"},
            timeout=5
        )
        if not resp.ok:
            return []
        rows = resp.json()
        # Live hub-name + primary flag by hub_ip.
        live, primary_ips = {}, set()
        try:
            hc = req.get(f"{postgrest_url}/hub_config",
                         params={"select": "hub_name,hub_ip,is_primary"}, timeout=5)
            if hc.ok:
                for h in hc.json():
                    live[h["hub_ip"]] = h["hub_name"]
                    if h.get("is_primary"):
                        primary_ips.add(h["hub_ip"])
        except Exception as e:
            logger.debug(f"hub-name live-enrich skipped: {e}")
        for r in rows:
            r["hub_name"] = live.get(r.get("hub_ip"), r.get("hub_name"))

        # Dedup by MAC. Group only rows that HAVE a mac; null-mac rows pass
        # through individually (keyed by unique_id so they can't merge).
        groups: Dict[str, List[Dict[str, Any]]] = {}
        singles: List[Dict[str, Any]] = []
        for r in rows:
            mac = (r.get("mac_address") or "").lower()
            if mac:
                groups.setdefault(mac, []).append(r)
            else:
                singles.append(r)
        out: List[Dict[str, Any]] = list(singles)
        for mac, grp in groups.items():
            if len(grp) == 1:
                out.append(grp[0]); continue
            # Representative: prefer a primary-hub row, then one that has our
            # node (already commissioned), then first.
            grp.sort(key=lambda x: (
                0 if x.get("hub_ip") in primary_ips else 1,
                0 if x.get("our_node_id") is not None else 1))
            rep = dict(grp[0])
            rep["also_on_hubs"] = [g.get("hub_name") for g in grp[1:]]
            rep["hub_count"] = len(grp)
            out.append(rep)
        out.sort(key=lambda x: (x.get("device_name") or "").lower())
        return out
    except Exception as e:
        logger.error(f"Failed to get hubitat matter devices: {e}")
        return []


class UpdateMatterDeviceMatchRequest(BaseModel):
    """Request body for manually correcting a Matter-to-Maker API match."""
    unique_id: str
    maker_api_device_id: str


@app.patch("/api/matter/hubitat-devices/match", tags=["matter"])
async def matter_update_match(body: UpdateMatterDeviceMatchRequest):
    """
    Manually correct the Maker API device match for a Hubitat Matter device.

    Used when auto-matching by name got it wrong. The user selects the
    correct Maker API device from a dropdown.
    """
    import requests as req

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

    # Get the Maker API device name for display
    maker_name = ''
    try:
        from services.device_to_hubs_classifier import fetch_device_live
        device = fetch_device_live(body.maker_api_device_id)
        if device:
            maker_name = device.get('label') or device.get('name', '')
    except Exception:
        pass

    try:
        resp = req.patch(
            f"{postgrest_url}/hubitat_matter_devices",
            params={"unique_id": f"eq.{body.unique_id}"},
            json={
                "maker_api_device_id": body.maker_api_device_id,
                "maker_api_device_name": maker_name,
                "match_confidence": "manual"
            },
            headers={"Content-Type": "application/json"},
            timeout=5
        )
        if resp.ok:
            return {"message": "Match updated", "maker_api_device_name": maker_name}
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class AutoCommissionRequest(BaseModel):
    """Request body for auto-commissioning a Hubitat Matter device."""
    unique_id: str


@app.post("/api/matter/auto-commission", tags=["matter"])
async def matter_auto_commission(body: AutoCommissionRequest):
    """
    Auto-commission ONE Hubitat Matter device — THE GATED ENTRY POINT.

    STRICT GATING (operator directive, 2026-07-13): "two pairing storms on one
    radio must be strictly gated." EVERY path that opens a pairing window now
    holds the ONE global mutex, with no exception and no bypass:

        manual commission      -> matter_commission            (mutex)
        single auto-commission -> THIS endpoint                (mutex)
        Commission All (bulk)  -> _bulk_commission_worker      (mutex, held for
                                  the whole run; it calls the INNER function
                                  below, never this endpoint)
        hub->hub COPY          -> matter_hub_port orchestrator (mutex)

    The earlier "deliberately not locked here to avoid deadlocking the bulk
    worker" was a WORKAROUND, not a gate: it left this endpoint wide open, so a
    double-click on a device card (or an auto-commission racing a manual one)
    still produced concurrent pairing storms. The correct fix is this split —
    the ENDPOINT takes the lock; the INNER function assumes the caller holds it.
    A radio pairs one device at a time; a second attempt now gets a 409 naming
    the holder instead of piling on.
    """
    try:
        async with matter_pairing_lock(
            "commission_auto", f"device:{body.unique_id}", ttl_s=300,
        ):
            return await _auto_commission_device(body.unique_id)
    except PairingLockBusy as e:
        raise HTTPException(status_code=409, detail=str(e))


async def _auto_commission_device(unique_id: str):
    """
    The auto-commission WORK — the caller MUST already hold the global Matter
    pairing mutex (see matter_auto_commission, which is the gated entry point,
    and _bulk_commission_worker, which holds the lock across an entire run and
    calls this directly so it cannot deadlock against itself).

    Auto-commission a Hubitat Matter device into our matter-server.

    Steps:
    1. Look up device in hubitat_matter_devices by unique_id
    2. Call Hubitat's openPairingWindow to get a setup code
    3. Commission into our matter-server using that code
    4. Create the device_matter_map entry
    5. Update hubitat_matter_devices with our_node_id

    This is the one-click commission flow.
    """
    import requests as req
    from services.matter_client import get_matter_client

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

    # Step 1: Look up device. NOTE: every outbound call in this async route is
    # wrapped in asyncio.to_thread — `requests` is SYNCHRONOUS and this app runs
    # a SINGLE uvicorn worker, so a bare req.get here blocks the WHOLE event loop
    # (every other request, including the Docker health check). The
    # openPairingWindow call below (a slow/flapping Hubitat hub, up to its
    # timeout) did exactly that: it froze the app long enough for the health
    # check to fail and autoheal to RESTART the container mid-commission — so
    # commissioning could never finish. Threading the blocking I/O is the fix.
    resp = await asyncio.to_thread(lambda: req.get(
        f"{postgrest_url}/hubitat_matter_devices",
        params={"unique_id": f"eq.{unique_id}"},
        headers={"Accept": "application/json"},
        timeout=5,
    ))
    if not resp.ok or not resp.json():
        raise HTTPException(status_code=404, detail="Device not found in discovery table")

    device = resp.json()[0]

    if not device.get('is_online'):
        raise HTTPException(status_code=400, detail=f"Device '{device['device_name']}' is offline")

    # Step 2: Open pairing window on the device's OWN hub (multi-hub: commission
    # is per-device via whichever hub currently administers it — "commission
    # auto"). No single-hub gate anymore; the once-per-physical-device invariant
    # is enforced by MAC dedup (one card per device), not by restricting hubs.
    # (threaded — see note above). Timeout lowered 90s -> 30s: a hub that can't
    # open a window in 30s is down, and 90s is a needlessly long freeze even threaded.
    hub_ip = device['hub_ip']
    hubitat_node = device['hubitat_node_id']

    try:
        pair_resp = await asyncio.to_thread(lambda: req.get(
            f"http://{hub_ip}/hub/matter/openPairingWindow",
            params={"node": hubitat_node},
            timeout=30,
        ))
        if not pair_resp.ok:
            raise HTTPException(
                status_code=502,
                detail=f"Hubitat returned {pair_resp.status_code} opening pairing window"
            )
        pair_data = pair_resp.json()
        setup_code = pair_data.get('setupCode') or pair_data.get('code') or pair_data.get('pairingCode')
        if not setup_code:
            # Maybe the response IS the code as a string
            if isinstance(pair_data, str):
                setup_code = pair_data
            else:
                logger.warning(f"Pairing window response: {pair_data}")
                raise HTTPException(
                    status_code=502,
                    detail=f"No setup code in Hubitat response: {pair_data}"
                )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to open pairing window on Hubitat: {e}"
        )

    # Step 3: Commission into our matter-server
    client = get_matter_client()
    if not client.is_connected:
        connected = await client.connect()
        if not connected:
            raise HTTPException(status_code=503, detail="Cannot connect to matter-server")

    try:
        result = await client.commission_with_code(str(setup_code))
        our_node_id = result.get('node_id') if isinstance(result, dict) else None
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"matter-server commission failed: {e}"
        )

    # Step 3b: SELF-CLEANING COMMISSION (MSG-696). The moment we hold a fresh
    # admin fabric on this device, sweep OUR stale 0xFFF1 orphans off it
    # (keep_current=True removes only our non-current fabrics). This stops the
    # slot accumulation that saturates devices and blocks future pairing — the
    # cleaner is now automatic at the one moment we're guaranteed to have admin
    # rights, instead of buried behind a commissioned card. Best-effort: a
    # cleanup hiccup must not fail an otherwise-successful commission.
    orphans_cleared = None
    if our_node_id is not None:
        try:
            from services.matter_debug import get_diagnostics
            _rep = await get_diagnostics().decommission_node(our_node_id, keep_current=True)
            orphans_cleared = _rep.get("removed_indices")
            if orphans_cleared:
                logger.info(f"auto-commission: self-cleaned {len(orphans_cleared)} "
                            f"stale 0xFFF1 fabric(s) on node {our_node_id}")
        except Exception as e:
            logger.warning(f"auto-commission: self-clean on node {our_node_id} failed: {e}")

    # Step 4: Create device_matter_map entry
    if our_node_id is not None:
        req.post(
            f"{postgrest_url}/device_matter_map",
            json={
                "hubitat_device_id": device['hubitat_device_id'],
                "matter_node_id": our_node_id,
                "matter_endpoint_id": 1,
                "device_name": device['device_name']
            },
            headers={
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates"
            },
            timeout=5
        )

    # Step 5: Update hubitat_matter_devices with our node ID
    req.patch(
        f"{postgrest_url}/hubitat_matter_devices",
        params={"unique_id": f"eq.{unique_id}"},
        json={
            "our_node_id": our_node_id,
            "is_commissioned": True
        },
        headers={"Content-Type": "application/json"},
        timeout=5
    )

    return {
        "message": f"Commissioned '{device['device_name']}'",
        "our_node_id": our_node_id,
        "hubitat_device_id": device['hubitat_device_id'],
        "setup_code_used": setup_code[:8] + "..." if setup_code else None,
        "orphans_cleared": orphans_cleared,   # our stale 0xFFF1 fabrics removed post-commission
    }


class BulkCommissionBody(BaseModel):
    """Body for POST /api/matter/auto-commission-all — the explicit-user gate."""
    confirmed: bool = False


# ---------------------------------------------------------------------------
# Bulk commission — STRICTLY SEQUENTIAL, background run + status polling.
#
# Operator directive 2026-07-12 ("Commission All doesn't work well. Probably
# overflow. Hubitat can handle only one device at a time"): the previous
# implementation ran 3 commissions CONCURRENTLY (Semaphore(3) + gather) — three
# simultaneous pairing windows on the same hub and three competing PASE
# sessions, with zero settle time. The required flow is:
#     get code for 1 → wait for that one to be fully done →
#     pause so the hub catches its breath → next device.
#
# It also runs as a BACKGROUND task with a status endpoint: a serialized run
# over N devices takes N × (commission ~15s + settle 8s) — minutes — and a
# synchronous HTTP response would be killed by the nginx proxy read timeout
# long before finishing. The UI polls /auto-commission-all/status instead.
# Single uvicorn worker → a module-level state dict is race-free enough here.
# ---------------------------------------------------------------------------
_BULK_COMMISSION_SETTLE_S = 8.0            # hub "breath" between devices
_BULK_COMMISSION_DEVICE_TIMEOUT_S = 120.0  # hard ceiling per device (a wedged
                                           # device must not stall the run)
_BULK_COMMISSION_MAX_CONSECUTIVE_FAILURES = 3  # circuit breaker: hub is wedged

_bulk_commission_state: Dict[str, Any] = {"running": False}


async def _bulk_commission_worker(devices: List[Dict[str, Any]], hub_label: str) -> None:
    """
    The sequential bulk-commission loop (background task).

    For each device row, in order: run the full one-click auto-commission
    (openPairingWindow on the device's OWN hub → commission_with_code → self-
    clean → map/PATCH), WAIT for it to finish (or hit the per-device ceiling),
    then sleep _BULK_COMMISSION_SETTLE_S before the next device so the hub's
    pairing subsystem recovers. Never two pairing windows at once.

    Guards:
    - Per-run MAC dedup: the same physical device present on 2+ selected hubs
      is two rows; commissioning it twice would burn two fabric slots. First
      row wins, later rows are recorded as "skipped".
    - Circuit breaker: _BULK_COMMISSION_MAX_CONSECUTIVE_FAILURES consecutive
      failures aborts the run (a wedged hub should not be ground through).

    Progress is written into _bulk_commission_state for the status endpoint.
    """
    from datetime import datetime  # app.py convention: datetime imported locally
    from services.matter_pairing_lock import matter_pairing_lock, PairingLockBusy

    st = _bulk_commission_state
    seen_macs: set = set()
    consecutive_failures = 0
    aborted = False

    # GLOBAL MATTER-PAIRING MUTEX (operator invariant, MSG-919): a Hubitat pairs
    # exactly ONE Matter device at a time — as source (open window) AND as target.
    # Commission All, the hub->hub COPY orchestrator, and manual pairing from a
    # hub's UI all contend for that single slot. Holding the shared lock for the
    # whole run is what makes "strictly sequential" true ACROSS FEATURES, not just
    # within this one (our per-run `running` flag cannot see the other feature).
    try:
        _lock_cm = matter_pairing_lock(
            "commission_all",
            f"{len(devices)} device(s) on {hub_label}",
            # Worst case ~ (commission ceiling + settle) per device, plus headroom.
            ttl_s=int(len(devices) * (_BULK_COMMISSION_DEVICE_TIMEOUT_S
                                      + _BULK_COMMISSION_SETTLE_S)) + 300,
        )
        await _lock_cm.__aenter__()
    except PairingLockBusy as e:
        st["running"] = False
        st["message"] = str(e)
        st["aborted"] = True
        logger.warning(f"Commission All refused to start: {e}")
        return
    try:
        for n, device in enumerate(devices):
            name = device.get('device_name', device['unique_id'])
            mac = (device.get('mac_address') or '').strip().lower()
            st["done"] = n
            st["current"] = name

            # Per-run MAC dedup — one commission per PHYSICAL device.
            if mac and mac in seen_macs:
                st["results"].append({
                    "device": name, "status": "skipped",
                    "detail": "same physical device (MAC) already commissioned this run",
                })
                st["skipped"] += 1
                continue

            try:
                result = await asyncio.wait_for(
                    _auto_commission_device(device['unique_id']),
                    timeout=_BULK_COMMISSION_DEVICE_TIMEOUT_S,
                )
                st["results"].append({"device": name, "status": "ok",
                                      "node_id": result.get("our_node_id")})
                st["ok"] += 1
                consecutive_failures = 0
                if mac:
                    seen_macs.add(mac)
            except asyncio.TimeoutError:
                logger.warning(f"Bulk commission: '{name}' exceeded "
                               f"{_BULK_COMMISSION_DEVICE_TIMEOUT_S:.0f}s ceiling")
                st["results"].append({"device": name, "status": "error",
                                      "detail": f"timed out after {_BULK_COMMISSION_DEVICE_TIMEOUT_S:.0f}s"})
                st["failed"] += 1
                consecutive_failures += 1
            except HTTPException as e:
                logger.warning(f"Bulk commission failed for {name}: {e.detail}")
                st["results"].append({"device": name, "status": "error", "detail": e.detail})
                st["failed"] += 1
                consecutive_failures += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Bulk commission failed for {name}: {e}")
                st["results"].append({"device": name, "status": "error", "detail": str(e)})
                st["failed"] += 1
                consecutive_failures += 1

            if consecutive_failures >= _BULK_COMMISSION_MAX_CONSECUTIVE_FAILURES:
                aborted = True
                logger.error(f"Bulk commission ABORTED: {consecutive_failures} consecutive "
                             f"failures — hub pairing flow looks wedged")
                break

            # Settle pause between devices (not after the last one): let the
            # hub close the window / recover before the next openPairingWindow.
            if n < len(devices) - 1:
                st["current"] = f"settling {int(_BULK_COMMISSION_SETTLE_S)}s (hub recovery)"
                await asyncio.sleep(_BULK_COMMISSION_SETTLE_S)
    finally:
        # Release the global pairing mutex FIRST — a wedged run must never keep
        # Matter pairing locked out for other features (or for the operator
        # pairing by hand). The lock also self-expires, but do not rely on that.
        try:
            await _lock_cm.__aexit__(None, None, None)
        except Exception as e:  # noqa: BLE001 — never mask the run's own outcome
            logger.warning(f"pairing-lock release failed (it will expire): {e}")
        st["done"] = len(st["results"])
        st["current"] = None
        st["running"] = False
        st["finished_at"] = datetime.now().isoformat(timespec="seconds")
        summary = (f"Commissioned {st['ok']}/{st['total']} on {hub_label}"
                   + (f", {st['failed']} failed" if st['failed'] else "")
                   + (f", {st['skipped']} skipped (MAC dup)" if st['skipped'] else "")
                   + (" — ABORTED (consecutive failures)" if aborted else ""))
        st["message"] = summary
        st["aborted"] = aborted
        logger.info(f"Bulk commission finished: {summary}")


@app.post("/api/matter/auto-commission-all", tags=["matter"])
async def matter_auto_commission_all(body: Optional[BulkCommissionBody] = None):
    """
    USER-INITIATED bulk commission over the SELECTED MATTER HUB SET
    (multi-select — same set the scan uses; devices on unselected hubs are
    excluded, not errored).

    HARD GATE: requires {"confirmed": true} in the body — only the UI's
    "Commission All" button (behind its confirm dialog) sends it. Any legacy or
    automatic caller without the flag gets 409, so the scan-chain class of bug
    (bulk commissioning fired without the operator asking) is structurally
    impossible to reintroduce. NEVER runs automatically.

    STARTS A BACKGROUND RUN and returns immediately with {started, total};
    the run is strictly sequential (one pairing window at a time, settle pause
    between devices — see _bulk_commission_worker). Poll
    GET /api/matter/auto-commission-all/status for live progress. 409 if a
    run is already in flight.
    """
    if body is None or not body.confirmed:
        raise HTTPException(
            status_code=409,
            detail="Bulk commissioning requires explicit confirmation "
                   "({\"confirmed\": true}) — it is a user action, never automatic.")

    if _bulk_commission_state.get("running"):
        raise HTTPException(
            status_code=409,
            detail="A bulk commission run is already in progress — poll "
                   "/api/matter/auto-commission-all/status.")

    # GLOBAL MATTER-PAIRING MUTEX pre-flight (operator invariant, MSG-919). A
    # Hubitat pairs ONE Matter device at a time, so we must refuse to START while
    # the hub->hub COPY orchestrator (or a manual pairing) holds the slot. Checked
    # HERE, not only in the worker, so the caller gets an immediate, actionable 409
    # naming the holder instead of a background run that dies silently.
    # NOTE: this is advisory (a TOCTOU gap exists between here and the worker's
    # own acquire) — the worker's atomic acquire is the real guarantee. This check
    # exists to make the refusal FAST and LEGIBLE, not to be the lock itself.
    from services.matter_pairing_lock import status as _pairing_status
    _lock = await _pairing_status()
    if _lock.get("is_held") and not _lock.get("is_stale"):
        raise HTTPException(
            status_code=409,
            detail=(f"Matter pairing is already in progress "
                    f"({_lock.get('holder')}"
                    f"{': ' + _lock['holder_detail'] if _lock.get('holder_detail') else ''}). "
                    f"A Hubitat can pair only ONE device at a time, so Commission All "
                    f"cannot start until it finishes (expires {_lock.get('expires_at')})."))

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

    hubs = await _resolve_matter_hubs()
    if not hubs:
        raise HTTPException(
            status_code=503,
            detail="No Matter hub resolvable: select one or more (POST /api/matter/hub) "
                   "or mark a hub as primary in Settings → Hubs.")
    hub_ips = [h["hub_ip"] for h in hubs if h.get("hub_ip")]
    hub_label = ", ".join(h.get("hub_name") or h["hub_ip"] for h in hubs)

    # REALITY CHECK (2026-07-12): reconcile is_commissioned against the LIVE
    # fabric before selecting. Rows can claim is_commissioned=true with an
    # our_node_id that no longer exists (nodes from the pre-VENDOR_ID-fix
    # fabric, or decommissions that never cleared the DB) — Commission All
    # trusted the flag and silently skipped those devices forever. Any
    # "commissioned" row whose node is NOT in the current fabric is flipped
    # back to uncommissioned here so it gets picked up below.
    try:
        from services.matter_client import get_matter_client
        import requests as req
        client = get_matter_client()
        if client.is_connected or await client.connect():
            nodes = await client.get_nodes()
            real_ids = {int(n["node_id"]) for n in (nodes or []) if n.get("node_id") is not None}
            params = {"is_commissioned": "eq.true"}
            if real_ids:   # nodes that exist stay commissioned; ghosts get flipped
                params["our_node_id"] = f"not.in.({','.join(str(i) for i in sorted(real_ids))})"
            r = await asyncio.to_thread(lambda: req.patch(
                f"{postgrest_url}/hubitat_matter_devices", params=params,
                json={"is_commissioned": False, "our_node_id": None},
                headers={"Content-Type": "application/json", "Prefer": "return=representation"},
                timeout=5))
            ghosts = len(r.json()) if r.ok else 0
            # NULL trap: SQL NOT IN never matches NULL, so rows claiming
            # commissioned with our_node_id NULL need their own sweep.
            r2 = await asyncio.to_thread(lambda: req.patch(
                f"{postgrest_url}/hubitat_matter_devices",
                params={"is_commissioned": "eq.true", "our_node_id": "is.null"},
                json={"is_commissioned": False},
                headers={"Content-Type": "application/json", "Prefer": "return=representation"},
                timeout=5))
            ghosts += len(r2.json()) if r2.ok else 0
            if ghosts:
                logger.info(f"Commission All reality-check: {ghosts} row(s) claimed "
                            f"commissioned but their node is not in the live fabric — reset")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Commission All reality-check skipped: {e}")

    # Online + uncommissioned + not soft-removed, on the selected hub SET.
    resp = await aget(
        f"{postgrest_url}/hubitat_matter_devices",
        params={
            "is_online": "eq.true",
            "is_commissioned": "eq.false",
            "is_removed": "eq.false",
            "hub_ip": f"in.({','.join(hub_ips)})",
            "order": "hub_ip,device_name",
        },
        timeout=5,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to query discovered devices")

    devices = resp.json()
    if not devices:
        return {"started": False, "total": 0,
                "message": f"No online uncommissioned devices on {hub_label}"}

    # Fresh run state, then hand off to the background worker.
    from datetime import datetime  # app.py convention: datetime imported locally
    _bulk_commission_state.clear()
    _bulk_commission_state.update({
        "running": True,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "hubs": hub_label,
        "total": len(devices),
        "done": 0, "ok": 0, "failed": 0, "skipped": 0,
        "current": None, "results": [], "message": None, "aborted": False,
    })
    asyncio.create_task(_bulk_commission_worker(devices, hub_label))

    return {"started": True, "total": len(devices), "hubs": hub_label,
            "message": f"Sequential commission of {len(devices)} device(s) on {hub_label} started"}


@app.get("/api/matter/pairing-lock", tags=["matter"])
async def matter_pairing_lock_status():
    """
    Who currently holds the GLOBAL Matter-pairing mutex.

    A Hubitat pairs exactly ONE Matter device at a time (operator invariant,
    MSG-919), so Commission All, the hub->hub COPY orchestrator, and manual
    pairing all contend for one slot. This tells the UI (and the other feature)
    whether pairing is available, who holds it, and when the lock expires —
    so a refusal is actionable ("hub_port_copy is running") rather than opaque.

    {is_held, holder, holder_detail, acquired_at, expires_at, is_stale}
    """
    from services.matter_pairing_lock import status as _lock_status
    return await _lock_status()


@app.get("/api/matter/auto-commission-all/status", tags=["matter"])
async def matter_auto_commission_all_status():
    """
    Live progress of the (background, sequential) bulk-commission run: {running,
    total, done, ok, failed, skipped, current, results[], message, aborted,
    started_at, finished_at}. {running: false} with no totals = no run yet
    this app lifetime. State is in-memory only (resets on app restart) — it is
    run telemetry, not durable data; the durable outcome lives in
    hubitat_matter_devices.is_commissioned / device_matter_map.
    """
    return dict(_bulk_commission_state)


# =============================================================================
# Live Logs (navbar "Logs" modal — Hubitat-style live backend log stream)
# =============================================================================


@app.get("/api/logs/tail", tags=["logs"])
async def logs_tail(after: int = 0, limit: int = 500):
    """
    Incremental tail of the in-process log ring (services/log_stream.py).

    Pass the returned ``cursor`` back as ``after`` for a live stream — only
    entries newer than it are returned (oldest first, capped at ``limit``).
    Entries: {id, ts (epoch float), level, src (logger name), msg}. Filtering
    (source/level/text) is CLIENT-side by design: the modal polls unfiltered
    increments and filter flips are instant with no cursor games. In-memory
    only — restarts empty the ring; `docker logs` stays the durable stream.
    """
    h = get_log_handler()
    entries = h.tail(after_id=after, limit=min(max(limit, 1), 2000))
    return {"entries": entries, "cursor": h.head_id()}


@app.get("/api/logs/sources", tags=["logs"])
async def logs_sources():
    """
    Distinct log sources currently in the ring, most-active first:
    {src, count}. Sources are logger names — per-instance app loggers
    ({AppClass}.{label}), service modules (services.*), app.py itself — i.e.
    the "running apps/drivers/processes" list the modal's filters show.
    """
    return {"sources": get_log_handler().sources()}


# =============================================================================
# E2E Testing
# =============================================================================


@app.get("/api/e2e/events/stream", tags=["e2e-testing"])
async def e2e_event_stream(instance_id: int = Query(...)):
    """
    SSE endpoint for E2E test events.

    Streams test execution progress (step start/complete, scenario summaries)
    for a specific instance. The frontend connects with:
        new EventSource('/api/e2e/events/stream?instance_id=2')

    Note: Live device state comes from a direct WebSocket to Hub4's
    EventSocket, not from this SSE stream. This stream is only for
    test runner progress.
    """
    from fastapi.responses import StreamingResponse
    from services.e2e_events import get_e2e_broadcaster
    import json

    broadcaster = get_e2e_broadcaster()

    async def generate():
        # Initial keepalive comment (SSE spec: lines starting with ':')
        yield ": connected\n\n"

        async for event in broadcaster.subscribe(instance_id):
            if event is None:
                yield ": keepalive\n\n"
            else:
                yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # Disable nginx buffering for SSE
        }
    )


@app.get("/api/e2e/test/{instance_id}/scenarios", tags=["e2e-testing"])
async def get_test_scenarios(instance_id: int):
    """
    Get available test scenarios for an instance.

    Scenarios are built dynamically from the instance's device_selections
    and settings. Only scenarios relevant to the configured devices are
    returned (e.g., no dim level test if useDim is disabled).
    """
    from services.e2e_test_runner import E2ETestRunner

    runner = E2ETestRunner(instance_id)
    await runner.initialize()
    return runner.get_scenarios()


@app.get("/api/e2e/test/{instance_id}/devices", tags=["e2e-testing"])
async def get_test_devices(instance_id: int):
    """
    Get all devices for an instance with their current states.

    Returns devices grouped by category (motion_sensors, switches,
    pause_buttons, pause_switches), with live attribute data fetched
    directly from Hubitat Maker API (not from cache).

    The Maker API token is used server-side only — never exposed
    to the browser.
    """
    from services.instance_manager import get_instance_manager
    from services.device_to_hubs_classifier import fetch_device_live

    manager = get_instance_manager()
    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    device_selections = instance.get("device_selections", {})

    # Pre-resolve every selected canonical id to its (hub_ip, hubitat_id)
    # via the canonical devices row joined with hub_config — used to enrich
    # the response so the frontend can render hub-specific links and open
    # one EventSocket per distinct hub without an extra roundtrip.
    from services.device_to_hubs_classifier import get_device_by_canonical_id

    result = {}
    for category, device_ids in device_selections.items():
        devices = []
        for did in device_ids:
            # Selection ids are canonical PKs (Phase 5). fetch_device_live
            # resolves them to (hub, hubitat_id) and queries the right hub.
            device = fetch_device_live(did)
            row = get_device_by_canonical_id(did)
            extras = {}
            if row:
                extras = {
                    "_canonical_id": row["id"],
                    "_hub_ip":       row.get("hub_ip"),
                    "_hub_name":     row.get("hub_name"),
                    "_hubitat_id":   row.get("hubitat_id"),
                }
            if device:
                # Carry canonical/hub metadata alongside the Maker API
                # device dict. Underscore-prefixed so they don't collide
                # with any future Hubitat field names.
                device.update(extras)
                devices.append(device)
            else:
                devices.append({
                    "id": did,
                    "label": (row.get("label") if row else f"Device {did}"),
                    "error": "not found in Maker API",
                    **extras,
                })
        result[category] = devices

    return {
        "instance_id": instance_id,
        "label": instance.get("label"),
        "settings": instance.get("settings", {}),
        "device_categories": result
    }


@app.post("/api/e2e/test/{instance_id}/run/{scenario_id}", tags=["e2e-testing"])
async def run_test_scenario(instance_id: int, scenario_id: str):
    """
    Run a specific test scenario.

    Executes the scenario steps asynchronously in a background task.
    Progress is streamed via the SSE endpoint. The HTTP response
    returns immediately with a confirmation.
    """
    from services.e2e_test_runner import E2ETestRunner
    import asyncio

    runner = E2ETestRunner(instance_id)
    await runner.initialize()

    async def run_in_background():
        """Run scenario in background task so HTTP response returns fast."""
        try:
            await runner.run_scenario(scenario_id)
        except Exception as e:
            logger.error(f"E2E scenario '{scenario_id}' failed: {e}", exc_info=True)

    # Supervised: holds a strong ref so the background task can't be
    # GC'd mid-execution before the HTTP response returns and the caller
    # discards their scope.
    supervised_spawn(run_in_background(), name=f"e2e_run_scenario_{scenario_id}")
    return {
        "message": f"Scenario '{scenario_id}' started",
        "instance_id": instance_id
    }


@app.post("/api/e2e/test/{instance_id}/stop", tags=["e2e-testing"])
async def stop_test(instance_id: int):
    """
    Cancel the currently-running scenario for this instance, if any.

    Sets the runner's cancel flag; the scenario loop checks it between
    steps. Returns immediately — does NOT block waiting for the in-flight
    step to actually unwind.
    """
    from services.e2e_test_runner import get_active_runner
    runner = get_active_runner(instance_id)
    if runner is None:
        return {"ok": True, "stopped": False, "reason": "no active run"}
    try:
        await runner.cancel()
        return {"ok": True, "stopped": True}
    except Exception as e:
        logger.error(f"stop_test({instance_id}) failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/e2e/test/{instance_id}/run-all", tags=["e2e-testing"])
async def run_all_test_scenarios(instance_id: int):
    """
    Run all test scenarios for an instance sequentially.

    Scenarios execute one after another in a background task.
    Progress is streamed via the SSE endpoint.
    """
    from services.e2e_test_runner import E2ETestRunner
    import asyncio

    runner = E2ETestRunner(instance_id)
    await runner.initialize()

    async def run_all():
        """Run all scenarios with device state save/restore."""
        try:
            # Snapshot device states before any tests run
            await runner.save_device_states()

            # Run all scenarios sequentially
            for scenario in runner.get_scenarios():
                try:
                    await runner.run_scenario(scenario["id"])
                except Exception as e:
                    logger.error(
                        f"E2E scenario '{scenario['id']}' failed: {e}",
                        exc_info=True
                    )
        finally:
            # Restore devices to their original states regardless of test outcome
            try:
                await runner.restore_device_states()
            except Exception as e:
                logger.error(
                    f"E2E device state restore failed: {e}",
                    exc_info=True
                )

    # Supervised: see comment on the per-scenario callsite above.
    supervised_spawn(run_all(), name=f"e2e_run_all_inst{instance_id}")
    return {"message": "All scenarios started", "instance_id": instance_id}


def _mode_client():
    """Return the client that owns LOCATION-MODE operations, honoring the
    ``maker_api_enabled`` system setting — the SAME transport switch device
    commands use (see device_commander). Default (False, since 2026-05-17) →
    Hubitat admin API against the ``is_primary`` hub (standalone, no Maker app
    needed); True → legacy Maker API (switchback).

    Consolidates the three mode routes onto ONE transport-selection point so
    the mode path can never again silently diverge from the rest of the app —
    which is exactly how the "can't change modes" bug hid (2026-07-04)."""
    from services.settings_resolver import get_resolver
    if get_resolver().get_system('maker_api_enabled', False):
        from services.hubitat_client import get_default_client
        return get_default_client()
    from services.hubitat_admin_client import get_client
    from services.mode_poller import _authoritative_hub
    hub = _authoritative_hub()
    if not hub:
        raise HTTPException(status_code=503,
                            detail="no primary hub configured for modes")
    hub_name, hub_ip = hub
    return get_client(hub_ip, hub_name)


@app.get("/api/modes", tags=["modes"])
async def get_modes():
    """Get available location modes."""
    try:
        client = _mode_client()
        modes = client.get_modes()
        return modes

    except Exception as e:
        logger.error(f"Failed to get modes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/modes/current", tags=["modes"])
async def get_current_mode():
    """Get current location mode."""
    try:
        client = _mode_client()
        mode_id, mode_name = client.get_current_mode()
        return {"id": mode_id, "name": mode_name}

    except Exception as e:
        logger.error(f"Failed to get current mode: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/modes/set", tags=["modes"])
async def set_mode(request: Request):
    """Set the Hubitat location mode. Body ``{mode_id}`` (or ``{name}``).

    Powers the navbar mode dropdown (change location mode from the global app,
    operator directive 2026-06-22)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    client = _mode_client()
    mode_id = (body or {}).get("mode_id")
    if mode_id is None:
        # Resolve by name if id not given.
        name = (body or {}).get("name")
        if not name:
            raise HTTPException(status_code=400, detail="mode_id or name required")
        match = next((m for m in (client.get_modes() or [])
                      if str(m.get("name")) == str(name)), None)
        if not match:
            raise HTTPException(status_code=404, detail=f"mode {name!r} not found")
        mode_id = match.get("id")
    try:
        ok = client.set_mode(str(mode_id))
        return {"ok": bool(ok), "mode_id": mode_id}
    except Exception as e:
        logger.error(f"Failed to set mode {mode_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Web UI
# =============================================================================


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request):
    """Main dashboard."""
    return templates.TemplateResponse(request, "dashboard.html")


@app.get("/instance/new", response_class=HTMLResponse, include_in_schema=False)
async def new_instance(request: Request):
    """Instance creation wizard."""
    return templates.TemplateResponse(request, "instance_wizard.html")


@app.get("/api/instances/{instance_id}/events", tags=["instances"])
async def stream_instance_events(instance_id: int):
    """Recent events for an instance's subscribed devices."""
    from services.instance_manager import get_instance_manager
    manager = get_instance_manager()

    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    # Collect all device IDs from instance's device_selections
    device_ids = []
    for ids in (instance.get('device_selections') or {}).values():
        device_ids.extend(str(d) for d in ids)

    if not device_ids:
        return []

    try:
        import requests as req
        postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
        response = req.get(
            f"{postgrest_url}/event_log",
            params={
                "hubitat_device_id": f"in.({','.join(device_ids)})",
                "order": "received_at.desc",
                "limit": "50"
            },
            timeout=5
        )
        if response.status_code == 200:
            return response.json()
        return []
    except Exception as e:
        logger.error(f"Failed to get instance events: {e}")
        return []


@app.get("/matter", response_class=HTMLResponse, include_in_schema=False)
async def matter_page(request: Request):
    """Matter device management page."""
    return templates.TemplateResponse(request, "matter.html")


@app.get("/hubs", include_in_schema=False)
async def hubs_page(request: Request):
    """
    DEPRECATED standalone hub page (was a DUPLICATE of Settings -> Hubs, with its
    own copy of the hub-card template — the source of drift). Redirect to the one
    canonical hub UI so there's no second page to keep in sync. templates/hubs.html
    is now unused.
    """
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin/settings", status_code=307)


@app.get("/sonos", response_class=HTMLResponse, include_in_schema=False)
async def sonos_page(request: Request):
    """Sonos driver — standalone speaker controller (Drivers section).
    TTS announce, set/restore/lock volume, play mp3, stop. See services/sonos/."""
    return templates.TemplateResponse(request, "sonos.html")


@app.get("/admin/settings", response_class=HTMLResponse, include_in_schema=False)
async def admin_settings_page(request: Request):
    """System settings page — edit rows in system_settings table.
    Reached via the gear icon in the navbar."""
    return templates.TemplateResponse(request, "admin_settings.html")


# =============================================================================
# Hub config CRUD
# =============================================================================
# All routing in the app reads from hub_config (joined into devices via
# hub_id FK). Editing hub_config from the UI lets the user change a hub's
# IP / app number / token env without redeploying. After every write we
# invalidate the in-process lookup caches so changes take effect within
# a single event-loop tick.

@app.get("/api/canonical-devices/{canonical_id}/recent-events", tags=["devices"])
async def get_recent_events_for_device(
    canonical_id: int,
    event_type: Optional[str] = None,
    limit: int = 20,
):
    """
    Return the most recent N events for a canonical device, optionally
    filtered by event_type. Used by the KPI modal's per-chip popover to
    show the last raw values that produced the breakdown count.

    event_log.hubitat_device_id contains the canonical PK post-Phase-5
    (the column name is legacy).
    """
    import requests as _req
    if limit <= 0 or limit > 200:
        limit = 20
    params = {
        "hubitat_device_id": f"eq.{canonical_id}",
        "order": "received_at.desc",
        "limit": str(limit),
        "select": "event_type,event_value,event_unit,received_at",
    }
    if event_type:
        params["event_type"] = f"eq.{event_type}"
    try:
        r = _req.get(
            f"{os.environ.get('POSTGREST_URL', 'http://postgrest:3001')}/event_log",
            params=params,
            timeout=5,
        )
        if r.status_code == 200:
            return r.json()
        raise HTTPException(status_code=r.status_code, detail=r.text)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"get_recent_events_for_device({canonical_id}, {event_type}) failed: {e}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/canonical-devices", tags=["devices"])
async def list_canonical_devices():
    """
    List all rows in the canonical `devices` table.
    Used by the wizard to render chips with labels for any saved selection,
    even when the selection's device id doesn't appear in the current
    category's capability-filtered device list.
    """
    try:
        r = await aget(
            f"{os.environ.get('POSTGREST_URL', 'http://postgrest:3001')}/devices",
            params={"select": "id,label,hub_ip,hubitat_id", "order": "label"},
            timeout=5,
        )
        if r.status_code == 200:
            return r.json()
        raise HTTPException(status_code=r.status_code, detail=r.text)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"list_canonical_devices failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/hubs/health", tags=["hubs"])
async def list_hub_health():
    """
    Per-hub WS + reconcile health. Used by the dashboard alert banner to
    decide whether to surface a 'recommend Maker API as fallback' warning.
    """
    pg = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    r = await aget(f"{pg}/hub_health", params={"order": "hub_id"}, timeout=5)
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    rows = r.json()
    # Enrich each health row with the hub's NAME. hub_health has no name column
    # (keyed by hub_id = hub_config.id), so the dashboard banner used to render
    # the raw id — which surfaced as the confusing phantom 'Hub 4' for home_3
    # (id 4). Join hub_config so the banner can label by name. Best-effort: if
    # the name lookup fails the rows still return, and the UI falls back to the id.
    try:
        cfg = await aget(f"{pg}/hub_config", params={"select": "id,hub_name"}, timeout=5)
        names = {c["id"]: c.get("hub_name") for c in cfg.json()} if cfg.status_code == 200 else {}
        for row in rows:
            row["hub_name"] = names.get(row.get("hub_id"))
    except Exception as e:
        logger.debug(f"hub health name-enrich skipped: {e}")
    return rows


@app.post("/api/hubs/{hub_ip}/reboot", tags=["hubs"])
async def reboot_hub(hub_ip: str):
    """
    Reboot a Hubitat hub (Settings -> Reboot Hub) via the admin API. The hub
    goes offline ~2-3 minutes. Primary use: revive a hung Matter bridge /
    eventsocket, which on Hubitat only recover on a reboot. UI gates this behind
    a confirmation modal.
    """
    hub_name = "default"
    try:
        r = await aget(
            f"{os.environ.get('POSTGREST_URL', 'http://postgrest:3001')}/hub_config",
            params={"hub_ip": f"eq.{hub_ip}", "select": "hub_name", "limit": "1"},
            timeout=5,
        )
        if r.status_code == 200 and r.json():
            hub_name = r.json()[0].get("hub_name", "default")
    except Exception:
        pass
    from services.hubitat_admin_client import get_client
    try:
        client = get_client(hub_ip, hub_name)
        ok = await asyncio.to_thread(client.reboot)
        return {"hub_ip": hub_ip, "hub_name": hub_name, "reboot_initiated": bool(ok)}
    except Exception as e:
        logger.error(f"reboot_hub {hub_ip} failed: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"reboot failed: {e}")


@app.post("/api/hubs/reboot-all", tags=["hubs"])
async def reboot_all_hubs():
    """
    Reboot ALL enabled Hubitat hubs. Each goes offline ~2-3 minutes. Use when
    the Matter bridge / eventsockets are dead across the board. UI gates this
    behind a confirmation modal.
    """
    r = await aget(
        f"{os.environ.get('POSTGREST_URL', 'http://postgrest:3001')}/hub_config",
        params={"is_enabled": "eq.true", "select": "hub_ip,hub_name"},
        timeout=5,
    )
    hubs = r.json() if r.status_code == 200 else []
    from services.hubitat_admin_client import get_client
    results = []
    for h in hubs:
        ip, name = h.get("hub_ip"), h.get("hub_name", "default")
        try:
            ok = await asyncio.to_thread(get_client(ip, name).reboot)
        except Exception as e:  # one hub failing must not abort the rest
            logger.error(f"reboot_all: {ip} failed: {e}")
            ok = False
        results.append({"hub_ip": ip, "hub_name": name, "reboot_initiated": bool(ok)})
    return {"count": len(results), "results": results}


# Restart trigger file — tmpfs shared with the host. The host-side
# smarthome-restart-watcher.service polls it and runs start.sh on "reboot".
# Canonical STANDARD RESTART.1-4 (mirrors NVR + TILES).
# The app writes its OWN request file (own-file design, 2026-07-09): the app
# owns it (appuser), so it can always overwrite — unlike the old shared 'trigger'
# file owned by the host watcher, which the container couldn't write (EACCES).
# Content is "<action> <nonce>"; the watcher acts on content-CHANGE (the nonce
# makes repeated same-action requests distinct). The host watcher only READS it.
RESTART_TRIGGER_FILE = '/dev/shm/smarthome-restart/request'


class RestartBody(BaseModel):
    reason: Optional[str] = None


@app.post("/api/restart", tags=["system"])
async def api_restart(body: Optional[RestartBody] = None):
    """
    Trigger a full host-side restart (start.sh) via the trigger-file pattern.

    The container can't run start.sh itself (it does `docker compose up`, which
    needs the host + AWS creds). Instead we write "reboot" to a tmpfs file shared
    with the host; the smarthome-restart-watcher systemd service picks it up and
    runs start.sh (full rebuild + AWS-cred reload + code reload). If the trigger
    dir isn't mounted the watcher isn't installed -> 503 (run ./start.sh once).
    """
    reason = (body.reason if body and body.reason else "UI requested restart")
    logger.info(f"[Restart] full restart requested via UI: {reason}")
    trigger_dir = os.path.dirname(RESTART_TRIGGER_FILE)
    if not os.path.isdir(trigger_dir):
        raise HTTPException(
            status_code=503,
            detail="Restart watcher not available. Run ./start.sh once on the host to install it.",
        )

    def _write_trigger():
        # Brief delay so the HTTP 200 reaches the browser before start.sh tears
        # the container down.
        import time as _time
        _time.sleep(1)
        try:
            with open(RESTART_TRIGGER_FILE, 'w') as f:
                f.write(f'reboot {_time.time()}')
            logger.info("[Restart] wrote 'reboot' trigger — host watcher will run start.sh")
        except Exception as e:
            logger.error(f"[Restart] failed to write trigger file: {e}")

    import threading as _threading
    _threading.Thread(target=_write_trigger, daemon=True, name='restart-trigger').start()
    return {
        "success": True,
        "message": "Full restart initiated (start.sh on host). App unavailable ~30-60s.",
    }


@app.get("/api/hubs", tags=["hubs"])
async def list_hubs():
    """List all configured Hubitat hubs (rows of hub_config)."""
    try:
        r = await aget(
            f"{os.environ.get('POSTGREST_URL', 'http://postgrest:3001')}/hub_config",
            params={"order": "id"},
            timeout=5,
        )
        if r.status_code == 200:
            return r.json()
        raise HTTPException(status_code=r.status_code, detail=r.text)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"list_hubs failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _invalidate_hub_caches():
    """Drop in-process caches that reference hub_config rows."""
    try:
        from services.device_to_hubs_classifier import invalidate_device_lookup_cache
        invalidate_device_lookup_cache()
    except Exception:
        pass
    try:
        from services.webhook_router import get_webhook_router
        get_webhook_router().invalidate_device_cache()
    except Exception:
        pass


@app.patch("/api/hubs/{hub_id}", tags=["hubs"])
async def update_hub(hub_id: int, body: Dict[str, Any]):
    """
    Update a hub_config row. Accepts any subset of:
      hub_name, hub_ip, maker_api_app_number, maker_api_token_env,
      is_primary, is_enabled.
    Other fields are ignored.

    On success, invalidates the in-process device-lookup caches and
    re-syncs `devices.hub_ip` from the new `hub_config.hub_ip` for any
    rows that referenced this hub (denormalized cache stays consistent).
    """
    allowed = {
        "hub_name", "hub_ip", "maker_api_app_number",
        "maker_api_token_env", "is_primary", "is_enabled",
    }
    patch = {k: v for k, v in body.items() if k in allowed}
    if not patch:
        raise HTTPException(status_code=400, detail="No editable fields in body")

    postgrest_url = os.environ.get("POSTGREST_URL", "http://postgrest:3001")

    # If enable/disable or rename is in this patch, capture the BEFORE row so we
    # can reconcile this hub's eventsocket task (spawn/teardown/respawn) after.
    old_row = None
    if "is_enabled" in patch or "hub_name" in patch:
        try:
            _pr = await aget(
                f"{postgrest_url}/hub_config",
                params={"id": f"eq.{hub_id}",
                        "select": "hub_name,hub_ip,is_enabled", "limit": "1"},
                timeout=5,
            )
            if _pr.status_code == 200 and _pr.json():
                old_row = _pr.json()[0]
        except Exception:
            pass

    # If the user is setting is_primary=true, clear it on every other row
    # first — exactly one primary at a time.
    if patch.get("is_primary") is True:
        try:
            await apatch(
                f"{postgrest_url}/hub_config",
                params={"id": f"neq.{hub_id}"},
                json={"is_primary": False},
                headers={"Content-Type": "application/json"},
                timeout=5,
            )
        except Exception as e:
            logger.warning(f"Could not clear is_primary on others: {e}")

    try:
        r = await apatch(
            f"{postgrest_url}/hub_config",
            params={"id": f"eq.{hub_id}"},
            json=patch,
            headers={
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            timeout=5,
        )
        if r.status_code not in (200, 204):
            raise HTTPException(status_code=r.status_code, detail=r.text)

        # If hub_ip changed, mirror it into devices.hub_ip (denormalized).
        if "hub_ip" in patch:
            try:
                await apatch(
                    f"{postgrest_url}/devices",
                    params={"hub_id": f"eq.{hub_id}"},
                    json={"hub_ip": patch["hub_ip"]},
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
            except Exception as e:
                logger.warning(f"Could not resync devices.hub_ip: {e}")

        _invalidate_hub_caches()

        # Reconcile this hub's eventsocket task with its new state — enable
        # spawns it, disable tears it down, rename respawns under the new name.
        # Makes hub edits take effect LIVE (audit F3.1 / no-restart-to-forget).
        if old_row is not None:
            try:
                from services.hubitat_eventsocket_client import (
                    start_hub_socket, stop_hub_socket,
                )
                old_name = old_row.get("hub_name")
                new_name = patch.get("hub_name", old_name)
                new_ip = patch.get("hub_ip", old_row.get("hub_ip"))
                new_enabled = patch.get("is_enabled", old_row.get("is_enabled"))
                if old_name and new_name != old_name:
                    await stop_hub_socket(old_name)  # rename: kill the old task
                if new_enabled:
                    await start_hub_socket({"id": hub_id, "hub_name": new_name,
                                            "hub_ip": new_ip})
                else:
                    await stop_hub_socket(new_name)
            except Exception as e:
                logger.warning(f"update_hub: eventsocket reconcile failed: {e}")

        return r.json() if r.status_code == 200 else {"ok": True, "id": hub_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"update_hub({hub_id}) failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/hubs", tags=["hubs"])
async def create_hub(body: Dict[str, Any]):
    """Create a new hub_config row."""
    required = ("hub_name", "hub_ip", "maker_api_app_number", "maker_api_token_env")
    missing = [k for k in required if not body.get(k)]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing fields: {missing}")

    payload = {k: body[k] for k in required}
    payload["is_primary"] = bool(body.get("is_primary", False))
    payload["is_enabled"] = bool(body.get("is_enabled", True))

    postgrest_url = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    try:
        r = await apost(
            f"{postgrest_url}/hub_config",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            timeout=5,
        )
        if r.status_code not in (200, 201):
            raise HTTPException(status_code=r.status_code, detail=r.text)
        _invalidate_hub_caches()
        created = r.json()
        # Spawn the eventsocket task for the new hub immediately if enabled — no
        # restart needed to pick it up (the inverse of no-restart-to-forget).
        row = (created[0] if isinstance(created, list) and created
               else created if isinstance(created, dict) else None)
        if row and row.get("is_enabled") and row.get("hub_name"):
            try:
                from services.hubitat_eventsocket_client import start_hub_socket
                await start_hub_socket({
                    "id": row.get("id"), "hub_name": row.get("hub_name"),
                    "hub_ip": row.get("hub_ip"),
                })
            except Exception as e:
                logger.warning(f"create_hub: eventsocket spawn failed: {e}")
        return created
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"create_hub failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/hubs/{hub_id}", tags=["hubs"])
async def delete_hub(hub_id: int):
    """
    Delete a hub. Devices still homed to this hub are DETACHED (hub_id set NULL)
    and then re-classified internally against the remaining hubs — so a hub can
    ALWAYS be removed. Devices that also live on another hub (hubMesh) get
    re-homed there by the classifier; devices unique to the removed hub become
    unclassified (hub_id NULL) until they reappear elsewhere. (Was: refused with
    409 while any device referenced the hub — operator directive 2026-07-09.)
    """
    postgrest_url = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    try:
        # 0) Capture this hub's NAME before the row is gone — the eventsocket
        #    tasks are keyed by hub_name, so we need it to tear the task down.
        hub_name = None
        try:
            nr = await aget(
                f"{postgrest_url}/hub_config",
                params={"id": f"eq.{hub_id}", "select": "hub_name", "limit": "1"},
                timeout=5,
            )
            if nr.status_code == 200 and nr.json():
                hub_name = nr.json()[0].get("hub_name")
        except Exception:
            pass

        # 1) INTELLIGENT CASCADE (NOT a SQL CASCADE — that would DELETE the
        #    devices). devices.hub_id is NOT NULL, so we can't detach; instead
        #    reassign this hub's devices to another remaining hub (prefer the
        #    primary) so the FK is satisfied and NO device is lost. The
        #    classifier (step 3) then re-homes each to the hub that actually
        #    owns it — correct for the common case where the deleted hub held
        #    only hubMesh MIRRORS whose native devices live on other hubs.
        tr = await aget(
            f"{postgrest_url}/hub_config",
            params={"id": f"neq.{hub_id}", "select": "id,is_primary",
                    "order": "is_primary.desc.nullslast", "limit": "1"},
            timeout=5,
        )
        target = (tr.json()[0]["id"]
                  if (tr.status_code == 200 and tr.json()) else None)
        if target is None:
            raise HTTPException(
                status_code=409,
                detail="Cannot delete the only remaining hub — its devices have nowhere to go.",
            )
        pr = await apatch(
            f"{postgrest_url}/devices",
            params={"hub_id": f"eq.{hub_id}"},
            json={"hub_id": target},
            headers={"Prefer": "return=representation"},
            timeout=15,
        )
        if pr.status_code not in (200, 204):
            raise HTTPException(
                status_code=pr.status_code,
                detail=f"reassign devices failed: {pr.text[:200]}",
            )
        try:
            reassigned = len(pr.json()) if pr.text else 0
        except Exception:
            reassigned = 0

        # 2) Delete the hub.
        d = await adelete(
            f"{postgrest_url}/hub_config",
            params={"id": f"eq.{hub_id}"},
            timeout=5,
        )
        if d.status_code not in (200, 204):
            raise HTTPException(status_code=d.status_code, detail=d.text)
        _invalidate_hub_caches()

        # 2b) Tear down this hub's eventsocket reconnect task and delete its
        #     hub_health row EXPLICITLY — do NOT rely on FK ON DELETE CASCADE
        #     (audit F3.1/F3.2; CASCADE-forbidden + no-restart-to-forget policy).
        #     Without this the deleted hub's task loops forever writing failures
        #     into a surviving hub_health row → the phantom 'Hub N' banner. Stop
        #     the task FIRST so a last-gasp write can't re-create the row.
        if hub_name:
            try:
                from services.hubitat_eventsocket_client import stop_hub_socket
                await stop_hub_socket(hub_name)
            except Exception as e:
                logger.warning(f"delete_hub: eventsocket teardown failed: {e}")
        try:
            await adelete(
                f"{postgrest_url}/hub_health",
                params={"hub_id": f"eq.{hub_id}"},
                timeout=5,
            )
        except Exception as e:
            logger.warning(f"delete_hub: hub_health row delete failed: {e}")

        # 3) Re-classify internally — re-home the detached devices to the
        #    remaining hubs. Best-effort: a classifier hiccup must not fail the
        #    delete (the hub is already gone).
        reclassified = False
        try:
            import asyncio as _asyncio
            from services.device_to_hubs_classifier import (
                run_classification, invalidate_cache,
            )
            loop = _asyncio.get_running_loop()
            await loop.run_in_executor(None, run_classification)
            invalidate_cache()
            reclassified = True
        except Exception as e:
            logger.warning("delete_hub: post-delete reclassification failed: %s", e)

        return {
            "ok": True, "id": hub_id,
            "reassigned_devices": reassigned, "reassigned_to_hub_id": target,
            "reclassified": reclassified,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"delete_hub({hub_id}) failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/instance/{instance_id}", response_class=HTMLResponse, include_in_schema=False)
async def instance_detail(request: Request, instance_id: int):
    """Instance detail/edit page."""
    return templates.TemplateResponse(
        request, "instance_detail.html", {"instance_id": instance_id}
    )


# =============================================================================
# Dashboard WebSocket (real-time updates — replaces polling)
# =============================================================================


@app.websocket("/ws/dashboard")
async def dashboard_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for real-time dashboard updates.

    Replaces the 30-second polling loop. When Hubitat webhooks arrive,
    events are pushed instantly to all connected dashboard clients.
    The frontend uses these events to patch individual cards instead
    of re-rendering the entire grid (no more flicker).

    Message types sent to client:
        - device_event: A device changed state
        - instance_update: Instance metadata changed
        - instances_snapshot: Full instance list (sent on connect)
    """
    from services.dashboard_broadcaster import get_dashboard_broadcaster
    from services.instance_manager import get_instance_manager
    import json

    await websocket.accept()
    broadcaster = get_dashboard_broadcaster()
    queue = await broadcaster.connect()

    try:
        # Send initial snapshot so the client doesn't need a separate fetch
        manager = get_instance_manager()
        instances = manager.get_all_instances()
        await websocket.send_json({
            "type": "instances_snapshot",
            "instances": instances
        })

        # Stream events to client
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
                await websocket.send_json(event)
            except asyncio.TimeoutError:
                # Keepalive ping — detect dead connections
                await websocket.send_json({"type": "ping"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"Dashboard WS closed: {e}")
    finally:
        await broadcaster.disconnect(queue)


# =============================================================================
# KPI Metrics
# =============================================================================


@app.get("/api/instances/{instance_id}/metrics", tags=["instances"])
async def get_instance_metrics(
    instance_id: int,
    hours: int = Query(24, description="Lookback window in hours")
):
    """
    Aggregated KPI metrics for a single instance.

    Queries event_log for the instance's subscribed devices and computes:
    - Event counts (total, per-hour, per-device, per-type)
    - Last activity timestamps per device
    - Switch on/off operation counts
    - Motion active/inactive ratios
    - Error tracking from app_instances table

    Args:
        instance_id: Target instance
        hours: Lookback window (default 24h)
    """
    from services.instance_manager import get_instance_manager
    import requests as req
    from datetime import datetime, timedelta, timezone

    manager = get_instance_manager()
    instance = manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    # Collect device IDs from instance
    device_ids = []
    for ids in (instance.get('device_selections') or {}).values():
        device_ids.extend(str(d) for d in ids)

    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=hours)).isoformat()

    # Fetch events for this instance's devices within the time window
    events = []
    if device_ids:
        try:
            resp = req.get(
                f"{postgrest_url}/event_log",
                params={
                    "hubitat_device_id": f"in.({','.join(device_ids)})",
                    "received_at": f"gte.{since}",
                    "order": "received_at.desc",
                    "limit": "2000"
                },
                timeout=10
            )
            if resp.status_code == 200:
                events = resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch metrics events: {e}")

    # Compute aggregations
    total_events = len(events)

    # Events per hour (for chart)
    hourly_buckets = {}
    for h in range(hours):
        bucket_time = now - timedelta(hours=h)
        key = bucket_time.strftime('%Y-%m-%dT%H:00:00')
        hourly_buckets[key] = 0

    # Per-device stats
    device_stats = {}
    # Per-event-type counts
    type_counts = {}

    for evt in events:
        # Hourly bucketing
        received = evt.get('received_at', '')
        if received:
            try:
                # Truncate to hour
                hour_key = received[:13] + ':00:00'
                if hour_key in hourly_buckets:
                    hourly_buckets[hour_key] += 1
            except (IndexError, TypeError):
                pass

        # Device stats
        dev_id = evt.get('hubitat_device_id', '')
        dev_name = evt.get('device_name', dev_id)
        evt_type = evt.get('event_type', '')
        evt_value = evt.get('event_value', '')

        if dev_id not in device_stats:
            device_stats[dev_id] = {
                'device_name': dev_name,
                'event_count': 0,
                'last_event_at': received,
                'last_event_type': evt_type,
                'last_event_value': evt_value,
                'type_breakdown': {}
            }
        device_stats[dev_id]['event_count'] += 1

        # Type breakdown per device
        if evt_type not in device_stats[dev_id]['type_breakdown']:
            device_stats[dev_id]['type_breakdown'][evt_type] = {
                'count': 0,
                'last_value': evt_value,
                'last_at': received
            }
        device_stats[dev_id]['type_breakdown'][evt_type]['count'] += 1

        # Global type counts
        type_counts[evt_type] = type_counts.get(evt_type, 0) + 1

    # Sort hourly buckets chronologically
    hourly_sorted = sorted(hourly_buckets.items())

    # Enrich device_stats with canonical-→hub mapping so the UI can render
    # a hyperlink to the device's edit page on the hub that natively owns
    # it. event_log.hubitat_device_id contains the CANONICAL devices.id PK
    # post-Phase-5 (the column name is legacy). For each id we look up the
    # canonical row + its hub_config join in one batch.
    if device_stats:
        try:
            import requests as _req
            postgrest_url = os.environ.get(
                "POSTGREST_URL", "http://postgrest:3001"
            )
            ids_csv = ",".join(str(k) for k in device_stats.keys())
            resp = _req.get(
                f"{postgrest_url}/devices",
                params={
                    "select": "id,hubitat_id,label,hub_config(hub_name,hub_ip)",
                    "id": f"in.({ids_csv})",
                },
                timeout=5,
            )
            if resp.status_code == 200:
                for row in resp.json():
                    cid = str(row["id"])
                    if cid in device_stats:
                        hub = row.get("hub_config") or {}
                        device_stats[cid]["canonical_id"]  = row["id"]
                        device_stats[cid]["hubitat_id"]    = row.get("hubitat_id")
                        device_stats[cid]["hub_ip"]        = hub.get("hub_ip")
                        device_stats[cid]["hub_name"]      = hub.get("hub_name")
                        # Prefer the canonical label when we have it — the
                        # event_log device_name may carry the mirror's
                        # ' on Home N' suffix.
                        if row.get("label"):
                            device_stats[cid]["device_name"] = row["label"]
        except Exception as e:
            logger.warning(f"KPI device enrichment failed: {e}")

    # Instance metadata
    running = manager.get_running_instance(instance_id) is not None

    return {
        "instance_id": instance_id,
        "label": instance.get("label"),
        "is_paused": instance.get("is_paused", False),
        "is_running": running,
        "error_count": instance.get("error_count", 0),
        "last_error": instance.get("last_error"),
        "last_activity_at": instance.get("last_activity_at"),
        "created_at": instance.get("created_at"),
        "device_count": len(device_ids),
        "window_hours": hours,
        "total_events": total_events,
        "hourly_events": [
            {"hour": h, "count": c} for h, c in hourly_sorted
        ],
        "device_stats": device_stats,
        "type_counts": type_counts,
        "device_selections": instance.get("device_selections", {}),
        "settings": instance.get("settings", {}),
    }


# =============================================================================
# Error Handlers
# =============================================================================


@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    """Handle 404 errors — JSON for API routes, HTML for web routes."""
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=404,
            content={"error": exc.detail if hasattr(exc, "detail") else "Not found"},
        )
    return templates.TemplateResponse(
        request, "error.html", {"error": "Page not found"}, status_code=404
    )


@app.exception_handler(500)
async def server_error_handler(request: Request, exc: Exception):
    """Handle 500 errors — JSON for API routes, HTML for web routes."""
    logger.error(f"Server error: {exc}")
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error"},
        )
    return templates.TemplateResponse(
        request, "error.html", {"error": "Server error"}, status_code=500
    )


# =============================================================================
# Development server
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=5000, reload=True)
