"""
Device category definitions for Advanced Motion Lighting.

Returned by get_device_categories() — drives the instance creation wizard
to show the right device pickers with the right Hubitat capability filters.
"""

from typing import List, Dict, Any


def get_device_categories() -> List[Dict[str, Any]]:
    """Return the list of device category definitions for the AML wizard."""
    return [
        {
            "key": "motion_sensors",
            "label": "Motion Sensors",
            "capability": "motionSensor",
            "multiple": True,
            "required": True,
            "description": "Select motion sensors that trigger lighting"
        },
        {
            "key": "switches",
            "label": "Switches to Control",
            "capability": "switch",
            "multiple": True,
            "required": True,
            "description": "Select switches and dimmers to control"
        },
        {
            "key": "illuminance_sensor",
            "label": "Illuminance Sensor",
            "capability": "illuminanceMeasurement",
            "multiple": False,
            "required": False,
            "description": "Optional: Prevent lights if already bright"
        },
        {
            "key": "pause_buttons",
            "label": "Pause/Resume Buttons",
            "capability": "pushableButton",
            "multiple": True,
            "required": False,
            "description": "Optional: Buttons to pause automation"
        },
        {
            "key": "contacts",
            "label": "Contact Sensors",
            "capability": "contactSensor",
            "multiple": True,
            "required": False,
            "description": "Optional: Turn on lights when door opens"
        },
        {
            "key": "pause_switches",
            "label": "Switches to Control on Pause/Resume",
            "capability": "switch",
            "multiple": True,
            "required": False,
            "description": (
                "Optional: Switches to turn on/off when pausing or resuming "
                "(can overlap with motion switches)"
            )
        },
        # keep_off_switches and keep_on_switches are declared here so the
        # backend / device subscription layer knows they exist, but the UI
        # treats them differently: hidden_in_devices_section=True keeps them
        # OUT of Step 2 (Devices) and the instance-controller renders them
        # inline inside the "Always-Off" / "Always-On" cards of Step 3
        # (Configure), conditional on the matching enable-toggle.
        # Operator directive 2026-06-17 (Q2+Q3 reiteration): keep the
        # toggle + mode picker + device picker for each feature in the
        # SAME card. The hidden_in_devices_section flag is the contract
        # the frontend reads to know not to draw the picker in Step 2.
        {
            "key": "keep_off_switches",
            "label": "Always Off",
            "capability": "switch",
            "multiple": True,
            "required": False,
            "description": "These switches will always be turned off",
            "hidden_in_devices_section": True,
            "rendered_with_settings_group": "keep_off"
        },
        {
            "key": "keep_on_switches",
            "label": "Always On",
            "capability": "switch",
            "multiple": True,
            "required": False,
            "description": "These switches will always be turned on",
            "hidden_in_devices_section": True,
            "rendered_with_settings_group": "keep_on"
        }
    ]
