"""
SONOS App — scheduled alarms / announcements on a Sonos speaker
===============================================================
One instance = one alarm (multi-instance model: create several for several
alarms). At the scheduled time, on the chosen days, it plays either a
text-to-speech announcement (Anamnesis neural voice) OR a picked mp3 URL on a
Sonos room, at a chosen volume. Local UPnP via services/sonos — no Hubitat,
no cloud.

Companion to the SONOS *driver* (the /sonos controller page for live testing
of set/restore/lock volume + TTS + play). See
docs/plans/sonos_driver_and_app_db_backed_*.md.

Lifecycle (BaseApp): ``initialize`` registers the alarm crons; ``shutdown``
clears them; ``master`` fires the alarm now (the dashboard "Run" button → a
live test). Every fire path is pause-guarded [universal pause contract].
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from apps.base_app import BaseApp


class SonosApp(BaseApp):
    """Scheduled Sonos alarm/announcement app type."""

    TYPE_NAME = 'sonos'
    DISPLAY_NAME = 'Sonos Alarm'
    DESCRIPTION = ('Play a text-to-speech announcement or an mp3 on a Sonos '
                   'speaker at a scheduled time on chosen days.')

    # Day handling (mirror Screen Time Planner's convention).
    DOW = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    DOW_CRON = {'Mon': 'mon', 'Tue': 'tue', 'Wed': 'wed', 'Thu': 'thu',
                'Fri': 'fri', 'Sat': 'sat', 'Sun': 'sun'}

    # =========================================================================
    # Lifecycle
    # =========================================================================
    def initialize(self) -> None:
        """Register the alarm cron(s) for the configured time + days."""
        self.logger.info(f"Initializing Sonos Alarm: {self.label}")
        if not (self.get_setting('room', '') or '').strip():
            self.logger.warning(f"{self.label}: no Sonos room configured — alarm inert")
        self._register_alarm_jobs()

    def shutdown(self) -> None:
        """Remove our alarm crons, then base cleanup."""
        self._clear_alarm_jobs()
        super().shutdown()

    def on_event(self, event) -> None:
        """No device events drive this app (time-scheduled only)."""
        return

    def master(self, **kwargs) -> None:
        """Manual trigger (dashboard Run button) — fire the alarm now as a test."""
        self._fire_alarm()

    # =========================================================================
    # Alarm firing
    # =========================================================================
    def _fire_alarm(self) -> None:
        """Play the configured TTS or mp3 on the configured room/volume.
        Pause-guarded; never raises (an alarm must not crash the worker)."""
        if self.is_paused:
            self.logger.debug(f"{self.label}: paused — alarm suppressed")
            return
        # room holds one OR MORE speakers (comma-separated, from the multi-select).
        rooms = [r.strip() for r in (self.get_setting('room', '') or '').split(',')
                 if r.strip()]
        if not rooms:
            self.logger.warning(f"{self.label}: no room — cannot fire alarm")
            return
        volume = self.get_setting('volume', 40)
        source = self.get_setting('source', 'tts')
        try:
            import services.sonos as sonos
            if source == 'mp3':
                url = (self.get_setting('mp3Url', '') or '').strip()
                if not url:
                    self.logger.warning(f"{self.label}: source=mp3 but no mp3Url set")
                    return
                for room in rooms:
                    sonos.play(room, url, volume=volume)
                self.logger.info(f"{self.label}: alarm → mp3 on {rooms} @ {volume}")
            else:  # tts
                text = (self.get_setting('ttsText', '') or '').strip()
                if not text:
                    self.logger.warning(f"{self.label}: source=tts but no ttsText set")
                    return
                voice = self.get_setting('voice', 'edge:en-US-AvaNeural')
                for room in rooms:
                    sonos.announce(room, text, volume=volume, voice=voice)
                self.logger.info(f"{self.label}: alarm → TTS on {rooms} ({voice}) @ {volume}")
        except Exception as e:
            self.logger.error(f"{self.label}: alarm fire failed: {e}", exc_info=True)

    # =========================================================================
    # Scheduling (mirror STP's cron registration)
    # =========================================================================
    def _register_alarm_jobs(self) -> None:
        """One cron per selected day at the alarm time, all calling _fire_alarm."""
        from services.scheduler_service import get_scheduler
        sched = get_scheduler()._scheduler
        self._clear_alarm_jobs()

        parsed = self._parse_hhmm(self.get_setting('alarmTime', ''))
        if parsed is None:
            self.logger.info(f"{self.label}: no/invalid alarmTime — no crons")
            return
        hour, minute = parsed
        days = self._parse_days()
        if not days:
            self.logger.info(f"{self.label}: no days selected — no crons")
            return

        tz = self._get_timezone()
        count = 0
        for dow in days:
            job_id = f"sonos_{self.instance_id}_{dow}_{hour:02d}{minute:02d}"
            try:
                sched.add_job(
                    func=self._fire_alarm, trigger='cron',
                    day_of_week=self.DOW_CRON[dow], hour=hour, minute=minute,
                    id=job_id, replace_existing=True, timezone=tz,
                    misfire_grace_time=120,
                )
                count += 1
            except Exception as e:
                self.logger.error(f"{self.label}: failed to schedule {dow} "
                                  f"{hour:02d}:{minute:02d}: {e}", exc_info=True)
        self.logger.info(f"{self.label}: scheduled {count} alarm cron(s) "
                         f"at {hour:02d}:{minute:02d} on {days} ({tz})")

    def _clear_alarm_jobs(self) -> None:
        """Remove every cron this instance registered (matched by id prefix)."""
        try:
            from services.scheduler_service import get_scheduler
            sched = get_scheduler()._scheduler
        except Exception as e:
            self.logger.warning(f"{self.label}: scheduler unavailable for cleanup: {e}")
            return
        prefix = f"sonos_{self.instance_id}_"
        for job in list(sched.get_jobs()):
            if job.id.startswith(prefix):
                try:
                    sched.remove_job(job.id)
                except Exception:
                    pass

    # =========================================================================
    # Helpers
    # =========================================================================
    def _parse_days(self) -> list:
        """Parse the comma-separated days setting → list of valid DOW codes."""
        raw = self.get_setting('days', 'Mon,Tue,Wed,Thu,Fri,Sat,Sun') or ''
        out = []
        for piece in str(raw).split(','):
            d = piece.strip().capitalize()[:3]
            if d in self.DOW and d not in out:
                out.append(d)
        return out

    @staticmethod
    def _parse_hhmm(value: Any) -> Optional[tuple]:
        try:
            parts = str(value).split(':')
            hour, minute = int(parts[0]), int(parts[1])
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return (hour, minute)
        except (ValueError, IndexError, TypeError):
            pass
        return None

    def _get_timezone(self):
        import pytz
        name = self.get_setting('timezone', 'America/New_York') or 'America/New_York'
        try:
            return pytz.timezone(name)
        except Exception:
            return pytz.timezone('America/New_York')

    # =========================================================================
    # Schema
    # =========================================================================
    @classmethod
    def get_settings_schema(cls) -> Dict[str, Any]:
        from apps.base.pause_settings import UNIVERSAL_PAUSE_SETTINGS
        return {
            "type": "object",
            "properties": {
                **UNIVERSAL_PAUSE_SETTINGS,
                "room": {
                    "type": "string", "default": "",
                    "title": "Sonos room",
                    "description": "Speaker room name (e.g. 'Office' or 'living'). "
                                   "Matched case-insensitively; aliases supported.",
                },
                "alarmTime": {
                    "type": "string", "default": "07:00",
                    "title": "Alarm time (HH:MM, 24h)",
                    "description": "Local time the alarm fires.",
                },
                "days": {
                    "type": "string", "default": "Mon,Tue,Wed,Thu,Fri",
                    "title": "Days",
                    "description": "Comma-separated days the alarm runs "
                                   "(Mon,Tue,Wed,Thu,Fri,Sat,Sun).",
                },
                "source": {
                    "type": "string", "default": "tts",
                    "title": "Source",
                    "description": "What to play: a TTS announcement or an mp3.",
                    "enum": ["tts", "mp3"],
                    "enumNames": ["Text-to-speech", "MP3 file (URL)"],
                },
                "ttsText": {
                    "type": "string", "default": "Good morning",
                    "title": "Announcement text (TTS)",
                    "description": "Spoken when Source = Text-to-speech.",
                },
                "voice": {
                    "type": "string", "default": "edge:en-US-AvaNeural",
                    "title": "Voice",
                    "description": "Neural TTS voice (via Anamnesis). See /api/sonos/voices.",
                    "enum": [
                        "edge:en-US-AvaNeural", "edge:en-US-AriaNeural",
                        "edge:en-US-EmmaNeural", "edge:en-US-JennyNeural",
                        "edge:en-US-GuyNeural", "edge:en-US-AndrewNeural",
                        "edge:en-US-AnaNeural", "edge:en-GB-SoniaNeural",
                        "edge:fr-FR-DeniseNeural", "edge:fr-FR-HenriNeural",
                    ],
                    "enumNames": [
                        "Ava (US, warm)", "Aria (US, confident)", "Emma (US, soft)",
                        "Jenny (US, friendly)", "Guy (US, male)", "Andrew (US, male)",
                        "Ana (US, child)", "Sonia (GB)", "Denise (FR)", "Henri (FR, male)",
                    ],
                },
                "mp3Url": {
                    "type": "string", "default": "",
                    "title": "MP3 URL",
                    "description": "Played when Source = MP3. A LAN-reachable http "
                                   "URL (e.g. /static/audio/… on this server).",
                },
                "volume": {
                    "type": "integer", "minimum": 0, "maximum": 100, "default": 40,
                    "title": "Volume",
                    "description": "Alarm volume (0-100).",
                },
                "timezone": {
                    "type": "string", "default": "America/New_York",
                    "title": "Timezone",
                    "description": "IANA timezone the alarm time is interpreted in.",
                },
            },
        }

    @classmethod
    def get_device_categories(cls) -> List[Dict[str, Any]]:
        """No Hubitat devices — Sonos speakers are addressed by room name."""
        return []
