"""
TransportMixin — Sonos AVTransport actions (what's playing / play / stop / seek)
================================================================================
One capability slice of the central ``Sonos`` facade. Every method targets a
single speaker by ``ip`` and delegates the wire work to ``_soap.soap_request``;
the mixin holds no state. Action argument order follows the UPnP AVTransport:1
declaration (InstanceID first).
"""

from __future__ import annotations

from ._soap import soap_request


class TransportMixin:
    """AVTransport:1 actions used for announcements and snapshot/restore."""

    def set_uri(self, ip: str, uri: str, meta: str = "") -> None:
        """Point the speaker at ``uri`` (the clip, or a restored stream).

        ``meta`` must be UNESCAPED DIDL (the snapshot layer stores it that way);
        the SOAP helper escapes it exactly once on the wire.
        """
        soap_request(ip, "AVTransport", "SetAVTransportURI", {
            "InstanceID": 0, "CurrentURI": uri, "CurrentURIMetaData": meta,
        })

    def play(self, ip: str) -> None:
        """Start playback at normal speed."""
        soap_request(ip, "AVTransport", "Play", {"InstanceID": 0, "Speed": 1})

    def stop(self, ip: str) -> None:
        soap_request(ip, "AVTransport", "Stop", {"InstanceID": 0})

    def pause(self, ip: str) -> None:
        soap_request(ip, "AVTransport", "Pause", {"InstanceID": 0})

    def seek(self, ip: str, rel_time: str) -> None:
        """Seek to an ``H:MM:SS`` position (REL_TIME). Not all sources support
        seek (radio/streams) — callers should tolerate a SonosSoapError."""
        soap_request(ip, "AVTransport", "Seek", {
            "InstanceID": 0, "Unit": "REL_TIME", "Target": rel_time,
        })

    def transport_state(self, ip: str) -> str:
        """Current transport state: PLAYING | PAUSED_PLAYBACK | STOPPED | TRANSITIONING."""
        return soap_request(ip, "AVTransport", "GetTransportInfo", {"InstanceID": 0}) \
            .get("CurrentTransportState", "")

    def media_info(self, ip: str) -> dict[str, str]:
        """Raw GetMediaInfo out-args (CurrentURI, CurrentURIMetaData, NrTracks, ...)."""
        return soap_request(ip, "AVTransport", "GetMediaInfo", {"InstanceID": 0})

    def position_info(self, ip: str) -> dict[str, str]:
        """Raw GetPositionInfo out-args (Track, TrackDuration, RelTime, TrackURI, ...)."""
        return soap_request(ip, "AVTransport", "GetPositionInfo", {"InstanceID": 0})
