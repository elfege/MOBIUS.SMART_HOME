"""
Settings cascade resolver.

Resolution order (first hit wins, evaluated at each consumer call):
    1. app_instances.settings[key]       — IF this key is in the instance's
                                           settings_schema (i.e., UI-exposed)
    2. app_type_settings[app_type, key]  — per-app-type global
    3. system_settings[key]              — cross-cutting platform knob
    4. caller-supplied default           — last resort

STRICT POLICY (enforced by tests/unit/test_settings_cascade_disjoint.py):
A setting key MUST live at exactly one configurable layer per app type. If a
key is in app_type_settings AND also in any instance_settings_schema for the
same app_type_id, the cascade is nondeterministic — global writes won't be
visible to instances that already store the same key.

Caching
-------
Each tier is fetched on first read and held for ``CACHE_TTL_SECS`` seconds.
PATCHes via the public setters invalidate the affected cache slice. The cache
is process-local — assumes a single FastAPI worker. For multi-worker setups,
we'd add a pg LISTEN/NOTIFY trigger.

Type coercion
-------------
Values are stored as TEXT in the DB with a value_type column:
    'int'    → int
    'float'  → float
    'bool'   → bool (case-insensitive 'true'/'false')
    'string' → str
    'json'   → json.loads(...)
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

POSTGREST_URL = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
CACHE_TTL_SECS = float(os.environ.get("SETTINGS_RESOLVER_TTL_SECS", "5"))


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------


def _coerce(value: str, value_type: str) -> Any:
    """Convert a TEXT-stored value to its Python type."""
    if value is None:
        return None
    vt = (value_type or "string").lower()
    if vt == "int":
        return int(value)
    if vt == "float":
        return float(value)
    if vt == "bool":
        return str(value).strip().lower() in ("true", "1", "yes", "on")
    if vt == "json":
        return json.loads(value)
    return str(value)


def _serialize(value: Any, value_type: str) -> str:
    """Convert a Python value back to TEXT for storage."""
    vt = (value_type or "string").lower()
    if vt == "json":
        return json.dumps(value)
    if vt == "bool":
        return "true" if bool(value) else "false"
    return str(value)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class SettingsResolver:
    """Cascade resolver. Process-local cache with TTL + explicit invalidation."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Cache shapes:
        #   _sys_cache: key → (coerced_value, value_type, fetched_at)
        #   _at_cache:  app_type_id → {key: (coerced_value, value_type, fetched_at)}
        self._sys_cache: Dict[str, tuple] = {}
        self._sys_cache_loaded_at: float = 0.0
        self._at_cache: Dict[int, Dict[str, tuple]] = {}
        self._at_cache_loaded_at: Dict[int, float] = {}

    # ---------------- public API ----------------

    def get(
        self,
        key: str,
        *,
        instance_settings: Optional[Dict[str, Any]] = None,
        instance_schema_properties: Optional[Dict[str, Any]] = None,
        app_type_id: Optional[int] = None,
        default: Any = None,
    ) -> Any:
        """
        Resolve a setting via the cascade.

        Args:
            key: The setting key.
            instance_settings: The instance's settings dict (JSONB from
                app_instances.settings). If None, tier 1 is skipped.
            instance_schema_properties: The properties dict from the app's
                settings_schema. Used to determine if the key is "exposed"
                at instance level. If None, we assume any key present in
                instance_settings is exposed (lenient).
            app_type_id: For tier 2 lookup. If None, tier 2 is skipped.
            default: Returned if no tier has the key.

        Returns:
            The first non-None value found in the cascade, coerced to the
            type recorded with the stored value. ``default`` if no hit.
        """
        # Tier 1: instance settings — only if the key is UI-exposed.
        if instance_settings is not None:
            exposed = (
                instance_schema_properties is None
                or key in instance_schema_properties
            )
            if exposed and key in instance_settings:
                v = instance_settings[key]
                if v is not None:
                    return v

        # Tier 2: app_type_settings
        if app_type_id is not None:
            v = self._get_app_type(app_type_id, key)
            if v is not None:
                return v

        # Tier 3: system_settings
        v = self._get_system(key)
        if v is not None:
            return v

        return default

    def get_system(self, key: str, default: Any = None) -> Any:
        """Convenience: tier-3 only (skip cascade)."""
        v = self._get_system(key)
        return v if v is not None else default

    def get_app_type(
        self,
        app_type_id: int,
        key: str,
        default: Any = None,
    ) -> Any:
        """Convenience: tier-2 only (skip cascade)."""
        v = self._get_app_type(app_type_id, key)
        return v if v is not None else default

    def set_system(self, key: str, value: Any) -> bool:
        """PATCH a system setting. Invalidates the system cache slot."""
        row = self._fetch_system_row(key)
        if row is None:
            logger.warning(f"set_system: unknown key {key!r}")
            return False
        try:
            r = requests.patch(
                f"{POSTGREST_URL}/system_settings",
                params={"key": f"eq.{key}"},
                json={"value": _serialize(value, row["value_type"])},
                headers={"Content-Type": "application/json",
                         "Prefer": "return=minimal"},
                timeout=5,
            )
            if r.status_code not in (200, 204):
                logger.warning(
                    f"set_system PATCH non-2xx for {key}: "
                    f"{r.status_code} {r.text[:200]}"
                )
                return False
        except Exception as e:
            logger.error(f"set_system {key}: {e}", exc_info=True)
            return False
        with self._lock:
            self._sys_cache.pop(key, None)
        return True

    def set_app_type(self, app_type_id: int, key: str, value: Any) -> bool:
        """PATCH an app-type setting. Invalidates the per-app cache slot."""
        row = self._fetch_app_type_row(app_type_id, key)
        if row is None:
            logger.warning(
                f"set_app_type: unknown ({app_type_id}, {key!r})"
            )
            return False
        try:
            r = requests.patch(
                f"{POSTGREST_URL}/app_type_settings",
                params={
                    "app_type_id": f"eq.{app_type_id}",
                    "key": f"eq.{key}",
                },
                json={"value": _serialize(value, row["value_type"])},
                headers={"Content-Type": "application/json",
                         "Prefer": "return=minimal"},
                timeout=5,
            )
            if r.status_code not in (200, 204):
                logger.warning(
                    f"set_app_type PATCH non-2xx: "
                    f"{r.status_code} {r.text[:200]}"
                )
                return False
        except Exception as e:
            logger.error(f"set_app_type ({app_type_id},{key}): {e}", exc_info=True)
            return False
        with self._lock:
            self._at_cache.get(app_type_id, {}).pop(key, None)
        return True

    def invalidate_all(self) -> None:
        """Drop all cached values. Use after schema changes or bulk imports."""
        with self._lock:
            self._sys_cache.clear()
            self._sys_cache_loaded_at = 0
            self._at_cache.clear()
            self._at_cache_loaded_at.clear()

    # ---------------- internals ----------------

    def _get_system(self, key: str) -> Any:
        with self._lock:
            tup = self._sys_cache.get(key)
            if tup is not None:
                value, _vt, fetched_at = tup
                if time.monotonic() - fetched_at < CACHE_TTL_SECS:
                    return value
        # cache miss or stale
        row = self._fetch_system_row(key)
        if row is None:
            return None
        coerced = _coerce(row["value"], row["value_type"])
        with self._lock:
            self._sys_cache[key] = (coerced, row["value_type"], time.monotonic())
        return coerced

    def _get_app_type(self, app_type_id: int, key: str) -> Any:
        with self._lock:
            per = self._at_cache.get(app_type_id, {})
            tup = per.get(key)
            if tup is not None:
                value, _vt, fetched_at = tup
                if time.monotonic() - fetched_at < CACHE_TTL_SECS:
                    return value
        row = self._fetch_app_type_row(app_type_id, key)
        if row is None:
            return None
        coerced = _coerce(row["value"], row["value_type"])
        with self._lock:
            self._at_cache.setdefault(app_type_id, {})[key] = (
                coerced, row["value_type"], time.monotonic(),
            )
        return coerced

    def _fetch_system_row(self, key: str) -> Optional[Dict[str, Any]]:
        try:
            r = requests.get(
                f"{POSTGREST_URL}/system_settings",
                params={"key": f"eq.{key}", "select": "key,value,value_type"},
                timeout=5,
            )
            rows = r.json() if r.status_code == 200 else []
            return rows[0] if rows else None
        except Exception as e:
            logger.warning(f"_fetch_system_row({key}) failed: {e}")
            return None

    def _fetch_app_type_row(
        self, app_type_id: int, key: str
    ) -> Optional[Dict[str, Any]]:
        try:
            r = requests.get(
                f"{POSTGREST_URL}/app_type_settings",
                params={
                    "app_type_id": f"eq.{app_type_id}",
                    "key": f"eq.{key}",
                    "select": "key,value,value_type",
                },
                timeout=5,
            )
            rows = r.json() if r.status_code == 200 else []
            return rows[0] if rows else None
        except Exception as e:
            logger.warning(
                f"_fetch_app_type_row({app_type_id},{key}) failed: {e}"
            )
            return None


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


_resolver: Optional[SettingsResolver] = None


def get_resolver() -> SettingsResolver:
    """Process-wide settings resolver."""
    global _resolver
    if _resolver is None:
        _resolver = SettingsResolver()
    return _resolver
