"""
Device Models

Data models for Hubitat devices.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class DeviceAttribute(BaseModel):
    """A device attribute with its current value."""

    name: str
    current_value: Any
    unit: Optional[str] = None
    data_type: Optional[str] = None


class HubitatDevice(BaseModel):
    """
    Represents a device from Hubitat Maker API.

    This model is used for device selection in the UI and
    for caching device state.
    """

    id: str
    name: str
    label: Optional[str] = None
    type: Optional[str] = None
    capabilities: List[str] = []
    attributes: Dict[str, Any] = {}

    @property
    def display_name(self) -> str:
        """Get the best display name for the device."""
        return self.label or self.name

    def has_capability(self, capability: str) -> bool:
        """Check if device has a specific capability."""
        return capability in self.capabilities

    def get_attribute(self, name: str, default: Any = None) -> Any:
        """Get an attribute value."""
        return self.attributes.get(name, default)

    @property
    def is_motion_sensor(self) -> bool:
        """Check if this is a motion sensor."""
        return self.has_capability('motionSensor')

    @property
    def is_switch(self) -> bool:
        """Check if this is a switch."""
        return self.has_capability('switch')

    @property
    def is_dimmer(self) -> bool:
        """Check if this is a dimmer."""
        return self.has_capability('switchLevel')

    @property
    def is_color_light(self) -> bool:
        """Check if this supports color control."""
        return self.has_capability('colorControl')

    @property
    def is_contact_sensor(self) -> bool:
        """Check if this is a contact sensor."""
        return self.has_capability('contactSensor')

    @property
    def is_illuminance_sensor(self) -> bool:
        """Check if this measures illuminance."""
        return self.has_capability('illuminanceMeasurement')

    @property
    def is_button(self) -> bool:
        """Check if this is a button device."""
        return self.has_capability('pushableButton')


class DevicePickerCategory(BaseModel):
    """
    Category definition for device picker UI.

    Used by app types to define what devices they need.
    """

    key: str  # Internal key (e.g., 'motion_sensors')
    label: str  # Display label (e.g., 'Motion Sensors')
    capability: str  # Required capability (e.g., 'motionSensor')
    multiple: bool = True  # Allow multiple devices
    required: bool = False  # Is selection required
    description: Optional[str] = None  # Help text
