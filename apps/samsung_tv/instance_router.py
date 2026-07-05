"""
apps/samsung_tv/instance_router.py

Instance-scoped FastAPI router for the multi-TV refactor.

Mounts at ``/samsung-tv/<instance_id>`` (UI) and ``/api/samsung-tv/<instance_id>/*``
(API). The instance_id is the PK of a row in ``dsapp.samsung_tv_instances``;
the registry (`services/samsung_tv_registry.py`) owns the live client for
that row.

Coexists with the legacy single-tenant router in ``blueprint.py`` during
the refactor — see plan §4.7 step 6 for the cutover. The legacy router
keeps backing the bare ``/samsung-tv`` / ``/api/samsung-tv/*`` paths until
the cut.

Plan: docs/plans/samsung_tv_multi_instance_refactor_per_instance_ip_mac_token_in_database.md
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from services.samsung_tv_registry import get_samsung_tv_registry

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/samsung-tv", tags=["samsung-tv-instance"])


# =============================================================================
# Pydantic models
# =============================================================================


class TVConfigureBody(BaseModel):
    """Body for ``POST /api/samsung-tv/<id>/configure`` — patch live config."""
    tv_ip:        Optional[str]  = None
    mac_address:  Optional[str]  = None
    token:        Optional[str]  = None
    use_ssl:      Optional[bool] = None
    samsung_name: Optional[str]  = None
    app_name:     Optional[str]  = None
    label:        Optional[str]  = None
    port:         Optional[int]  = None


# =============================================================================
# Helpers
# =============================================================================


def _require_client(instance_id: int):
    """Resolve the live SamsungTVClient for instance_id, or raise HTTP 404/409.

    Splits the failure cases:
        404 — no such row at all
        409 — row exists but is disabled / paused (caller should resume first)
    """
    registry = get_samsung_tv_registry()
    client = registry.get(instance_id)
    if client is not None:
        return client

    row = registry.get_row(instance_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"samsung_tv instance {instance_id} not found",
        )
    if not row.get("is_enabled", True):
        raise HTTPException(
            status_code=409,
            detail=f"samsung_tv instance {instance_id} is disabled",
        )
    if row.get("is_paused"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"samsung_tv instance {instance_id} is paused: "
                f"{row.get('pause_reason') or 'no reason recorded'}"
            ),
        )
    # Last-ditch: row is enabled + unpaused but client missing. Likely a
    # mid-restart race. 503 reflects "transient, retry" semantics.
    raise HTTPException(
        status_code=503,
        detail=f"samsung_tv instance {instance_id} client not running yet",
    )


# =============================================================================
# UI route (placeholder until templates/samsung_tv.html is restored)
# =============================================================================


_MANAGE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Samsung TVs</title><style>
 body{font-family:system-ui,sans-serif;background:#0f141b;color:#e6edf3;margin:0;padding:16px}
 h1{font-size:1.2rem;margin:.2rem 0} h3{margin:.2rem 0}
 .row{background:#1a2230;border:1px solid #2b3648;border-radius:10px;padding:12px;margin:10px 0}
 label{display:block;font-size:.72rem;color:#93a2b8;margin:6px 0 2px}
 input,select{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #35415a;background:#0d1219;color:#e6edf3;font-size:16px}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
 button{padding:12px 14px;border:0;border-radius:8px;font-size:15px;font-weight:600;cursor:pointer}
 .save{background:#2563eb;color:#fff}.del{background:#7f1d1d;color:#fff}.add{background:#15803d;color:#fff;width:100%}
 .run{font-size:.7rem;padding:2px 8px;border-radius:99px}.on{background:#14532d;color:#86efac}.off{background:#3f1d1d;color:#fca5a5}
 .bar{display:flex;gap:8px;align-items:center;justify-content:space-between}
 .muted{color:#7c8aa0;font-size:.75rem}
</style></head><body>
<div class="bar"><h1>Samsung TVs</h1><button class="save" onclick="load()">Refresh</button></div>
<div id="list">loading...</div>
<div class="row"><h3>Add TV</h3>
 <label>Label</label><input id="a_label" placeholder="Office TV">
 <div class="grid"><div><label>IP</label><input id="a_ip" placeholder="<LAN_IP>"></div>
 <div><label>MAC</label><input id="a_mac" placeholder="84:A4:66:AD:EE:0B"></div></div>
 <div class="grid"><div><label>Port</label><select id="a_port">
  <option value="">auto (8001/8002)</option><option>8001</option><option>8002</option><option>8000</option><option>8080</option><option>55000</option></select></div>
 <div><label>SSL (WSS)</label><select id="a_ssl"><option value="false">no</option><option value="true">yes</option></select></div></div>
 <br><button class="add" onclick="addTv()">Add TV</button></div>
<p class="muted" id="msg"></p>
<script>
const B='/samsung-tv';
function val(id){return document.getElementById(id).value.trim()}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function msg(m){document.getElementById('msg').textContent=m}
async function load(){const el=document.getElementById('list');el.textContent='loading...';
 try{const r=await fetch(B+'/api/list');const d=await r.json();const xs=d.instances||[];
  el.innerHTML=xs.length?xs.map(card).join(''):'<p class=muted>no TVs</p>';}catch(e){el.textContent='error: '+e}}
function card(t){const run=t._is_running?'<span class="run on">running</span>':'<span class="run off">stopped</span>';
 const p=t.port==null?'':t.port;
 return '<div class=row><div class=bar><b>'+esc(t.label)+'</b>'+run+' <span class=muted>id '+t.id+'</span></div>'
 +'<div class=grid><div><label>IP</label><input id=ip_'+t.id+' value="'+esc(t.tv_ip)+'"></div>'
 +'<div><label>MAC</label><input id=mac_'+t.id+' value="'+esc(t.mac_address)+'"></div></div>'
 +'<div class=grid><div><label>Port</label><input id=port_'+t.id+' value="'+p+'" placeholder=auto></div>'
 +'<div><label>SSL</label><select id=ssl_'+t.id+'><option value=false '+(!t.use_ssl?'selected':'')+'>no</option>'
 +'<option value=true '+(t.use_ssl?'selected':'')+'>yes</option></select></div></div>'
 +'<br><div class=grid><button class=save onclick="save('+t.id+')">Save</button>'
 +'<button class=del onclick="del('+t.id+',\\''+esc(t.label)+'\\')">Remove</button></div></div>';}
async function save(id){const b={tv_ip:val('ip_'+id),mac_address:val('mac_'+id),use_ssl:val('ssl_'+id)==='true',
  port:val('port_'+id)===''?null:parseInt(val('port_'+id),10)};msg('saving...');
 const r=await fetch(B+'/api/'+id+'/configure',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
 msg(r.ok?'saved, reconnecting...':'save failed '+r.status);load();}
async function del(id,label){if(!confirm('Remove '+label+'?'))return;msg('removing...');
 const r=await fetch(B+'/api/'+id,{method:'DELETE'});msg(r.ok?'removed':'delete failed '+r.status);load();}
async function addTv(){const b={label:val('a_label'),tv_ip:val('a_ip'),mac_address:val('a_mac'),
  use_ssl:val('a_ssl')==='true',port:val('a_port')===''?null:parseInt(val('a_port'),10)};
 if(!b.label||!b.tv_ip){msg('label + IP required');return}msg('adding...');
 const r=await fetch(B+'/api/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
 msg(r.ok?'added':'add failed '+r.status);
 if(r.ok){document.getElementById('a_label').value='';document.getElementById('a_ip').value='';document.getElementById('a_mac').value=''}load();}
load();
</script></body></html>"""


