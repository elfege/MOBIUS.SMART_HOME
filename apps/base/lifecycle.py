"""
Instance lifecycle hooks.

Default implementations for shutdown and location mode changes.
Subclasses override on_mode_change() to add exclusion-mode logic,
per-mode timeout recalculation, or other custom behavior.
"""


class LifecycleMixin:
    """Mixin: instance lifecycle — shutdown and mode-change handling."""

    def shutdown(self) -> None:
        """
        Cancel all scheduled jobs for this instance.

        Called when the instance is stopped or deleted.
        Subclasses can override to release additional resources.
        """
        from services.scheduler_service import get_scheduler
        scheduler = get_scheduler()
        scheduler.cancel_for_instance(self.instance_id)
        self.logger.info(f"Shutdown instance: {self.label}")

    def on_mode_change(self, new_mode: str) -> None:
        """
        Handle a Hubitat location mode change.

        Default behavior:
          1. Reset memoization (clears all manual overrides)
          2. Re-evaluate state via master()

        Subclasses (e.g., AdvancedMotionLightingApp) override this to add
        exclusion-mode auto-pause/resume logic before calling super().

        Args:
            new_mode: Name of the new active mode (e.g., 'Home', 'Away')
        """
        self.logger.info(f"Mode changed to: {new_mode}")
        self._reset_memoization()
        self.master()
