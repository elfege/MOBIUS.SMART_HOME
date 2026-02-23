"""
Base App Class

Abstract base class for all app types. Each app type (Advanced Motion Lighting,
Thermostat Manager, etc.) inherits from this class and implements its specific
automation logic.

The base class provides:
- Standard lifecycle methods (initialize, shutdown)
- Event handling interface
- Settings and device access
- Memoization state management
- Scheduler integration
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional, TYPE_CHECKING
from datetime import datetime
import logging
import traceback

from models.event import DeviceEvent
from models.command import CommandResult
from models.instance import RuntimeInstanceState

if TYPE_CHECKING:
    from services.instance_manager import InstanceManager


class BaseApp(ABC):
    """
    Abstract base class for smart home apps.

    Each app type inherits from this class and implements its specific
    automation logic. The base class provides common functionality for:
    - Instance configuration and state access
    - Device command execution
    - Event handling
    - Memoization
    - Scheduling

    Class Attributes:
        TYPE_NAME: Internal type identifier (e.g., 'advanced_motion_lighting')
        DISPLAY_NAME: Human-readable name (e.g., 'Advanced Motion Lighting')
        DESCRIPTION: App description for UI
        VERSION: App version string

    Example:
        class MyApp(BaseApp):
            TYPE_NAME = 'my_app'
            DISPLAY_NAME = 'My Smart App'

            def initialize(self):
                # Set up subscriptions, schedules
                pass

            def on_event(self, event):
                # Handle device events
                pass

            def master(self):
                # Main logic loop
                pass
    """

    # Override these in subclasses
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
        Initialize the app instance.

        Args:
            instance_data: Database row for this instance (from app_instances)
            instance_manager: InstanceManager for database operations
        """
        self.instance_data = instance_data
        self.instance_manager = instance_manager

        # Extract commonly used fields
        self.instance_id: int = instance_data['id']
        self.label: str = instance_data['label']
        self.settings: Dict[str, Any] = instance_data.get('settings', {})
        self.device_selections: Dict[str, List[str]] = instance_data.get(
            'device_selections', {}
        )

        # Memoization state (persistent, saved to database)
        self._memoization: Dict[str, Any] = instance_data.get(
            'memoization_state', {}
        )

        # Runtime state (not persisted)
        self._runtime = RuntimeInstanceState()

        # Logger for this instance
        self.logger = logging.getLogger(
            f"{self.__class__.__name__}.{self.label}"
        )

        # Pause state
        self._is_paused: bool = instance_data.get('is_paused', False)

        # Hubitat client (lazy loaded)
        self._hubitat = None

    # =========================================================================
    # Abstract Methods (must be implemented by subclasses)
    # =========================================================================

    @abstractmethod
    def initialize(self) -> None:
        """
        Called when instance is loaded.

        Subclasses should:
        - Set up any initial state
        - Register scheduled jobs (health checks, etc.)
        - Perform any startup logic

        This is called once when the instance is created or when the
        application restarts.
        """
        pass

    @abstractmethod
    def on_event(self, event: DeviceEvent) -> None:
        """
        Handle incoming device event.

        Called by the webhook router when a subscribed device emits an event.
        The event has already been filtered to only include events this
        instance subscribed to.

        Args:
            event: DeviceEvent with device_id, event_type, value, etc.

        Note:
            This method should return quickly. Long-running operations
            should be scheduled as background jobs.
        """
        pass

    @abstractmethod
    def master(self, **kwargs) -> None:
        """
        Main logic loop.

        This is the core automation logic for the app. It's called:
        - After motion events (with motion_active=True)
        - After scheduled timeouts
        - After mode changes
        - After resume from pause

        Subclasses should implement their main decision logic here:
        - Should lights be on or off?
        - What brightness level?
        - Are we in an exception state?
        """
        pass

    @classmethod
    @abstractmethod
    def get_settings_schema(cls) -> Dict[str, Any]:
        """
        Return JSON Schema for settings validation.

        Used by the UI to generate settings forms and validate input.

        Returns:
            JSON Schema dictionary
        """
        pass

    @classmethod
    @abstractmethod
    def get_device_categories(cls) -> List[Dict[str, Any]]:
        """
        Return device categories for the instance wizard.

        Each category defines what type of devices the app needs.
        The wizard uses this to show device pickers.

        Returns:
            List of category definitions:
            [
                {
                    "key": "motion_sensors",
                    "label": "Motion Sensors",
                    "capability": "motionSensor",
                    "multiple": True,
                    "required": True,
                    "description": "Select motion sensors that trigger lighting"
                },
                ...
            ]
        """
        pass

    # =========================================================================
    # Lifecycle Methods
    # =========================================================================

    def shutdown(self) -> None:
        """
        Called when instance is being stopped.

        Subclasses can override to clean up resources.
        Base implementation cancels scheduled jobs.
        """
        from services.scheduler_service import get_scheduler
        scheduler = get_scheduler()
        scheduler.cancel_for_instance(self.instance_id)
        self.logger.info(f"Shutdown instance: {self.label}")

    def on_mode_change(self, new_mode: str) -> None:
        """
        Handle location mode change.

        Called when the Hubitat location mode changes (e.g., Home → Away).
        Default implementation resets memoization and calls master().

        Subclasses can override for custom mode change behavior.

        Args:
            new_mode: Name of the new mode
        """
        self.logger.info(f"Mode changed to: {new_mode}")

        # Reset memoization on mode change (common pattern)
        self._reset_memoization()

        # Re-evaluate state
        self.master()

    # =========================================================================
    # Pause/Resume
    # =========================================================================

    def pause(self, duration_minutes: int = 0) -> None:
        """
        Pause the instance.

        Resets memoization (matching Groovy resetStates pattern) so that
        when the instance resumes, it doesn't carry stale override records.
        Schedules auto-resume if duration_minutes > 0.

        Args:
            duration_minutes: How long to pause (0 = indefinite)
        """
        self._is_paused = True
        self.logger.info(f"Paused for {duration_minutes} minutes")

        # Reset memoization on pause (Groovy pattern: resetStates on pause)
        self._reset_memoization()

        from services.scheduler_service import get_scheduler
        scheduler = get_scheduler()

        # Cancel pending timeout jobs
        if self._runtime.timeout_job_id:
            scheduler.cancel(self._runtime.timeout_job_id)

        # Schedule auto-resume if duration specified
        if duration_minutes > 0:
            job_id = f"auto_resume_{self.instance_id}"
            # Cancel any existing auto-resume job first
            if self._runtime.auto_resume_job_id:
                scheduler.cancel(self._runtime.auto_resume_job_id)
            scheduler.schedule_once(
                job_id=job_id,
                delay_seconds=duration_minutes * 60,
                callback=lambda **kwargs: self._auto_resume(),
                instance_id=self.instance_id,
                job_type='auto_resume'
            )
            self._runtime.auto_resume_job_id = job_id
            self.logger.info(f"Auto-resume scheduled in {duration_minutes} minutes")

    def _auto_resume(self) -> None:
        """Auto-resume after pause duration expires."""
        self.logger.info("Auto-resume triggered")
        self._runtime.auto_resume_job_id = None
        # Use instance_manager to resume (updates DB + in-memory state)
        self.instance_manager.resume_instance(self.instance_id)

    def resume(self) -> None:
        """
        Resume from paused state.

        Resets memoization so the instance starts fresh.
        Cancels any pending auto-resume job.
        Calls master() to re-evaluate state. Subclasses may override
        to change resume behavior (e.g. enforce keep switches instead).
        """
        self._is_paused = False
        self.logger.info("Resumed")

        # Cancel auto-resume if we're being manually resumed
        if self._runtime.auto_resume_job_id:
            from services.scheduler_service import get_scheduler
            scheduler = get_scheduler()
            scheduler.cancel(self._runtime.auto_resume_job_id)
            self._runtime.auto_resume_job_id = None

        # Reset memoization on resume (Groovy pattern: resetStates on resume)
        self._reset_memoization()

        # Re-evaluate state (subclasses may override for different behavior)
        self.master()

    @property
    def is_paused(self) -> bool:
        """Check if instance is paused."""
        return self._is_paused

    # =========================================================================
    # Settings Access
    # =========================================================================

    def get_setting(self, key: str, default: Any = None) -> Any:
        """
        Get a setting value.

        Args:
            key: Setting key
            default: Default value if not set

        Returns:
            Setting value or default
        """
        return self.settings.get(key, default)

    def get_devices(self, category: str) -> List[str]:
        """
        Get device IDs for a category.

        Args:
            category: Device category key (e.g., 'motion_sensors')

        Returns:
            List of device IDs
        """
        return self.device_selections.get(category, [])

    # =========================================================================
    # Memoization
    # =========================================================================

    def get_memo(self, key: str, default: Any = None) -> Any:
        """
        Get a memoized value.

        Args:
            key: Memoization key
            default: Default value

        Returns:
            Memoized value or default
        """
        return self._memoization.get(key, default)

    def set_memo(self, key: str, value: Any) -> None:
        """
        Set a memoized value.

        Args:
            key: Memoization key
            value: Value to store
        """
        self._memoization[key] = value

    def _reset_memoization(self) -> None:
        """Reset all memoization state."""
        self._memoization = {}
        self._save_memoization()

    def _save_memoization(self) -> None:
        """Save memoization state to database."""
        try:
            self.instance_manager.update_memoization(
                self.instance_id,
                self._memoization
            )
        except Exception as e:
            self.logger.error(
                f"Failed to save memoization for instance "
                f"{self.instance_id}: {e}",
                exc_info=True
            )

    # =========================================================================
    # Hubitat Integration
    # =========================================================================

    @property
    def hubitat(self):
        """Get the Hubitat client (lazy loaded)."""
        if self._hubitat is None:
            from services.hubitat_client import get_default_client
            self._hubitat = get_default_client()
        return self._hubitat

    def send_command(
        self,
        device_id: str,
        command: str,
        args: List = None,
        verify: bool = True,
    ) -> CommandResult:
        """
        Send a command to a device via the centralized DeviceCommander.

        The commander handles:
        - Threaded execution (does not block asyncio event loop)
        - Nested retries with state verification
        - Matter dual-command dispatch (fire-and-forget)
        - Full traceback logging on errors

        Args:
            device_id: Hubitat device ID
            command: Command name (e.g., 'on', 'off', 'setLevel')
            args: Optional command arguments
            verify: Whether to verify device state after command (default: True)

        Returns:
            CommandResult with success, verified, actual_state, timing, etc.
        """
        from services.device_commander import get_device_commander

        try:
            commander = get_device_commander()
            device_name = self._get_device_display_name(device_id)
            return commander.send_command_sync(
                device_id=device_id,
                command=command,
                args=args,
                verify=verify,
                device_name=device_name,
            )
        except Exception as e:
            self.logger.error(
                f"send_command failed for device {device_id}, "
                f"cmd={command}: {e}",
                exc_info=True
            )
            return CommandResult(
                device_id=device_id,
                command=command,
                args=args,
                error=str(e),
                traceback_str=traceback.format_exc(),
            )

    def _get_device_display_name(self, device_id: str) -> str:
        """
        Get human-readable display name for a device, for logging context.

        Falls back to the raw device_id if cache lookup fails.

        Args:
            device_id: Hubitat device ID

        Returns:
            Device label, name, or raw ID
        """
        try:
            device = self.get_device_state(device_id)
            if device:
                return device.get(
                    'device_label',
                    device.get('device_name', device_id)
                )
        except Exception:
            pass
        return device_id

    def get_device_state(self, device_id: str) -> Optional[Dict[str, Any]]:
        """
        Get current device state from cache.

        Args:
            device_id: Device ID

        Returns:
            Device state dictionary or None
        """
        from services.device_cache import get_default_cache
        cache = get_default_cache()
        return cache.get_device(device_id)

    # =========================================================================
    # Scheduling
    # =========================================================================

    def schedule_timeout(
        self,
        delay_seconds: int,
        callback_name: str = 'master'
    ) -> str:
        """
        Schedule a timeout job.

        Args:
            delay_seconds: Seconds until job runs
            callback_name: Method name to call (default: 'master')

        Returns:
            Job ID
        """
        from services.scheduler_service import get_scheduler
        scheduler = get_scheduler()

        # Cancel existing timeout to prevent scheduling leak
        if self._runtime.timeout_job_id:
            scheduler.cancel(self._runtime.timeout_job_id)

        job_id = f"timeout_{self.instance_id}_{datetime.now().timestamp()}"

        # Get the callback method
        callback = getattr(self, callback_name, self.master)

        scheduler.schedule_once(
            job_id=job_id,
            delay_seconds=delay_seconds,
            callback=lambda **kwargs: callback(),
            instance_id=self.instance_id,
            job_type='timeout'
        )

        self._runtime.timeout_job_id = job_id
        return job_id

    def reschedule_timeout(self, delay_seconds: int) -> bool:
        """
        Reschedule the current timeout job.

        Args:
            delay_seconds: New delay from now

        Returns:
            True if rescheduled successfully
        """
        if not self._runtime.timeout_job_id:
            # No existing timeout, schedule new one
            self.schedule_timeout(delay_seconds)
            return True

        from services.scheduler_service import get_scheduler
        scheduler = get_scheduler()
        return scheduler.reschedule(self._runtime.timeout_job_id, delay_seconds)

    def cancel_timeout(self) -> None:
        """Cancel any pending timeout job."""
        if self._runtime.timeout_job_id:
            from services.scheduler_service import get_scheduler
            scheduler = get_scheduler()
            scheduler.cancel(self._runtime.timeout_job_id)
            self._runtime.timeout_job_id = None

    # =========================================================================
    # Activity Tracking
    # =========================================================================

    def update_last_activity(self) -> None:
        """Update last activity timestamp in database."""
        import requests
        try:
            requests.patch(
                f"{self.instance_manager.postgrest_url}/app_instances",
                params={"id": f"eq.{self.instance_id}"},
                json={"last_activity_at": datetime.now().isoformat()},
                headers={"Content-Type": "application/json"},
                timeout=5
            )
        except Exception as e:
            self.logger.warning(
                f"Failed to update last activity: {e}", exc_info=True
            )
