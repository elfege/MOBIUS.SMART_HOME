"""
App Type Registry

Registers all available app types and populates the app_types table.
"""

import logging
import traceback
import requests
import os
from typing import Dict, Type, List

from apps.base_app import BaseApp

logger = logging.getLogger(__name__)

# Registry of app type classes
_app_types: Dict[str, Type[BaseApp]] = {}


def register_app_type(app_class: Type[BaseApp]) -> None:
    """
    Register an app type class.

    Args:
        app_class: Class that extends BaseApp
    """
    type_name = app_class.TYPE_NAME
    if not type_name:
        raise ValueError(f"App class {app_class} has no TYPE_NAME")

    _app_types[type_name] = app_class
    logger.info(f"Registered app type: {type_name}")


def get_app_class(type_name: str) -> Type[BaseApp]:
    """
    Get app class by type name.

    Args:
        type_name: App type identifier

    Returns:
        App class

    Raises:
        KeyError: If type not registered
    """
    return _app_types[type_name]


def get_all_app_types() -> Dict[str, Type[BaseApp]]:
    """Get all registered app types."""
    return _app_types.copy()


def initialize_registry(instance_manager=None) -> None:
    """
    Initialize the app registry.

    Registers all app types and syncs to database.

    Args:
        instance_manager: Optional InstanceManager for registration
    """
    # Import and register app types
    from apps.advanced_motion_lighting.app_logic import AdvancedMotionLightingApp
    register_app_type(AdvancedMotionLightingApp)

    from apps.fan_automation.app import FanAutomationApp
    register_app_type(FanAutomationApp)

    from apps.screen_time_planner.app import ScreenTimePlannerApp
    register_app_type(ScreenTimePlannerApp)

    # Power Management — avg-watts threshold cutoff for breaker-overload
    # protection (pool pumps, EV chargers, dryers, etc.). Optional
    # dry-run detection (low-threshold) gated on pump HP + rated watts.
    from apps.power_management.app import PowerManagementApp
    register_app_type(PowerManagementApp)

    # Register with instance manager if provided
    if instance_manager:
        for type_name, app_class in _app_types.items():
            instance_manager.register_app_type(type_name, app_class)

    # Sync to database
    sync_to_database()


def sync_to_database() -> None:
    """
    Sync registered app types to the app_types table.

    Creates or updates rows for each registered app type.
    """
    postgrest_url = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

    for type_name, app_class in _app_types.items():
        try:
            data = {
                'type_name': type_name,
                'display_name': app_class.DISPLAY_NAME,
                'description': app_class.DESCRIPTION,
                'version': app_class.VERSION,
                'settings_schema': app_class.get_settings_schema(),
                'device_categories': app_class.get_device_categories()
            }

            response = requests.post(
                f"{postgrest_url}/app_types?on_conflict=type_name",
                json=data,
                headers={
                    "Content-Type": "application/json",
                    "Prefer": "resolution=merge-duplicates"
                },
                timeout=10
            )

            if response.status_code in (200, 201):
                logger.info(f"Synced app type to database: {type_name}")
            else:
                logger.warning(
                    f"Failed to sync app type {type_name}: {response.text}"
                )

        except Exception as e:
            logger.error(f"Failed to sync app type {type_name}: {e}", exc_info=True)
