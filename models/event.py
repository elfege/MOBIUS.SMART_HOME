"""
Event Models

Data models for device events received from Hubitat webhooks.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class DeviceEvent:
    """
    Represents a device event from Hubitat.

    Events are generated when device attributes change (e.g., motion sensor
    becomes active, switch turns on, dimmer level changes).

    Attributes:
        device_id: Hubitat device ID
        device_name: Human-readable device name/label
        event_type: Event/attribute name (motion, switch, level, etc.)
        value: New attribute value (active, inactive, on, off, 0-100, etc.)
        unit: Optional unit for numeric values (%, lux, F, etc.)
        description: Human-readable event description
        source: Where the event came from (hubitat_webhook, api, etc.)
        timestamp: When the event was received
        raw_payload: Original webhook payload for debugging

    Example:
        # Motion sensor event
        DeviceEvent(
            device_id='123',
            device_name='Office Motion',
            event_type='motion',
            value='active',
            source='hubitat_webhook'
        )

        # Dimmer level event
        DeviceEvent(
            device_id='456',
            device_name='Living Room Dimmer',
            event_type='level',
            value='75',
            unit='%',
            source='hubitat_webhook'
        )
    """

    device_id: str
    event_type: str
    value: str

    device_name: str = ""
    unit: Optional[str] = None
    description: Optional[str] = None
    source: str = "hubitat_webhook"
    timestamp: datetime = field(default_factory=datetime.now)
    raw_payload: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_motion_active(self) -> bool:
        """Check if this is a motion active event."""
        return self.event_type == 'motion' and self.value == 'active'

    @property
    def is_motion_inactive(self) -> bool:
        """Check if this is a motion inactive event."""
        return self.event_type == 'motion' and self.value == 'inactive'

    @property
    def is_switch_on(self) -> bool:
        """Check if this is a switch on event."""
        return self.event_type == 'switch' and self.value == 'on'

    @property
    def is_switch_off(self) -> bool:
        """Check if this is a switch off event."""
        return self.event_type == 'switch' and self.value == 'off'

    @property
    def is_contact_open(self) -> bool:
        """Check if this is a contact open event."""
        return self.event_type == 'contact' and self.value == 'open'

    @property
    def is_contact_closed(self) -> bool:
        """Check if this is a contact closed event."""
        return self.event_type == 'contact' and self.value == 'closed'

    @property
    def numeric_value(self) -> Optional[float]:
        """
        Get numeric value if applicable.

        Returns:
            Float value or None if not numeric
        """
        try:
            return float(self.value)
        except (ValueError, TypeError):
            return None

    def __str__(self) -> str:
        """String representation for logging."""
        return (
            f"DeviceEvent({self.device_name}[{self.device_id}] "
            f"{self.event_type}={self.value})"
        )


@dataclass
class ModeChangeEvent:
    """
    Represents a location mode change event.

    Mode changes affect all app instances (e.g., Home → Away → Night).

    Attributes:
        mode_id: Mode identifier
        mode_name: Human-readable mode name
        previous_mode: Previous mode name (if known)
        timestamp: When the mode changed
    """

    mode_name: str
    mode_id: Optional[str] = None
    previous_mode: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)

    def __str__(self) -> str:
        """String representation for logging."""
        if self.previous_mode:
            return f"ModeChange({self.previous_mode} → {self.mode_name})"
        return f"ModeChange(→ {self.mode_name})"
