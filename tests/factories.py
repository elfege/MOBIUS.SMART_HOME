"""
Builder helpers that produce realistic objects with sensible defaults.

Every factory returns the same shape PostgREST / Hubitat / the eventsocket
would return for that kind of object, so tests can be terse:

    payload = make_eventsocket_frame(name="motion", value="active")

Override only what your test cares about; everything else stays at a
plausible default. Adding new fields to a factory shouldn't break old tests
as long as the new field is NULL-able / has a sensible default downstream.
"""

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Hubitat eventsocket frames
# ---------------------------------------------------------------------------


def make_eventsocket_frame(
    *,
    source: str = "DEVICE",
    deviceId: str = "100",
    name: str = "motion",
    value: str = "active",
    displayName: str = "Test Motion Sensor",
    descriptionText: Optional[str] = None,
    unit: Optional[str] = None,
    type_: Optional[str] = None,
    data: Optional[Any] = None,
) -> Dict[str, Any]:
    """One frame as Hubitat would emit it on ws://hub/eventsocket."""
    return {
        "source": source,
        "deviceId": deviceId,
        "name": name,
        "value": value,
        "displayName": displayName,
        "descriptionText": descriptionText
            or f"{displayName} {name} is {value}",
        "unit": unit,
        "type": type_,
        "data": data,
    }


# ---------------------------------------------------------------------------
# Webhook-shaped payloads (what eventsocket client hands to route_event,
# also what the legacy webhook dispatcher used to produce).
# ---------------------------------------------------------------------------


def make_router_payload(
    *,
    deviceId: str = "100",
    name: str = "motion",
    value: str = "active",
    displayName: str = "Test Motion Sensor",
    hub_ip: str = "<LAN_IP>",
    intake: str = "eventsocket",
    descriptionText: Optional[str] = None,
    unit: Optional[str] = None,
    received_at_monotonic_ms: Optional[float] = None,
) -> Dict[str, Any]:
    """A payload shaped exactly as WebhookRouter.route_event expects."""
    return {
        "deviceId": deviceId,
        "name": name,
        "value": value,
        "displayName": displayName,
        "descriptionText": descriptionText
            or f"{displayName} {name} is {value}",
        "unit": unit,
        "type": None,
        "data": None,
        "_hub_ip": hub_ip,
        "_intake": intake,
        "_received_at_monotonic_ms": (
            received_at_monotonic_ms
            if received_at_monotonic_ms is not None
            else time.monotonic() * 1000
        ),
    }


# ---------------------------------------------------------------------------
# PostgREST row shapes
# ---------------------------------------------------------------------------


def make_hub_config_row(
    *,
    id: int = 1,
    hub_name: str = "hub4",
    hub_ip: str = "<LAN_IP>",
    maker_api_app_number: str = "268",
    maker_api_token_env: str = "TOKEN_HUB_4",
    is_primary: bool = True,
    is_enabled: bool = True,
) -> Dict[str, Any]:
    return {
        "id": id,
        "hub_name": hub_name,
        "hub_ip": hub_ip,
        "maker_api_app_number": maker_api_app_number,
        "maker_api_token_env": maker_api_token_env,
        "is_primary": is_primary,
        "is_enabled": is_enabled,
    }


def make_canonical_device_row(
    *,
    id: int = 10,
    hub_ip: str = "<LAN_IP>",
    hubitat_id: str = "100",
    label: str = "Test Motion Sensor",
    name: Optional[str] = None,
    device_type: str = "Generic Z-Wave Motion Sensor",
    protocol: str = "zwave",
    capabilities: Optional[List[str]] = None,
    attributes: Optional[Dict[str, Any]] = None,
    hub_id: int = 1,
) -> Dict[str, Any]:
    return {
        "id": id,
        "hub_ip": hub_ip,
        "hubitat_id": hubitat_id,
        "label": label,
        "name": name or label,
        "device_type": device_type,
        "protocol": protocol,
        "capabilities": capabilities or ["MotionSensor"],
        "attributes": attributes or {"motion": "inactive"},
        "hub_id": hub_id,
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
    }


def make_device_subscription_row(
    *,
    id: int = 1,
    device_id: int = 10,
    instance_id: int = 1,
    event_type: str = "motion",
) -> Dict[str, Any]:
    return {
        "id": id,
        "device_id": device_id,
        "instance_id": instance_id,
        "event_type": event_type,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def make_app_instance_row(
    *,
    id: int = 1,
    label: str = "Test Instance",
    app_type_id: int = 1,
    app_type_name: str = "advanced_motion_lighting",
    settings: Optional[Dict[str, Any]] = None,
    device_selections: Optional[Dict[str, Any]] = None,
    is_paused: bool = False,
    memoization_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "id": id,
        "label": label,
        "app_type_id": app_type_id,
        "app_type_name": app_type_name,
        "settings": settings or _default_aml_settings(),
        "device_selections": device_selections or {
            "motion_sensors": [10],
            "switches": [20],
        },
        "is_paused": is_paused,
        "memoization_state": memoization_state or {
            "switch_state": {},
            "dim_level": {},
            "color_state": {},
        },
    }


def _default_aml_settings() -> Dict[str, Any]:
    """Sane defaults for an AdvancedMotionLighting instance."""
    return {
        "noMotionTime": 5,
        "timeUnit": "minutes",
        "useDim": False,
        "useColor": False,
        "useIlluminance": False,
        "illuminanceThreshold": 50,
        "memoize": True,
        "defaultDimLevel": 50,
        "exclusionModes": [],
        "keepOnModes": [],
        "keepOffModes": [],
        "modeTimeouts": {},
        "timeWithMode": False,
        "pauseDuration": 60,
        "pauseDurationUnit": "Minutes",
        "buttonEventType": "held",
        "pauseSwitchAction": "toggle",
        "considerActiveWhenFail": False,
    }


# ---------------------------------------------------------------------------
# event_log + event_routings rows (what PostgREST returns post-insert)
# ---------------------------------------------------------------------------


def make_event_log_row(
    *,
    id: int = 1,
    hubitat_device_id: str = "100",
    device_name: str = "Test Motion Sensor",
    event_type: str = "motion",
    event_value: str = "active",
    hub_ip: str = "<LAN_IP>",
    canonical_device_id: Optional[int] = 10,
    intake_path: str = "eventsocket",
    processing_ms: Optional[int] = 5,
    routed_to_instances: Optional[List[int]] = None,
) -> Dict[str, Any]:
    return {
        "id": id,
        "hubitat_device_id": hubitat_device_id,
        "device_name": device_name,
        "event_type": event_type,
        "event_value": event_value,
        "event_unit": None,
        "hub_ip": hub_ip,
        "canonical_device_id": canonical_device_id,
        "intake_path": intake_path,
        "processing_ms": processing_ms,
        "routed_to_instances": routed_to_instances or [],
        "raw_payload": {},
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
