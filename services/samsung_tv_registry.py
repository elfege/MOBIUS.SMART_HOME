"""
services/samsung_tv_registry.py

Database-backed registry that owns one SamsungTVClient per row in
`dsapp.samsung_tv_instances`. Replaces the env-var-driven singleton from
the pre-2026-06-05 single-tenant design.

Lifecycle
---------
    start_all()          - lifespan startup: load enabled rows, spawn clients
    reload_instance(id)  - PATCH on a row: tear down old client, spawn new
    remove_instance(id)  - DELETE of a row: tear down + forget
    stop_all()           - lifespan shutdown: stop every client cleanly

Lookups
-------
    get(instance_id) -> SamsungTVClient | None
        Returns the live client for the given DB row id. None if the row
        is unknown, disabled, or paused. The blueprint uses this on every
        instance-scoped request (`/samsung-tv/<id>/*`).

    list_instances() -> List[Dict]
        Returns the row metadata (no client object) for UI population.

Token persistence
-----------------
When the TV hands the client a new auth token (after the user accepts
the SmartView prompt), the registry persists it back to the DB row's
`token` column. Survives container restarts; no on-disk files involved.

Per-instance callbacks
----------------------
The Hubitat-device → URL callback map (used by the push subsystem) is
stored in the row's `callbacks` JSONB column. The registry surfaces
helpers to read / write it scoped by instance.

Plan
----
docs/plans/samsung_tv_multi_instance_refactor_per_instance_ip_mac_token_in_database.md
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

import requests

from services.samsung_tv_client import (
    SamsungTVClient,
    SamsungTVConfig,
    TVConnectionState,
    TVPowerState,
)

logger = logging.getLogger(__name__)

POSTGREST_URL = os.environ.get("POSTGREST_URL", "http://postgrest:3001")


# =============================================================================
# Registry
# =============================================================================


class SamsungTVRegistry:
    """
    Process-wide registry of SamsungTVClient instances, one per enabled
    row in `dsapp.samsung_tv_instances`. Thread-safe under asyncio.

    Lookups go through .get(instance_id). Lifecycle (start_all, stop_all,
    reload_instance, remove_instance) is called from the FastAPI lifespan
    and from the PATCH/DELETE endpoints on the samsung_tv_instances
    resource.
    """

    def __init__(self) -> None:
        # Maps DB row id -> live client. Disabled / paused rows are NOT in
        # this dict (they don't have a running client). The membership of
        # this dict is the authoritative answer to "does instance X have
        # a live WS connection right now?".
        self._clients: Dict[int, SamsungTVClient] = {}

        # Maps DB row id -> the latest row data we used to construct that
        # client. Used by reload_instance to decide whether a full restart
        # is needed or whether the row update is metadata-only (label
        # rename, callbacks edit) and the client can keep running.
        self._row_snapshot: Dict[int, Dict[str, Any]] = {}

        # Coarse-grain serialization. start_all / reload / remove all
        # potentially mutate _clients; a single lock keeps them mutually
        # exclusive without per-instance lock proliferation.
        self._lock = asyncio.Lock()

        # PostgREST HTTP session — reused across calls to amortize the
        # TCP/TLS setup. Closed in stop_all().
        self._http: Optional[requests.Session] = None

    # ------------------------------------------------------------------
    # HTTP plumbing (sync requests, called via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _session(self) -> requests.Session:
        if self._http is None:
            self._http = requests.Session()
        return self._http

    def _fetch_rows(self) -> List[Dict[str, Any]]:
        """
        Return every row in `dsapp.samsung_tv_instances` regardless of
        enable / pause state. Caller filters as needed.

        Sync — call via asyncio.to_thread from async contexts.
        """
        try:
            r = self._session().get(
                f"{POSTGREST_URL}/samsung_tv_instances",
                params={"order": "id.asc"},
                timeout=5,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error("samsung_tv_registry: failed to fetch rows: %s", e)
            return []

    def _fetch_row(self, instance_id: int) -> Optional[Dict[str, Any]]:
        """Read one row by id. Returns None if not found or on error."""
        try:
            r = self._session().get(
                f"{POSTGREST_URL}/samsung_tv_instances",
                params={"id": f"eq.{instance_id}"},
                timeout=5,
            )
            r.raise_for_status()
            rows = r.json()
            return rows[0] if rows else None
        except Exception as e:
            logger.error(
                "samsung_tv_registry: failed to fetch row %s: %s",
                instance_id, e,
            )
            return None

    def _patch_row(self, instance_id: int, fields: Dict[str, Any]) -> bool:
        """PATCH a row. Used for token persistence + heartbeat fields."""
        try:
            r = self._session().patch(
                f"{POSTGREST_URL}/samsung_tv_instances",
                params={"id": f"eq.{instance_id}"},
                json=fields,
                headers={"Prefer": "return=minimal"},
                timeout=5,
            )
            return r.status_code in (200, 204)
        except Exception as e:
            logger.debug(
                "samsung_tv_registry: PATCH row %s failed (suppressed): %s",
                instance_id, e,
            )
            return False

    # ------------------------------------------------------------------
    # Client construction
    # ------------------------------------------------------------------

    def _config_from_row(self, row: Dict[str, Any]) -> SamsungTVConfig:
        """Map a DB row dict to a SamsungTVConfig."""
        return SamsungTVConfig(
            tv_ip       = row["tv_ip"],
            mac_address = (row.get("mac_address") or "").replace(":", "").upper(),
            token       = row.get("token") or "",
            use_ssl     = bool(row.get("use_ssl", True)),
            ws_port     = row.get("port"),
            name        = row.get("samsung_name") or "samsung_tv",
        )

    def _build_token_saver(self, instance_id: int) -> Callable[[str], Awaitable[None]]:
        """
        Return an async callback the client invokes when the TV issues a
        new auth token. The callback writes the token to this row's
        `token` column so it survives container restarts.
        """
        async def save(new_token: str) -> None:
            ok = await asyncio.to_thread(
                self._patch_row, instance_id, {"token": new_token}
            )
            if ok:
                logger.info(
                    "samsung_tv_registry: persisted new token for instance %s",
                    instance_id,
                )
            else:
                logger.warning(
                    "samsung_tv_registry: failed to persist token for "
                    "instance %s — TV will re-prompt on next pair",
                    instance_id,
                )
        return save

    async def _spawn_client(self, row: Dict[str, Any]) -> Optional[SamsungTVClient]:
        """
        Build + start a client for one row. Returns the client on success,
        None if the row is disabled / paused / unrunnable.

        Safe to call under _lock — does not acquire it itself.
        """
        instance_id = row["id"]
        if not row.get("is_enabled", True):
            logger.info(
                "samsung_tv_registry: instance %s (%s) disabled — skip",
                instance_id, row.get("label"),
            )
            return None
        if row.get("is_paused"):
            logger.info(
                "samsung_tv_registry: instance %s (%s) paused — skip",
                instance_id, row.get("label"),
            )
            return None

        cfg = self._config_from_row(row)
        client = SamsungTVClient(
            cfg,
            on_token_save = self._build_token_saver(instance_id),
        )
        try:
            await client.start()
        except Exception as e:
            logger.error(
                "samsung_tv_registry: failed to start client for "
                "instance %s (%s): %s",
                instance_id, row.get("label"), e,
            )
            return None

        logger.info(
            "samsung_tv_registry: instance %s (%s) → %s (%s) started",
            instance_id, row.get("label"), cfg.name, cfg.tv_ip,
        )
        return client

    # ------------------------------------------------------------------
    # Public lifecycle API
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # One-shot env → DB importer (migration helper for first boot post-009)
    # ------------------------------------------------------------------

    def _import_env_to_db_if_empty(self) -> Optional[Dict[str, Any]]:
        """
        First-boot bootstrap. If no rows exist in samsung_tv_instances yet
        AND `SAMSUNG_TV_IP` is set in the environment, create one row from
        env vars + on-disk state files (the legacy single-tenant config
        location). Returns the new row if created, None otherwise.

        Idempotent: a second call with a non-empty table is a no-op. Safe
        to call on every boot — only the very first one with rows.empty
        and SAMSUNG_TV_IP set will write.

        Sync — call via asyncio.to_thread.
        """
        existing = self._fetch_rows()
        if existing:
            return None

        tv_ip = os.environ.get("SAMSUNG_TV_IP", "").strip()
        if not tv_ip:
            return None

        # Pull MAC + token + name from env. The on-disk token file is the
        # legacy persistence location — read it as a fallback if the env
        # token is empty, so we don't lose a paired token on migration.
        mac = os.environ.get("SAMSUNG_TV_MAC", "").strip()
        env_token = os.environ.get("SAMSUNG_TV_TOKEN", "").strip()
        token = env_token or self._read_legacy_token_file()
        use_ssl = (os.environ.get("SAMSUNG_TV_SSL", "true").lower() != "false")
        samsung_name = os.environ.get(
            "SAMSUNG_TV_NAME", "mobius_smart_home"
        ).strip() or "mobius_smart_home"
        app_name = os.environ.get(
            "SAMSUNG_TV_APP_NAME", "Smart Home Controller"
        ).strip() or "Smart Home Controller"
        callbacks = self._read_legacy_callbacks_file()

        # Label is operator-friendly; first row reuses samsung_name with a
        # human-readable transform so the dashboard doesn't show the slug.
        label = samsung_name.replace("_", " ").title() or "Samsung TV"

        row = {
            "label":        label,
            "tv_ip":        tv_ip,
            "mac_address":  self._normalize_mac(mac) if mac else None,
            "use_ssl":      use_ssl,
            "samsung_name": samsung_name,
            "app_name":     app_name,
            "token":        token or None,
            "callbacks":    callbacks,
            "is_enabled":   True,
            "is_paused":    False,
        }
        try:
            r = self._session().post(
                f"{POSTGREST_URL}/samsung_tv_instances",
                json=row,
                headers={"Prefer": "return=representation"},
                timeout=5,
            )
            if r.status_code not in (200, 201):
                logger.warning(
                    "samsung_tv_registry: env→DB import POST failed: "
                    "%s %s", r.status_code, r.text[:200],
                )
                return None
            created = r.json()
            if isinstance(created, list):
                created = created[0]
            logger.info(
                "samsung_tv_registry: env→DB import created instance %s "
                "(%s, %s) from environment + legacy state files",
                created.get("id"), created.get("label"), created.get("tv_ip"),
            )
            return created
        except Exception as e:
            logger.error("samsung_tv_registry: env→DB import error: %s", e)
            return None

    @staticmethod
    def _normalize_mac(mac: str) -> str:
        """Canonicalize a MAC string: uppercase, no separators."""
        return mac.replace(":", "").replace("-", "").upper()

    @staticmethod
    def _read_legacy_token_file() -> str:
        """Read /app/state/samsung_tv_token.txt if present. Empty string
        if missing — legacy file is created by the old blueprint."""
        path = "/app/state/samsung_tv_token.txt"
        try:
            with open(path, "r") as fh:
                return fh.read().strip()
        except FileNotFoundError:
            return ""
        except Exception as e:
            logger.debug("legacy token file read error (ignored): %s", e)
            return ""

    @staticmethod
    def _read_legacy_callbacks_file() -> Dict[str, Any]:
        """Read /app/state/samsung_tv_callbacks.json if present. Empty
        dict if missing — same shape as the new JSONB column."""
        import json
        path = "/app/state/samsung_tv_callbacks.json"
        try:
            with open(path, "r") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    return data
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug("legacy callbacks file read error (ignored): %s", e)
        return {}

    async def start_all(self) -> None:
        """
        Lifespan startup hook — load enabled rows, spawn clients.

        On the very first boot after migration 009 (rows.empty AND
        SAMSUNG_TV_IP set), this also runs the one-shot env→DB importer
        so the previously-env-driven single TV becomes row id 1.
        """
        async with self._lock:
            # First-boot bootstrap (no-op if rows already exist).
            imported = await asyncio.to_thread(self._import_env_to_db_if_empty)
            if imported is not None:
                logger.info(
                    "samsung_tv_registry: first-boot env→DB import done — "
                    "new instance id %s", imported.get("id"),
                )

            rows = await asyncio.to_thread(self._fetch_rows)
            for row in rows:
                client = await self._spawn_client(row)
                if client is not None:
                    self._clients[row["id"]] = client
                    self._row_snapshot[row["id"]] = row
            logger.info(
                "samsung_tv_registry: start_all complete — %d active client(s)",
                len(self._clients),
            )

    async def stop_all(self) -> None:
        """Lifespan shutdown hook — stop every client cleanly."""
        async with self._lock:
            for instance_id, client in list(self._clients.items()):
                try:
                    await client.stop()
                except Exception as e:
                    logger.warning(
                        "samsung_tv_registry: client %s stop error: %s",
                        instance_id, e,
                    )
            self._clients.clear()
            self._row_snapshot.clear()
            if self._http is not None:
                self._http.close()
                self._http = None
            logger.info("samsung_tv_registry: stop_all complete")

    async def reload_instance(self, instance_id: int) -> None:
        """
        Re-read the row and respawn the client if anything reconnect-
        relevant changed (IP, MAC, SSL, samsung_name). For metadata-only
        edits (label rename, app_name) the existing client keeps running.
        """
        async with self._lock:
            row = await asyncio.to_thread(self._fetch_row, instance_id)
            if row is None:
                # Row vanished — fall through to removal behavior.
                await self._tear_down_locked(instance_id)
                return

            old = self._clients.get(instance_id)
            old_snap = self._row_snapshot.get(instance_id, {})

            wire_keys = ("tv_ip", "mac_address", "use_ssl",
                         "samsung_name", "is_enabled", "is_paused")
            wire_changed = any(
                row.get(k) != old_snap.get(k) for k in wire_keys
            )

            if old is None or wire_changed:
                if old is not None:
                    try:
                        await old.stop()
                    except Exception as e:
                        logger.warning(
                            "samsung_tv_registry: old client %s "
                            "stop error during reload: %s",
                            instance_id, e,
                        )
                self._clients.pop(instance_id, None)
                client = await self._spawn_client(row)
                if client is not None:
                    self._clients[instance_id] = client

            self._row_snapshot[instance_id] = row

    async def remove_instance(self, instance_id: int) -> None:
        """Tear down the client for a deleted row."""
        async with self._lock:
            await self._tear_down_locked(instance_id)

    async def _tear_down_locked(self, instance_id: int) -> None:
        """Stop + forget the client for instance_id. Caller holds _lock."""
        client = self._clients.pop(instance_id, None)
        self._row_snapshot.pop(instance_id, None)
        if client is None:
            return
        try:
            await client.stop()
        except Exception as e:
            logger.warning(
                "samsung_tv_registry: client %s stop error: %s",
                instance_id, e,
            )

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def get(self, instance_id: int) -> Optional[SamsungTVClient]:
        """
        Return the live client for `instance_id`, or None if the row is
        unknown / disabled / paused. The blueprint short-circuits to
        404 / 409 when this returns None.
        """
        return self._clients.get(instance_id)

    def get_row(self, instance_id: int) -> Optional[Dict[str, Any]]:
        """Return the last-known row data for `instance_id` (no DB roundtrip)."""
        return self._row_snapshot.get(instance_id)

    def list_instances(self) -> List[Dict[str, Any]]:
        """
        Return all known rows (enabled + disabled + paused) as a list of
        dicts with metadata + a `_is_running` flag derived from membership
        in self._clients. Used by the UI to populate the Drivers list.
        """
        out = []
        for instance_id, row in self._row_snapshot.items():
            entry = dict(row)
            entry["_is_running"] = instance_id in self._clients
            out.append(entry)
        return out

    # ------------------------------------------------------------------
    # Callbacks JSONB helpers (per-instance)
    # ------------------------------------------------------------------

    async def read_callbacks(self, instance_id: int) -> Dict[str, Any]:
        """Fetch the current callbacks JSONB for an instance. Empty dict
        if the row is missing or the column is null."""
        row = await asyncio.to_thread(self._fetch_row, instance_id)
        return (row or {}).get("callbacks") or {}

    async def write_callbacks(
        self,
        instance_id: int,
        callbacks: Dict[str, Any],
    ) -> bool:
        """Replace the callbacks JSONB column for an instance."""
        return await asyncio.to_thread(
            self._patch_row, instance_id, {"callbacks": callbacks}
        )


# =============================================================================
# Singleton accessor + lifespan integration
# =============================================================================
#
# The registry itself IS a process-wide singleton — but unlike the old
# get_tv_client() singleton, this one owns multiple per-row clients. The
# singleton-of-multi pattern (vs multi-of-singletons) is what enables the
# refactor.

_registry: Optional[SamsungTVRegistry] = None


def get_samsung_tv_registry() -> SamsungTVRegistry:
    """Return the process-wide registry. Constructs on first call."""
    global _registry
    if _registry is None:
        _registry = SamsungTVRegistry()
    return _registry


async def start_samsung_tv_registry() -> None:
    """Lifespan startup hook — call from FastAPI lifespan()."""
    await get_samsung_tv_registry().start_all()


async def stop_samsung_tv_registry() -> None:
    """Lifespan shutdown hook."""
    global _registry
    if _registry is not None:
        await _registry.stop_all()
        _registry = None
