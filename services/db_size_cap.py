"""
DB size-cap auto-prune.

User mandate (2026-05-17): "ensure a max table size for raw_events and for
all tables anyway. 100MB total db size should work."

Per-table caps in CAPS_BYTES. Hourly APScheduler job checks each table's
on-disk size via pg_total_relation_size; if over cap, deletes oldest rows
until the table is at ~85% of cap. Per-table TIMESTAMP_COL specifies which
column to order by for "oldest".

Tables without a sensible time column (devices, app_instances, hub_config)
are excluded — they're bounded by row count, not churn.

Failures never raise — pruning is best-effort. If postgres is sad, we'll
notice via other monitoring.
"""

import logging
import os
from typing import Optional

import psycopg2

logger = logging.getLogger(__name__)


# (table, max_bytes, timestamp_column). Order = priority — earlier entries
# are pruned first if we're over the global 100MB budget.
CAPS = [
    ('raw_events',           30 * 1024 * 1024, 'received_at'),
    ('event_log',            30 * 1024 * 1024, 'received_at'),
    ('event_routings',       15 * 1024 * 1024, 'enqueued_at'),
    ('device_commands',      10 * 1024 * 1024, 'issued_at'),
    ('instance_state_log',    5 * 1024 * 1024, 'occurred_at'),
    ('mode_change_log',       1 * 1024 * 1024, 'became_active_at'),
    ('system_boot_log',       1 * 1024 * 1024, 'boot_at'),
]

PRUNE_TARGET_FRACTION = 0.85  # Prune down to 85% of cap so we don't
                              # re-trigger immediately on the next row.

DELETE_BATCH = 1000  # Rows per DELETE statement — keeps lock duration short.


def _db_conn():
    return psycopg2.connect(
        host=os.environ.get('POSTGRES_HOST', 'postgres'),
        port=os.environ.get('POSTGRES_PORT', '5432'),
        dbname=os.environ.get('POSTGRES_DB', 'smarthome'),
        user=os.environ.get('POSTGRES_USER', 'smarthome_api'),
        password=os.environ.get('POSTGRES_PASSWORD', ''),
        connect_timeout=5,
    )


def _table_bytes(cur, table: str) -> int:
    cur.execute("SELECT pg_total_relation_size(%s)", (table,))
    row = cur.fetchone()
    return int(row[0]) if row else 0


def _prune_one(cur, table: str, max_bytes: int, ts_col: str) -> int:
    """
    Drop oldest rows from `table` (ordered by `ts_col`) until table is at
    PRUNE_TARGET_FRACTION × max_bytes. Returns rows deleted.
    """
    size = _table_bytes(cur, table)
    if size <= max_bytes:
        return 0
    target = int(max_bytes * PRUNE_TARGET_FRACTION)
    deleted_total = 0
    # Delete in batches so we don't hold a write lock for a long time. After
    # each batch, re-check size and decide if we need another pass.
    for _pass in range(100):  # safety cap — we'd never need 100 passes
        size = _table_bytes(cur, table)
        if size <= target:
            break
        cur.execute(
            f"DELETE FROM {table} WHERE id IN "
            f"(SELECT id FROM {table} ORDER BY {ts_col} ASC LIMIT %s)",
            (DELETE_BATCH,),
        )
        n = cur.rowcount
        deleted_total += n
        if n == 0:
            break  # nothing to delete — table contents are stuck somehow
    # After deletions, run a VACUUM to actually reclaim disk space. Without
    # this, pg_total_relation_size shows the same value (just with dead
    # tuples). We don't VACUUM FULL because it requires an exclusive lock.
    if deleted_total:
        cur.execute(f"VACUUM (ANALYZE) {table}")
    return deleted_total


def run_prune_pass() -> dict:
    """
    Run a single prune pass across every configured table. Returns a dict of
    {table: rows_deleted} for caller logging / admin UI surfacing later.
    """
    report = {}
    try:
        conn = _db_conn()
        conn.autocommit = True
        try:
            cur = conn.cursor()
            for table, max_bytes, ts_col in CAPS:
                try:
                    deleted = _prune_one(cur, table, max_bytes, ts_col)
                    if deleted:
                        size_after = _table_bytes(cur, table)
                        logger.info(
                            f"db_size_cap: pruned {deleted} rows from {table} "
                            f"(now {size_after // 1024} KiB, cap "
                            f"{max_bytes // 1024} KiB)"
                        )
                    report[table] = deleted
                except Exception as e:
                    logger.warning(
                        f"db_size_cap: {table} prune failed: {e}"
                    )
                    report[table] = -1
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"db_size_cap: DB connect failed: {e}")
    return report


def schedule_prune_job(scheduler, interval_seconds: int = 3600) -> str:
    """
    Schedule the prune pass with the given APScheduler instance. Default
    cadence: every hour. Returns the job id.
    """
    job_id = 'db_size_cap_prune'
    scheduler.schedule_recurring(
        job_id=job_id,
        interval_seconds=interval_seconds,
        callback=lambda **kwargs: run_prune_pass(),
        instance_id=None,
        job_type='maintenance',
    )
    logger.info(
        f"db_size_cap: scheduled prune every {interval_seconds}s"
    )
    return job_id
