"""
Hubitat ADMIN API client — alternative to Maker API.

The admin API is what Hubitat's web UI itself uses. Two key advantages over
Maker API:
  1. No per-device opt-in (Maker API requires you to enable each device).
  2. Richer device data (preferences, drivers, mesh status).

Endpoints (reverse-engineered, undocumented but stable in community use):
  GET /device/list/data           — JSON array of all devices + current state
  GET /device/fullJson/<id>       — single device, full detail
  POST /device/<id>/<command>     — command issue (with optional argument)
                                    NB: command path is firmware-specific;
                                    we wrap via try-fallback below.

Authentication:
  - If the hub has NO login password set, requests are unauthenticated.
  - If the hub has a password, POST /login first; server sets a session
    cookie. We persist the cookie in a `requests.Session` and re-login
    automatically on 401/302→/login.

Auth credentials come from:
  - `encrypted_secrets` table (preferred — encrypted at rest, see plan doc)
  - env var fallback: HUBITAT_ADMIN_USER_<hub_name_upper>,
    HUBITAT_ADMIN_PASSWORD_<hub_name_upper>
  - if neither: skip auth, hope the hub is open (current state for Elfege).

Per-hub instance kept in module-level dict, keyed by hub_ip.
"""

import logging
import os
import threading
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Per-hub client (one Session per hub for cookie persistence)
# -------------------------------------------------------------------------


class HubitatAdminClient:
    """Cookie-session client for one Hubitat hub's admin API."""

    def __init__(
        self,
        hub_ip: str,
        hub_name: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 8.0,
    ) -> None:
        self.hub_ip = hub_ip
        self.hub_name = hub_name
        self.base_url = f"http://{hub_ip}"
        self.username = username
        self.password = password
        self.timeout = timeout
        self._session = requests.Session()
        self._authed = False  # True after a successful POST /login
        self._lock = threading.RLock()
        # Cache the no-auth state: we try unauthenticated first; if anything
        # 401s we flip to "needs auth" and call _login.
        self._needs_auth = bool(username and password)

    # ---------------- core ----------------

    def _login(self) -> bool:
        """POST /login. Returns True on success. Cookie stored in session."""
        if not (self.username and self.password):
            return False
        try:
            r = self._session.post(
                f"{self.base_url}/login",
                data={
                    "username": self.username,
                    "password": self.password,
                    "submit": "Login",
                },
                timeout=self.timeout,
                allow_redirects=False,
            )
            # Login on Hubitat returns 302 on success (redirect to /), 200
            # on failure (re-renders form). Cookie is set in either case
            # but only valid on 302.
            if r.status_code in (200, 302):
                self._authed = True
                logger.info(
                    f"hubitat_admin [{self.hub_name}]: logged in (status {r.status_code})"
                )
                return True
            logger.warning(
                f"hubitat_admin [{self.hub_name}]: login failed status={r.status_code}"
            )
        except Exception as e:
            logger.warning(f"hubitat_admin [{self.hub_name}]: login error: {e}")
        return False

    def _request(self, method: str, path: str, **kw) -> requests.Response:
        """One-time-retry-on-401 request wrapper."""
        url = f"{self.base_url}{path}"
        with self._lock:
            kw.setdefault("timeout", self.timeout)
            r = self._session.request(method, url, **kw)
            if r.status_code == 401 and self._needs_auth and not self._authed:
                if self._login():
                    r = self._session.request(method, url, **kw)
            elif r.status_code == 401 and self._needs_auth:
                # Cookie may have expired — try re-login once
                self._authed = False
                if self._login():
                    r = self._session.request(method, url, **kw)
            return r

    # ---------------- public API ----------------

    def get_all_devices(self) -> List[Dict[str, Any]]:
        """GET /device/list/data → list of device dicts.

        Schema (subset of fields we care about):
          id, name, displayName, label, deviceTypeName, capability,
          currentStates (list of {name, value, dataType}),
          locationId, hubId, ...
        """
        r = self._request("GET", "/device/list/data")
        r.raise_for_status()
        return r.json()

    def get_device(self, device_id: int) -> Optional[Dict[str, Any]]:
        """GET /device/fullJson/<id> → single device with current state."""
        r = self._request("GET", f"/device/fullJson/{device_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def get_devices_with_state(
        self, device_ids: List[int],
    ) -> List[Dict[str, Any]]:
        """
        Fetch full state for an iterable of device ids. Returns ONLY
        successfully-fetched devices (404s are skipped silently).

        Unlike get_all_devices(), this hits /device/fullJson/<id> per device
        and DOES include currentStates. Used by reconcile poll when the
        Maker API is disabled — pricier than Maker's bulk endpoint but
        confined to actually-subscribed devices (~30/hub rather than 225/hub).
        """
        out = []
        for did in device_ids:
            try:
                d = self.get_device(int(did))
                if d:
                    out.append(d)
            except Exception as e:
                logger.debug(
                    f"hubitat_admin [{self.hub_name}] get_device({did}): {e}"
                )
        return out

    def send_command(
        self,
        device_id: int,
        command: str,
        argument: Optional[str] = None,
    ) -> bool:
        """
        Issue a command. Tries the conventional admin paths in order:
          1. POST /device/<id>/<command> (with body if argument given)
          2. GET  /device/<id>/<command>[/argument]   (legacy GET-style)
        Returns True on 2xx.

        IMPORTANT: this method is currently UNTESTED against live devices
        on the user's hubs. Wave D of the admin-API plan tests + cuts over
        commands. For now we ship the method so the wiring exists but it
        should NOT be invoked in production until verified.
        """
        # Attempt 1: POST style
        try:
            payload = {"command_argument": argument} if argument else {}
            r = self._request(
                "POST", f"/device/{device_id}/{command}", data=payload
            )
            if 200 <= r.status_code < 300:
                return True
        except Exception as e:
            logger.debug(f"hubitat_admin send_command POST: {e}")
        # Attempt 2: GET fallback (some firmwares use GET)
        try:
            path = f"/device/{device_id}/{command}"
            if argument is not None:
                path += f"/{argument}"
            r = self._request("GET", path)
            return 200 <= r.status_code < 300
        except Exception as e:
            logger.warning(
                f"hubitat_admin [{self.hub_name}] send_command "
                f"({device_id}/{command}) both POST + GET failed: {e}"
            )
            return False


