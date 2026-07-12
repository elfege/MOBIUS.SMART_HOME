"""
Sonos — the central minimal facade for local Sonos control
==========================================================
Per the operator's architecture directive (2026-06-20): a central minimal class
that EXTENDS classes imported from submodules. Each capability lives in its own
module as a mixin; ``Sonos`` composes them and adds only cross-cutting glue
(discovery cache + room/coordinator resolution). Feature services
(``announce.AnnounceService``, and future ones) build ON this facade — they
never speak SOAP directly.

    Sonos
    ├── DiscoveryMixin   (_discovery)  SSDP find + ip/room cache
    ├── TransportMixin   (_transport)  play / stop / seek / what's-playing
    ├── RenderingMixin   (_rendering)  volume / mute
    ├── TopologyMixin    (_topology)   zone groups / coordinator resolution
    └── SnapshotMixin    (_snapshot)   capture + restore (announce-then-resume)

All local, LAN-only, no cloud, no Hubitat, no third-party dependency.
"""

from __future__ import annotations

import re

from ._discovery import DiscoveryMixin
from ._rendering import RenderingMixin
from ._snapshot import SnapshotMixin
from ._topology import TopologyMixin
from ._transport import TransportMixin

_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


class Sonos(DiscoveryMixin, TransportMixin, RenderingMixin, TopologyMixin, SnapshotMixin):
    """Composed Sonos controller. Methods take a speaker ``ip``; use
    :meth:`resolve` to turn a room name into an IP first."""

    def __init__(self) -> None:
        # Discovery cache (read by DiscoveryMixin).
        self._speakers: dict[str, str] = {}
        self._discovered_at: float = 0.0

    def resolve(self, room_or_ip: str) -> str | None:
        """Turn a room name OR a literal IP into a speaker IP.

        A dotted-quad is returned as-is (no discovery needed); anything else is
        resolved against the discovered room map (case-insensitive, substring).
        Returns None if the room can't be found.
        """
        if not room_or_ip:
            return None
        candidate = room_or_ip.strip()
        if _IPV4.match(candidate):
            return candidate
        return self.ip_for_room(candidate)

    def target_ip(self, room_or_ip: str) -> str | None:
        """Resolve a room/IP AND fold in group-coordinator resolution, so the
        returned IP is always safe to send transport commands to."""
        ip = self.resolve(room_or_ip)
        if ip is None:
            return None
        return self.coordinator_ip_for(ip)
