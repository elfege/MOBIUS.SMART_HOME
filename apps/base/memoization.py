"""
Memoization state management.

Provides get/set/reset for per-instance memoization. State is persisted
to PostgreSQL via instance_manager so it survives container restarts.

Overrides are cleared on mode changes, pause, and resume — never on motion.
"""

from typing import Any


class MemoizationMixin:
    """Mixin: persistent key-value memo store, saved to PostgreSQL."""

    def get_memo(self, key: str, default: Any = None) -> Any:
        """Get a memoized value by key."""
        return self._memoization.get(key, default)

    def set_memo(self, key: str, value: Any) -> None:
        """Set a memoized value. Does not auto-save; caller must call _save_memoization()."""
        self._memoization[key] = value

    def _reset_memoization(self) -> None:
        """
        Clear all memoization state and persist the empty state.

        Called on: mode change, pause, resume.
        NOT called on motion events — overrides survive until one of the above.
        """
        self._memoization = {}
        self._save_memoization()

    def _save_memoization(self) -> None:
        """Persist current memoization state to the database."""
        try:
            self.instance_manager.update_memoization(
                self.instance_id,
                self._memoization
            )
        except Exception as e:
            self.logger.error(
                f"Failed to save memoization for instance {self.instance_id}: {e}",
                exc_info=True
            )
