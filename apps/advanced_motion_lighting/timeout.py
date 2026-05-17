"""
Timeout calculation and next-run scheduling.

Groovy parity: getTimeout() — supports a default timeout and optional
per-mode overrides when timeWithMode is enabled.

2026-05-17: added a SYSTEM-LEVEL FLOOR via system_settings cascade. PIR
sensors typically have 10-60s re-trigger cooldown; setting an app timeout
below that causes off/on flicker (e.g., Kitchen Night=5s incident).
The floor (default 60s) clamps the result of this function unless the
instance has `bypassTimeoutFloor=true` in its settings.
"""


class TimeoutMixin:
    """Mixin: compute motion timeout and schedule the next master() call."""

    def _get_timeout_seconds(self) -> int:
        """
        Get the no-motion timeout in seconds for the current mode.

        Logic:
          1. Start with noMotionTime (default: 5)
          2. If timeWithMode enabled, look up modeTimeouts[current_mode]
          3. Convert to seconds using timeUnit ('seconds' or 'minutes')
          4. Clamp to the system-level floor (motion_timeout_floor_seconds)
             unless this instance has bypassTimeoutFloor=true

        Returns:
            Timeout in seconds (after floor clamp)
        """
        timeout = self.get_setting('noMotionTime', 5)
        time_unit = self.get_setting('timeUnit', 'minutes')

        if self.get_setting('timeWithMode', False):
            mode_timeouts = self.get_setting('modeTimeouts', {})
            current_mode = self._get_current_mode()
            if current_mode and current_mode in mode_timeouts:
                mode_timeout = mode_timeouts[current_mode]
                if mode_timeout is not None:
                    self.logger.debug(
                        f"Per-mode timeout for '{current_mode}': {mode_timeout} {time_unit}"
                    )
                    timeout = mode_timeout
                else:
                    self.logger.debug(
                        f"No timeout for mode '{current_mode}', using default: {timeout}"
                    )
            else:
                self.logger.debug(
                    f"Mode '{current_mode}' not in modeTimeouts, using default: {timeout}"
                )

        if time_unit == 'minutes':
            timeout *= 60

        # System-level floor enforcement (cascade tier 3). Per-FIELD exception
        # via instance_setting_exceptions (DB-backed audit-friendly opt-out)
        # OR the legacy whole-instance `bypassTimeoutFloor` flag both skip
        # the clamp. The DB exception row is the policy-preferred form.
        setting_path = (
            f"modeTimeouts.{current_mode}"
            if (self.get_setting('timeWithMode', False)
                and current_mode and current_mode in (self.get_setting('modeTimeouts', {}) or {}))
            else 'noMotionTime'
        )
        has_field_exception = self._has_setting_exception(setting_path)
        has_instance_bypass = self.get_setting('bypassTimeoutFloor', False)
        if not (has_field_exception or has_instance_bypass):
            try:
                from services.settings_resolver import get_resolver
                floor = get_resolver().get_system('motion_timeout_floor_seconds', 60)
                if isinstance(floor, (int, float)) and floor > 0 and timeout < floor:
                    self.logger.info(
                        f"_get_timeout_seconds: clamping {timeout}s → {floor}s "
                        f"(field={setting_path}, motion_timeout_floor_seconds); "
                        f"grant a per-field exception via the UI ? icon to "
                        f"disable for this specific field"
                    )
                    timeout = int(floor)
            except Exception as e:
                # Never block automation on a resolver/DB failure
                self.logger.warning(
                    f"_get_timeout_seconds: floor lookup failed, using raw "
                    f"{timeout}s: {e}"
                )

        self.logger.debug(f"_get_timeout_seconds() → {timeout}s")
        return timeout

    def _schedule_next_run(self) -> None:
        """Schedule master() to run again after the configured timeout."""
        timeout = self._get_timeout_seconds()
        self.schedule_timeout(timeout)

    # ------------------------------------------------------------------
    # Per-field exception lookup (DB-backed via instance_setting_exceptions)
    # ------------------------------------------------------------------

    # In-memory cache: setting_path → exists?. TTL'd by _exception_cache_at.
    _exception_cache: dict = {}
    _exception_cache_at: float = 0.0
    _EXCEPTION_CACHE_TTL = 5.0  # seconds

    def _has_setting_exception(self, setting_path: str) -> bool:
        """
        True iff this instance has a row in instance_setting_exceptions for
        the given setting_path. Cached for 5s per instance; cache busts on
        write (the UI POSTs to /api/instances/{id}/setting-exceptions which
        also invalidates).

        Errors return False (i.e., apply the floor) — fail closed.
        """
        import time
        cache = getattr(self, '_exception_cache', None)
        cache_at = getattr(self, '_exception_cache_at', 0.0)
        now = time.monotonic()
        if cache is None or (now - cache_at) > self._EXCEPTION_CACHE_TTL:
            try:
                import os, requests
                pg = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')
                r = requests.get(
                    f'{pg}/instance_setting_exceptions',
                    params={
                        'instance_id': f'eq.{self.instance_id}',
                        'select': 'setting_path',
                    },
                    timeout=2,
                )
                rows = r.json() if r.status_code == 200 else []
                cache = {row['setting_path']: True for row in rows}
                self._exception_cache = cache
                self._exception_cache_at = now
            except Exception:
                # Fail closed — apply the floor when in doubt.
                return False
        return bool(cache.get(setting_path))