@router.get("/manage", response_class=HTMLResponse)
async def manage_page() -> HTMLResponse:
    """Multi-TV management UI: list / add / edit (incl. port) / remove."""
    return HTMLResponse(_MANAGE_HTML)


@router.get("/{instance_id}", response_class=HTMLResponse)
async def samsung_tv_page(request: Request, instance_id: int):
    """
    Render the per-instance Samsung TV control page.

    Currently a placeholder: ``templates/samsung_tv.html`` lives in
    ``stash@{0}`` and has not been restored to the working tree. When the
    template is back this hook switches to a ``templates.TemplateResponse``
    that takes both ``request`` and the instance row. Until then this
    returns a tiny diagnostic page so the route is at least reachable.
    """
    registry = get_samsung_tv_registry()
    row = registry.get_row(instance_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"samsung_tv instance {instance_id} not found",
        )
    client = registry.get(instance_id)
    running = client is not None
    return HTMLResponse(
        f"""<!doctype html>
        <html><head><title>Samsung TV {instance_id}</title></head>
        <body style='font-family:sans-serif;padding:2em;'>
        <h1>Samsung TV — {row.get('label', '(unlabeled)')}</h1>
        <p><strong>Instance id:</strong> {instance_id}</p>
        <p><strong>IP:</strong> {row.get('tv_ip')}</p>
        <p><strong>Running:</strong> {running}</p>
        <p><em>Full UI page is pending restoration of
        <code>templates/samsung_tv.html</code> from stash@{{0}}.</em></p>
        </body></html>"""
    )


