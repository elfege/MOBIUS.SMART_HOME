"""
Activity timestamp tracking.

Updates last_activity_at in PostgreSQL whenever the app processes
a meaningful device event. Used by the dashboard to display
"last seen" information per instance.
"""

import requests
from datetime import datetime


class ActivityMixin:
    """Mixin: record last automation activity timestamp in the database."""

    def update_last_activity(self) -> None:
        """
        Stamp this instance's last_activity_at with the current time.

        Fires a PATCH to PostgREST. Failure is non-fatal — logged as
        a warning, never raised.
        """
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
