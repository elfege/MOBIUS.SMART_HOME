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
import time
import logging
import traceback
from datetime import datetime, timedelta, timezone
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

        # Shared HTTP session: reuses one TCP connection to PostgREST instead of
        # opening a fresh socket per request. Saves ~30% latency per call and
        # compounds significantly during initialize_all_instances() where we
        # fire 4 PostgREST calls per instance back-to-back.
        # requests.Session is documented thread-safe for GET/POST.
        self._http = requests.Session()

        # Runtime instances (keyed by instance_id)
        # These are the actual Python app objects that process events
        self._running_instances: Dict[int, Any] = {}

        # instance_id → monotonic ts of its last stop_instance(). Used by
        # the dead-instance watchdog (revive_dead_instances) to leave a
        # transient edit-stop alone during its grace window while still
        # reviving a worker that's been stopped-and-not-restarted past it
        # (abandoned edit / crashed start). Cleared on successful start.
        self._recently_stopped: Dict[int, float] = {}

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
            response = self._http.get(
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
            response = self._http.get(
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
            response = self._http.post(
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
            response = self._http.get(
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
            response = self._http.get(
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
            response = self._http.get(
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

        # Always clear memoization on update — manual overrides (dim_level
        # source='manual', color_state source='manual', switch_state) MUST
        # NOT survive a settings or device-selection change, otherwise a
        # stale manual override silently shadows the new settings forever.
        # Concrete bug this prevents: user sets modeDimLevels.WatchingTV=10,
        # but a prior manual override at level=80 was still in memo, and the
        # cascade returned 80 every time. Clearing here means the fresh
        # instance starts with empty memo; AML's _init_memoization_keys()
        # re-seeds keep_off / keep_on entries on __init__. Label-only updates
        # are unaffected because nothing about the memo depends on the label.
        if 'settings' in update_data or 'device_selections' in update_data:
            update_data['memoization_state'] = {}
            self.logger.info(
                f"Instance {instance_id} update includes settings/devices — "
                f"clearing memoization_state to drop any stale overrides"
            )

        # Kill the running instance FIRST — guarantees no stale in-memory state
        self.stop_instance(instance_id)

        try:
            response = self._http.patch(
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
            response = self._http.delete(
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
            # Tz-aware UTC so the stored pause_expires_at compares
            # correctly against now(timezone.utc) when checking expiry.
            expires = (datetime.now(timezone.utc)
                       + timedelta(minutes=duration_minutes))
            update_data['pause_expires_at'] = expires.isoformat()

        try:
            response = self._http.patch(
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
            response = self._http.patch(
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
            response = self._http.patch(
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
        device_id: int,
        event_type: str
    ) -> List[int]:
        """
        Get instance IDs subscribed to a (canonical) device + event_type.

        Used by WebhookRouter to dispatch events. The caller MUST resolve
        the inbound Hubitat id + hub_ip to a canonical devices.id before
        calling this — that is the only sub-routing key now (Phase 5).

        Args:
            device_id: Canonical devices.id PK
            event_type: Event type (motion, switch, etc.)

        Returns:
            List of instance IDs
        """
        if device_id is None:
            return []
        try:
            response = self._http.get(
                f"{self.postgrest_url}/device_subscriptions",
                params={
                    "device_id": f"eq.{device_id}",
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
        enabled = [i for i in instances if i.get('is_enabled', True)]

        # Bulk-fetch the canonical → hubitat-id map ONCE for all instances'
        # selections combined, instead of issuing one GET per instance inside
        # _create_subscriptions. With N instances this collapses N PostgREST
        # roundtrips into 1 — the dominant fixed cost during startup.
        all_canon_ids: set = set()
        for inst in enabled:
            for ids in (inst.get('device_selections') or {}).values():
                for d in (ids or []):
                    if str(d).isdigit():
                        all_canon_ids.add(int(d))
        hubitat_by_canon = self._fetch_hubitat_by_canon(sorted(all_canon_ids))

        for instance in enabled:
            if self._start_instance(
                instance['id'], instance,
                hubitat_by_canon=hubitat_by_canon,
            ):
                count += 1

        self.logger.info(f"Initialized {count} instances")
        return count

    def _fetch_hubitat_by_canon(
        self,
        canon_ids: List[int],
    ) -> Dict[int, str]:
        """
        Resolve canonical devices.id → hubitat_id for the given PKs in one GET.

        Returns an empty dict on empty input or PostgREST failure (callers
        treat a missing entry as "skip subscription with warning", same as the
        per-instance fallback in _create_subscriptions).
        """
        if not canon_ids:
            return {}
        try:
            resp = self._http.get(
                f"{self.postgrest_url}/devices",
                params={
                    "select": "id,hubitat_id",
                    "id": f"in.({','.join(str(i) for i in canon_ids)})",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                return {int(row["id"]): str(row["hubitat_id"]) for row in resp.json()}
        except Exception as e:
            self.logger.error(
                f"Bulk canonical→hubitat fetch failed (will fall back to "
                f"per-instance lookup): {e}",
                exc_info=True,
            )
        return {}

    def _start_instance(
        self,
        instance_id: int,
        instance_data: Dict[str, Any],
        hubitat_by_canon: Optional[Dict[int, str]] = None,
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

            # Initialize in-memory state (timers, schedulers, runtime).
            # NOTE: this does NOT touch the device_subscriptions DB table.
            app_instance.initialize()

            # Self-heal device_subscriptions from the current DB row. Without
            # this, an instance whose subs were ever wiped (e.g. via an old
            # update_instance path that deleted-without-recreating) stays
            # silent forever because the webhook router has nothing to route.
            # Rebuilding on every start makes ./start.sh idempotent.
            self._rebuild_subscriptions(
                instance_id, hubitat_by_canon=hubitat_by_canon
            )

            # Track
            self._running_instances[instance_id] = app_instance
            # Successful (re)start clears any stale stop marker so the
            # watchdog's grace logic starts fresh next time it's stopped.
            self._recently_stopped.pop(instance_id, None)
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
        # Stamp for the dead-instance watchdog's grace window. A normal
        # edit (stop → save/cancel → start) clears this on the restart;
        # an ABANDONED edit leaves it set, and the watchdog revives the
        # instance once the grace window elapses.
        self._recently_stopped[instance_id] = time.monotonic()
        self.logger.info(f"Stopped instance {instance_id} ({label})")
        return True

    def revive_dead_instances(self, grace_seconds: int = 900) -> Dict[str, Any]:
        """
        Watchdog: revive instances that SHOULD be running but whose worker
        is dead — present in the DB, not paused, yet absent from
        ``_running_instances``.

        Transient edit-stops are protected by the ``_recently_stopped``
        grace window: the wizard stops a worker on edit-entry and restarts
        it on save/cancel, so a recently-stopped instance is left alone for
        ``grace_seconds``. Past that window a still-stopped, not-paused
        instance is treated as an ABANDONED edit (or a start that crashed)
        and revived. PAUSED instances are never revived — pause is the
        supported way to keep an instance intentionally off; ``stop`` is
        meant to be transient.

        Root cause this closes: 2026-06-18, instance 5 (Motion Kitchen) was
        left stopped after an abandoned edit and ran dead ~7 h, leaving the
        kitchen unmanaged with no self-heal. Returns a small report dict.
        """
        revived: List[int] = []
        skipped_paused = 0
        skipped_grace = 0
        now = time.monotonic()

        try:
            instances = self.get_all_instances()
        except Exception as e:
            self.logger.warning(f"revive_dead_instances: list failed: {e}")
            return {"status": "error", "error": str(e)}

        for inst in instances:
            iid = inst.get('id')
            if iid is None or iid in self._running_instances:
                continue
            if inst.get('is_paused'):
                skipped_paused += 1
                continue
            stopped_at = self._recently_stopped.get(iid)
            if stopped_at is not None and (now - stopped_at) < grace_seconds:
                skipped_grace += 1
                continue

            self.logger.warning(
                f"watchdog: instance {iid} ({inst.get('label', '?')}) is in "
                f"DB, not paused, not running — reviving"
            )
            try:
                if self._start_from_db(iid):
                    revived.append(iid)
            except Exception as e:
                self.logger.error(
                    f"watchdog: revive of instance {iid} raised: {e}",
                    exc_info=True
                )

        if revived:
            self.logger.info(
                f"watchdog: revived {len(revived)} dead instance(s): {revived}"
            )
        return {
            "status": "ok",
            "revived": revived,
            "skipped_paused": skipped_paused,
            "skipped_grace": skipped_grace,
        }

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

    def _rebuild_subscriptions(
        self,
        instance_id: int,
        hubitat_by_canon: Optional[Dict[int, str]] = None,
    ) -> None:
        """
        Delete all subscriptions for an instance and recreate them from the
        current DB state.

        hubitat_by_canon (optional): pre-fetched canonical→hubitat-id map.
        When provided (e.g. by initialize_all_instances), _create_subscriptions
        skips its per-call PostgREST GET. Pass None for ad-hoc rebuilds where
        the cost of one GET is irrelevant.
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
                settings=instance.get('settings', {}),
                hubitat_by_canon=hubitat_by_canon,
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
        settings: Dict[str, Any] = None,
        hubitat_by_canon: Optional[Dict[int, str]] = None,
    ) -> None:
        """
        Create device subscriptions for an instance.

        hubitat_by_canon (optional): pre-fetched canonical→hubitat-id map,
        typically supplied by initialize_all_instances after one bulk fetch.
        When None, this method does its own per-call GET (the original path
        used by create_instance / update_instance).
        """
        # Button event type is configurable (default: held)
        button_event = (settings or {}).get('buttonEventType', 'held')

        # Map device categories to event types.
        #
        # Each entry says "when this category is in an instance's
        # device_selections, auto-subscribe the instance to events of THIS
        # type from THOSE devices". The map intentionally has gaps — output
        # devices we control but don't want to listen to ourselves must NOT
        # be listed, or every command we send echoes back as an event and
        # the next master() re-issues it (2026-06-05 Fan Bathroom storm,
        # see apps/fan_automation/app.py docstring).
        #
        # Rule of thumb: list a category here only when reacting to its
        # state changes is part of the app's input model. Pure outputs
        # (fans, AML-controlled main 'switches' would arguably qualify but
        # AML's manual-override convention depends on the subscription
        # remaining wired) stay off the map. Explicit user-override
        # categories (keep_off/keep_on, manual_fan_level_override) DO
        # belong here — they ARE control inputs masquerading as switches.
        category_events = {
            'motion_sensors': 'motion',
            'switches': 'switch',
            'contacts': 'contact',
            'illuminance_sensor': 'illuminance',
            'pause_buttons': button_event,
            'keep_off_switches': 'switch',
            'keep_on_switches': 'switch',
            # 'fans': 'switch' — DELIBERATELY UNMAPPED. The fan is a
            # pure output of fan_automation; subscribing creates a
            # send-command-echo-back-as-event feedback loop. Manual user
            # override is expressed via the dedicated
            # 'manual_fan_level_override_switches' category instead.
            'manual_fan_level_override_switches': 'switch',
            'humidity_sensors': 'humidity',
            'presence_sensors': 'presence',
            # Power Management — see apps/power_management/app.py.
            # power_sensors are INPUTS (the rolling-average source); we
            # subscribe so each power event drives the threshold check.
            # cutoff_switches are intentionally OMITTED for the same
            # reason 'fans' is: they're pure outputs of this app, and
            # subscribing would create an echo loop on every cutoff
            # actuation.
            'power_sensors': 'power',
            # Rules app — a single trigger button drives multiple actions
            # by event type, so this category fans out to ALL THREE button
            # event types (the only multi-event entry in this map). The
            # value is a LIST; the loop below handles list-or-str. The
            # pool_water_switches / pump_switch categories are pure OUTPUTS
            # and are deliberately UNMAPPED (subscribing an output re-feeds
            # our own commands back as events — the fan-storm failure mode).
            'trigger_button': ['pushed', 'held', 'doubleTapped'],
        }

        # device_selections stores CANONICAL devices.id PKs (Phase 5).
        # Subscriptions are keyed on the canonical device_id FK only —
        # routing is hub-aware by construction via devices.hub_id, no
        # need for the Hubitat per-hub id at this layer.
        sub_keys = set()
        subscriptions = []

        for category, device_ids in device_selections.items():
            event_spec = category_events.get(category)
            if not event_spec:
                continue

            # A category may subscribe to ONE event type (str) or SEVERAL
            # (list/tuple — e.g. Rules' trigger_button → pushed/held/
            # doubleTapped). Normalize to a list so the inner loop is uniform.
            event_types = (
                list(event_spec)
                if isinstance(event_spec, (list, tuple))
                else [event_spec]
            )

            for device_id in device_ids:
                # Selection entries are canonical PKs (post-Phase-5 schema).
                try:
                    canonical_id = int(device_id)
                except (TypeError, ValueError):
                    self.logger.warning(
                        f"Skipping non-numeric selection {device_id!r} "
                        f"in instance {instance_id} ({category})"
                    )
                    continue
                for event_type in event_types:
                    key = (canonical_id, event_type)
                    if key in sub_keys:
                        continue
                    sub_keys.add(key)
                    subscriptions.append({
                        'device_id':   canonical_id,
                        'instance_id': instance_id,
                        'event_type':  event_type,
                    })

        if subscriptions:
            try:
                response = self._http.post(
                    f"{self.postgrest_url}/device_subscriptions",
                    json=subscriptions,
                    headers={
                        "Content-Type": "application/json",
                        "Prefer": "resolution=ignore-duplicates"
                    },
                    timeout=10
                )
                if response.status_code not in (200, 201, 204):
                    self.logger.error(
                        f"Failed to create subscriptions for instance {instance_id}: "
                        f"HTTP {response.status_code} — {response.text}"
                    )
            except Exception as e:
                self.logger.error(f"Failed to create subscriptions: {e}", exc_info=True)

    def _delete_subscriptions(self, instance_id: int) -> None:
        """Delete all subscriptions for an instance."""
        try:
            self._http.delete(
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
            response = self._http.get(
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
            response = self._http.get(
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
# reload
# reload-phase5