# =============================================================================
# API routes — every action takes an explicit instance_id
# =============================================================================


@router.get("/api/{instance_id}/status")
async def api_status(instance_id: int) -> Dict[str, Any]:
    """Live status snapshot for the given TV instance."""
    client = _require_client(instance_id)
    return client.get_status()


@router.post("/api/{instance_id}/on")
async def api_on(instance_id: int) -> Dict[str, Any]:
    """Turn the TV on (Wake-on-LAN + KEY_POWER)."""
    client = _require_client(instance_id)
    return await client.power_on()


@router.post("/api/{instance_id}/off")
async def api_off(instance_id: int) -> Dict[str, Any]:
    """Toggle KEY_POWER. Idempotent for already-off TVs."""
    client = _require_client(instance_id)
    return await client.power_off()


@router.post("/api/{instance_id}/key/{key}")
async def api_key(instance_id: int, key: str) -> Dict[str, Any]:
    """Send an arbitrary Samsung remote key (e.g. KEY_VOLUP, KEY_HDMI)."""
    client = _require_client(instance_id)
    return await client.send_key(key)


@router.post("/api/{instance_id}/configure")
async def api_configure(
    instance_id: int, body: TVConfigureBody,
) -> Dict[str, Any]:
    """
    Patch a TV instance's configuration. Writes the changed columns to
    the DB row, then asks the registry to reload (which respawns the
    client if any wire-relevant field changed: tv_ip, mac, ssl,
    samsung_name).

    Returns the updated row plus a flag indicating whether the client was
    restarted as part of the reload.
    """
    registry = get_samsung_tv_registry()
    row = registry.get_row(instance_id)
    if row is None:
        # If the row exists in DB but we have no snapshot yet, reload
        # forces a fresh fetch. After that, surfacing 404 to the caller
        # is the right move when it's genuinely missing.
        await registry.reload_instance(instance_id)
        row = registry.get_row(instance_id)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"samsung_tv instance {instance_id} not found",
            )

    patch: Dict[str, Any] = {}
    if body.tv_ip is not None:
        patch["tv_ip"] = body.tv_ip
    if body.mac_address is not None:
        patch["mac_address"] = body.mac_address.replace(":", "").upper() or None
    if body.token is not None:
        patch["token"] = body.token
    if body.use_ssl is not None:
        patch["use_ssl"] = body.use_ssl
    if body.samsung_name is not None:
        patch["samsung_name"] = body.samsung_name
    if body.app_name is not None:
        patch["app_name"] = body.app_name
    if body.label is not None:
        patch["label"] = body.label
    if body.port is not None:
        patch["port"] = body.port or None

    if not patch:
        return {"ok": True, "noop": True, "row": row}

    # Run the PATCH via the registry's helper so we don't open a second
    # PostgREST session here.
    import asyncio as _asyncio
    ok = await _asyncio.to_thread(
        registry._patch_row, instance_id, patch  # noqa: SLF001 — intentional
    )
    if not ok:
        raise HTTPException(
            status_code=500,
            detail=f"failed to PATCH samsung_tv instance {instance_id}",
        )

    await registry.reload_instance(instance_id)
    new_row = registry.get_row(instance_id) or {}
    return {"ok": True, "row": new_row}


