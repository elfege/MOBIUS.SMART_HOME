"""
RenderingMixin — Sonos RenderingControl actions (volume / mute)
===============================================================
One capability slice of the central ``Sonos`` facade. Stateless; targets a
single speaker by ``ip``. Used to set the announcement volume and to capture /
restore the listener's volume around an announcement.
"""

from __future__ import annotations

from ._soap import soap_request


class RenderingMixin:
    """RenderingControl:1 actions (Master channel)."""

    def volume(self, ip: str) -> int:
        """Current Master volume 0..100."""
        out = soap_request(ip, "RenderingControl", "GetVolume",
                           {"InstanceID": 0, "Channel": "Master"})
        try:
            return int(out.get("CurrentVolume", 0))
        except (TypeError, ValueError):
            return 0

    def set_volume(self, ip: str, level: int) -> None:
        """Set Master volume, clamped to 0..100."""
        level = max(0, min(100, int(level)))
        soap_request(ip, "RenderingControl", "SetVolume",
                     {"InstanceID": 0, "Channel": "Master", "DesiredVolume": level})

    def mute(self, ip: str) -> bool:
        out = soap_request(ip, "RenderingControl", "GetMute",
                           {"InstanceID": 0, "Channel": "Master"})
        return out.get("CurrentMute", "0") == "1"

    def set_mute(self, ip: str, muted: bool) -> None:
        soap_request(ip, "RenderingControl", "SetMute",
                     {"InstanceID": 0, "Channel": "Master", "DesiredMute": 1 if muted else 0})
