"""
Authentication / authorization for the panel surface — MAX-CONVENTIONAL posture.

Operator ruling 2026-07-12 (plan §6b): "Max conventional security measures."

WHAT WE ARE DELIBERATELY *NOT* INHERITING FROM TILES
----------------------------------------------------
Verified first-hand in MOBIUS.TILES/app.py:
  * `POST /api/device/<id>/command` carried **no auth decorator at all** — any
    host that could reach the port could command any device (locks included).
  * `GET  /api/devices/native` — likewise unauthenticated (full device roster).
  * a `before_request` hook auto-minted a session for **user_id=1 (the admin)**
    for any client on the same /24 when "trusted network" was enabled.
TILES held privileged hub credentials behind an unauthenticated front door — a
textbook CONFUSED DEPUTY: an attacker never needed the hub token, they just
asked TILES to use it. We do not reproduce this.

THE MODEL
---------
1. DEFAULT-DENY. Every state-changing route requires an authenticated principal.
   There is no "open" command route. Ever.
2. TWO PRINCIPAL CLASSES, separate credentials, least privilege:
     - PANEL   : an ENROLLED device (wall tablet / phone). Enrolled once by an
                 admin, holds its own token, and is REVOCABLE INDIVIDUALLY —
                 which a "trusted LAN" gate can never be.
     - SERVICE : server-to-server (another MOBIUS project). Scoped token.
3. TOKENS: 256-bit random (`secrets.token_urlsafe(32)`). We persist **only a
   SHA-256 hash**; the raw token is displayed ONCE at enrollment and never
   stored. (A slow KDF — bcrypt/scrypt — is for *human passwords*, which are
   low-entropy and guessable. For 256-bit random tokens a single SHA-256 is the
   conventional, correct choice: brute-forcing the preimage is infeasible.)
4. CONSTANT-TIME comparison via `hmac.compare_digest` — no timing oracle.
5. LAN IS A SECOND FACTOR, NEVER THE GATE. A panel request must present a valid
   token **AND** originate from a trusted subnet. The client IP is read from the
   nginx-set `X-Real-IP` / `X-Forwarded-For` chain — nginx OVERWRITES those
   headers, so they are not client-spoofable (the same trust anchor MOBIUS.NVR
   relies on). Trusted subnets are configurable (`PANEL_TRUSTED_SUBNETS`).
6. SCOPES (least privilege): reading the roster is not the same right as
   commanding a lock. A token carries only what it needs.
7. AUDIT: every authenticated command records the principal.
8. GENERIC FAILURES: 401/403 never reveal *which* check failed (no oracle).
"""

import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
from dataclasses import dataclass, field
from typing import List, Optional

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

# --- scopes -----------------------------------------------------------------
SCOPE_PANEL_READ = "panel:read"        # device roster, state, preferences
SCOPE_PANEL_COMMAND = "panel:command"  # send a device command (locks, switches)
SCOPE_SERVICE_READ = "service:read"
SCOPE_SERVICE_COMMAND = "service:command"
ALL_SCOPES = (SCOPE_PANEL_READ, SCOPE_PANEL_COMMAND,
              SCOPE_SERVICE_READ, SCOPE_SERVICE_COMMAND)

KIND_PANEL = "panel"
KIND_SERVICE = "service"

# Default trusted subnets for the LAN second-factor. Overridable via
# PANEL_TRUSTED_SUBNETS (comma-separated CIDRs). RFC1918 + loopback by default —
# deliberately NOT "any IP", and deliberately not the *only* check.
_DEFAULT_SUBNETS = "192.168.0.0/16,10.0.0.0/8,172.16.0.0/12,127.0.0.0/8"

_TOKEN_BYTES = 32          # 256-bit
_TOKEN_PREFIX_LEN = 8      # shown in the UI to identify a token without revealing it


@dataclass(frozen=True)
class Principal:
    """An authenticated caller. `scopes` is the authoritative permission set."""
    id: int
    name: str
    kind: str                       # KIND_PANEL | KIND_SERVICE
    scopes: List[str] = field(default_factory=list)
    require_lan: bool = True

    def has(self, scope: str) -> bool:
        return scope in self.scopes


# --- token primitives -------------------------------------------------------

