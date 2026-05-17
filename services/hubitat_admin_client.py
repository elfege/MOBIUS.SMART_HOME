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
        Issue a command via the admin API's runmethod endpoint:
          POST /device/runmethod
          Content-Type: application/x-www-form-urlencoded
          body: id=<device_id>&method=<command>[&arg=<argument>]

        Endpoint shape verified against live hubs via Elfege's chrome_nvr
        function in .bash_utils (firmware 2.5.x).  Success = HTTP 2xx OR 3xx
        (302 is the standard admin-form-flow response).
        """
        body = {"id": str(device_id), "method": command}
        if argument is not None:
            # Hubitat's runmethod takes the argument as a separate field.
            # Field name varies by firmware ("arg" or "value"); we send
            # both so either parses.
            body["arg"] = str(argument)
            body["value"] = str(argument)
        try:
            r = self._request(
                "POST", "/device/runmethod",
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                allow_redirects=False,
            )
            ok = (200 <= r.status_code < 400)
            if ok:
                logger.info(
                    f"hubitat_admin [{self.hub_name}] device {device_id}/"
                    f"{command} via /device/runmethod HTTP {r.status_code}"
                )
            else:
                logger.warning(
                    f"hubitat_admin [{self.hub_name}] device {device_id}/"
                    f"{command} non-2xx/3xx: HTTP {r.status_code} "
                    f"body={r.text[:120]!r}"
                )
            return ok
        except Exception as e:
            logger.warning(
                f"hubitat_admin [{self.hub_name}] send_command "
                f"({device_id}/{command}) failed: {e}"
            )
            return False


# -------------------------------------------------------------------------
# Per-hub singleton registry
# -------------------------------------------------------------------------


_clients: Dict[str, HubitatAdminClient] = {}
_clients_lock = threading.Lock()


def _credentials_for_hub(hub_name: str, hub_ip: str = '') -> tuple:
    """
    Look up admin-login credentials for the given hub.

    Resolution order (first non-empty pair wins):
      1. hub_config row — admin_username + admin_password columns
         (populated via the /hubs page form).
      2. Env vars HUBITAT_ADMIN_USER_<n> / HUBITAT_ADMIN_PASSWORD_<n>
         where <n> = hub_config.admin_creds_index. Populated by
         pull_aws_secrets HUBITAT — same convention as Elfege's
         chrome_nvr bash helper (.bash_utils:7373 et seq).
      3. (None, None) → unauthenticated. Correct for the default state
         where Hubitat Hub Login Security is OFF.

    Empty strings are treated as "no credential" (matching the bash
    helper's `[[ -n "$VAR" ]]` test). pull_aws_secrets exports every key
    in the secret including empties, so we must NOT treat empty as set.
    """
    pg = os.environ.get('POSTGREST_URL', 'http://postgrest:3001')

    # Step 1 + 2: query hub_config for both the inline credentials AND
    # the admin_creds_index that maps to env vars. One round trip.
    try:
        r = requests.get(
            f'{pg}/hub_config',
            params={
                'hub_name': f'eq.{hub_name}',
                'select': 'admin_username,admin_password,admin_creds_index',
            },
            timeout=3,
        )
        rows = r.json() if r.status_code == 200 else []
    except Exception:
        rows = []

    row = rows[0] if rows else {}
    user = (row.get('admin_username') or '').strip()
    password = (row.get('admin_password') or '').strip()
    if user and password:
        return (user, password)

    # Fall back to env vars via the slot index.
    slot = row.get('admin_creds_index')
    if slot:
        env_user = (os.environ.get(f'HUBITAT_ADMIN_USER_{slot}') or '').strip()
        env_pw = (os.environ.get(f'HUBITAT_ADMIN_PASSWORD_{slot}') or '').strip()
        if env_user and env_pw:
            return (env_user, env_pw)

    return (None, None)


def get_client(hub_ip: str, hub_name: str) -> HubitatAdminClient:
    """Get or create the singleton client for this hub."""
    key = hub_ip
    with _clients_lock:
        if key not in _clients:
            user, password = _credentials_for_hub(hub_name, hub_ip)
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
