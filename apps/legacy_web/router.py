"""
The /legacy router — CANONICAL route map for the strangler-fig cutover.

THIS FILE is the authority for the /legacy/<surface> strings (Architect,
2026-07-14, closing MSG-1059's placeholder guesses). The RN home shell's
link-tiles must use exactly these:

    /legacy                  legacy dashboard (dashboard.html — FROZEN, ruling Q4)
    /legacy/instance/new     instance creation wizard
    /legacy/instance/{id}    instance detail / edit
    /legacy/matter           Matter management (biggest surface; RN port = P3a)
    /legacy/sonos            Sonos driver page
    /legacy/admin/settings   system settings (hubs + TVs live here)
    /legacy/hubs             307 -> /legacy/admin/settings (pre-existing dedupe)

    (MSG-1059 guessed /legacy/dashboard, /legacy/hubs, /legacy/samsung-tv:
     the dashboard is /legacy itself; hubs+TVs are inside admin/settings —
     there is no standalone TV page today.)

The OLD top-level paths 301 to their /legacy twins (bookmarks, muscle memory,
and every legacy template's internal navbar keep working unchanged — the
redirect does the translation). GET / is NOT here: it serves the RN bundle
(see app.py). 301 = permanent: these paths' canonical homes are /legacy/*.

Built as a factory because `templates` is constructed in app.py — importing it
here would be circular.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

# surface -> template. Single source for the router AND the burndown list the
# RN port works through (delete a line here when a surface goes native).
LEGACY_SURFACES = {
    "": "dashboard.html",
    "/instance/new": "instance_wizard.html",
    "/matter": "matter.html",
    "/sonos": "sonos.html",
    "/admin/settings": "admin_settings.html",
}


def build_legacy_router(templates) -> APIRouter:
    """Assemble the /legacy router around app.py's Jinja2Templates instance."""
    router = APIRouter(prefix="/legacy", include_in_schema=False)

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    async def legacy_dashboard(request: Request):
        """Legacy dashboard — FROZEN (ruling Q4): no new features land here;
        RN home subsumes it and this page retires at parity + burn-in."""
        return templates.TemplateResponse(request, "dashboard.html")

    @router.get("/instance/new", response_class=HTMLResponse)
    async def legacy_new_instance(request: Request):
        """Instance creation wizard (legacy)."""
        return templates.TemplateResponse(request, "instance_wizard.html")

    @router.get("/matter", response_class=HTMLResponse)
    async def legacy_matter(request: Request):
        """Matter management (legacy) — RN port is P3a, the biggest surface."""
        return templates.TemplateResponse(request, "matter.html")

    @router.get("/hubs")
    async def legacy_hubs(request: Request):
        """Hubs page was deduplicated into Settings long ago; keep that
        behavior under /legacy too."""
        return RedirectResponse(url="/legacy/admin/settings", status_code=307)

    @router.get("/sonos", response_class=HTMLResponse)
    async def legacy_sonos(request: Request):
        """Sonos driver page (legacy)."""
        return templates.TemplateResponse(request, "sonos.html")

    @router.get("/admin/settings", response_class=HTMLResponse)
    async def legacy_admin_settings(request: Request):
        """System settings (legacy) — includes hub + TV configuration."""
        return templates.TemplateResponse(request, "admin_settings.html")

    @router.get("/instance/{instance_id}", response_class=HTMLResponse)
    async def legacy_instance_detail(request: Request, instance_id: int):
        """Instance detail / edit page (legacy). Kept LAST in the file: the
        path parameter would otherwise shadow /instance/new."""
        return templates.TemplateResponse(
            request, "instance_detail.html", {"instance_id": instance_id}
        )

    return router