@router.get("/api/list")
async def api_list() -> Dict[str, Any]:
    """
    Return every known TV instance with metadata + a `_is_running` flag.
    Used by the dashboard Drivers section to populate one card per TV.
    """
    registry = get_samsung_tv_registry()
    return {"instances": registry.list_instances()}


class TVCreateBody(BaseModel):
    """Body for ``POST /samsung-tv/api/create`` — add a new TV instance."""
    label:        str
    tv_ip:        str
    mac_address:  Optional[str] = None
    use_ssl:      bool          = False
    port:         Optional[int] = None
    samsung_name: Optional[str] = None
    app_name:     Optional[str] = "Smart Home Controller"


@router.post("/api/create")
async def api_create(body: TVCreateBody) -> Dict[str, Any]:
    """Insert a new samsung_tv_instances row and start its client."""
    import os
    import asyncio as _asyncio
    import requests as _requests
    pg = os.environ.get("POSTGREST_URL", "http://postgrest:3001")
    row = {
        "label":        body.label,
        "tv_ip":        body.tv_ip,
        "mac_address":  (body.mac_address or "").replace(":", "").upper() or None,
        "use_ssl":      body.use_ssl,
        "port":         body.port or None,
        "samsung_name": body.samsung_name or body.label.lower().replace(" ", "_"),
        "app_name":     body.app_name or "Smart Home Controller",
        "token":        None,
        "callbacks":    {},
        "is_enabled":   True,
        "is_paused":    False,
    }

    def _insert() -> Dict[str, Any]:
        r = _requests.post(
            f"{pg}/samsung_tv_instances", json=row,
            headers={"Prefer": "return=representation"}, timeout=6,
        )
        r.raise_for_status()
        d = r.json()
        return d[0] if isinstance(d, list) else d

    try:
        created = await _asyncio.to_thread(_insert)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"create failed: {e}")
    # Spawn the client for the new row.
    await get_samsung_tv_registry().reload_instance(int(created["id"]))
    return {"ok": True, "row": created}


@router.delete("/api/{instance_id}")
async def api_delete(instance_id: int) -> Dict[str, Any]:
    """Stop the client for a TV instance and delete its DB row."""
    import os
    import asyncio as _asyncio
    import requests as _requests
    await get_samsung_tv_registry().remove_instance(instance_id)
    pg = os.environ.get("POSTGREST_URL", "http://postgrest:3001")

    def _del() -> None:
        r = _requests.delete(
            f"{pg}/samsung_tv_instances?id=eq.{instance_id}", timeout=6,
        )
        r.raise_for_status()

    try:
        await _asyncio.to_thread(_del)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"delete failed: {e}")
    return {"ok": True, "deleted": instance_id}


# =============================================================================
# (No bare-prefix redirect here)
# =============================================================================
#
# `GET /samsung-tv` (no id) intentionally stays bound to the legacy
# blueprint while both routers coexist. FastAPI's trailing-slash redirect
# would collide if this router also declared the bare prefix. The bare-
# prefix-redirect lives instead in plan §4.7 step 6 (cutover), which
# replaces the legacy handler with `RedirectResponse → /samsung-tv/<id>`
# once we're confident no callers depend on the legacy HTML body.
