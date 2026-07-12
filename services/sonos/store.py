"""
DB-backed per-speaker Sonos state (values → database, always)
=============================================================
Persists driver/app speaker state in ``dsapp.sonos_speakers`` (exposed via the
``api.sonos_speakers`` PostgREST view). Replaces the earlier in-memory dict.

Fields per speaker (keyed by coordinator ip):
  - saved_volume    : prior volume captured by setVolume; restoreVolume undoes to it.
  - persist_volume  : the volume-LOCK option — re-assert level on external change.
  - persisted_level : the level to hold when locked.

Uses PostgREST upsert (``Prefer: resolution=merge-duplicates``) so a payload only
updates the columns it carries (e.g. writing saved_volume never clobbers the lock).
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

POSTGREST_URL = os.environ.get("POSTGREST_URL", "http://postgrest:3001").rstrip("/")
_TABLE = f"{POSTGREST_URL}/sonos_speakers"
_TIMEOUT = 5


def get_speaker(ip: str) -> dict | None:
    """Return the stored row for ``ip``, or None (missing or on error)."""
    try:
        r = requests.get(_TABLE, params={"ip": f"eq.{ip}", "limit": 1}, timeout=_TIMEOUT)
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None
    except Exception as e:
        logger.warning("sonos.store get_speaker(%s) failed: %s", ip, e)
        return None


def upsert_speaker(ip: str, **fields) -> bool:
    """Insert/merge a speaker row. Only the provided (non-None) fields are
    written; unprovided columns keep their existing values."""
    body = {"ip": ip}
    body.update({k: v for k, v in fields.items() if v is not None})
    try:
        r = requests.post(
            _TABLE, json=body, timeout=_TIMEOUT,
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
        r.raise_for_status()
        return True
    except Exception as e:
        logger.warning("sonos.store upsert_speaker(%s, %s) failed: %s", ip, fields, e)
        return False


def persisted_speakers() -> list[dict]:
    """All speakers with the volume lock enabled (for the enforcement hook)."""
    try:
        r = requests.get(_TABLE, params={"persist_volume": "eq.true"}, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("sonos.store persisted_speakers failed: %s", e)
        return []