def generate_token() -> str:
    """A fresh 256-bit URL-safe token. Shown to the operator ONCE."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(raw: str) -> str:
    """SHA-256 hex of a raw token — the only form we persist."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def token_prefix(raw: str) -> str:
    """Short non-secret identifier so the UI can label a token it can't show."""
    return raw[:_TOKEN_PREFIX_LEN]


def tokens_equal(a: str, b: str) -> bool:
    """Constant-time equality — never use `==` on secrets."""
    return hmac.compare_digest(a, b)


# --- network second-factor --------------------------------------------------

def _trusted_networks() -> List[ipaddress._BaseNetwork]:
    raw = os.environ.get("PANEL_TRUSTED_SUBNETS", _DEFAULT_SUBNETS)
    nets = []
    for cidr in raw.split(","):
        cidr = cidr.strip()
        if not cidr:
            continue
        try:
            nets.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            logger.warning(f"PANEL_TRUSTED_SUBNETS: ignoring invalid CIDR {cidr!r}")
    return nets


def client_ip(request: Request) -> Optional[str]:
    """
    The caller's IP, taken from the reverse proxy's headers.

    SECURITY: `X-Real-IP` / `X-Forwarded-For` are only trustworthy because our
    nginx SETS (overwrites) them on every proxied request — a client cannot
    forge them through the proxy. If a deployment ever exposes the app port
    directly (bypassing nginx), this assumption breaks; that is why the token is
    the primary factor and the network check is only the second.
    """
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()      # first hop = original client
    return request.client.host if request.client else None


def is_trusted_lan(ip: Optional[str]) -> bool:
    """True if `ip` sits inside a configured trusted subnet."""
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _trusted_networks())


# --- authentication ---------------------------------------------------------

def _bearer(request: Request) -> Optional[str]:
    """Extract a Bearer token. Bearer-only (no cookies) is deliberate: it makes
    the surface immune to CSRF by construction — there is no ambient credential
    a browser will attach automatically."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    tok = header[7:].strip()
    return tok or None


def authenticate(request: Request) -> Optional[Principal]:
    """Resolve the caller to a Principal, or None. Never raises."""
    raw = _bearer(request)
    if not raw:
        return None
    from apps.tiles_api import db          # local import: avoids a cycle
    row = db.find_active_device_by_token_hash(hash_token(raw))
    if not row:
        return None
    # Defense-in-depth: re-compare in constant time (the DB lookup already
    # matched on the hash, but this keeps the comparison discipline explicit
    # and survives any future change to the lookup path).
    if not tokens_equal(row["token_hash"], hash_token(raw)):
        return None
    return Principal(id=row["id"], name=row["name"], kind=row["kind"],
                     scopes=list(row["scopes"] or []),
                     require_lan=bool(row["require_lan"]))


def require_scope(scope: str):
    """
    FastAPI dependency factory — DEFAULT-DENY gate for a given scope.

    Enforces, in order: a valid token -> the required scope -> (for principals
    flagged `require_lan`) the trusted-subnet second factor. Failures are
    deliberately generic so they leak nothing about which check failed.

    Usage:
        @router.post("/devices/{id}/command",
                     dependencies=[Depends(require_scope(SCOPE_PANEL_COMMAND))])
    """
    def _dep(request: Request) -> Principal:
        principal = authenticate(request)
        ip = client_ip(request)
        if principal is None:
            logger.warning(f"panel auth: rejected unauthenticated request "
                           f"{request.method} {request.url.path} from {ip}")
            raise HTTPException(status_code=401, detail="Authentication required.",
                                headers={"WWW-Authenticate": "Bearer"})
        if not principal.has(scope):
            logger.warning(f"panel auth: '{principal.name}' lacks {scope} for "
                           f"{request.method} {request.url.path}")
            raise HTTPException(status_code=403, detail="Not permitted.")
        if principal.require_lan and not is_trusted_lan(ip):
            logger.warning(f"panel auth: '{principal.name}' presented a valid token "
                           f"from OFF-LAN ip {ip} — denied (LAN second factor)")
            raise HTTPException(status_code=403, detail="Not permitted.")
        # Best-effort liveness/audit trail; never fail the request on this.
        try:
            from apps.tiles_api import db
            db.touch_device(principal.id, ip)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"panel auth: touch_device failed (non-fatal): {e}")
        request.state.principal = principal
        return principal
    return _dep
