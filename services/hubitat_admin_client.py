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
from typing import Any, Dict, List, Optional, Tuple

import requests

from services.circuit_breaker import get_breaker, CircuitBreakerOpen

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
        # Per-hub circuit breaker. Trips after 5 consecutive request failures
        # within 60s; cools down 30s before letting a probe through. Each hub
        # has its own breaker so hub_72 degrading doesn't open hub_69's.
        # Observable via the future /api/health/breakers endpoint.
        self._breaker = get_breaker(
            f"hubitat:{hub_ip}",
            fail_threshold=5,
            reset_timeout_secs=30.0,
            fail_window_secs=60.0,
        )

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
        """One-time-retry-on-401 request wrapper, gated by the per-hub
        circuit breaker.

        Breaker semantics:
          - A failed call (any exception) increments the failure count.
            After 5 consecutive failures within 60s the breaker opens.
          - While OPEN, this method raises CircuitBreakerOpen instead of
            hitting the hub. Callers can catch it and fall back to
            cached state, or surface the degradation upstream.
          - A 401 that triggers a retry-with-login is NOT counted as a
            failure in the breaker — only the OUTER call's outcome
            matters (the breaker sees one successful response after
            the re-login).

        We pass the wrapped lambda into call_sync so the breaker also
        catches exceptions from the underlying requests library (read
        timeouts, ConnectionError, etc.) — those ARE the failure modes
        we're protecting against.
        """
        url = f"{self.base_url}{path}"

        def _do_request() -> requests.Response:
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
                # Treat 5xx as a failure for breaker purposes: raise so
                # call_sync increments the failure count. The caller's
                # try/except still sees the raised RuntimeError; if the
                # caller wants to inspect the original response.status_code
                # it can catch CircuitBreakerOpen separately.
                if 500 <= r.status_code < 600:
                    raise RuntimeError(
                        f"hubitat {self.hub_name} {method} {path} -> {r.status_code}"
                    )
                return r

        return self._breaker.call_sync(_do_request)

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
        """GET /device/fullJson/<id> → single device with current state.

        Response shape (top-level keys we care about):
          device.id, device.label, device.displayName, device.name
          device.currentStates  →  dict keyed by attribute name, each value
                                   has {value, stringValue, dataType, ...}

        Callers needing Maker-API-shape `{id, label, attributes: [...]}`
        should pass the result through `to_maker_shape()` rather than
        reading these paths inline.
        """
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
        Issue a command via the admin API's runmethod endpoint.

        CURRENT contract (Hubitat firmware 2.5.0.x, C-8):
          POST /device/runmethod
          Content-Type: application/json
          body: {"id": <id>, "method": "<cmd>",
                 "args": [{"type": "<NUMBER|STRING>", "value": <v>}, ...]}
          No-arg commands send "args": []. Success = HTTP 200 with a JSON
          body {"success": true}. Shape taken from the live UI's
          /ui2/js/vue-hub2.min.js.

        HISTORY: firmware 2.5.0.x changed this endpoint from the old
        form-urlencoded shape (id=&method=&arg=&value=) to JSON, with NO
        backward compatibility. A hub that receives the wrong body type
        answers with a bare HTTP 500 ("Server error") rather than a 415,
        which is why this surfaced as "every command fails verification
        with got=None" instead of an obvious content-type error.

        DEFENSIVE: we try JSON first; if the hub rejects it at the
        transport level (>=400 — i.e. it is on a different/older firmware
        that wants the legacy shape), we retry ONCE with the old
        form-urlencoded body. A 200 with {"success": false} is a genuine
        command failure, NOT a transport problem, so it does NOT trigger
        the fallback. This keeps a mixed-firmware fleet working and makes
        the next contract flip self-healing in one direction.
        """
        # --- attempt 1: current JSON contract ---
        json_payload = {
            "id": int(device_id) if str(device_id).isdigit() else device_id,
            "method": command,
            "args": self._build_runmethod_args(argument),
        }
        try:
            r = self._request(
                "POST", "/device/runmethod",
                json=json_payload,
                allow_redirects=False,
            )
            ok, transport_failed = self._runmethod_result(r)

            # --- attempt 2 (fallback): legacy form-urlencoded contract ---
            if transport_failed:
                logger.warning(
                    f"hubitat_admin [{self.hub_name}] device {device_id}/"
                    f"{command}: JSON runmethod rejected (HTTP {r.status_code}) "
                    f"— hub may be on the legacy contract; retrying form-encoded"
                )
                body = {"id": str(device_id), "method": command}
                if argument is not None:
                    body["arg"] = str(argument)
                    body["value"] = str(argument)
                r_fallback = self._request(
                    "POST", "/device/runmethod",
                    data=body,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    allow_redirects=False,
                )
                ok2, _ = self._runmethod_result(r_fallback)
                if ok2:
                    r, ok = r_fallback, True

            if ok:
                logger.info(
                    f"hubitat_admin [{self.hub_name}] device {device_id}/"
                    f"{command} via /device/runmethod HTTP {r.status_code}"
                )
            else:
                logger.warning(
                    f"hubitat_admin [{self.hub_name}] device {device_id}/"
                    f"{command} runmethod not OK: HTTP {r.status_code} "
                    f"body={r.text[:160]!r}"
                )
            return ok
        except Exception as e:
            logger.warning(
                f"hubitat_admin [{self.hub_name}] send_command "
                f"({device_id}/{command}) failed: {e}"
            )
            return False

    @staticmethod
    def _build_runmethod_args(argument: Optional[str]) -> List[Dict[str, Any]]:
        """
        Build the JSON `args` array for /device/runmethod.

        No argument → []. Single argument → one typed entry. Type is
        inferred the way the UI declares it: numeric values are NUMBER,
        everything else STRING. Hubitat coerces within reason, but sending
        the right type avoids parameter-binding surprises (e.g. setLevel).
        """
        if argument is None:
            return []
        s = str(argument)
        try:
            num = float(s)
            # send an int when it is integral (setLevel expects 0-100)
            value: Any = int(num) if num.is_integer() else num
            return [{"type": "NUMBER", "value": value}]
        except (TypeError, ValueError):
            return [{"type": "STRING", "value": s}]

    @staticmethod
    def _runmethod_result(r: requests.Response) -> Tuple[bool, bool]:
        """
        Interpret a /device/runmethod response.

        Returns (ok, transport_failed):
          - ok             — the command was accepted AND not reported failed.
          - transport_failed — the hub rejected the request shape itself
            (HTTP >= 400), signalling we may be speaking the wrong contract
            and should try the fallback encoding.

        A 2xx/3xx with a JSON body {"success": false} is a real command
        failure (ok=False) but NOT a transport failure — we do not retry
        with a different encoding in that case.
        """
        transport_failed = r.status_code >= 400
        if transport_failed:
            return (False, True)
        # 2xx/3xx — inspect the JSON envelope if present.
        ctype = r.headers.get("Content-Type", "")
        if "application/json" in ctype:
            try:
                payload = r.json()
                if isinstance(payload, dict) and payload.get("success") is False:
                    return (False, False)
            except ValueError:
                pass  # not parseable JSON despite header — treat status as truth
        return (True, False)

    def probe_command_path(self, device_id: int) -> Dict[str, Any]:
        """
        Non-mutating health probe of the /device/runmethod TRANSPORT.

        Sends a no-arg `refresh` (harmless) and reports which contract the
        hub accepts. This is the canary used by hub_contract_watch to detect
        the kind of firmware contract flip that broke commands on 2026-05-26.

        We care about the *transport*, not whether the device obeys: a hub on
        the JSON contract answers HTTP 200 (even `{success:false}` if the
        device lacks refresh) → transport OK. Only an HTTP >= 400 means the
        hub rejected the request shape itself.

        Returns:
            {
              "ok": bool,                 # transport accepted by some contract
              "contract": "json"|"form"|"none",
              "status": int,              # last HTTP status observed
              "error": str|None,          # body snippet when ok is False
            }
        """
        # JSON contract (current firmware 2.5.0.x).
        try:
            r = self._request(
                "POST", "/device/runmethod",
                json={"id": int(device_id), "method": "refresh", "args": []},
                allow_redirects=False,
            )
            if not self._runmethod_result(r)[1]:  # not transport_failed
                return {"ok": True, "contract": "json",
                        "status": r.status_code, "error": None}
            last_status, last_body = r.status_code, r.text[:160]
        except Exception as e:
            last_status, last_body = 0, str(e)[:160]

        # Legacy form-urlencoded contract (older firmware).
        try:
            r2 = self._request(
                "POST", "/device/runmethod",
                data={"id": str(device_id), "method": "refresh"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                allow_redirects=False,
            )
            if not self._runmethod_result(r2)[1]:
                return {"ok": True, "contract": "form",
                        "status": r2.status_code, "error": None}
            last_status, last_body = r2.status_code, r2.text[:160]
        except Exception as e:
            last_status, last_body = 0, str(e)[:160]

        return {"ok": False, "contract": "none",
                "status": last_status, "error": last_body}


def to_maker_shape(raw: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Normalize a `/device/fullJson/<id>` response to the Maker-API shape
    the rest of the codebase consumes (`attributes` list of
    `{name, currentValue}` plus `id`/`label`).

    The admin endpoint nests state at `raw['device']['currentStates']`
    as a *dict* keyed by attribute name; Maker returns a *list*. Without
    this conversion, downstream `extract_attribute()` sees no attributes
    and returns None — which is what was breaking command verification.
    """
    if not raw or not isinstance(raw, dict):
        return None
    device = raw.get("device") or {}
    states = device.get("currentStates") or {}
    attrs: List[Dict[str, Any]] = []
    if isinstance(states, dict):
        # Admin nested-dict shape.
        for name, payload in states.items():
            if isinstance(payload, dict):
                attrs.append({
                    "name": payload.get("name") or name,
                    "currentValue": payload.get("value"),
                })
    elif isinstance(states, list):
        # Defensive: some firmware versions may return a list.
        for s in states:
            if isinstance(s, dict):
                attrs.append({
                    "name": s.get("name"),
                    "currentValue": s.get("value"),
                })
    return {
        "id": str(device.get("id", "")),
        "label": (device.get("label") or device.get("displayName")
                  or device.get("name") or ""),
        "attributes": attrs,
    }


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
