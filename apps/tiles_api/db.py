"""
Persistence for the panel surface: `dsapp.panel_devices` + `dsapp.panel_preferences`.

SERVER-SIDE ONLY. Neither table gets an `api.*` view — `panel_devices` holds
credential material (token hashes) and must never be a PostgREST resource, and
preferences are reached through our own authenticated FastAPI routes, not by a
client talking to the database directly.

Tables are created idempotently by app.py's run_db_migrations() at boot.
"""

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _conn():
    """psycopg2 connection (same env as app.py's run_db_migrations)."""
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get('POSTGRES_HOST', 'postgres'),
        port=os.environ.get('POSTGRES_PORT', '5432'),
        dbname=os.environ.get('POSTGRES_DB', 'smarthome'),
        user=os.environ.get('POSTGRES_USER', 'smarthome_api'),
        password=os.environ.get('POSTGRES_PASSWORD', ''),
    )


# --- panel_devices (enrolled principals) ------------------------------------

def find_active_device_by_token_hash(token_hash: str) -> Optional[Dict[str, Any]]:
    """The hot auth path: resolve a token hash to a NON-REVOKED device row."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id, name, kind, token_hash, scopes, require_lan
                     FROM dsapp.panel_devices
                    WHERE token_hash = %s AND revoked_at IS NULL""",
                (token_hash,))
            r = cur.fetchone()
            if not r:
                return None
            return {"id": r[0], "name": r[1], "kind": r[2], "token_hash": r[3],
                    "scopes": r[4], "require_lan": r[5]}
    finally:
        conn.close()


def create_device(name: str, kind: str, token_hash: str, token_prefix: str,
                  scopes: List[str], require_lan: bool) -> Dict[str, Any]:
    """Enroll a device/service. Stores ONLY the token hash — the raw token is
    returned to the caller once by the route and never persisted here."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO dsapp.panel_devices
                     (name, kind, token_hash, token_prefix, scopes, require_lan)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   RETURNING id, created_at""",
                (name, kind, token_hash, token_prefix, scopes, require_lan))
            r = cur.fetchone()
            return {"id": r[0], "created_at": r[1].isoformat() if r[1] else None}
    finally:
        conn.close()


def list_devices() -> List[Dict[str, Any]]:
    """Enrolled principals for the admin UI — NEVER returns token material
    (only the non-secret prefix)."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id, name, kind, token_prefix, scopes, require_lan,
                          created_at, last_seen_at, last_seen_ip, revoked_at
                     FROM dsapp.panel_devices ORDER BY created_at DESC""")
            return [{"id": r[0], "name": r[1], "kind": r[2], "token_prefix": r[3],
                     "scopes": r[4], "require_lan": r[5],
                     "created_at": r[6].isoformat() if r[6] else None,
                     "last_seen_at": r[7].isoformat() if r[7] else None,
                     "last_seen_ip": r[8],
                     "revoked_at": r[9].isoformat() if r[9] else None,
                     "revoked": r[9] is not None} for r in cur.fetchall()]
    finally:
        conn.close()


def revoke_device(device_id: int) -> bool:
    """Revoke ONE enrolled device — the capability a 'trusted LAN' gate can
    never give you. Idempotent; returns False if the id doesn't exist."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE dsapp.panel_devices SET revoked_at = NOW()
                    WHERE id = %s AND revoked_at IS NULL RETURNING id""",
                (device_id,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def touch_device(device_id: int, ip: Optional[str]) -> None:
    """Liveness/audit: record last-seen. Best-effort — callers ignore failures."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE dsapp.panel_devices
                      SET last_seen_at = NOW(), last_seen_ip = %s
                    WHERE id = %s""", (ip, device_id))
    finally:
        conn.close()


# --- panel_preferences (JSONB by category, profile-keyed) -------------------

def get_preferences(profile: str, category: Optional[str] = None) -> Dict[str, Any]:
    """Preferences for a profile — all categories, or one. Replaces TILES'
    per-user `user_preferences`/`user_settings` (MOBIUS.HOME has no user model;
    wall tablets are profiles, not people)."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            if category:
                cur.execute(
                    """SELECT category, value FROM dsapp.panel_preferences
                        WHERE profile = %s AND category = %s""", (profile, category))
            else:
                cur.execute(
                    """SELECT category, value FROM dsapp.panel_preferences
                        WHERE profile = %s""", (profile,))
            return {r[0]: r[1] for r in cur.fetchall()}
    finally:
        conn.close()


def set_preference(profile: str, category: str, value: Any) -> None:
    """Upsert one preference category (whole-category replace, JSONB)."""
    import json
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO dsapp.panel_preferences (profile, category, value, updated_at)
                   VALUES (%s,%s,%s::jsonb, NOW())
                   ON CONFLICT (profile, category)
                   DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()""",
                (profile, category, json.dumps(value)))
    finally:
        conn.close()
