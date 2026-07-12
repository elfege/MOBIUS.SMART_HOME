"""
Sonos UPnP SOAP-over-HTTP transport (the shared low-level primitive)
====================================================================
Every Sonos capability mixin (transport, rendering, topology) talks to a
speaker by POSTing a SOAP envelope to its UPnP control endpoint on port 1400.
This module isolates that mechanics so the mixins contain only domain logic and
so a future backend (e.g. the Sonos S2 local websocket API) could slot in
underneath without touching the mixins.

This is intentionally NOT a mixin — it is a stateless helper imported by all of
them. Validated against real hardware 2026-06-20 (Sonos Office / Ray, .80):
read + SetAVTransportURI + Play + SetVolume + restore all confirmed.

No third-party dependency, no cloud, LAN-only.
"""

from __future__ import annotations

import html
import re
import socket
import urllib.error
import urllib.request

SONOS_PORT = 1400

# service-name -> (UPnP service type, control URL path)
SERVICES = {
    "AVTransport": (
        "urn:schemas-upnp-org:service:AVTransport:1",
        "/MediaRenderer/AVTransport/Control",
    ),
    "RenderingControl": (
        "urn:schemas-upnp-org:service:RenderingControl:1",
        "/MediaRenderer/RenderingControl/Control",
    ),
    "ZoneGroupTopology": (
        "urn:schemas-upnp-org:service:ZoneGroupTopology:1",
        "/ZoneGroupTopology/Control",
    ),
}


class SonosSoapError(Exception):
    """Raised when a SOAP call fails (unreachable, HTTP error, or UPnP fault)."""


def _xml_escape(value) -> str:
    """Escape a scalar for embedding as SOAP arg text.

    NOTE on metadata round-tripping: DIDL metadata read back from a speaker is
    stored UNESCAPED by the snapshot layer (it calls html.unescape on the parsed
    value). Passing that unescaped DIDL through here re-escapes it exactly once,
    which is what Sonos expects on the wire — avoiding the classic double-escape
    bug where ``&lt;`` becomes ``&amp;lt;``.
    """
    return html.escape(str(value), quote=False)


def soap_request(ip: str, service: str, action: str, args: dict | None = None,
                 *, timeout: float = 6.0) -> dict[str, str]:
    """Issue one SOAP action against a speaker and return its out-arguments.

    Args:
        ip: speaker LAN address.
        service: key into SERVICES ("AVTransport" | "RenderingControl" | "ZoneGroupTopology").
        action: UPnP action name (e.g. "Play", "GetVolume").
        args: ordered mapping of in-argument name -> value. Order matters for
            UPnP; Python dicts preserve insertion order, so pass them in the
            order the action declares (InstanceID first for the media renderer
            services). Pass None / {} for actions that take no arguments.
        timeout: per-call socket timeout in seconds.

    Returns:
        dict of out-argument-name -> raw text (entity-escaped values such as
        DIDL metadata or ZoneGroupState are returned as-is; callers that need
        the decoded form call html.unescape themselves).

    Raises:
        SonosSoapError: on unreachable host, HTTP error, or a SOAP <Fault>.
    """
    stype, control = SERVICES[service]
    inner = "".join(f"<{k}>{_xml_escape(v)}</{k}>" for k, v in (args or {}).items())
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{stype}">{inner}</u:{action}>'
        '</s:Body></s:Envelope>'
    )
    req = urllib.request.Request(
        f"http://{ip}:{SONOS_PORT}{control}",
        data=body.encode("utf-8"),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{stype}#{action}"',
        },
        method="POST",
    )
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise SonosSoapError(
            f"{service}#{action} on {ip}: HTTP {e.code} {_fault_text(detail)}"
        ) from e
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise SonosSoapError(f"{service}#{action} on {ip}: unreachable ({e})") from e

    if "<s:Fault>" in raw or "<SOAP-ENV:Fault>" in raw:
        raise SonosSoapError(f"{service}#{action} on {ip}: UPnP fault {_fault_text(raw)}")
    return _parse_out_args(raw, action)


def _parse_out_args(raw: str, action: str) -> dict[str, str]:
    """Extract the out-arguments from an ``<u:ActionResponse>`` envelope."""
    m = re.search(rf"<u:{action}Response[^>]*>(.*?)</u:{action}Response>", raw, re.S)
    segment = m.group(1) if m else raw
    return dict(re.findall(r"<([A-Za-z][\w]*)>(.*?)</\1>", segment, re.S))


def _fault_text(raw: str) -> str:
    """Best-effort UPnP error code/description for an exception message."""
    code = re.search(r"<errorCode>(.*?)</errorCode>", raw)
    desc = re.search(r"<errorDescription>(.*?)</errorDescription>", raw)
    if code or desc:
        return f"[{code.group(1) if code else '?'}] {desc.group(1) if desc else ''}".strip()
    return raw[:160]
