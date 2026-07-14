"""
matter_hub_port.db — Postgres connection helper for this package.

Deliberately identical to services/matter_pairing_lock._conn (that one is
module-private, so it is mirrored here rather than imported): direct psycopg2
with the container's standard POSTGRES_* env. Used for the audit table and
hub_config reads — internal tables with no api.* view (migration 014/015
convention), so PostgREST is not in this path.
"""

import os


def connect():
    """A new psycopg2 connection from the container's POSTGRES_* env."""
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get('POSTGRES_HOST', 'postgres'),
        port=os.environ.get('POSTGRES_PORT', '5432'),
        dbname=os.environ.get('POSTGRES_DB', 'smarthome'),
        user=os.environ.get('POSTGRES_USER', 'smarthome_api'),
        password=os.environ.get('POSTGRES_PASSWORD', ''),
    )


def fetch_hub(hub_id: int):
    """One dshub.hub_config row as a dict, or None.

    Returns: {id, hub_name, hub_ip, hardware_version, is_enabled}
    """
    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id, hub_name, hub_ip, hardware_version, is_enabled
                     FROM dshub.hub_config WHERE id = %s""",
                (hub_id,))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row[0], "hub_name": row[1], "hub_ip": row[2],
                "hardware_version": row[3], "is_enabled": row[4],
            }
    finally:
        conn.close()
