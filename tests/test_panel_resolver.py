"""
Unit tests for the panel roster resolver (apps/tiles_api/resolver.py).

These prove the DATA-ORIENTED resolution the operator asked for: tile renderer
and section are decided by the panel_* rows, richest control wins, and an
explicit affinity always overrides the auto rules. Pure functions, no DB.
"""

from apps.tiles_api import resolver

# Minimal fixtures mirroring migration 014's seed shape.
SECTIONS = [
    {"id": 1, "slug": "favorites", "name": "Favorites", "icon": "star",
     "sort_order": 10, "is_hidden": False},
    {"id": 2, "slug": "kitchen", "name": "Kitchen", "icon": "kitchen",
     "sort_order": 20, "is_hidden": False},
    {"id": 3, "slug": "office", "name": "Office", "icon": "desk",
     "sort_order": 60, "is_hidden": False},
    {"id": 99, "slug": "other", "name": "Other", "icon": "dots",
     "sort_order": 900, "is_hidden": False},
]
TILE_TYPES = [
    {"capability": "ColorControl", "tile_type": "color", "priority": 10,
     "primary_attribute": "switch", "is_actionable": True, "is_enabled": True},
    {"capability": "SwitchLevel", "tile_type": "dimmer", "priority": 20,
     "primary_attribute": "switch", "is_actionable": True, "is_enabled": True},
    {"capability": "Switch", "tile_type": "switch", "priority": 30,
     "primary_attribute": "switch", "is_actionable": True, "is_enabled": True},
    {"capability": "MotionSensor", "tile_type": "motion", "priority": 60,
     "primary_attribute": "motion", "is_actionable": False, "is_enabled": True},
]
RULES = [
    {"section_slug": "kitchen", "match_kind": "name_keyword", "pattern": "kitchen",
     "priority": 10, "is_enabled": True},
    {"section_slug": "office", "match_kind": "name_keyword", "pattern": "office",
     "priority": 10, "is_enabled": True},
    {"section_slug": "office", "match_kind": "capability", "pattern": "MotionSensor",
     "priority": 200, "is_enabled": True},
]


def test_richest_control_wins():
    """An RGBW bulb (Switch+SwitchLevel+ColorControl) resolves to 'color', not
    'switch' — the lowest-priority row among its capabilities."""
    dev = {"id": 1, "label": "Light Desk", "device_type": "Generic Matter RGBW",
           "capabilities": ["Switch", "SwitchLevel", "ColorControl"],
           "attributes": {"switch": "on", "level": "100"}}
    t = resolver.resolve_tile_type(dev, TILE_TYPES, None)
    assert t["tile_type"] == "color"
    assert t["is_actionable"] is True


def test_capability_case_insensitive():
    """Hubitat ships PascalCase; a lowercase capability must still match."""
    dev = {"id": 2, "label": "Plug", "capabilities": ["switch"], "attributes": {}}
    assert resolver.resolve_tile_type(dev, TILE_TYPES, None)["tile_type"] == "switch"


def test_non_tile_bearing_gets_generic():
    """A bare device with only utility capabilities is display-only, not dropped."""
    dev = {"id": 3, "label": "Thing", "capabilities": ["Configuration"],
           "attributes": {}}
    t = resolver.resolve_tile_type(dev, TILE_TYPES, None)
    assert t["tile_type"] == "generic"
    assert t["is_actionable"] is False


def test_affinity_tile_override_wins():
    """A forced tile_type beats capability resolution but recovers actionability
    from the matching type row."""
    dev = {"id": 4, "label": "Bulb", "capabilities": ["ColorControl", "Switch"],
           "attributes": {}}
    t = resolver.resolve_tile_type(dev, TILE_TYPES, {"tile_type": "switch"})
    assert t["tile_type"] == "switch"
    assert t["is_actionable"] is True  # recovered from the 'switch' row


def test_section_by_name_keyword():
    dev = {"id": 5, "label": "Kitchen Counter", "capabilities": ["Switch"]}
    slug = resolver.resolve_section_slug(dev, RULES, None, {})
    assert slug == "kitchen"


def test_section_capability_fallback():
    """A motion sensor whose name reveals no room lands in 'office' via the
    capability rule, not in 'other'."""
    dev = {"id": 6, "label": "Sensor 12ab", "capabilities": ["MotionSensor"]}
    slug = resolver.resolve_section_slug(dev, RULES, None, {})
    assert slug == "office"


def test_affinity_section_override_wins():
    """An explicit placement beats the auto-sectionizer."""
    dev = {"id": 7, "label": "Kitchen Counter", "capabilities": ["Switch"]}
    id_to_slug = {2: "kitchen", 3: "office"}
    slug = resolver.resolve_section_slug(dev, RULES, {"section_id": 3}, id_to_slug)
    assert slug == "office"  # pinned to office despite 'kitchen' in the name


def test_unmatched_goes_to_other():
    dev = {"id": 8, "label": "Mystery", "capabilities": ["Battery"]}
    assert resolver.resolve_section_slug(dev, RULES, None, {}) == "other"


def test_resolve_panel_hides_and_favorites():
    """End-to-end: hidden device is absent; favorite flag propagates; primary
    value is pulled from the flat attribute map."""
    devices = [
        {"id": 1, "label": "Kitchen Light", "device_type": "dimmer",
         "capabilities": ["Switch", "SwitchLevel"],
         "attributes": {"switch": "on", "level": "80"}},
        {"id": 2, "label": "Secret", "capabilities": ["Switch"], "attributes": {}},
    ]
    affinities = {
        1: {"device_id": 1, "section_id": None, "tile_type": None,
            "custom_label": None, "sort_order": 5, "is_hidden": False,
            "is_favorite": True},
        2: {"device_id": 2, "is_hidden": True},
    }
    out = resolver.resolve_panel(devices, SECTIONS, TILE_TYPES, RULES, affinities)
    ids = [t["id"] for t in out["tiles"]]
    assert 2 not in ids                      # hidden
    assert ids == [1]
    tile = out["tiles"][0]
    assert tile["tile_type"] == "dimmer"     # SwitchLevel beats Switch
    assert tile["section_slug"] == "kitchen"
    assert tile["is_favorite"] is True
    assert tile["primary_value"] == "on"     # from attributes["switch"]
    assert [s["slug"] for s in out["sections"]][0] == "favorites"  # ordered
