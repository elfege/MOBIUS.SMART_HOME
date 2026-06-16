"""
Universal pause settings shared by every app type.

Project rule (2026-06-16, operator directive): every app's settings
schema MUST include these three fields with identical names, types,
and semantics, so the dashboard's pause button + the framework's
mode-change-resume hook can treat all apps uniformly.

Usage
-----
In each app's ``get_settings_schema()``:

    from apps.base.pause_settings import UNIVERSAL_PAUSE_SETTINGS

    @classmethod
    def get_settings_schema(cls):
        return {
            "type": "object",
            "properties": {
                **UNIVERSAL_PAUSE_SETTINGS,
                # ...app-specific properties
            },
        }

The dashboard's ``togglePause()`` reads ``pauseDuration`` and
``pauseDurationUnit`` from the instance's settings and converts to
the unit the API accepts. The framework's mode-change dispatcher
(``services/webhook_router.route_mode_change``) checks
``resumeOnModeChange`` before invoking ``on_mode_change`` and
auto-resumes the instance if true and currently paused.
"""

UNIVERSAL_PAUSE_SETTINGS = {
    "pauseDuration": {
        "type": "integer",
        "minimum": 0,
        "maximum": 100000,
        "default": 0,
        "title": "Default pause duration",
        "description": (
            "How long the Pause button on the dashboard pauses this "
            "instance for, in the unit chosen below. 0 = INDEFINITE "
            "(no auto-resume; you must hit Resume manually)."
        ),
    },
    "pauseDurationUnit": {
        "type": "string",
        "enum": ["Seconds", "Minutes"],
        "default": "Minutes",
        "title": "Pause duration unit",
        "description": (
            "Unit for the pause duration above. The dashboard converts "
            "to seconds when calling the pause API. Hours and Days are "
            "not exposed — if you need long pauses, use 0 for "
            "indefinite or set a large number of Minutes."
        ),
    },
    "resumeOnModeChange": {
        "type": "boolean",
        "default": False,
        "title": "Resume on mode change",
        "description": (
            "When enabled, ANY Hubitat location-mode change will "
            "auto-resume this instance if it's currently paused. "
            "Useful for instances that should reactivate when the "
            "household transitions (e.g. Day → Evening) regardless "
            "of how the pause was originally set."
        ),
    },
}
