"""
Global Matter-pairing mutex — SHARED by every feature that opens a pairing window.

THE INVARIANT (operator, reinforced 2026-07-13 / intercom MSG-919):
    A Hubitat hub processes exactly ONE Matter device at a time — both as the
    SOURCE (holding an open pairing window) and as the TARGET (consuming a code).

Three things can open a pairing window today:
    1. Commission All            — app.py `_bulk_commission_worker`   (Architect)
    2. Matter hub->hub COPY      — services/matter_hub_port.py        (Assistant-2)
    3. The operator, by hand, from a hub's own UI.

If any two overlap they contend for the same single pairing slot: devices fail to
pair, or pair into the wrong fabric. A per-feature in-memory "am I running?" flag
CANNOT see the other feature, so the guard must be GLOBAL, SHARED, and PERSISTENT:

  * SHARED   — one primitive both lanes import, so neither forks its own guard
               (fanatic-modularization ruling: shared logic lives in services/).
  * PERSISTENT (a DB row, not process memory) — an in-memory flag silently
               "unlocks" when the app restarts, while the hub's pairing window is
               still physically open. Data-oriented per operator directive:
               "everything registered in tables".
  * EXPIRING — a holder that dies mid-run must not wedge Matter pairing forever,
               so every lock carries an explicit expires_at and an expired lock
               may be taken over (and the takeover is recorded).

Usage (async):

    from services.matter_pairing_lock import matter_pairing_lock, PairingLockBusy

    try:
        async with matter_pairing_lock("commission_all", "17 devices on home_1", ttl_s=1800):
            ...open windows, commission, one device at a time...
    except PairingLockBusy as e:
        raise HTTPException(status_code=409, detail=str(e))

Table: dscore.tbl_matter_pairing_lock (migration 013), single row, id=1.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# A pairing window is short-lived (Hubitat's is ~2-5 min), but a BULK run holds the
# mutex across many devices. Default generous; callers should pass a real estimate.
DEFAULT_TTL_S = 1800  # 30 min


class PairingLockBusy(RuntimeError):
    """Raised when the global Matter-pairing mutex is held by someone else."""


def _conn():
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get('POSTGRES_HOST', 'postgres'),
        port=os.environ.get('POSTGRES_PORT', '5432'),
        dbname=os.environ.get('POSTGRES_DB', 'smarthome'),
        user=os.environ.get('POSTGRES_USER', 'smarthome_api'),
        password=os.environ.get('POSTGRES_PASSWORD', ''),
    )


def _try_acquire_sync(holder: str, detail: str, ttl_s: int) -> Dict[str, Any]:
    """
    Atomically take the lock if it is free OR expired. Returns
    {"acquired": bool, "holder": ..., "expires_at": ..., "took_over": bool}.

    The whole decision happens inside ONE UPDATE with a WHERE clause, so two
    callers racing cannot both win (the loser's WHERE simply matches no row) —
    a read-then-write would be a classic TOCTOU race.
    """
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE dscore.tbl_matter_pairing_lock
                      SET is_held = true,
                          taken_over_from = CASE
                              WHEN is_held AND expires_at <= NOW() THEN holder
                              ELSE NULL END,
                          holder = %s,
                          holder_detail = %s,
                          acquired_at = NOW(),
                          expires_at = NOW() + (%s || ' seconds')::interval,
                          released_at = NULL,
                          updated_at = NOW()
                    WHERE id = 1
                      AND (is_held = false OR expires_at <= NOW())
                RETURNING holder, expires_at, taken_over_from""",
                (holder, detail, str(int(ttl_s))))
            row = cur.fetchone()
            if row:
                return {"acquired": True, "holder": row[0],
                        "expires_at": row[1].isoformat() if row[1] else None,
                        "took_over": row[2] is not None, "took_over_from": row[2]}
            # Lost the race (or someone healthy holds it) — report WHO, so the
            # operator gets an actionable 409 instead of a bare "busy".
            cur.execute(
                """SELECT holder, holder_detail, expires_at
                     FROM dscore.tbl_matter_pairing_lock WHERE id = 1""")
            cur_row = cur.fetchone()
            return {"acquired": False,
                    "holder": cur_row[0] if cur_row else None,
                    "holder_detail": cur_row[1] if cur_row else None,
                    "expires_at": cur_row[2].isoformat() if cur_row and cur_row[2] else None}
    finally:
        conn.close()


def _release_sync(holder: str) -> bool:
    """Release, but ONLY if we still hold it — never stomp a holder that took
    over after our lock expired."""
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE dscore.tbl_matter_pairing_lock
                      SET is_held = false, released_at = NOW(), updated_at = NOW()
                    WHERE id = 1 AND is_held = true AND holder = %s
                RETURNING id""", (holder,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def _status_sync() -> Dict[str, Any]:
    conn = _conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT is_held, holder, holder_detail, acquired_at, expires_at,
                          (is_held AND expires_at <= NOW()) AS is_stale
                     FROM dscore.tbl_matter_pairing_lock WHERE id = 1""")
            r = cur.fetchone()
            if not r:
                return {"is_held": False}
            return {"is_held": r[0], "holder": r[1], "holder_detail": r[2],
                    "acquired_at": r[3].isoformat() if r[3] else None,
                    "expires_at": r[4].isoformat() if r[4] else None,
                    "is_stale": r[5]}
    finally:
        conn.close()


async def status() -> Dict[str, Any]:
    """Who holds the Matter-pairing mutex right now (for the UI / a 409 body)."""
    try:
        return await asyncio.to_thread(_status_sync)
    except Exception as e:  # noqa: BLE001 — status must never break a caller
        logger.debug(f"pairing-lock status failed: {e}")
        return {"is_held": False, "error": str(e)}


@asynccontextmanager
async def matter_pairing_lock(holder: str, detail: str = "", ttl_s: int = DEFAULT_TTL_S):
    """
    Hold the GLOBAL Matter-pairing mutex for the duration of the block.

    Raises PairingLockBusy (map it to HTTP 409) if another feature — or the
    operator pairing by hand — already holds it. ALWAYS releases, including on
    exception, so a failed run cannot wedge Matter pairing.

    `ttl_s` is the stale-lock boundary, NOT a timeout: pick something longer than
    the worst-case run (a 20-device bulk commission at ~30s each needs > 10 min).
    """
    got = await asyncio.to_thread(_try_acquire_sync, holder, detail, ttl_s)
    if not got.get("acquired"):
        who = got.get("holder") or "another process"
        what = got.get("holder_detail") or ""
        raise PairingLockBusy(
            f"Matter pairing is already in progress ({who}"
            f"{': ' + what if what else ''}). A Hubitat can pair only ONE device at a "
            f"time, so this must wait. Expires at {got.get('expires_at')}.")
    if got.get("took_over"):
        logger.warning(
            f"matter-pairing lock: '{holder}' TOOK OVER an expired lock previously held "
            f"by '{got.get('took_over_from')}' (the prior holder died mid-run)")
    logger.info(f"matter-pairing lock ACQUIRED by '{holder}' ({detail})")
    try:
        yield
    finally:
        released = await asyncio.to_thread(_release_sync, holder)
        if released:
            logger.info(f"matter-pairing lock released by '{holder}'")
        else:
            # We no longer held it — our lock had expired and someone took over.
            logger.warning(
                f"matter-pairing lock: '{holder}' finished but no longer held the lock "
                f"(it expired and was taken over). Consider a longer ttl_s.")
