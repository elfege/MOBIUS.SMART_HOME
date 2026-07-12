"""
DiscoveryMixin — find Sonos speakers on the LAN via SSDP
========================================================
One capability slice of the central ``Sonos`` facade. Unlike the transport /
rendering slices, discovery DOES hold state: a cached ``{ip: room_name}`` map,
so we SSDP-probe the network at most once per refresh window instead of on
every announcement.

SSDP M-SEARCH for ``urn:schemas-upnp-org:device:ZonePlayer:1`` returns the
speaker IPs; the human room name is read from each speaker's
``/xml/device_description.xml`` (``<roomName>``). Validated 2026-06-20: 7
ZonePlayers discovered on this LAN.

LAN-only, no cloud, no dependency.
"""

from __future__ import annotations

import os
import re
import socket
import time
import urllib.request

_SSDP_ADDR = ("239.255.255.250", 1900)
_SSDP_ST = "urn:schemas-upnp-org:device:ZonePlayer:1"


class DiscoveryMixin:
    """SSDP discovery + a cached ip->room map. ``__init__`` of the facade sets
    up ``self._speakers`` and ``self._discovered_at``."""

    # populated by Sonos.__init__: self._speakers: dict[str,str]; self._discovered_at: float
    _discovery_ttl = 300.0  # seconds before a discover() result is considered stale

    def discover(self, *, timeout: float = 2.0, force: bool = False) -> dict[str, str]:
        """Return ``{ip: room_name}`` for all reachable speakers.

        Sources, unioned: SSDP multicast probe + seed IPs from the
        ``SONOS_SPEAKER_IPS`` env var. SSDP works from the host but NOT from a
        bridged Docker container (multicast doesn't cross the bridge — verified
        2026-06-20); the seed list is what makes discovery work in-container,
        since UNICAST to each speaker DOES cross the bridge. Room names + control
        all go over unicast, so a seeded IP is fully usable.

        Cached for ``_discovery_ttl`` seconds; ``force=True`` re-probes.
        Best-effort: a speaker whose description fetch fails is still listed
        under its IP so it stays addressable.
        """
        now = time.monotonic()
        if not force and self._speakers and (now - self._discovered_at) < self._discovery_ttl:
            return dict(self._speakers)

        ips = self._ssdp_search(timeout=timeout) | self._seed_ips()
        speakers: dict[str, str] = {}
        for ip in sorted(ips):
            speakers[ip] = self._room_name(ip) or ip
        if speakers:
            self._speakers = speakers
            self._discovered_at = now
        return dict(speakers)

    @staticmethod
    def _seed_ips() -> set[str]:
        """Static speaker IPs from ``SONOS_SPEAKER_IPS`` (comma-separated).

        The fallback/augment for environments where SSDP is unavailable (the
        app container). DHCP can move these — for warning-free operation give
        the speakers reserved leases, or this env is overridden."""
        raw = os.environ.get("SONOS_SPEAKER_IPS", "")
        return {ip.strip() for ip in raw.split(",") if ip.strip()}

    def rooms(self) -> dict[str, str]:
        """Cached ip->room map, discovering on first use."""
        return self.discover()

    def ip_for_room(self, name: str) -> str | None:
        """Resolve a room name to an IP (case-insensitive, substring match).

        Substring so "Office" matches "Sonos Office". An alias map
        (``SONOS_ROOM_ALIASES``) is applied first so a user's natural term
        resolves to the speaker's actual roomName — e.g. "living" -> the
        "Home Theater" speaker (operator naming, 2026-06-20). Returns the first
        match, or None. Exact (case-insensitive) matches win over substring.
        """
        if not name:
            return None
        speakers = self.discover()
        low = name.strip().lower()
        # Apply alias (alias -> canonical roomName) before matching.
        low = self._room_aliases().get(low, low)
        for ip, room in speakers.items():
            if room.lower() == low:
                return ip
        for ip, room in speakers.items():
            if low in room.lower():
                return ip
        return None

    @staticmethod
    def _room_aliases() -> dict[str, str]:
        """Alias map ``{alias_lower: canonical_room_lower}`` from
        ``SONOS_ROOM_ALIASES`` ("living=Home Theater,den=Office"), defaulting to
        the operator's living-room naming. Lets STP/config use natural terms."""
        raw = os.environ.get("SONOS_ROOM_ALIASES",
                             "living=Home Theater,living room=Home Theater")
        out: dict[str, str] = {}
        for pair in raw.split(","):
            if "=" in pair:
                a, _, b = pair.partition("=")
                a, b = a.strip().lower(), b.strip().lower()
                if a and b:
                    out[a] = b
        return out

    # ---- internals -------------------------------------------------------
    @staticmethod
    def _ssdp_search(*, timeout: float) -> set[str]:
        """Broadcast an SSDP M-SEARCH and collect responding speaker IPs."""
        msg = "\r\n".join([
            "M-SEARCH * HTTP/1.1",
            f"HOST: {_SSDP_ADDR[0]}:{_SSDP_ADDR[1]}",
            'MAN: "ssdp:discover"',
            "MX: 1",
            f"ST: {_SSDP_ST}",
            "", "",
        ]).encode()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.settimeout(timeout)
        found: set[str] = set()
        try:
            s.sendto(msg, _SSDP_ADDR)
            t0 = time.monotonic()
            while time.monotonic() - t0 < timeout:
                try:
                    _, addr = s.recvfrom(2048)
                except socket.timeout:
                    break
                found.add(addr[0])
        finally:
            s.close()
        return found

    @staticmethod
    def _room_name(ip: str) -> str | None:
        """Read ``<roomName>`` from the speaker's device description."""
        try:
            body = urllib.request.urlopen(
                f"http://{ip}:1400/xml/device_description.xml", timeout=3
            ).read().decode("utf-8", "replace")
        except Exception:
            return None
        m = re.search(r"<roomName>(.*?)</roomName>", body)
        return m.group(1) if m else None
