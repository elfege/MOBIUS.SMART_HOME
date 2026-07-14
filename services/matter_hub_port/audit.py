"""
matter_hub_port.audit — writer for dshub.matter_hub_ports (migration 015).

Every state transition is written AS IT HAPPENS (design §4 "no restart-to-
forget"): a container restart mid-run leaves an honest partial trail. Rows are
append-per-device with in-place status updates; nothing here deletes anything
(P5: audit tables are append-only, no CASCADE anywhere).

All functions are synchronous (psycopg2) — the orchestrator wraps calls in
asyncio.to_thread like every other blocking I/O in this feature.
"""

import logging
from typing import Optional

from services.matter_hub_port.db import connect

logger = logging.getLogger(__name__)


def open_row(run_id: str, source_hub_id: int, target_hub_id: int,
             mac: Optional[str], serial: Optional[str],
             device_name: str) -> Optional[int]:
    """Insert the device's audit row (status='pending'). Returns row id.

    Best-effort: an audit outage must not abort a run — returns None on
    failure (and the orchestrator's in-memory state still has the trail)."""
    try:
        conn = connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO dshub.matter_hub_ports
                           (run_id, source_hub_id, target_hub_id,
                            mac_address, serial_number, device_name, status)
                       VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                       RETURNING id""",
                    (run_id, source_hub_id, target_hub_id, mac, serial, device_name))
                return cur.fetchone()[0]
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — audit must never kill the run
        logger.warning(f"matter_hub_ports insert failed for '{device_name}': {e}")
        return None


def update_row(row_id: Optional[int], status: str, detail: str = "") -> None:
    """Write one state transition. No-op when row_id is None (insert failed)."""
    if row_id is None:
        return
    try:
        conn = connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(
                    """UPDATE dshub.matter_hub_ports
                          SET status = %s, detail = %s, updated_at = now()
                        WHERE id = %s""",
                    (status, detail[:2000], row_id))
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"matter_hub_ports update({row_id} -> {status}) failed: {e}")
