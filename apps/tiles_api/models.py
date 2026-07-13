"""Pydantic models for the panel API (one responsibility per module)."""

from typing import Any, List, Optional

from pydantic import BaseModel, Field

from apps.tiles_api.auth import (KIND_PANEL, SCOPE_PANEL_COMMAND,
                                 SCOPE_PANEL_READ)


class EnrollDeviceRequest(BaseModel):
    """Enroll a wall tablet / phone / service as an authenticated principal.

    The raw token is returned ONCE in the response and never stored — only its
    SHA-256 hash is persisted. Least privilege: a device that only displays
    state should be enrolled with `panel:read` alone.
    """
    name: str = Field(..., min_length=1, max_length=120,
                      description="Human label, e.g. 'kitchen wall tablet'.")
    kind: str = Field(default=KIND_PANEL, description="panel | service")
    scopes: List[str] = Field(
        default_factory=lambda: [SCOPE_PANEL_READ, SCOPE_PANEL_COMMAND],
        description="Least privilege — grant only what this device needs.")
    require_lan: bool = Field(
        default=True,
        description="Require the trusted-subnet SECOND factor (token alone is "
                    "never sufficient for a panel). Only set False for a "
                    "deliberate off-LAN service integration.")


class EnrollDeviceResponse(BaseModel):
    """The ONLY time the raw token is ever shown. It is not recoverable."""
    id: int
    name: str
    kind: str
    scopes: List[str]
    require_lan: bool
    token: str = Field(..., description="RAW TOKEN — shown once. Store it now; "
                                        "we keep only a hash and cannot show it again.")
    created_at: Optional[str] = None


class PreferenceRequest(BaseModel):
    """Whole-category JSONB upsert for a profile (wall tablets are profiles,
    not people — MOBIUS.HOME has no user model)."""
    profile: str = Field(default="default", max_length=60)
    category: str = Field(..., min_length=1, max_length=60)
    value: Any


class DeviceCommandRequest(BaseModel):
    """A device command from a panel. Requires `panel:command` — commanding a
    lock is NOT the same right as reading the roster."""
    command: str = Field(..., min_length=1, max_length=60)
    value: Optional[Any] = None


class AffinityRequest(BaseModel):
    """Explicit per-device placement / override for a panel profile.

    Everything the panel groups by is DATA (operator directive: "everything
    registered in tables especially affinities"). This is how the operator pins
    a device to a section, forces a tile renderer, renames it on the panel, hides
    it, or stars it onto the home grid — without a code change. Only the fields
    present are written; omitted fields keep their stored value.
    """
    profile: str = Field(default="default", max_length=60)
    section_id: Optional[int] = Field(
        default=None, description="Pin to this section (NULL = auto-sectionize).")
    tile_type: Optional[str] = Field(
        default=None, max_length=60,
        description="Force this tile renderer (NULL = resolve from capabilities).")
    custom_label: Optional[str] = Field(
        default=None, max_length=120, description="Panel display name override.")
    sort_order: Optional[int] = Field(
        default=None, description="Position within the section.")
    is_hidden: Optional[bool] = Field(
        default=None, description="Hide from the panel entirely.")
    is_favorite: Optional[bool] = Field(
        default=None, description="Also surface on the home 'Favorites' grid.")
