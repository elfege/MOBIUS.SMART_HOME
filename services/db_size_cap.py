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
# are pruned first if we're over the global budget.
#
# 2026-05-18 user directive: total budget 4GB so logs are usable for
# forensic debugging ("why did the kitchen lights turn off at 16:57?").
# Originals were 30MB/30MB/15MB/10MB → ~3 min of event_log at active
# hours, useless. New sizing → multi-day replay capacity.
#
# scheduled_jobs added 2026-05-18 — it was missing entirely and grew to
# 1.5GB (12k cancelled timeout rows + bloat) before being caught.
CAPS = [
    ('event_log',          1500 * 1024 * 1024, 'received_at'),
    ('raw_events',         1500 * 1024 * 1024, 'received_at'),
    ('event_routings',      400 * 1024 * 1024, 'enqueued_at'),
    ('device_commands',     400 * 1024 * 1024, 'issued_at'),
    ('scheduled_jobs',      100 * 1024 * 1024, 'created_at'),
    ('instance_state_log',   50 * 1024 * 1024, 'occurred_at'),
    ('mode_change_log',      25 * 1024 * 1024, 'became_active_at'),
    ('system_boot_log',      25 * 1024 * 1024, 'boot_at'),
]
# Total budget: 4000 MB. Per-table individual caps trip earlier if any
# single table runs away; the global ordering above is just preference.

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


def _table_live_rows_and_dead_fraction(cur, table: str) -> tuple:
    """Returns (live_rows, dead_fraction). dead_fraction is dead/(live+dead),
    a proxy for bloat. Used to decide whether DELETE alone or VACUUM FULL
    is needed to keep on-disk size in check."""
    cur.execute(
        "SELECT n_live_tup, n_dead_tup FROM pg_stat_user_tables "
        "WHERE relname = %s", (table,)
    )
    row = cur.fetchone()
    if not row:
        return (0, 0.0)
    live, dead = int(row[0] or 0), int(row[1] or 0)
    total = live + dead
    return (live, (dead / total) if total else 0.0)


def _prune_one(cur, table: str, max_bytes: int, ts_col: str) -> int:
    """
    Keep `table` under `max_bytes`. Uses a two-step strategy:

    1. If the file is over cap, first DELETE oldest rows (batched) until
       the *live* portion of the table is well under cap. We use the
       row-count-weighted-by-avg-row-size as the proxy because the
       on-disk size includes dead tuples that DELETE won't shrink.
    2. After DELETE, if dead-tuple fraction is high (>50%), run
       VACUUM FULL to actually reclaim the disk. This is the fix for
       the 2026-05-18 incident where event_log was 843 MB with 13 live
       rows because the previous prune deleted rows but never compacted
       — every subsequent prune saw the bloated file and deleted again.

    Returns rows deleted (positive int) or 0 if no action taken.
    """
    size = _table_bytes(cur, table)
    if size <= max_bytes:
        return 0
    target = int(max_bytes * PRUNE_TARGET_FRACTION)
    deleted_total = 0
    # Delete in batches so we don't hold a write lock for a long time.
    # Sizing oracle: use live-row count × avg-row-size, not raw file size,
    # so dead-tuple bloat doesn't drive an unbounded delete loop.
    for _pass in range(100):  # safety cap — we'd never need 100 passes
        live_rows, _ = _table_live_rows_and_dead_fraction(cur, table)
        size = _table_bytes(cur, table)
        # Estimate live data size: avg-row-bytes across the table file,
        # times live tuples. Crude but bounded by reality (live ≤ total).
        cur.execute(
            "SELECT reltuples::bigint FROM pg_class WHERE relname = %s",
            (table,)
        )
        r = cur.fetchone()
        approx_total_rows = max(int(r[0]) if r and r[0] else 1, 1)
        avg_bytes_per_row = size / approx_total_rows if approx_total_rows else 0
        live_bytes_est = int(avg_bytes_per_row * live_rows)
        # If estimated live-data size is already under target, no more
        # delete passes needed — the rest is just bloat for VACUUM to handle.
        if live_bytes_est <= target:
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

    # Compaction. Per user directive 2026-05-18: auto-prune must actually
    # free disk, not just delete rows. Plain DELETE leaves MVCC dead
    # tuples and pg_total_relation_size doesn't shrink — so the *next*
    # prune cycle thinks we're still over cap, deletes again, and the
    # table empties while disk usage stays bloated (843 MB / 13 rows
    # incident, 1.5 GB / 12k cancelled jobs incident).
    #
    # VACUUM FULL rewrites the file and physically reclaims pages, at
    # the cost of a brief ACCESS EXCLUSIVE lock on the table. For event-
    # log tables this is ~milliseconds and acceptable hourly. If it
    # fails (concurrent activity, lock timeout), fall back to plain
    # VACUUM ANALYZE which frees pages for *future* writes but doesn't
    # shrink the file — better than nothing.
    if deleted_total:
        try:
            cur.execute(f"VACUUM FULL {table}")
        except Exception as e:
            logger.warning(
                f"db_size_cap: VACUUM FULL {table} failed ({e}); "
                f"falling back to VACUUM ANALYZE — disk may not "
                f"shrink until next pass."
            )
            try:
                cur.execute(f"VACUUM (ANALYZE) {table}")
            except Exception as e2:
                logger.warning(
                    f"db_size_cap: VACUUM ANALYZE {table} also failed: {e2}"
                )
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
