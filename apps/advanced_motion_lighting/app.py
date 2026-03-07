"""
Advanced Motion Lighting Management — main class.

Ported from Hubitat Groovy: Advanced_Motion_Lighting_Management_V2.groovy

Assembles all mixins into the final AdvancedMotionLightingApp class.
Each mixin lives in its own focused module (~100 lines each).

Mixin map:
  AMLMemoizationMixin          memoization.py         — key seeding, reset override, resume
  AMLLifecycleMixin            lifecycle.py            — initialize()
  EventDispatchMixin           event_handlers/dispatch — on_event(), illuminance, contact
  MotionEventMixin             event_handlers/motion   — _handle_motion()
  SwitchOverrideMixin          event_handlers/switch   — _handle_switch() override detection
  ButtonAndPauseMixin          event_handlers/button   — _handle_button(), _control_pause_switches()
  ModeChangeMixin              mode_change.py          — on_mode_change() with exclusion modes
  MainLogicMixin               main_logic.py           — master(), _in_exception_state()
  MotionDetectionMixin         motion_detection.py     — _is_motion_active() three-tier check
  LightControllerMixin         light_control/control   — _control_lights(), memo check
  SwitchCommandsMixin          light_control/switch    — _turn_on_switch(), _turn_off_switch()
  ColorAndDimMixin             light_control/color_dim — _set_color(), dim level, illuminance
  KeepSwitchEnforcementMixin   keep_switches/enforce   — _enforce_keep_switches()
  KeepSwitchHelpersMixin       keep_switches/helpers   — device name/state extraction
  TimeoutMixin                 timeout.py              — _get_timeout_seconds(), _schedule_next_run()
  HealthCheckMixin             health_check.py         — _health_check()

Key behaviors:
  1. Motion active → turn on lights (unless illuminance above threshold)
  2. No motion for timeout → turn off lights
  3. User manually changes keep switches → remember that override (memoization)
  4. Mode change / pause / resume → reset memoization (overrides expire)
"""

from typing import Dict, List, Any

from apps.base.core import BaseApp

from apps.advanced_motion_lighting.memoization import AMLMemoizationMixin
from apps.advanced_motion_lighting.lifecycle import AMLLifecycleMixin
from apps.advanced_motion_lighting.event_handlers.dispatch import EventDispatchMixin
from apps.advanced_motion_lighting.event_handlers.motion import MotionEventMixin
from apps.advanced_motion_lighting.event_handlers.switch_override import SwitchOverrideMixin
from apps.advanced_motion_lighting.event_handlers.button_and_pause import ButtonAndPauseMixin
from apps.advanced_motion_lighting.mode_change import ModeChangeMixin
from apps.advanced_motion_lighting.main_logic import MainLogicMixin
from apps.advanced_motion_lighting.motion_detection import MotionDetectionMixin
from apps.advanced_motion_lighting.light_control.controller import LightControllerMixin
from apps.advanced_motion_lighting.light_control.switch_commands import SwitchCommandsMixin
from apps.advanced_motion_lighting.light_control.color_and_dim import ColorAndDimMixin
from apps.advanced_motion_lighting.keep_switches.enforcement import KeepSwitchEnforcementMixin
from apps.advanced_motion_lighting.keep_switches.helpers import KeepSwitchHelpersMixin
from apps.advanced_motion_lighting.timeout import TimeoutMixin
from apps.advanced_motion_lighting.health_check import HealthCheckMixin
from apps.advanced_motion_lighting.schema.settings import get_settings_schema
from apps.advanced_motion_lighting.schema.devices import get_device_categories
from apps.advanced_motion_lighting.constants import COLOR_PRESETS


class AdvancedMotionLightingApp(
    # AML-specific overrides first (highest MRO priority)
    AMLMemoizationMixin,     # resume() and _reset_memoization() override base versions
    ModeChangeMixin,         # on_mode_change() override with exclusion-mode logic
    # Lifecycle
    AMLLifecycleMixin,       # initialize()
    # Event handling
    EventDispatchMixin,      # on_event(), _handle_illuminance(), _handle_contact()
    MotionEventMixin,        # _handle_motion()
    SwitchOverrideMixin,     # _handle_switch()
    ButtonAndPauseMixin,     # _handle_button(), _control_pause_switches()
    # Core logic
    MainLogicMixin,          # master(), _in_exception_state()
    MotionDetectionMixin,    # _is_motion_active()
    # Light control
    LightControllerMixin,    # _control_lights(), _should_skip_due_to_memo()
    SwitchCommandsMixin,     # _turn_on_switch(), _turn_off_switch()
    ColorAndDimMixin,        # _set_color(), _get_current_dim_level(), _get_current_illuminance()
    # Keep switches
    KeepSwitchEnforcementMixin,  # _enforce_keep_switches()
    KeepSwitchHelpersMixin,      # _extract_switch_state(), _extract_device_name(), _get_current_mode()
    # Utilities
    TimeoutMixin,            # _get_timeout_seconds(), _schedule_next_run()
    HealthCheckMixin,        # _health_check()
    # Base class (lowest priority — provides __init__, abstract interface, and base mixins)
    BaseApp,
):
    """
    Motion-triggered lighting automation with advanced features.

    Settings:
        memoize: Remember user overrides (default: False)
        useDim: Enable dimming (default: False)
        defaultDimLevel: Base dimming level 0-100 (default: 50)
        useColor: Enable color control (default: False)
        colorPreset: Color preset name (default: 'Warm White')
        customColorTemperature: Color temp in Kelvin (default: 2700)
        timeUnit: 'seconds' or 'minutes' (default: 'minutes')
        noMotionTime: Timeout value (default: 5)
        useIlluminance: Enable lux threshold (default: False)
        illuminanceThreshold: Lux threshold (default: 50)
        considerActiveWhenFail: Treat sensor failure as active (default: False)

    Device Categories:
        motion_sensors: Motion sensors that trigger automation
        switches: Switches/dimmers to control
        illuminance_sensor: Optional lux sensor
        pause_buttons: Optional buttons for pause/resume
        contacts: Optional contact sensors
        pause_switches: Switches to control on pause/resume
        keep_off_switches: Always stay off (with manual override support)
        keep_on_switches: Always stay on (with manual override support)
    """

    TYPE_NAME = "advanced_motion_lighting"
    DISPLAY_NAME = "Advanced Motion Lighting"
    DESCRIPTION = "Motion-triggered lighting with dimming, color, and memoization"
    VERSION = "2.0.0"

    # Color presets available for selection in settings
    COLOR_PRESETS = COLOR_PRESETS

    def __init__(self, instance_data: Dict[str, Any], instance_manager):
        super().__init__(instance_data, instance_manager)

        # Track which sensors have emitted events (updated in _handle_motion())
        self._functional_sensors: Dict[str, bool] = {}

        # Seed memoization keys for keep devices
        self._init_memoization_keys()

    @classmethod
    def get_settings_schema(cls) -> Dict[str, Any]:
        """Return JSON Schema for settings validation and UI form generation."""
        return get_settings_schema()

    @classmethod
    def get_device_categories(cls) -> List[Dict[str, Any]]:
        """Return device category definitions for the instance creation wizard."""
        return get_device_categories()
