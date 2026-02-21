"""
Instance Models

Data models for app instances.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class AppInstanceCreate(BaseModel):
    """Model for creating a new app instance."""

    app_type: str
    label: str
    device_selections: Dict[str, List[str]]
    settings: Dict[str, Any] = {}


class AppInstanceUpdate(BaseModel):
    """Model for updating an app instance."""

    label: Optional[str] = None
    device_selections: Optional[Dict[str, List[str]]] = None
    settings: Optional[Dict[str, Any]] = None


class AppInstanceResponse(BaseModel):
    """Model for app instance API responses."""

    id: int
    instance_uuid: str
    app_type_id: int
    app_type_name: Optional[str] = None
    label: str
    settings: Dict[str, Any]
    device_selections: Dict[str, List[str]]
    is_paused: bool
    pause_expires_at: Optional[datetime] = None
    pause_reason: Optional[str] = None
    memoization_state: Dict[str, Any]
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass
class RuntimeInstanceState:
    """
    Runtime state for an app instance.

    This is the in-memory state that doesn't need to survive restarts.
    Persistent state should be in the database (memoization_state column).

    Attributes:
        last_motion_time: Timestamp of last motion active event
        last_switch_control: Timestamp of last switch command sent
        functional_sensors: Map of sensor IDs to their functional status
        timeout_job_id: ID of the scheduled timeout job
    """

    last_motion_time: Optional[datetime] = None
    last_switch_control: Optional[datetime] = None
    functional_sensors: Dict[str, bool] = field(default_factory=dict)
    timeout_job_id: Optional[str] = None
    health_check_job_id: Optional[str] = None
    auto_resume_job_id: Optional[str] = None

    def is_motion_recent(self, threshold_seconds: int) -> bool:
        """Check if motion was detected within threshold."""
        if not self.last_motion_time:
            return False
        age = (datetime.now() - self.last_motion_time).total_seconds()
        return age < threshold_seconds
