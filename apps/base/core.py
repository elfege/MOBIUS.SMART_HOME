"""
BaseApp — Abstract base class for all smart home app types.

Each app type (Advanced Motion Lighting, Thermostat Manager, etc.)
inherits from BaseApp and implements its specific automation logic.

Assembled from focused mixins:
  LifecycleMixin        — shutdown, on_mode_change
  PauseResumeMixin      — pause, resume, auto_resume, is_paused
  SettingsMixin         — get_setting, get_devices
  MemoizationMixin      — get_memo, set_memo, _reset_memoization, _save_memoization
  HubitatMixin          — hubitat client, send_command, get_device_state
  SchedulingMixin       — schedule_timeout, reschedule_timeout, cancel_timeout
  ActivityMixin         — update_last_activity

Usage:
    class MyApp(BaseApp):
        TYPE_NAME = 'my_app'
        DISPLAY_NAME = 'My Smart App'

        def initialize(self):
            # Set up subscriptions, schedules, initial state
            pass

        def on_event(self, event):
            # Handle device events from webhook router
            pass

        def master(self):
            # Main decision logic — what state should devices be in?
            pass
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional, TYPE_CHECKING
import logging

from models.event import DeviceEvent
from models.command import CommandResult
from models.instance import RuntimeInstanceState

from apps.base.lifecycle import LifecycleMixin
from apps.base.pause_resume import PauseResumeMixin
from apps.base.settings import SettingsMixin
from apps.base.memoization import MemoizationMixin
from apps.base.hubitat_bridge import HubitatMixin
from apps.base.scheduling import SchedulingMixin
from apps.base.activity import ActivityMixin

if TYPE_CHECKING:
    from services.instance_manager import InstanceManager


class BaseApp(
    LifecycleMixin,
    PauseResumeMixin,
    SettingsMixin,
    MemoizationMixin,
    HubitatMixin,
    SchedulingMixin,
    ActivityMixin,
    ABC
):
    """
    Abstract base class for smart home apps.

    Class Attributes:
        TYPE_NAME:    Internal type identifier (e.g., 'advanced_motion_lighting')
        DISPLAY_NAME: Human-readable name shown in the UI
        DESCRIPTION:  Short description for the app picker
        VERSION:      App version string
    """

    TYPE_NAME: str = ""
    DISPLAY_NAME: str = ""
    DESCRIPTION: str = ""
    VERSION: str = "1.0.0"

    def __init__(
        self,
        instance_data: Dict[str, Any],
        instance_manager: 'InstanceManager'
    ):
        """
        Initialize from a database row.

        Args:
            instance_data: Row from the app_instances table
            instance_manager: InstanceManager for DB ops and lifecycle callbacks
        """
        self.instance_data = instance_data
        self.instance_manager = instance_manager

        self.instance_id: int = instance_data['id']
        self.label: str = instance_data['label']
        self.settings: Dict[str, Any] = instance_data.get('settings', {})
        self.device_selections: Dict[str, List[str]] = instance_data.get(
            'device_selections', {}
        )

        # Memoization state (persisted to DB between restarts)
        self._memoization: Dict[str, Any] = instance_data.get(
            'memoization_state', {}
        )

        # Runtime state (in-memory only, reset on restart)
        self._runtime = RuntimeInstanceState()

        self.logger = logging.getLogger(
            f"{self.__class__.__name__}.{self.label}"
        )

        self._is_paused: bool = instance_data.get('is_paused', False)
        self._pause_reason: Optional[str] = instance_data.get('pause_reason')

        # Hubitat client — lazy-loaded by HubitatMixin on first access
        self._hubitat = None

    # =========================================================================
    # Abstract Interface (implemented by each app type)
    # =========================================================================

    @abstractmethod
    def initialize(self) -> None:
        """
        Called when instance is loaded.

        Set up initial state, scheduled jobs, and any startup logic.
        Do NOT call master() here — it would turn off all lights before any
        motion event arrives (no motion → lights off).
        """
        pass

    @abstractmethod
    def on_event(self, event: DeviceEvent) -> None:
        """
        Handle an incoming device event from the webhook router.

        The event has already been filtered to events this instance subscribed to.
        Should return quickly — long-running work belongs in scheduled jobs.

        Args:
            event: DeviceEvent with device_id, event_type, value, etc.
        """
        pass

    @abstractmethod
    def master(self, **kwargs) -> None:
        """
        Main logic loop — decide and execute device state.

        Called after motion events, timeout expiry, mode changes, and resume.
        Subclasses implement their core automation decision here.
        """
        pass

    @classmethod
    @abstractmethod
    def get_settings_schema(cls) -> Dict[str, Any]:
        """
        Return JSON Schema for settings validation and UI form generation.

        Returns:
            JSON Schema dict (type, properties, required, etc.)
        """
        pass

    @classmethod
    @abstractmethod
    def get_device_categories(cls) -> List[Dict[str, Any]]:
        """
        Return device category definitions for the instance creation wizard.

        Each category specifies what type of devices the app needs.

        Returns:
            List of category dicts:
            [
                {
                    "key": "motion_sensors",
                    "label": "Motion Sensors",
                    "capability": "motionSensor",
                    "multiple": True,
                    "required": True,
                    "description": "..."
                },
                ...
            ]
        """
        pass
