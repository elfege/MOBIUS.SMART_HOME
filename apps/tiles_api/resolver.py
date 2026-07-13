"""
Panel roster resolver — turns the DATA in the panel_* tables into a fully
resolved tile list the client renders verbatim.

WHY THIS MODULE EXISTS (operator directive, 2026-07-13): "beware of data-oriented
logic: everything registered in tables especially affinities." In TILES, two
decisions lived in the frontend as if/else chains:
    - which tile renderer a device gets (over its capabilities);
    - which room/section a device lands in (a hand-written keyword list).
Both are DATA. Here the SERVER reads panel_tile_types / panel_section_rules /
panel_device_affinities and ships an already-resolved roster; the client renders
what it is told. Adding a room or re-mapping a capability is an INSERT, not a
redeploy — and the web panel and the native app can never drift, because neither
carries its own copy of the chain.

These are PURE functions over already-fetched rows (no DB, no HTTP) so the
resolution rules are unit-testable in isolation. The db reads live in db.py; the
route in routes.py fetches, calls resolve_panel(), and returns.
"""

from typing import Any, Dict, List, Optional

# Section every device falls back to when no rule and no affinity places it.
FALLBACK_SECTION_SLUG = "other"
# Tile a device gets when NONE of its capabilities is tile-bearing (e.g. a bare
# "Device" carrying only Configuration). Display-only: never offer a control we
# can't drive. Kept rather than dropped so nothing silently vanishes.
GENERIC_TILE = {"tile_type": "generic", "is_actionable": False,
                "primary_attribute": None}


def _norm(s: Optional[str]) -> str:
    """Lowercase + strip for case-insensitive compares. Hubitat capabilities are
    PascalCase; comparing raw would silently miss (capability-case ruling)."""
    return (s or "").strip().lower()


def resolve_tile_type(device: Dict[str, Any],
                      tile_types: List[Dict[str, Any]],
                      affinity: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Decide the tile renderer for one device.

    Order:
      1. An affinity `tile_type` override wins outright (operator forced it).
      2. Otherwise the LOWEST-priority panel_tile_types row among the device's
         capabilities — so the richest control surfaces (color > dimmer > switch).
      3. If no capability is tile-bearing -> GENERIC (display-only).

    Returns {tile_type, is_actionable, primary_attribute}.
    """
    if affinity and affinity.get("tile_type"):
        # Honor the override, but recover is_actionable/primary_attribute from the
        # matching type row when we know it, so a forced 'switch' still commands.
        forced = affinity["tile_type"]
        match = next((t for t in tile_types
                      if _norm(t["tile_type"]) == _norm(forced)), None)
        return {"tile_type": forced,
                "is_actionable": match["is_actionable"] if match else True,
                "primary_attribute": match["primary_attribute"] if match else None}

    caps = {_norm(c) for c in (device.get("capabilities") or [])}
    candidates = [t for t in tile_types
                  if t.get("is_enabled", True) and _norm(t["capability"]) in caps]
    if not candidates:
        return dict(GENERIC_TILE)
    best = min(candidates, key=lambda t: t["priority"])
    return {"tile_type": best["tile_type"],
            "is_actionable": best["is_actionable"],
            "primary_attribute": best["primary_attribute"]}


def resolve_section_slug(device: Dict[str, Any],
                         rules: List[Dict[str, Any]],
                         affinity: Optional[Dict[str, Any]],
                         section_id_to_slug: Dict[int, str]) -> str:
    """
    Decide which section a device belongs to.

    Order:
      1. An affinity `section_id` wins (explicit operator placement).
      2. Otherwise the auto-sectionizer: the first matching rule, ordered by
         priority ASC then pattern length DESC (a longer pattern is more
         specific: "living room" beats "room").
      3. No match -> FALLBACK_SECTION_SLUG ('other').

    match_kind:
      name_keyword -> pattern is a substring of the device label/name;
      device_type  -> pattern equals the device_type;
      capability   -> pattern is one of the device's capabilities.
    All comparisons are case-insensitive.
    """
    if affinity and affinity.get("section_id"):
        slug = section_id_to_slug.get(affinity["section_id"])
        if slug:
            return slug

    label = _norm(device.get("label")) or _norm(device.get("name"))
    dtype = _norm(device.get("device_type"))
    caps = {_norm(c) for c in (device.get("capabilities") or [])}

    ordered = sorted(
        (r for r in rules if r.get("is_enabled", True)),
        key=lambda r: (r["priority"], -len(r["pattern"] or "")),
    )
    for r in ordered:
        pat = _norm(r["pattern"])
        kind = r["match_kind"]
        if kind == "name_keyword" and pat and pat in label:
            return r["section_slug"]
        if kind == "device_type" and pat and pat == dtype:
            return r["section_slug"]
        if kind == "capability" and pat in caps:
            return r["section_slug"]
    return FALLBACK_SECTION_SLUG


def resolve_panel(devices: List[Dict[str, Any]],
                  sections: List[Dict[str, Any]],
                  tile_types: List[Dict[str, Any]],
                  rules: List[Dict[str, Any]],
                  affinities_by_device: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Produce the full resolved panel: ordered sections + a flat list of resolved
    tiles. The client groups tiles by `section_slug` using `sections` order — no
    grouping logic (and therefore no drift) on the client.

    Each tile carries everything the renderer needs:
        id, label (custom_label override honored), device_type, capabilities,
        attributes (already a flat map in dshub.devices), protocol,
        tile_type, is_actionable, primary_attribute, primary_value,
        section_slug, sort_order, is_favorite, is_hidden.
    """
    section_id_to_slug = {s["id"]: s["slug"] for s in sections}
    known_slugs = {s["slug"] for s in sections}

    tiles: List[Dict[str, Any]] = []
    for d in devices:
        aff = affinities_by_device.get(d["id"])
        if aff and aff.get("is_hidden"):
            continue  # operator hid it; keep it out of the roster entirely

        tile = resolve_tile_type(d, tile_types, aff)
        slug = resolve_section_slug(d, rules, aff, section_id_to_slug)
        if slug not in known_slugs:
            slug = FALLBACK_SECTION_SLUG  # a rule referenced an unseeded section

        attrs = d.get("attributes") or {}
        primary_attr = tile["primary_attribute"]
        tiles.append({
            "id": d["id"],
            "label": (aff.get("custom_label") if aff and aff.get("custom_label")
                      else d.get("label") or d.get("name")),
            "device_type": d.get("device_type"),
            "capabilities": d.get("capabilities") or [],
            "attributes": attrs,
            "protocol": d.get("protocol"),
            "tile_type": tile["tile_type"],
            "is_actionable": tile["is_actionable"],
            "primary_attribute": primary_attr,
            "primary_value": attrs.get(primary_attr) if primary_attr else None,
            "section_slug": slug,
            "sort_order": (aff.get("sort_order") if aff and aff.get("sort_order") is not None
                           else 500),
            "is_favorite": bool(aff and aff.get("is_favorite")),
            "is_hidden": False,
        })

    # Deterministic tile order within a section: sort_order, then label.
    tiles.sort(key=lambda t: (t["sort_order"], _norm(t["label"])))

    visible_sections = [s for s in sections if not s.get("is_hidden")]
    visible_sections.sort(key=lambda s: (s["sort_order"], _norm(s["name"])))
    return {
        "sections": [{"slug": s["slug"], "name": s["name"], "icon": s.get("icon"),
                      "sort_order": s["sort_order"]} for s in visible_sections],
        "tiles": tiles,
    }
