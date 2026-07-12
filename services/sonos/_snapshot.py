"""
SnapshotMixin — capture and restore a speaker's playback state
==============================================================
This is the "announce then resume" mechanism and the HIGHEST-RISK slice of the
subsystem: getting restore wrong means an announcement permanently interrupts
whatever the listener was playing. The capture/restore field set and the
metadata round-trip were validated on real hardware 2026-06-20 (Sonos Office):
snapshot -> play a clip -> restore returned the exact prior CurrentURI + volume.

Algorithm modeled on SoCo's proven Snapshot (read as reference, reimplemented
here — no SoCo dependency):

    capture: transport state, CurrentURI + DIDL metadata, RelTime position,
             volume, mute.
    restore: SetAVTransportURI(uri, metadata) -> SetVolume/SetMute -> if the
             speaker was PLAYING, Seek(RelTime) then Play.

Metadata is stored UNESCAPED (``html.unescape`` on capture) so the SOAP layer
re-escapes it exactly once on restore — see _soap._xml_escape.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass

from ._soap import SonosSoapError

logger = logging.getLogger(__name__)


@dataclass
class Snapshot:
    """Immutable capture of a speaker's playback state for later restore."""
    ip: str
    state: str            # PLAYING | PAUSED_PLAYBACK | STOPPED | TRANSITIONING
    uri: str              # CurrentURI
    metadata: str         # CurrentURIMetaData, UNESCAPED DIDL
    rel_time: str         # RelTime "H:MM:SS" (best-effort; "" if unknown)
    volume: int
    mute: bool


class SnapshotMixin:
    """Depends on TransportMixin + RenderingMixin (the facade composes all)."""

    def snapshot(self, ip: str) -> Snapshot:
        """Capture the speaker's current state. Best-effort on the optional
        position field; the essential fields (uri/volume) always populate."""
        media = self.media_info(ip)
        state = self.transport_state(ip)
        try:
            rel_time = self.position_info(ip).get("RelTime", "") or ""
        except SonosSoapError:
            rel_time = ""
        return Snapshot(
            ip=ip,
            state=state,
            uri=media.get("CurrentURI", "") or "",
            metadata=html.unescape(media.get("CurrentURIMetaData", "") or ""),
            rel_time=rel_time,
            volume=self.volume(ip),
            mute=self.mute(ip),
        )

    def restore(self, snap: Snapshot) -> None:
        """Restore a previously captured snapshot.

        Tolerant: a missing prior URI (nothing was loaded) skips the transport
        restore; a non-seekable source (radio/stream) swallows the Seek error
        and still resumes playback.
        """
        ip = snap.ip
        # Volume/mute first so a resumed stream doesn't blare at announce volume.
        try:
            self.set_volume(ip, snap.volume)
            self.set_mute(ip, snap.mute)
        except SonosSoapError as e:
            logger.warning("Sonos restore: volume/mute on %s failed: %s", ip, e)

        if not snap.uri:
            return  # nothing was loaded before the announcement

        try:
            self.set_uri(ip, snap.uri, snap.metadata)
        except SonosSoapError as e:
            logger.warning("Sonos restore: SetAVTransportURI on %s failed: %s", ip, e)
            return

        if snap.state == "PLAYING":
            if snap.rel_time and snap.rel_time not in ("0:00:00", "NOT_IMPLEMENTED"):
                try:
                    self.seek(ip, snap.rel_time)
                except SonosSoapError:
                    pass  # streams/radio aren't seekable — fine
            try:
                self.play(ip)
            except SonosSoapError as e:
                logger.warning("Sonos restore: Play on %s failed: %s", ip, e)
