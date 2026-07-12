"""
AnnounceService — feature service #1 on the Sonos facade
========================================================
Plays a spoken (or chimed) announcement on a Sonos speaker and restores whatever
was playing afterward. This is the first of several feature services that build
on the central ``Sonos`` facade; it owns NO UPnP knowledge — it composes
discovery + coordinator resolution + snapshot/restore from the facade and clip
generation from ClipFactory.

The full play->restore loop was validated on real hardware 2026-06-20.

Consumers:
    - Screen Time Planner: advance warnings + cutoff announcement.
    - Rules app (future): replace the misfiring HE-Rule-Machine "Water is on".
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass

from ._soap import SonosSoapError
from .sonos import Sonos
from .tts import ClipFactory

logger = logging.getLogger(__name__)


@dataclass
class AnnounceResult:
    ok: bool
    room: str
    ip: str | None = None
    clip_url: str | None = None
    error: str | None = None


class AnnounceService:
    """Announce text on a Sonos speaker with snapshot/restore + debounce."""

    def __init__(self, sonos: Sonos, clips: ClipFactory,
                 *, min_interval: float = 3.0, max_clip_wait: float = 15.0,
                 start_grace: float = 5.0) -> None:
        self.sonos = sonos
        self.clips = clips
        self._min_interval = min_interval      # coalesce repeat announces per speaker
        self._max_clip_wait = max_clip_wait    # hard cap waiting for a clip to finish
        self._start_grace = start_grace        # give up if PLAYING never observed by now
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}
        self._guard = threading.Lock()

    # ---- public API ------------------------------------------------------
    def announce(self, room: str, text: str, *, volume: int | None = None,
                 voice: str | None = None) -> AnnounceResult:
        """Blocking announce: resolve -> snapshot -> play clip -> restore.

        ``voice`` selects the TTS voice (e.g. ``edge:en-US-AvaNeural``); None uses
        the ClipFactory default. Debounced per resolved speaker: a second call
        within ``min_interval`` (or while one is already playing on that speaker)
        is coalesced (dropped) so overlapping schedules don't stack.
        """
        ip = self.sonos.target_ip(room)
        if not ip:
            logger.warning("Sonos announce: could not resolve speaker for %r", room)
            return AnnounceResult(False, room, error="speaker not found")

        lock = self._lock_for(ip)
        if not lock.acquire(blocking=False):
            logger.info("Sonos announce: %s busy, coalescing %r", room, text)
            return AnnounceResult(False, room, ip, error="busy")
        try:
            now = time.monotonic()
            if now - self._last.get(ip, 0.0) < self._min_interval:
                return AnnounceResult(False, room, ip, error="debounced")

            clip_url = self.clips.clip_url(text, voice)
            if not clip_url:
                return AnnounceResult(False, room, ip, error="no clip / no TTS backend")

            try:
                self._play_with_restore(ip, clip_url, volume)
            except SonosSoapError as e:
                logger.error("Sonos announce on %s failed: %s", ip, e)
                return AnnounceResult(False, room, ip, clip_url, error=str(e))

            self._last[ip] = time.monotonic()
            logger.info("Sonos announce on %s (%s): %r", room, ip, text)
            return AnnounceResult(True, room, ip, clip_url)
        finally:
            lock.release()

    def announce_in_background(self, room: str, text: str, *, volume: int | None = None,
                               voice: str | None = None) -> None:
        """Fire-and-forget: run :meth:`announce` on a daemon thread so a caller
        (e.g. a scheduler job) isn't blocked for the clip duration."""
        threading.Thread(
            target=self.announce, args=(room, text),
            kwargs={"volume": volume, "voice": voice},
            name="sonos-announce", daemon=True,
        ).start()

    async def announce_async(self, room: str, text: str, *, volume: int | None = None,
                             voice: str | None = None) -> AnnounceResult:
        """Await an announce from async code without blocking the event loop."""
        return await asyncio.to_thread(self.announce, room, text, volume=volume, voice=voice)

    # ---- internals -------------------------------------------------------
    def _play_with_restore(self, ip: str, clip_url: str, volume: int | None) -> None:
        snap = self.sonos.snapshot(ip)
        try:
            if volume is not None:
                self.sonos.set_volume(ip, volume)
            self.sonos.set_uri(ip, clip_url)
            self.sonos.play(ip)
            self._wait_until_done(ip)
        finally:
            # Always attempt restore, even if play/poll raised mid-way.
            self.sonos.restore(snap)

    def _wait_until_done(self, ip: str) -> None:
        """Poll transport state until the clip stops (or the hard cap elapses).

        Waits for PLAYING to be observed first (the speaker briefly reports
        TRANSITIONING/STOPPED right after Play) so we don't return before the
        clip has actually started.
        """
        start = time.monotonic()
        deadline = start + self._max_clip_wait
        started = False
        while time.monotonic() < deadline:
            try:
                state = self.sonos.transport_state(ip)
            except SonosSoapError:
                return
            if state == "PLAYING":
                started = True
            elif started and state in ("STOPPED", "PAUSED_PLAYBACK"):
                return
            # If the clip never reached PLAYING within the start grace, it failed
            # to start (bad URL / unreachable) — don't block the full window.
            elif not started and (time.monotonic() - start) > self._start_grace:
                return
            time.sleep(0.4)

    def _lock_for(self, ip: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(ip, threading.Lock())
