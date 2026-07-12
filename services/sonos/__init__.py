"""
services.sonos — local Sonos control subsystem (LAN-only, no cloud, no Hubitat)
==============================================================================
First citizen of the ``services/`` subpackage convention (see
docs/plans/reorganize_flat_services_directory_*.md). Public surface:

    from services.sonos import announce, get_sonos, get_announce_service

    announce("Office", "Screen time is over", volume=35)   # fire-and-forget

Layout:
    sonos.py        central facade: Sonos(DiscoveryMixin, TransportMixin,
                    RenderingMixin, TopologyMixin, SnapshotMixin)
    _soap.py        UPnP SOAP-over-HTTP primitive (shared by the mixins)
    _discovery / _transport / _rendering / _topology / _snapshot   capability mixins
    tts.py          ClipFactory — text -> local mp3 URL (canonical / generated / chime)
    announce.py     AnnounceService — feature service #1 (play + restore)
    routes.py       FastAPI clip-serving + operator test endpoints

Singletons are lazy so importing this package is cheap (no SSDP at import time).
"""

from __future__ import annotations

import threading

from .announce import AnnounceResult, AnnounceService
from .sonos import Sonos
from .tts import ClipFactory

__all__ = [
    "Sonos", "AnnounceService", "AnnounceResult", "ClipFactory",
    "get_sonos", "get_clip_factory", "get_announce_service", "announce",
]

# RLock, not Lock: get_announce_service() holds this while calling
# get_sonos()/get_clip_factory(), which re-acquire it — a plain Lock deadlocks
# (non-reentrant). RLock allows the same thread to re-enter.
_lock = threading.RLock()
_sonos: Sonos | None = None
_clips: ClipFactory | None = None
_announce: AnnounceService | None = None


def get_sonos() -> Sonos:
    """Process-wide Sonos facade (lazy; discovery happens on first use)."""
    global _sonos
    if _sonos is None:
        with _lock:
            if _sonos is None:
                _sonos = Sonos()
    return _sonos


def get_clip_factory() -> ClipFactory:
    """Process-wide ClipFactory."""
    global _clips
    if _clips is None:
        with _lock:
            if _clips is None:
                _clips = ClipFactory()
    return _clips


def get_announce_service() -> AnnounceService:
    """Process-wide AnnounceService bound to the singleton facade + clips."""
    global _announce
    if _announce is None:
        with _lock:
            if _announce is None:
                _announce = AnnounceService(get_sonos(), get_clip_factory())
    return _announce


def announce(room: str, text: str, *, volume: int | None = None,
             voice: str | None = None) -> None:
    """Convenience fire-and-forget announcement (does not block the caller).
    ``voice`` e.g. ``edge:en-US-AvaNeural``; None uses the ClipFactory default."""
    get_announce_service().announce_in_background(room, text, volume=volume, voice=voice)


def play(room: str, url: str, *, volume: int | None = None) -> None:
    """Fire-and-forget: play an mp3 URL on a speaker (no snapshot/restore — for
    alarms / 'play this now'). Resolves room→coordinator; silent on failure."""
    import threading

    def _do():
        s = get_sonos()
        ip = s.target_ip(room)
        if not ip:
            return
        try:
            if volume is not None:
                s.set_volume(ip, volume)
            s.set_uri(ip, url)
            s.play(ip)
        except Exception:
            pass

    threading.Thread(target=_do, name="sonos-play", daemon=True).start()


def enforce_locked_volumes() -> int:
    """Re-assert the persisted volume on every speaker with the lock enabled.

    The volume-LOCK feature ("persist volume when another system changes it").
    Polls each locked speaker's CURRENT volume over UPnP using its stored
    coordinator ip (from dsapp.sonos_speakers) and re-sets it if it drifted —
    deliberately poll-based (not Hubitat-event-based) so it doesn't depend on
    fuzzy device-name → speaker mapping. Scheduled periodically from app.py.
    Returns the number of speakers corrected.
    """
    from . import store
    s = get_sonos()
    corrected = 0
    for row in store.persisted_speakers():
        ip, level = row.get("ip"), row.get("persisted_level")
        if not ip or level is None:
            continue
        try:
            if s.volume(ip) != int(level):
                s.set_volume(ip, int(level))
                corrected += 1
        except Exception:
            pass  # speaker offline / transient — try again next tick
    return corrected
