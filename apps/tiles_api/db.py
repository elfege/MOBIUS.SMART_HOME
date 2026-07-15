"""
Persistence for the panel surface: `dsapp.panel_devices` + `dsapp.panel_preferences`.

SERVER-SIDE ONLY. None of these tables gets an `api.*` view — `panel_devices`
holds credential material (token hashes) and must never be a PostgREST resource,
and everything else is reached through our own authenticated FastAPI routes, not
by a client talking to the database directly.

Tables are created by the VERSIONED migrations (canonical SQL.1), not by app.py:
`panel_devices` / `panel_preferences` in migration 010, and the panel-roster
tables (`panel_sections`, `panel_tile_types`, `panel_section_rules`,
`panel_device_affinities`) in migration 014. app.py's run_db_migrations() is a
documented NO-OP — the schema-as-code DDL was removed 2026-07-13.
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


# --- panel roster resolution inputs (migration 014) -------------------------
# These reads feed resolver.resolve_panel(). Everything the panel groups by is
# DATA here — no capability/room logic lives in the client (operator directive
# 2026-07-13: "everything registered in tables especially affinities").

def list_present_devices() -> List[Dict[str, Any]]:
    """Live device roster from the canonical table — is_present only (a device
    pruned from its hub must not appear on a wall panel), and ONE tile per
    physical device: classifier-flagged same-label mirrors (is_name_duplicate)
    are excluded, same policy as the wizard pickers (2026-07-14 dedup fix).
    attributes is ALREADY a flat JSONB map here (e.g. {"switch":"on"}), so no
    client-side Maker-list flattening is needed."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id, label, name, device_type, protocol,
                          capabilities, attributes
                     FROM dshub.devices
                    WHERE is_present = true
                      AND is_name_duplicate IS NOT TRUE
                    ORDER BY label""")
            return [{"id": r[0], "label": r[1], "name": r[2], "device_type": r[3],
                     "protocol": r[4], "capabilities": r[5], "attributes": r[6]}
                    for r in cur.fetchall()]
    finally:
        conn.close()


def list_sections(profile: str = "default") -> List[Dict[str, Any]]:
    """Panel sections for a profile, ordered."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id, slug, name, icon, sort_order, is_hidden
                     FROM dsapp.panel_sections
                    WHERE profile = %s
                    ORDER BY sort_order, name""", (profile,))
            return [{"id": r[0], "slug": r[1], "name": r[2], "icon": r[3],
                     "sort_order": r[4], "is_hidden": r[5]} for r in cur.fetchall()]
    finally:
        conn.close()


def list_tile_types() -> List[Dict[str, Any]]:
    """Capability -> tile renderer map (lowest priority wins among a device's
    capabilities)."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT capability, tile_type, priority, primary_attribute,
                          is_actionable, is_enabled
                     FROM dsapp.panel_tile_types
                    WHERE is_enabled = true
                    ORDER BY priority""")
            return [{"capability": r[0], "tile_type": r[1], "priority": r[2],
                     "primary_attribute": r[3], "is_actionable": r[4],
                     "is_enabled": r[5]} for r in cur.fetchall()]
    finally:
        conn.close()


def list_section_rules() -> List[Dict[str, Any]]:
    """Auto-sectionizer rules (name_keyword | device_type | capability)."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT section_slug, match_kind, pattern, priority, is_enabled
                     FROM dsapp.panel_section_rules
                    WHERE is_enabled = true
                    ORDER BY priority""")
            return [{"section_slug": r[0], "match_kind": r[1], "pattern": r[2],
                     "priority": r[3], "is_enabled": r[4]} for r in cur.fetchall()]
    finally:
        conn.close()


def affinities_by_device(profile: str = "default") -> Dict[int, Dict[str, Any]]:
    """Explicit per-device placements/overrides, keyed by device_id for O(1)
    lookup during resolution. Absence of a key means 'auto'."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT device_id, section_id, tile_type, custom_label,
                          sort_order, is_hidden, is_favorite
                     FROM dsapp.panel_device_affinities
                    WHERE profile = %s""", (profile,))
            return {r[0]: {"device_id": r[0], "section_id": r[1], "tile_type": r[2],
                           "custom_label": r[3], "sort_order": r[4],
                           "is_hidden": r[5], "is_favorite": r[6]}
                    for r in cur.fetchall()}
    finally:
        conn.close()


def set_affinity(profile: str, device_id: int, *, section_id: Optional[int] = None,
                 tile_type: Optional[str] = None, custom_label: Optional[str] = None,
                 sort_order: Optional[int] = None, is_hidden: Optional[bool] = None,
                 is_favorite: Optional[bool] = None) -> None:
    """Upsert a device's affinity (explicit placement / override). Only the
    fields the caller passes are written; the rest keep their stored value, so a
    'pin to Kitchen' call does not clobber an existing 'is_favorite'."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO dsapp.panel_device_affinities
                     (profile, device_id, section_id, tile_type, custom_label,
                      sort_order, is_hidden, is_favorite, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,
                           COALESCE(%s, false), COALESCE(%s, false), NOW())
                   ON CONFLICT (profile, device_id) DO UPDATE SET
                       section_id   = COALESCE(EXCLUDED.section_id,   dsapp.panel_device_affinities.section_id),
                       tile_type    = COALESCE(EXCLUDED.tile_type,    dsapp.panel_device_affinities.tile_type),
                       custom_label = COALESCE(EXCLUDED.custom_label, dsapp.panel_device_affinities.custom_label),
                       sort_order   = COALESCE(EXCLUDED.sort_order,   dsapp.panel_device_affinities.sort_order),
                       is_hidden    = COALESCE(%s, dsapp.panel_device_affinities.is_hidden),
                       is_favorite  = COALESCE(%s, dsapp.panel_device_affinities.is_favorite),
                       updated_at   = NOW()""",
                (profile, device_id, section_id, tile_type, custom_label,
                 sort_order, is_hidden, is_favorite, is_hidden, is_favorite))
    finally:
        conn.close()


# --- auto-discovery sectionizer (migration 018 origin layer) -----------------

def discovery_inputs(profile: str = "default") -> Dict[str, Any]:
    """Everything the pure sectionizer needs, in one connection: the deduped
    present roster (id+label), existing sections {slug: name}, the derived
    stoplist ingredients (tile-type/capability words from panel_tile_types —
    data-oriented, auto-tracks new capabilities) and hub names (a hub name must
    never become a room)."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id, COALESCE(label, name) FROM dshub.devices
                    WHERE is_present = true AND is_name_duplicate IS NOT TRUE""")
            devices = [{"id": r[0], "label": r[1]} for r in cur.fetchall()]
            cur.execute(
                "SELECT slug, name FROM dsapp.panel_sections WHERE profile = %s",
                (profile,))
            sections = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute(
                "SELECT DISTINCT lower(capability), lower(tile_type) FROM dsapp.panel_tile_types")
            words = set()
            for cap, tt in cur.fetchall():
                words.add(cap)
                words.add(tt)
            # device_type words are THING words too ("WINDOW ESP", "SWITCH
            # ESP", "Generic Matter Outlet") — deriving them here keeps the
            # stoplist data-oriented and stops thing-rooms like WINDOW or
            # OUTLET without a hand-curated blocklist.
            cur.execute(
                "SELECT DISTINCT lower(device_type) FROM dshub.devices WHERE is_present")
            for (dt,) in cur.fetchall():
                if dt:
                    words.update(w for w in dt.replace('-', ' ').split() if w)
            cur.execute("SELECT hub_name FROM dshub.hub_config")
            hubs = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    return {"devices": devices, "sections": sections,
            "tile_type_words": sorted(words), "hub_names": hubs}


def apply_auto_layer(profile: str, rooms: List[Dict[str, Any]],
                     rules: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Commit a discovery proposal: EXPLICITLY replace the origin='auto' layer.

    One transaction:
      1. DELETE prior auto rules (RETURNING, logged — explicit removal, no
         CASCADE anywhere, per the 2026-07-11 policy).
      2. DELETE prior auto sections not reused by the new proposal (ditto).
      3. Upsert proposal sections: new rooms INSERT as origin='auto' (sorted
         after the seeded ones); reused rooms keep their row/origin but get the
         UPPER display name (ratified decision #2: one casing system).
      4. INSERT the new rules as origin='auto'.

    Operator material — origin='operator' rows and ALL panel_device_affinities
    — is never read for deletion here, by construction.
    """
    new_slugs = [r["slug"] for r in rooms]
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM dsapp.panel_section_rules WHERE origin = 'auto' RETURNING section_slug, pattern, priority")
            removed_rules = cur.fetchall()
            cur.execute(
                """DELETE FROM dsapp.panel_sections
                    WHERE origin = 'auto' AND profile = %s
                      AND NOT (slug = ANY(%s))
                RETURNING slug""", (profile, new_slugs))
            removed_sections = [r[0] for r in cur.fetchall()]
            if removed_rules or removed_sections:
                logger.info(
                    "sectionizer apply: removed %d auto rule(s) %s and %d auto "
                    "section(s) %s (explicit replace, no cascade)",
                    len(removed_rules),
                    [f"{r[0]}<-'{r[1]}'" for r in removed_rules],
                    len(removed_sections), removed_sections)
            cur.execute(
                "SELECT COALESCE(MAX(sort_order), 0) FROM dsapp.panel_sections WHERE profile = %s",
                (profile,))
            next_sort = (cur.fetchone()[0] or 0) + 10
            created, reused = [], []
            for room in rooms:
                cur.execute(
                    """INSERT INTO dsapp.panel_sections
                           (profile, slug, name, sort_order, origin)
                    VALUES (%s, %s, %s, %s, 'auto')
                    ON CONFLICT (profile, slug)
                    DO UPDATE SET name = EXCLUDED.name
                    RETURNING (xmax = 0)""",
                    (profile, room["slug"], room["name"], next_sort))
                if cur.fetchone()[0]:
                    created.append(room["slug"])
                    next_sort += 10
                else:
                    reused.append(room["slug"])
            for rule in rules:
                cur.execute(
                    """INSERT INTO dsapp.panel_section_rules
                           (section_slug, match_kind, pattern, priority, origin)
                    VALUES (%s, %s, %s, %s, 'auto')""",
                    (rule["section_slug"], rule["match_kind"],
                     rule["pattern"], rule["priority"]))
        return {"sections_created": created, "sections_reused": reused,
                "sections_removed": removed_sections,
                "rules_written": len(rules), "rules_removed": len(removed_rules)}
    finally:
        conn.close()
