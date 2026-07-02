"""
TopologyMixin — Sonos zone groups + coordinator resolution
==========================================================
One capability slice of the central ``Sonos`` facade.

WHY THIS MATTERS: in a Sonos group only the GROUP COORDINATOR accepts transport
commands. Sending Play to a non-coordinator member is silently wrong. Before any
announcement we resolve the coordinator for the target speaker and command that.

GOTCHA (validated 2026-06-20): ``GetZoneGroupState`` returns the topology as an
ENTITY-ESCAPED XML string nested inside the SOAP response. We must
``html.unescape`` it before parsing, or the coordinator regex matches nothing
(the read-only spike returned "zone groups: 0" until this was handled).
"""

from __future__ import annotations

import html
import re

from ._soap import soap_request, SonosSoapError


class TopologyMixin:
    """ZoneGroupTopology:1 — group state + coordinator lookup."""

    def zone_groups(self) -> list[dict]:
        """Return the current zone groups.

        Each group: ``{"coordinator_uuid", "coordinator_ip", "members":
        [{"uuid","ip","name"}]}``. Empty list if topology is unavailable.
        """
        try:
            out = soap_request("", "ZoneGroupTopology", "GetZoneGroupState")  # ip filled below
        except SonosSoapError:
            return []
        return self._parse_zone_state(out.get("ZoneGroupState", ""))

    def zone_groups_via(self, ip: str) -> list[dict]:
        """Same as :meth:`zone_groups` but query a specific speaker (any member
        returns the whole-household topology)."""
        try:
            out = soap_request(ip, "ZoneGroupTopology", "GetZoneGroupState")
        except SonosSoapError:
            return []
        return self._parse_zone_state(out.get("ZoneGroupState", ""))

    def coordinator_ip_for(self, ip: str) -> str:
        """Return the coordinator IP for the group containing ``ip``.

        Falls back to ``ip`` itself when the speaker is standalone or topology
        can't be read — a standalone speaker is its own coordinator, so this is
        always safe to send transport commands to.
        """
        for group in self.zone_groups_via(ip):
            member_ips = {m["ip"] for m in group["members"]}
            if ip in member_ips and group.get("coordinator_ip"):
                return group["coordinator_ip"]
        return ip

    # ---- internals -------------------------------------------------------
    @staticmethod
    def _parse_zone_state(escaped: str) -> list[dict]:
        """Parse the (entity-escaped) ZoneGroupState payload into groups."""
        if not escaped:
            return []
        xml = html.unescape(escaped)
        groups: list[dict] = []
        for coord_uuid, body in re.findall(
            r'<ZoneGroup\b[^>]*\bCoordinator="([^"]+)"[^>]*>(.*?)</ZoneGroup>', xml, re.S
        ):
            members = []
            for attrs in re.findall(r"<ZoneGroupMember\b([^>]*)/?>", body):
                uuid = _attr(attrs, "UUID")
                name = _attr(attrs, "ZoneName")
                loc = _attr(attrs, "Location")  # http://IP:1400/xml/device_description.xml
                ip = ""
                if loc:
                    m = re.search(r"https?://([^:/]+)", loc)
                    ip = m.group(1) if m else ""
                members.append({"uuid": uuid, "name": name, "ip": ip})
            coord_ip = next((m["ip"] for m in members if m["uuid"] == coord_uuid), "")
            groups.append({
                "coordinator_uuid": coord_uuid,
                "coordinator_ip": coord_ip,
                "members": members,
            })
        return groups


def _attr(attr_blob: str, name: str) -> str:
    m = re.search(rf'{name}="([^"]*)"', attr_blob)
    return m.group(1) if m else ""
