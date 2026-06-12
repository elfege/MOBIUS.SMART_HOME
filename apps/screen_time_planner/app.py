"""
Screen Time Planner (v2 — allowed windows + real-time enforcement)
=================================================================
.. note:: See docs/plans/screen_time_planner_v2_allowed_windows_with_realtime_enforcement_and_power_on_init_suppression.md

Manages kids' (or anyone's) TV time by ALLOWED WINDOWS rather than a single
nightly cutoff. The TV is permitted only inside open windows; turning it on
outside a window is enforced in real time (the app immediately cuts it).

Device roles
------------
- primary_switch  : the TV. Watched (events) and cut when not allowed.
- secondary_switch: the "power device" (master power to the TV). Powered ON at a
  window's start so the TV becomes *available*; cut after the TV at a window's
  close / on enforcement. Optional — without it, enforcement still cuts the TV.

Behavior (per confirmed design)
-------------------------------
- Each day has a LIST of windows [{start,end}] (HH:MM), per-day differentiable
  with a "same every day" toggle. A window ending at midnight means end-of-day;
  a cross-midnight window (start > end) is auto-split into an evening piece today
  + a morning piece tomorrow (see _effective_windows).
- Window START  -> power the secondary ON (TV available). Many TVs boot to ON on
  power restore; that is NOT user intent, so for `suppressTvWakeOnPowerSeconds`
  after a power restore, a TV-ON is turned back off (suppression).
- Window END    -> cutoff: TV off (optionally confirmed within
  `offConfirmTimeoutSeconds`) then power off.
- OUTSIDE any window, TV turned on -> immediate full cutoff (+ Sonos warning,
  stubbed). On startup/resume/mode-change -> re-evaluate and enforce.

Control flow
------------
``master()`` is the reconciler and is safe to call from any lifecycle hook
(init / resume / on_mode_change / a window-boundary cron):
    in a window  -> ensure power ON
    not in a window -> run the cutoff
``on_event()`` watches ONLY the primary switch turning ON and decides allow /
suppress / enforce. We act only on TV-ON, so our own OFF commands (and their
echo events) never create an enforcement loop.

Scheduling
----------
One cron per distinct window boundary per day (start AND end), each calling
``master()``. Cron jobs are added straight to APScheduler with ids prefixed
``stp_<instance_id>_`` and removed by that prefix in ``shutdown()`` (the base
shutdown only cancels SchedulerService-tracked jobs).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from apps.base_app import BaseApp
from models.event import DeviceEvent

logger = logging.getLogger(__name__)

# ANSI colors for log readability.
_C = "\033[96m"   # cyan — device names
_Y = "\033[93m"   # yellow — decisions
_G = "\033[92m"   # green — on/off success
_R = "\033[0m"    # reset


class ScreenTimePlannerApp(BaseApp):
    """Allow the TV only inside scheduled windows; enforce in real time."""

    TYPE_NAME    = 'screen_time_planner'
    DISPLAY_NAME = 'Screen Time Planner'
    DESCRIPTION  = ('Allow the TV only inside daily time windows; turning it on '
                    'outside a window is cut immediately')
    VERSION      = '2.0.0'

    DOW: List[str] = [
        'monday', 'tuesday', 'wednesday', 'thursday',
        'friday', 'saturday', 'sunday',
    ]
    DOW_CRON: Dict[str, str] = {
        'monday': 'mon', 'tuesday': 'tue', 'wednesday': 'wed', 'thursday': 'thu',
        'friday': 'fri', 'saturday': 'sat', 'sunday': 'sun',
    }

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def initialize(self) -> None:
        """Register window-boundary crons, then reconcile current state once."""
        self.logger.info(f"Initializing Screen Time Planner: {self.label}")
        if not self.get_devices('primary_switch'):
            self.logger.warning("No primary (TV) switch selected — nothing to control")
        self._register_schedule_jobs()
        # Reconcile now: if we're outside a window, enforce (cut the TV); if
        # inside, make sure power is available. Safe at init.
        self.master()

    def shutdown(self) -> None:
        """Remove our boundary crons, then run base cleanup (timeout jobs)."""
        self._clear_schedule_jobs()
        super().shutdown()

    def on_mode_change(self, new_mode: str) -> None:
        """A Hubitat mode change just re-reconciles (windows are time-based)."""
        self.logger.debug(f"Mode changed to {new_mode} — re-evaluating window state")
        self.master()

    # =========================================================================
    # Event enforcement (primary TV switch only)
    # =========================================================================

    def on_event(self, event: DeviceEvent) -> None:
        """
        React to the TV (primary) turning ON:
          - inside a window, just after a power restore -> suppress (turn off);
          - inside a window otherwise -> allow (user intent);
          - outside any window -> enforce full cutoff (+ Sonos warning, stubbed).

        We deliberately act only on TV-ON. OFF events (including the echo of our
        own off-commands) are ignored, so enforcement can't loop. Secondary
        (power) device events are ignored here too.
        """
        try:
            if self.is_paused or not self._has_any_windows():
                return
            primary_ids = {str(p) for p in self.get_devices('primary_switch')}
            if str(event.device_id) not in primary_ids:
                return
            if str(event.value).lower() != 'on':
                return

            self.update_last_activity()

            if self._in_window_now():
                if self._within_suppression():
                    self.logger.info(
                        f"{self.label}: TV woke on power restore — suppressing "
                        f"(off)"
                    )
                    self._turn_off_primary_only()
                # else: allowed window + user turned it on → allow, do nothing
            else:
                self.logger.info(
                    f"{self.label}: {_Y}TV turned on outside allowed window — "
                    f"enforcing cutoff{_R}"
                )
                self._notify_blocked()
                self._execute_shutoff()
        except Exception as e:
            self.logger.error(f"{self.label}: on_event failed: {e}", exc_info=True)

    # =========================================================================
    # Master reconciler (also the cron target for every window boundary)
    # =========================================================================

    def master(self, **kwargs) -> None:
        """
        Reconcile to the current window state. Safe from any lifecycle hook:
          inside a window  -> ensure the power device is ON (TV available);
          outside a window -> run the cutoff (TV off, then power off).
        """
        try:
            if self.is_paused or not self._has_any_windows():
                return  # unconfigured (no windows) → inert; never auto-cut
            if self._in_window_now():
                self._ensure_power_on()
            else:
                self._execute_shutoff()
        except Exception as e:
            self.logger.error(f"{self.label}: master() failed: {e}", exc_info=True)

    # =========================================================================
    # Window model
    # =========================================================================

    def _effective_windows(self) -> Dict[str, List[Dict[str, str]]]:
        """
        Collapse the weeklyWindows setting into {dow: [{start,end}, ...]}.

        Endpoint + cross-midnight handling:
          - A midnight END ("00:00" from the time picker = 12:00 AM) means END OF
            DAY → stored as "24:00", NOT 00:00 (which is < start and would drop
            the window). So "4:00 PM → 12:00 AM" is 16:00 → 24:00.
          - A CROSS-MIDNIGHT window (start > end, e.g. 22:00 → 02:00) is AUTO-SPLIT
            into two within-day pieces: an evening piece {start → 24:00} on the
            day itself, plus a morning piece {00:00 → end} on the NEXT day — so a
            "Monday 22:00 → 02:00" correctly allows Tuesday 00:00–02:00, not
            Monday's early hours. For a uniform schedule every day carries both,
            so the result is the same either way.
        Malformed and zero-length (start == end) entries are dropped. Each day's
        list is sorted by start time for determinism.
        """
        ww = self.get_setting('weeklyWindows', {}) or {}
        uniform = ww.get('uniform', True)
        uniform_windows = ww.get('uniformWindows', []) or []
        days = ww.get('days', {}) or {}

        out: Dict[str, List[Dict[str, str]]] = {dow: [] for dow in self.DOW}
        for i, dow in enumerate(self.DOW):
            raw = uniform_windows if uniform else (days.get(dow, []) or [])
            nxt = self.DOW[(i + 1) % 7]
            for w in raw:
                start = (w or {}).get('start')
                end = (w or {}).get('end')
                if self._parse_hhmm(start) is None:
                    continue
                if self._parse_hhmm(end) == (0, 0):
                    end = '24:00'  # midnight end → end of day
                if end != '24:00' and self._parse_hhmm(end) is None:
                    continue
                if str(start) < str(end):                 # within-day (incl. → 24:00)
                    out[dow].append({'start': start, 'end': end})
                elif str(start) > str(end):               # cross-midnight → split
                    out[dow].append({'start': start, 'end': '24:00'})
                    out[nxt].append({'start': '00:00', 'end': end})
                # start == end → zero-length, skip
        for dow in out:
            out[dow].sort(key=lambda w: w['start'])
        return out

    def _windows_contain(self, dow: str, hhmm: str) -> bool:
        """True if HH:MM falls in any window for `dow` ([start, end) — end-exclusive
        so a window-close boundary reads as OUTSIDE)."""
        for w in self._effective_windows().get(dow, []):
            if w['start'] <= hhmm < w['end']:
                return True
        return False

    def _in_window_now(self) -> bool:
        """True if 'now' (in the instance timezone) is inside an allowed window."""
        tz = self._get_timezone()
        now = datetime.now(tz)
        return self._windows_contain(self.DOW[now.weekday()], now.strftime('%H:%M'))

    def _has_any_windows(self) -> bool:
        """True if at least one window is configured on any day. When False the
        instance is treated as UNCONFIGURED and does nothing — so a half-set-up
        (or freshly migrated V1) instance never enforces / cuts anything."""
        return any(self._effective_windows().values())

    # =========================================================================
    # Power management + suppression
    # =========================================================================

    def _ensure_power_on(self) -> None:
        """
        Turn the secondary 'power device' ON if it isn't already. On an actual
        off->on restore, stamp the suppression timer so a TV that boots-to-ON on
        power is caught by on_event and turned back off.

        Pause guard (defensive): every action-issuing method MUST check
        is_paused at its own top, not rely on the caller's check. Pause
        state can flip between the caller's check and our send_command
        if pause arrives mid-tick — the only way to guarantee no rule
        executes while paused is to re-check here too.
        """
        if self.is_paused:
            return
        for sid in self.get_devices('secondary_switch'):
            if self._read_switch(sid) == 'on':
                continue
            result = self.send_command(sid, 'on', verify=True)
            if result.success:
                self._runtime.power_restored_at = time.monotonic()
                self.logger.info(
                    f"{self.label}: power device {_C}{sid}{_R} → {_G}on{_R} "
                    f"(TV available; suppression armed)"
                )
                # Lag workaround: device state right after a command is the
                # driver's optimistic echo, not the real callback. Don't poll
                # immediately — schedule a single re-check halfway through the
                # suppression window so a wake-on-power has time to actually
                # register on the eventsocket-backed cache.
                self._schedule_wake_suppression_check()
            else:
                self.logger.warning(
                    f"{self.label}: power device {sid} on not confirmed: {result.error}"
                )

    def _within_suppression(self) -> bool:
        """True if we're within `suppressTvWakeOnPowerSeconds` of a power restore."""
        secs = int(self.get_setting('suppressTvWakeOnPowerSeconds', 30) or 0)
        if secs <= 0:
            return False
        stamp = getattr(self._runtime, 'power_restored_at', None)
        if stamp is None:
            return False
        return (time.monotonic() - stamp) < secs

    def _turn_off_primary_only(self) -> None:
        """Cut the TV (primary) only — used for wake-on-power suppression inside a
        window (we keep power ON so the TV stays *available*).

        Pause guard (defensive): see _ensure_power_on docstring.
        """
        if self.is_paused:
            return
        for pid in self.get_devices('primary_switch'):
            self.send_command(pid, 'off', verify=False)

    def _schedule_wake_suppression_check(self) -> None:
        """
        Schedule a one-shot wake-on-power re-check at HALF the suppression window
        (`suppressTvWakeOnPowerSeconds` / 2). Belt-and-braces alongside the
        event-driven path in on_event: if the TV's wake report is laggy or the
        event is missed, this proactively reads the settled state and cuts it.
        Off when the setting is 0.
        """
        secs = int(self.get_setting('suppressTvWakeOnPowerSeconds', 30) or 0)
        if secs <= 0:
            return
        try:
            from services.scheduler_service import get_scheduler
            get_scheduler().schedule_once(
                job_id=f"stp_{self.instance_id}_suppress",
                delay_seconds=max(1, secs // 2),
                callback=lambda **kw: self._suppress_wake_check(),
                instance_id=self.instance_id,
                job_type='timeout',
            )
        except Exception as e:
            self.logger.warning(f"{self.label}: could not schedule wake check: {e}")

    def _suppress_wake_check(self, **kwargs) -> None:
        """
        Fired N/2 after a power restore. Reads the TV's REAL (eventsocket-backed
        cache) state — not the optimistic command echo — and turns it off if it
        woke on power. Skips if paused or the window has since closed.
        """
        try:
            if self.is_paused or not self._in_window_now():
                return
            for pid in self.get_devices('primary_switch'):
                if self._read_switch(pid) == 'on':
                    self.logger.info(
                        f"{self.label}: TV woke on power (N/2 re-check) — "
                        f"suppressing (off)"
                    )
                    self.send_command(pid, 'off', verify=False)
        except Exception as e:
            self.logger.error(f"{self.label}: _suppress_wake_check failed: {e}", exc_info=True)

    # =========================================================================
    # Cutoff sequence (window close + enforcement)
    # =========================================================================

    def _execute_shutoff(self, **kwargs) -> None:
        """
        primary (TV) off → [confirm within timeout] → secondary (power) off.

        Runs in a scheduler/worker thread or inline from on_event;
        ``send_command`` is synchronous so blocking here is safe.
        """
        if self.is_paused:
            return

        await_off = bool(self.get_setting('awaitPrimaryOff', True))
        timeout = int(self.get_setting('offConfirmTimeoutSeconds', 20) or 20)

        primary_ids = self.get_devices('primary_switch')
        if not primary_ids:
            self.logger.warning(f"{self.label}: cutoff with no primary switch — nothing to do")
            return

        primary_ok = True
        for pid in primary_ids:
            if await_off:
                ok = self._confirm_off(pid, timeout)
            else:
                result = self.send_command(pid, 'off', verify=False)
                ok = bool(result.success)
            primary_ok = primary_ok and ok
            self.logger.info(
                f"{self.label}: TV {_C}{pid}{_R} → {_G}off{_R}"
                f"{' (confirmed)' if (await_off and ok) else ''}"
                f"{'' if ok else ' [NOT CONFIRMED]'}"
            )

        secondary_ids = self.get_devices('secondary_switch')
        if not secondary_ids:
            return

        unconditional = bool(self.get_setting('secondaryUnconditional', True))
        if not unconditional and not primary_ok:
            self.logger.info(
                f"{self.label}: power left ON — TV not confirmed off and "
                f"'always cut power' is disabled"
            )
            return

        # The manual delay only applies when we are NOT waiting for confirmation,
        # OR when we cut unconditionally (mirrors the UI's show/hide rule).
        use_delay = (not await_off) or unconditional
        delay = int(self.get_setting('secondaryDelaySeconds', 0) or 0) if use_delay else 0
        if delay > 0:
            self.schedule_timeout(delay, callback_name='_turn_off_secondary')
            self.logger.info(f"{self.label}: power off scheduled in {_Y}{delay}s{_R}")
        else:
            self._turn_off_secondary()

    def _confirm_off(self, device_id, timeout_s: int) -> bool:
        """
        Confirm the device is REALLY off, working around the driver's optimistic
        state echo (it reports 'off' the instant it receives the command, before
        the device's real callback lands). So instead of trusting
        ``send_command(verify=True)`` — which would report verified-off on the
        first iteration and make the timeout meaningless — we send OFF once, then
        poll the EVENTSOCKET-BACKED cache (the device's real reported state) with
        a settle delay until it reads 'off' or `timeout_s` elapses.

        Returns True only on a genuine reported off. NOTE: if this device never
        pushes a real state over the eventsocket (only the optimistic echo), no
        real 'off' will arrive and this returns False at the timeout.

        Pause guard (defensive): see _ensure_power_on docstring. We
        also re-check inside the poll loop so a pause that lands
        mid-loop short-circuits the remaining sleeps.
        """
        if self.is_paused:
            return False
        self.send_command(device_id, 'off', verify=False)
        deadline = time.monotonic() + max(1, int(timeout_s))
        step = min(2.0, max(1.0, timeout_s / 10.0))
        while time.monotonic() < deadline:
            time.sleep(step)
            if self.is_paused:
                return False
            if self._read_switch(device_id) == 'off':
                return True
        return False

    def _turn_off_secondary(self, **kwargs) -> None:
        """Turn the secondary 'power device' off. Inline or via the delay timer."""
        try:
            self._runtime.timeout_job_id = None
            if self.is_paused:
                return
            for sid in self.get_devices('secondary_switch'):
                result = self.send_command(sid, 'off', verify=True)
                if result.success:
                    self.logger.info(f"{self.label}: power {_C}{sid}{_R} → {_G}off{_R}")
                else:
                    self.logger.warning(
                        f"{self.label}: power {sid} off failed: {result.error}"
                    )
        except Exception as e:
            self.logger.error(f"{self.label}: _turn_off_secondary failed: {e}", exc_info=True)

    def _notify_blocked(self) -> None:
        """
        Hook for the future Sonos warning when the TV is blocked outside its
        window. The Sonos control layer does not exist yet — TODO. For now this
        only logs, so the integration point is in place.
        """
        self.logger.debug(
            f"{self.label}: [blocked] would announce a warning on Sonos "
            f"(Sonos control layer TODO)"
        )

    # =========================================================================
    # Schedule registration
    # =========================================================================

    def _register_schedule_jobs(self) -> None:
        """One cron per distinct window boundary (start AND end) per day, each
        calling master()."""
        from services.scheduler_service import get_scheduler

        sched = get_scheduler()._scheduler
        self._clear_schedule_jobs()

        tz = self._get_timezone()
        eff = self._effective_windows()
        boundaries = set()  # set of (dow, 'HH:MM')
        for dow, wins in eff.items():
            for w in wins:
                boundaries.add((dow, w['start']))
                if w['end'] == '24:00':
                    # Close is at midnight = 00:00 of the NEXT day; register the
                    # reconcile cron there (a same-day 00:00 is the wrong edge).
                    # master() re-evaluates and cuts if no window then covers it.
                    nxt = self.DOW[(self.DOW.index(dow) + 1) % 7]
                    boundaries.add((nxt, '00:00'))
                else:
                    boundaries.add((dow, w['end']))

        if not boundaries:
            self.logger.info(f"{self.label}: no window boundaries configured")
            return

        for dow, hhmm in sorted(boundaries):
            parsed = self._parse_hhmm(hhmm)
            if parsed is None:
                continue
            hour, minute = parsed
            job_id = f"stp_{self.instance_id}_{dow}_{hour:02d}{minute:02d}"
            try:
                sched.add_job(
                    func=self.master,
                    trigger='cron',
                    day_of_week=self.DOW_CRON[dow],
                    hour=hour,
                    minute=minute,
                    id=job_id,
                    replace_existing=True,
                    timezone=tz,
                    misfire_grace_time=3600,
                )
            except Exception as e:
                self.logger.error(
                    f"{self.label}: failed to schedule boundary {dow} {hhmm}: {e}",
                    exc_info=True,
                )
        self.logger.info(
            f"{self.label}: scheduled {len(boundaries)} window boundary job(s) ({tz})"
        )

    def _clear_schedule_jobs(self) -> None:
        """Remove every cron this instance registered (matched by id prefix)."""
        try:
            from services.scheduler_service import get_scheduler
            sched = get_scheduler()._scheduler
        except Exception as e:
            self.logger.warning(f"{self.label}: scheduler unavailable for cleanup: {e}")
            return
        prefix = f"stp_{self.instance_id}_"
        for job in list(sched.get_jobs()):
            if job.id.startswith(prefix):
                try:
                    sched.remove_job(job.id)
                except Exception:
                    pass

    # =========================================================================
    # Small helpers
    # =========================================================================

    @staticmethod
    def _parse_hhmm(value: Any) -> Optional[tuple]:
        """Parse 'HH:MM' → (hour, minute), or None if malformed / out of range."""
        try:
            parts = str(value).split(':')
            hour, minute = int(parts[0]), int(parts[1])
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return (hour, minute)
        except (ValueError, IndexError, TypeError):
            pass
        return None

    def _read_switch(self, device_id) -> Optional[str]:
        """Cached 'on'/'off' for a switch, or None if unknown."""
        state = self.get_device_state(device_id) or {}
        attrs = state.get('attributes', {}) or {}
        v = attrs.get('switch')
        return v if v in ('on', 'off') else None

    def _get_timezone(self):
        """Resolve the instance timezone setting to a pytz tz (with fallback)."""
        import pytz
        name = self.get_setting('timezone', 'America/New_York') or 'America/New_York'
        try:
            return pytz.timezone(name)
        except Exception:
            self.logger.warning(
                f"{self.label}: unknown timezone '{name}', using America/New_York"
            )
            return pytz.timezone('America/New_York')

    # =========================================================================
    # Schema
    # =========================================================================

    @classmethod
    def get_settings_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # Custom per-day windows widget (see instance-controller.js
                # weeklyWindows case).
                "weeklyWindows": {
                    "type": "object",
                    "title": "Allowed Windows",
                    "description": (
                        "When the TV is ALLOWED each day. Outside these windows the "
                        "app turns the TV off immediately. Turn off 'same windows "
                        "every day' to set different windows per day."
                    ),
                    "default": {
                        "uniform": True,
                        "uniformWindows": [{"start": "08:00", "end": "20:30"}],
                        "days": {dow: [] for dow in cls.DOW},
                    },
                },
                "awaitPrimaryOff": {
                    "type": "boolean", "default": True,
                    "title": "Confirm the TV is off",
                    "description": (
                        "Verify the TV actually turned off (polling up to the "
                        "confirmation timeout) before cutting the power device."
                    ),
                },
                "offConfirmTimeoutSeconds": {
                    "type": "integer", "minimum": 1, "maximum": 600, "default": 20,
                    "title": "Off-confirmation timeout (s)",
                    "description": (
                        "How long to wait for the TV to report off before giving up. "
                        "Used when 'Confirm the TV is off' is on."
                    ),
                },
                "secondaryUnconditional": {
                    "type": "boolean", "default": True,
                    "title": "Always cut the power device",
                    "description": (
                        "Cut the power device even if the TV could not be confirmed "
                        "off. On by default."
                    ),
                },
                "secondaryDelaySeconds": {
                    "type": "integer", "minimum": 0, "maximum": 3600, "default": 0,
                    "title": "Delay before power device (s)",
                    "description": (
                        "Seconds to wait after the TV turns off before cutting the "
                        "power device. 0 = immediately."
                    ),
                },
                "suppressTvWakeOnPowerSeconds": {
                    "type": "integer", "minimum": 0, "maximum": 300, "default": 30,
                    "title": "Suppress TV wake-on-power (s)",
                    "description": (
                        "Some TVs power themselves on when mains power returns. For "
                        "this many seconds after a window opens (power restored), "
                        "turn the TV back off. 0 disables."
                    ),
                },
                "timezone": {
                    "type": "string", "default": "America/New_York",
                    "title": "Timezone",
                    "description": "IANA timezone the window times are interpreted in.",
                },
            },
        }

    @classmethod
    def get_device_categories(cls) -> List[Dict[str, Any]]:
        return [
            {
                "key": "primary_switch", "label": "TV / Primary Switch",
                "capability": "Switch",
                "multiple": False, "required": True,
                "description": "The TV switch — watched and cut when outside an allowed window.",
            },
            {
                "key": "secondary_switch", "label": "Power Device (optional)",
                "capability": "Switch",
                "multiple": False, "required": False,
                "description": (
                    "Optional master power to the TV — turned ON at a window's "
                    "start to make the TV available, cut at the window's close."
                ),
            },
        ]
