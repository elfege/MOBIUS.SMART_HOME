"""
tiles_api — the absorbed TILES/panel surface (MOBIUS.HOME).

Scope of this package (FANATIC MODULARIZATION, operator ruling 2026-07-12):
tiles/panel-EXCLUSIVE glue lives here and nowhere else. Anything shared with the
rest of MOBIUS.HOME (device control, event ingestion, Matter, hubs) stays in the
shared `services/` layer and is IMPORTED — never forked, never duplicated. One
responsibility per module; no god-files (TILES' 4015-LOC UIManager is the
cautionary tale this whole decommission exists to correct).

Modules:
    auth.py    — authentication/authorization (default-deny; enrolled device
                 tokens + scopes + LAN as a second factor).
    db.py      — panel_devices / panel_preferences persistence (server-side only).
    models.py  — pydantic request/response models.
    routes.py  — the FastAPI router (enrollment admin + panel API).

Governing principle (adopted from the TILES retrospective):
    "A dashboard is a VIEW, not an APPLICATION."
"""
