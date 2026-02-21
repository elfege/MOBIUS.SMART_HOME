"""
Advanced Motion Lighting Router

FastAPI APIRouter for app-specific API endpoints.
"""

from fastapi import APIRouter

# Create router
router = APIRouter(prefix="/api/apps/motion", tags=["motion-lighting"])


@router.get("/schema")
async def get_schema():
    """Get settings schema for this app type."""
    from apps.advanced_motion_lighting.app_logic import AdvancedMotionLightingApp

    return {
        "type_name": AdvancedMotionLightingApp.TYPE_NAME,
        "display_name": AdvancedMotionLightingApp.DISPLAY_NAME,
        "description": AdvancedMotionLightingApp.DESCRIPTION,
        "version": AdvancedMotionLightingApp.VERSION,
        "settings_schema": AdvancedMotionLightingApp.get_settings_schema(),
        "device_categories": AdvancedMotionLightingApp.get_device_categories(),
    }