# -------------------------------------------------------------------------
# Per-hub singleton registry
# -------------------------------------------------------------------------


_clients: Dict[str, HubitatAdminClient] = {}
_clients_lock = threading.Lock()


def _credentials_for_hub(hub_name: str) -> tuple:
    """
    Look up admin-login credentials for the given hub.

    Resolution order:
      1. encrypted_secrets table — keys 'hubitat_admin_user_<hub>',
         'hubitat_admin_password_<hub>'. (Not implemented yet — schema is
         in place, decrypt path is part of the deferred KEK work.)
      2. Env vars HUBITAT_ADMIN_USER_<HUB>, HUBITAT_ADMIN_PASSWORD_<HUB>
         (uppercase hub name).
      3. (None, None) → unauthenticated. Fine for hubs without a login
         password set in the Hubitat admin UI.
    """
    upper = hub_name.upper()
    user = os.environ.get(f"HUBITAT_ADMIN_USER_{upper}")
    password = os.environ.get(f"HUBITAT_ADMIN_PASSWORD_{upper}")
    return (user, password)


def get_client(hub_ip: str, hub_name: str) -> HubitatAdminClient:
    """Get or create the singleton client for this hub."""
    key = hub_ip
    with _clients_lock:
        if key not in _clients:
            user, password = _credentials_for_hub(hub_name)
            _clients[key] = HubitatAdminClient(
                hub_ip=hub_ip,
                hub_name=hub_name,
                username=user,
                password=password,
            )
        return _clients[key]


def invalidate_clients() -> None:
    """Drop all cached clients (forces re-login on next request).
    Useful when admin credentials are rotated via the settings UI."""
    with _clients_lock:
        for c in _clients.values():
            c._session.close()
        _clients.clear()
