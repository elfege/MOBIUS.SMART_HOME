"""
Settings JSON Schema for Advanced Motion Lighting.

Returned by get_settings_schema() — used by the UI to build the settings
form and by the API to validate incoming settings payloads.
"""

from typing import Dict, Any


def get_settings_schema() -> Dict[str, Any]:
    """Return the complete JSON Schema for AML instance settings."""
    return {
        "type": "object",
        "properties": {
            "memoize": {
                "type": "boolean",
                "title": "Memoization",
                "description": "Remember manual switch changes to avoid conflicts",
                "default": False
            },
            "useDim": {
                "type": "boolean",
                "title": "Enable Dimming",
                "description": "Set dim level when turning on lights",
                "default": False
            },
            "defaultDimLevel": {
                "type": "integer",
                "title": "Default Dim Level",
                "description": "Brightness level (0-100)",
                "minimum": 0,
                "maximum": 100,
                "default": 50
            },
            "useColor": {
                "type": "boolean",
                "title": "Enable Color Control",
                "description": "Set color/temperature when turning on lights",
                "default": False
            },
            "colorPreset": {
                "type": "string",
                "title": "Color Preset",
                "enum": [
                    "Soft White", "Warm White", "Cool White", "Daylight",
                    "Red", "Green", "Blue", "Yellow", "Purple", "Pink",
                    "Custom"
                ],
                "default": "Warm White"
            },
            "customColorTemperature": {
                "type": "integer",
                "title": "Custom Color Temperature",
                "description": "Color temperature in Kelvin",
                "minimum": 2000,
                "maximum": 6500,
                "default": 2700
            },
            "timeUnit": {
                "type": "string",
                "title": "Time Unit",
                "enum": ["seconds", "minutes"],
                "default": "minutes"
            },
            "noMotionTime": {
                "type": "integer",
                "title": "No Motion Timeout",
                "description": "Time to wait before turning off lights",
                "minimum": 1,
                "default": 5
            },
            "timeWithMode": {
                "type": "boolean",
                "title": "Use Different Timeouts per Mode",
                "description": "Set different motion timeouts for each Hubitat mode",
                "default": False
            },
            "modeTimeouts": {
                "type": "object",
                "title": "Per-Mode Timeouts",
                "description": "Timeout value for each mode (same unit as default). Empty = use default.",
                "additionalProperties": {"type": "integer", "minimum": 1},
                "default": {}
            },
            "useIlluminance": {
                "type": "boolean",
                "title": "Enable Illuminance Threshold",
                "description": "Don't turn on lights if already bright",
                "default": False
            },
            "illuminanceThreshold": {
                "type": "integer",
                "title": "Illuminance Threshold (lux)",
                "description": "Don't turn on if illuminance is above this value",
                "minimum": 0,
                "default": 50
            },
            "considerActiveWhenFail": {
                "type": "boolean",
                "title": "Treat Sensor Failure as Active",
                "description": "If all sensors fail, assume motion active (conservative)",
                "default": False
            },
            "buttonEventType": {
                "type": "string",
                "title": "Button Event Type",
                "description": "Which button action triggers pause/resume",
                "enum": ["held", "pushed", "doubleTapped"],
                "default": "held"
            },
            "pauseDuration": {
                "type": "integer",
                "title": "Pause Duration",
                "description": "How long to pause when button pressed",
                "minimum": 1,
                "default": 60
            },
            "pauseDurationUnit": {
                "type": "string",
                "title": "Pause Duration Unit",
                "enum": ["Minutes", "Hours"],
                "default": "Minutes"
            },
            "pauseSwitchAction": {
                "type": "string",
                "title": "Pause Switch Action",
                "description": "What to do with pause switches when pausing (reverses on resume)",
                "enum": ["toggle", "on", "off"],
                "default": "toggle"
            },
            "keepOffModes": {
                "type": "array",
                "title": "Always-Off: Active Modes",
                "description": "Modes where Always-Off is enforced. Empty = all modes.",
                "items": {"type": "string"},
                "default": []
            },
            "keepOnModes": {
                "type": "array",
                "title": "Always-On: Active Modes",
                "description": "Modes where Always-On is enforced. Empty = all modes.",
                "items": {"type": "string"},
                "default": []
            },
            "exclusionModes": {
                "type": "array",
                "title": "Exclusion Modes",
                "description": "App pauses automatically in these modes, resumes when mode changes out",
                "items": {"type": "string"},
                "default": []
            }
        }
    }
