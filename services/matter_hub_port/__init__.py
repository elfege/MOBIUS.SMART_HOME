"""
matter_hub_port — Matter hub->hub COPY ("port all devices from one hub to
another"). Implements docs/plans/matter_port_copy_all_devices_from_one_hub_to_
another_hub_multi_admin_ecm_fanout_sequential_orchestrator_name_parity_no_
transfer_semantics.md (design: Assistant-2; implementation: assistant,
operator-assigned MSG-1002, 2026-07-13).

COPY semantics ONLY: the source hub keeps its fabric; this package contains
ZERO fabric-removal code paths by construction (never RemoveFabric, never
decommission).

Modules (one responsibility each):
    db.py            — Postgres connection helper (mirrors matter_pairing_lock)
    hub_endpoints.py — the S1 hub HTTP primitives (open window / pair / status
                       / matterDetails scan), all synchronous `requests`
    eligibility.py   — preview builder: per-device verdicts + hardware gates
    audit.py         — dshub.matter_hub_ports writer (migration 015)
    orchestrator.py  — the strictly-sequential background worker + run state
    router.py        — FastAPI APIRouter (/api/matter/port-devices*)

Wiring (Architect's lane, one line in app.py):
    from services.matter_hub_port.router import router as matter_hub_port_router
    app.include_router(matter_hub_port_router)

This __init__ deliberately imports NOTHING (no router re-export): the wiring
line above imports the router module directly, and keeping the package root
empty makes circular imports structurally impossible.
"""
