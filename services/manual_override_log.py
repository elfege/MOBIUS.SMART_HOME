"""
Manual-override log (cross-app substrate)
=========================================
Records every time a USER overrides an app's intended device state — i.e. a
device reports a value different from what the app's logic last commanded. Two
purposes (operator directive 2026-06-24):

  1. Troubleshooting — see false positives/negatives in the automations with full
     context (what the app wanted, what the user did, the world state at the time).
  2. Training base — a future state-prediction model treats "user intervened" as a
     negative-reward signal; the ``context`` JSONB is the feature vector. The model
     may be general-for-all-apps or per-app (TBD), so this log is app-agnostic.

Writes to ``dsapp.manual_overrides`` (via the ``api.manual_overrides`` PostgREST
view). Best-effort + never raises — logging an override must never break an
automation's control path.
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

POSTGREST_URL = os.environ.get("POSTGREST_URL", "http://postgrest:3001").rstrip("/")
_TABLE = f"{POSTGREST_URL}/manual_overrides"


def record_override(
    *,
    instance_id: int | None,
    app_type: str,
    device_id: int | str | None,
    expected: str,
    actual: str,
    device_label: str | None = None,
    attribute: str | None = None,
    location_mode: str | None = None,
    context: dict | None = None,
) -> bool:
    """Insert one manual-override record. Returns True on success.

    Args:
        instance_id: the app instance whose intent was overridden.
        app_type: e.g. 'fan_automation', 'advanced_motion_lighting'.
        device_id: canonical device id the user changed.
        expected: what the app wanted, as a compact token (e.g. 'off', 'on:25').
        actual: what the user set (e.g. 'on:100').
        device_label: human name, for readability in the table.
        attribute: which attribute changed ('switch' | 'level' | 'speed').
        location_mode: Hubitat mode at override time.
        context: ML feature snapshot (sensor readings, app phase/decision,
            relevant settings, time-of-day, …). Kept generous on purpose.
    """
    row = {
        "instance_id": instance_id,
        "app_type": app_type,
        "device_id": int(device_id) if str(device_id).isdigit() else None,
        "device_label": device_label,
        "attribute": attribute,
        "expected": expected,
        "actual": actual,
        "location_mode": location_mode,
        "context": context or {},
    }
    try:
        r = requests.post(_TABLE, json=row, timeout=5,
                          headers={"Prefer": "return=minimal"})
        r.raise_for_status()
        return True
    except Exception as e:
        logger.warning("manual_override_log: failed to record override "
                       "(instance=%s device=%s): %s", instance_id, device_id, e)
        return False
