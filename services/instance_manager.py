"""
Instance Manager Service

Manages the lifecycle of app instances:
- CRUD operations (create, read, update, delete)
- Device subscription management
- Runtime instance tracking
- Pause/resume functionality

This is the central service for multi-instance management. Each instance
represents a user-created automation (e.g., "Advanced Lights - Office").
"""

import os
import logging
import traceback
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Type
import requests


class InstanceManager:
    """
    Manages app instance lifecycle and runtime state.

    Responsibilities:
    - CRUD operations on app_instances table
    - Managing device_subscriptions for event routing
    - Tracking running instance objects in memory
    - Handling pause/resume operations

    Example usage:
        manager = InstanceManager()

        # Create a new instance
        instance_id = manager.create_instance(
            app_type='advanced_motion_lighting',
            label='Office Lights',
            device_selections={'motion_sensors': ['123'], 'switches': ['456']},
            settings={'noMotionTime': 5, 'useDim': True}
        )

        # Get instance
        instance = manager.get_instance(instance_id)

        # Update settings
        manager.update_instance(instance_id, settings={'noMotionTime': 10})

        # Pause/resume
        manager.pause_instance(instance_id, duration_minutes=60)
        manager.resume_instance(instance_id)

        # Delete
        manager.delete_instance(instance_id)
    """

    def __init__(self, postgrest_url: str = None):
        """
        Initialize the instance manager.

        Args:
            postgrest_url: URL to PostgREST service
        """
        self.postgrest_url = postgrest_url or os.environ.get(
            'POSTGREST_URL', 'http://postgrest:3001'
        )
        self.logger = logging.getLogger(__name__)

        # Runtime instances (keyed by instance_id)
        # These are the actual Python app objects that process events
        self._running_instances: Dict[int, Any] = {}

        # App type registry (populated by app modules on import)
        self._app_types: Dict[str, Type] = {}

    # =========================================================================
    # App Type Registration
    # =========================================================================

    def register_app_type(self, type_name: str, app_class: Type) -> None:
        """
        Register an app type class.

        Called by app modules during initialization to make their
        app type available for instance creation.

        Args:
            type_name: Type name (e.g., 'advanced_motion_lighting')
            app_class: Class that implements the app logic
        """
        self._app_types[type_name] = app_class
        self.logger.info(f"Registered app type: {type_name}")

    def get_app_types(self) -> List[Dict[str, Any]]:
        """
        Get all registered app types from database.

        Returns:
            List of app type dictionaries
        """
        try:
            response = requests.get(
                f"{self.postgrest_url}/app_types",
                timeout=5
            )
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            self.logger.error(f"Failed to get app types: {e}", exc_info=True)
        return []

    def get_app_type_schema(self, type_name: str) -> Optional[Dict[str, Any]]:
        """
        Get settings schema for an app type.

        Returns:
            Dictionary with settings_schema and device_categories
        """
        try:
            response = requests.get(
                f"{self.postgrest_url}/app_types",
                params={"type_name": f"eq.{type_name}"},
                timeout=5
            )
            if response.status_code == 200:
                types = response.json()
                if types:
                    return {
                        'settings_schema': types[0].get('settings_schema', {}),
                        'device_categories': types[0].get('device_categories', [])
                    }
        except Exception as e:
            self.logger.error(f"Failed to get app type schema: {e}", exc_info=True)
        return None

    # =========================================================================
    # Instance CRUD
    # =========================================================================

    def create_instance(
        self,
        app_type: str,
        label: str,
        device_selections: Dict[str, List[str]],
        settings: Dict[str, Any] = None
    ) -> Optional[int]:
        """
        Create a new app instance.

        Args:
            app_type: App type name (e.g., 'advanced_motion_lighting')
            label: User-defined label (e.g., 'Office Lights')
            device_selections: Devices by category (e.g., {'motion_sensors': ['123']})
            settings: Instance settings (merged with app type defaults)

        Returns:
            Instance ID or None on failure
        """
        # Get app type ID
        app_type_id = self._get_app_type_id(app_type)
        if not app_type_id:
            self.logger.error(f"Unknown app type: {app_type}")
            return None

        # Create instance record
        instance_data = {
            'app_type_id': app_type_id,
            'label': label,
            'device_selections': device_selections,
            'settings': settings or {},
            'is_enabled': True,
            'is_paused': False
        }

        try:
            response = requests.post(
                f"{self.postgrest_url}/app_instances",
                json=instance_data,
                headers={
                    "Content-Type": "application/json",
                    "Prefer": "return=representation"
                },
                timeout=10
            )

            if response.status_code in (200, 201):
                instance = response.json()
                if isinstance(instance, list):
                    instance = instance[0]

                instance_id = instance['id']

                # Create device subscriptions
                self._create_subscriptions(
                    instance_id, device_selections, app_type,
                    settings=settings or {}
                )

                # Initialize runtime instance
                self._start_instance(instance_id, instance)

                self.logger.info(f"Created instance: {label} (id={instance_id})")
                return instance_id

            else:
                self.logger.error(f"Failed to create instance: {response.text}")
                return None

        except Exception as e:
            self.logger.error(f"Failed to create instance: {e}", exc_info=True)
            return None

    def get_instance(self, instance_id: int) -> Optional[Dict[str, Any]]:
        """
        Get instance by ID.

        Args:
            instance_id: Instance ID

        Returns:
            Instance dictionary or None
        """
        try:
            response = requests.get(
                f"{self.postgrest_url}/app_instances",
                params={"id": f"eq.{instance_id}"},
                timeout=5
            )
            if response.status_code == 200:
                instances = response.json()
                return instances[0] if instances else None
        except Exception as e:
            self.logger.error(f"Failed to get instance: {e}", exc_info=True)
        return None

    def get_all_instances(self) -> List[Dict[str, Any]]:
        """
        Get all instances.

        Returns:
            List of instance dictionaries
        """
        try:
            response = requests.get(
                f"{self.postgrest_url}/app_instances",
                params={"order": "created_at.desc"},
                timeout=10
            )
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            self.logger.error(f"Failed to get instances: {e}", exc_info=True)
        return []

    def get_instances_by_type(self, app_type: str) -> List[Dict[str, Any]]:
        """
        Get all instances of a specific app type.

        Args:
            app_type: App type name

        Returns:
            List of instance dictionaries
        """
        app_type_id = self._get_app_type_id(app_type)
        if not app_type_id:
            return []

        try:
            response = requests.get(
                f"{self.postgrest_url}/app_instances",
                params={"app_type_id": f"eq.{app_type_id}"},
                timeout=5
            )
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            self.logger.error(f"Failed to get instances by type: {e}", exc_info=True)
        return []

    def update_instance(
        self,
        instance_id: int,
        label: str = None,
        device_selections: Dict[str, List[str]] = None,
        settings: Dict[str, Any] = None
    ) -> bool:
        """
        Update an instance.

        The running instance is killed BEFORE the DB patch. After a successful
        patch, a fresh instance is started from the new DB state. This avoids
        stale in-memory state surviving a failed or partial reload.

        Args:
            instance_id: Instance ID
            label: New label (optional)
            device_selections: New device selections (optional)
            settings: New settings (merged with existing)

        Returns:
            True if update succeeded
        """
        update_data = {}

        if label is not None:
            update_data['label'] = label

        if device_selections is not None:
            update_data['device_selections'] = device_selections

        if settings is not None:
            # Merge with existing settings
            instance = self.get_instance(instance_id)
            if instance:
                existing = instance.get('settings', {})
                existing.update(settings)
                update_data['settings'] = existing

        if not update_data:
            return True  # Nothing to update

        # Kill the running instance FIRST — guarantees no stale in-memory state
        self.stop_instance(instance_id)

        try:
            response = requests.patch(
                f"{self.postgrest_url}/app_instances",
                params={"id": f"eq.{instance_id}"},
                json=update_data,
                headers={"Content-Type": "application/json"},
                timeout=10
            )

            if response.status_code not in (200, 204):
                self.logger.error(
                    f"Failed to patch instance {instance_id}: "
                    f"HTTP {response.status_code} — {response.text}"
                )
                # Restart from old DB state so the instance isn't left dead
                self._start_from_db(instance_id)
                return False

            self.logger.info(f"Patched instance {instance_id} in DB")

        except Exception as e:
            self.logger.error(f"Failed to patch instance {instance_id}: {e}", exc_info=True)
            # Restart from old DB state so the instance isn't left dead
            self._start_from_db(instance_id)
            return False

        # Rebuild subscriptions and start fresh instance from new DB state
        self._rebuild_subscriptions(instance_id)
        started = self._start_from_db(instance_id)
        if not started:
            self.logger.error(f"Instance {instance_id} updated in DB but failed to restart")
        return True

    def delete_instance(self, instance_id: int) -> bool:
        """
        Delete an instance.

        Args:
            instance_id: Instance ID

        Returns:
            True if deletion succeeded
        """
        # Stop running instance
        self.stop_instance(instance_id)

        # Subscriptions deleted by CASCADE
        try:
            response = requests.delete(
                f"{self.postgrest_url}/app_instances",
                params={"id": f"eq.{instance_id}"},
                timeout=10
            )

            if response.status_code in (200, 204):
                self.logger.info(f"Deleted instance {instance_id}")
                return True

            self.logger.error(f"Failed to delete instance: {response.text}")
            return False

        except Exception as e:
            self.logger.error(f"Failed to delete instance: {e}", exc_info=True)
            return False

    # =========================================================================
    # Pause/Resume
    # =========================================================================

    def pause_instance(
        self,
        instance_id: int,
        duration_minutes: int = None,
        reason: str = None
    ) -> bool:
        """
        Pause an instance.

        Args:
            instance_id: Instance ID
            duration_minutes: Pause duration (None for indefinite)
            reason: Optional pause reason

        Returns:
            True if pause succeeded
        """
        update_data = {
            'is_paused': True,
            'pause_reason': reason
        }

        if duration_minutes:
            expires = datetime.now() + timedelta(minutes=duration_minutes)
            update_data['pause_expires_at'] = expires.isoformat()

        try:
            response = requests.patch(
                f"{self.postgrest_url}/app_instances",
                params={"id": f"eq.{instance_id}"},
                json=update_data,
                headers={"Content-Type": "application/json"},
                timeout=5
            )

            if response.status_code in (200, 204):
                # Notify running instance
                if instance_id in self._running_instances:
                    self._running_instances[instance_id].pause(duration_minutes or 0)
                self.logger.info(f"Paused instance {instance_id}")
                return True

        except Exception as e:
            self.logger.error(f"Failed to pause instance: {e}", exc_info=True)

        return False

    def resume_instance(self, instance_id: int) -> bool:
        """
        Resume a paused instance.

        Args:
            instance_id: Instance ID

        Returns:
            True if resume succeeded
        """
        try:
            response = requests.patch(
                f"{self.postgrest_url}/app_instances",
                params={"id": f"eq.{instance_id}"},
                json={
                    'is_paused': False,
                    'pause_expires_at': None,
                    'pause_reason': None
                },
                headers={"Content-Type": "application/json"},
                timeout=5
            )

            if response.status_code in (200, 204):
                # Notify running instance
                if instance_id in self._running_instances:
                    self._running_instances[instance_id].resume()
                self.logger.info(f"Resumed instance {instance_id}")
                return True

        except Exception as e:
            self.logger.error(f"Failed to resume instance: {e}", exc_info=True)

        return False

    # =========================================================================
    # Memoization State
    # =========================================================================

    def update_memoization(
        self,
        instance_id: int,
        memoization_state: Dict[str, Any]
    ) -> bool:
        """
        Update memoization state for an instance.

        Args:
            instance_id: Instance ID
            memoization_state: New memoization state

        Returns:
            True if update succeeded
        """
        try:
            response = requests.patch(
                f"{self.postgrest_url}/app_instances",
                params={"id": f"eq.{instance_id}"},
                json={'memoization_state': memoization_state},
                headers={"Content-Type": "application/json"},
                timeout=5
            )
            return response.status_code in (200, 204)
        except Exception as e:
            self.logger.error(f"Failed to update memoization: {e}", exc_info=True)
            return False

    # =========================================================================
    # Event Routing
    # =========================================================================

    def get_subscribed_instances(
        self,
        device_id: str,
        event_type: str
    ) -> List[int]:
        """
        Get instance IDs subscribed to a device/event combination.

        Used by WebhookRouter to dispatch events.

        Args:
            device_id: Hubitat device ID
            event_type: Event type (motion, switch, etc.)

        Returns:
            List of instance IDs
        """
        try:
            response = requests.get(
                f"{self.postgrest_url}/device_subscriptions",
                params={
                    "hubitat_device_id": f"eq.{device_id}",
                    "event_type": f"eq.{event_type}",
                    "select": "instance_id"
                },
                timeout=5
            )

            if response.status_code == 200:
                subs = response.json()
                return [s['instance_id'] for s in subs]

        except Exception as e:
            self.logger.error(f"Failed to get subscribed instances: {e}", exc_info=True)

        return []

    def get_running_instance(self, instance_id: int) -> Optional[Any]:
        """
        Get the running Python app object for an instance.

        Args:
            instance_id: Instance ID

        Returns:
            App object or None
        """
        return self._running_instances.get(instance_id)

    # =========================================================================
    # Instance Lifecycle
    # =========================================================================

    def initialize_all_instances(self) -> int:
        """
        Initialize all enabled instances on startup.

        Called when the application starts to load all instances
        and begin processing events.

        Returns:
            Number of instances initialized
        """
        count = 0
        instances = self.get_all_instances()

        for instance in instances:
            if instance.get('is_enabled', True):
                if self._start_instance(instance['id'], instance):
                    count += 1

        self.logger.info(f"Initialized {count} instances")
        return count

    def _start_instance(
        self,
        instance_id: int,
        instance_data: Dict[str, Any]
    ) -> bool:
        """Start a runtime instance."""
        # Get app type class
        app_type_name = self._get_app_type_name(instance_data['app_type_id'])
        if not app_type_name or app_type_name not in self._app_types:
            self.logger.warning(
                f"No app class registered for type: {app_type_name}"
            )
            return False

        try:
            # Create app object
            app_class = self._app_types[app_type_name]
            app_instance = app_class(instance_data, self)

            # Initialize (sets up subscriptions, schedules, etc.)
            app_instance.initialize()

            # Track
            self._running_instances[instance_id] = app_instance
            self.logger.info(
                f"Started instance {instance_id} ({app_instance.label}) "
                f"— devices: {list(instance_data.get('device_selections', {}).keys())}"
            )
            return True

        except Exception as e:
            self.logger.error(f"Failed to start instance {instance_id}: {e}", exc_info=True)
            return False

    def stop_instance(self, instance_id: int) -> bool:
        """
        Kill a running instance — cancel all scheduler jobs, remove from
        tracking dict. Safe to call even if the instance is not running.

        Returns:
            True if an instance was stopped, False if nothing was running.
        """
        if instance_id not in self._running_instances:
            self.logger.info(f"stop_instance({instance_id}): not running, nothing to stop")
            return False

        label = getattr(self._running_instances[instance_id], 'label', '?')
        self.logger.info(f"Stopping instance {instance_id} ({label})")

        try:
            self._running_instances[instance_id].shutdown()
        except Exception as e:
            self.logger.warning(
                f"Error in shutdown() for instance {instance_id}: {e}", exc_info=True
            )

        # Cancel the per-instance event worker so its queue is dropped
        # together with the instance. Imported lazily to avoid a circular
        # import at module load time.
        try:
            from services.webhook_router import get_webhook_router
            get_webhook_router().stop_instance_worker(instance_id)
        except Exception as e:
            self.logger.warning(
                f"Failed to stop event worker for instance {instance_id}: {e}",
                exc_info=True
            )

        del self._running_instances[instance_id]
        self.logger.info(f"Stopped instance {instance_id} ({label})")
        return True

    def _start_from_db(self, instance_id: int) -> bool:
        """
        Fetch the current DB row for an instance and start it.

        Returns:
            True if started successfully, False otherwise.
        """
        instance = self.get_instance(instance_id)
        if not instance:
            self.logger.error(f"_start_from_db({instance_id}): instance not found in DB")
            return False

        started = self._start_instance(instance_id, instance)
        if started:
            self.logger.info(
                f"Started instance {instance_id} ({instance.get('label', '?')})"
            )
        else:
            self.logger.error(
                f"_start_from_db({instance_id}): _start_instance returned False"
            )
        return started

    def _rebuild_subscriptions(self, instance_id: int) -> None:
        """
        Delete all subscriptions for an instance and recreate them from the
        current DB state.
        """
        self._delete_subscriptions(instance_id)

        instance = self.get_instance(instance_id)
        if not instance:
            self.logger.warning(
                f"_rebuild_subscriptions({instance_id}): instance not found in DB"
            )
            return

        device_selections = instance.get('device_selections', {})
        app_type = self._get_app_type_name(instance['app_type_id'])

        if device_selections and app_type:
            self._create_subscriptions(
                instance_id, device_selections, app_type,
                settings=instance.get('settings', {})
            )
            self.logger.info(
                f"Rebuilt subscriptions for instance {instance_id}"
            )

    # =========================================================================
    # Subscription Management
    # =========================================================================

    def _create_subscriptions(
        self,
        instance_id: int,
        device_selections: Dict[str, List[str]],
        app_type: str,
        settings: Dict[str, Any] = None
    ) -> None:
        """Create device subscriptions for an instance."""
        # Button event type is configurable (default: held)
        button_event = (settings or {}).get('buttonEventType', 'held')

        # Map device categories to event types
        # keep_off/keep_on switches need subscriptions so _handle_switch()
        # can detect manual overrides via webhook events
        category_events = {
            'motion_sensors': 'motion',
            'switches': 'switch',
            'contacts': 'contact',
            'illuminance_sensor': 'illuminance',
            'pause_buttons': button_event,
            'keep_off_switches': 'switch',
            'keep_on_switches': 'switch'
        }

        subscriptions = []

        for category, device_ids in device_selections.items():
            event_type = category_events.get(category)
            if not event_type:
                continue

            for device_id in device_ids:
                subscriptions.append({
                    'hubitat_device_id': str(device_id),
                    'instance_id': instance_id,
                    'event_type': event_type
                })

        if subscriptions:
            try:
                requests.post(
                    f"{self.postgrest_url}/device_subscriptions",
                    json=subscriptions,
                    headers={
                        "Content-Type": "application/json",
                        "Prefer": "resolution=ignore-duplicates"
                    },
                    timeout=10
                )
            except Exception as e:
                self.logger.error(f"Failed to create subscriptions: {e}", exc_info=True)

    def _delete_subscriptions(self, instance_id: int) -> None:
        """Delete all subscriptions for an instance."""
        try:
            requests.delete(
                f"{self.postgrest_url}/device_subscriptions",
                params={"instance_id": f"eq.{instance_id}"},
                timeout=5
            )
        except Exception as e:
            self.logger.error(f"Failed to delete subscriptions: {e}", exc_info=True)

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _get_app_type_id(self, type_name: str) -> Optional[int]:
        """Get app type ID by name."""
        try:
            response = requests.get(
                f"{self.postgrest_url}/app_types",
                params={"type_name": f"eq.{type_name}", "select": "id"},
                timeout=5
            )
            if response.status_code == 200:
                types = response.json()
                return types[0]['id'] if types else None
        except Exception as e:
            self.logger.error(f"Failed to get app type ID for '{type_name}': {e}", exc_info=True)
        return None

    def _get_app_type_name(self, type_id: int) -> Optional[str]:
        """Get app type name by ID."""
        try:
            response = requests.get(
                f"{self.postgrest_url}/app_types",
                params={"id": f"eq.{type_id}", "select": "type_name"},
                timeout=5
            )
            if response.status_code == 200:
                types = response.json()
                return types[0]['type_name'] if types else None
        except Exception as e:
            self.logger.error(f"Failed to get app type name for ID {type_id}: {e}", exc_info=True)
        return None


# Global instance manager
_instance_manager: Optional[InstanceManager] = None


def get_instance_manager() -> InstanceManager:
    """Get the global instance manager."""
    global _instance_manager
    if _instance_manager is None:
        _instance_manager = InstanceManager()
    return _instance_manager
