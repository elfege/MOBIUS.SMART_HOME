"""
Hubitat admin-API contract-drift watcher.

Why this exists
---------------
On 2026-05-26 every outbound command silently broke: Hubitat firmware
2.5.0.143 changed POST /device/runmethod from form-urlencoded to a JSON body
with no backward compatibility, and answered the old shape with a bare
HTTP 500 instead of a 415. Event intake (WS) and reads (GET) kept working, so
the failure masqueraded as "verify-poll got None" for hours before anyone
could see it was a command-transport contract flip.

This watcher makes the next such flip self-announcing. Two signals:

  1. Firmware version drift — poll each enabled hub's /hub/details/json
     `platformVersion`. A change is the *trigger* ("go look").
  2. Command-path canary — run a harmless no-arg `refresh` through the admin
     client's runmethod path (HubitatAdminClient.probe_command_path) and
     record whether the TRANSPORT was accepted and which contract answered
     (json | form | none). This detects breakage *directly*, regardless of
     cause — it would have caught 2026-05-26 on its own.

State is persisted into `hub_health` (the existing per-hub health table) and
surfaced on the Hubs settings cards. A WARN log fires only on a TRANSITION
(version change, or command path flipping ok<->broken), never every pass.

Cadence: 6h default. Firmware changes are rare; the canary is cheap but does
not need to be hot. First pass runs at boot in a background thread.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


def _postgrest_url() -> str:
    return os.environ.get('POSTGREST_URL', 'http://postgrest:3001')


def _enabled_hubs() -> List[Dict]:
    """Return [{id, hub_name, hub_ip}] for every enabled hub_config row."""
    pg = _postgrest_url()
    try:
        r = requests.get(
            f'{pg}/hub_config',
            params={
                'is_enabled': 'eq.true',
                'select': 'id,hub_name,hub_ip',
                'order': 'id',
            },
            timeout=4,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug(f"contract_watch: hub_config lookup failed: {e}")
    return []


def _query_platform_version(hub_ip: str, hub_name: str) -> Optional[str]:
    """GET /hub/details/json → platformVersion (e.g. '2.5.0.143'), or None."""
    try:
        from services.hubitat_admin_client import get_client
        client = get_client(hub_ip, hub_name)
        r = client._request('GET', '/hub/details/json')
        if r.status_code != 200:
            return None
        d = r.json()
        # platformVersion is the canonical field; fall back to the older key.
        return d.get('version') or d.get('platformVersion') \
            or d.get('firmwareVersionString')
    except Exception as e:
        logger.debug(f"contract_watch: /hub/details/json on {hub_name}: {e}")
    return None


def _canary_device_id(hub_ip: str) -> Optional[int]:
    """Pick any canonical device on this hub to probe `refresh` against.

    The probe checks the runmethod TRANSPORT, not the device, so any device
    id on the hub works — even one that doesn't support refresh (that returns
    HTTP 200 {success:false}, which is still a healthy transport).
    """
    pg = _postgrest_url()
    try:
        r = requests.get(
            f'{pg}/devices',
            params={
                'hub_ip': f'eq.{hub_ip}',
                'select': 'hubitat_id',
                'limit': '1',
            },
            timeout=4,
        )
        if r.status_code == 200 and r.json():
            return int(r.json()[0]['hubitat_id'])
    except Exception as e:
        logger.debug(f"contract_watch: canary-device lookup ({hub_ip}): {e}")
    return None


def _run_canary(hub_ip: str, hub_name: str) -> Dict:
    """Probe the command path. Returns the probe dict, or a 'no_device'
    sentinel when we have no canonical device to probe against yet."""
    did = _canary_device_id(hub_ip)
    if did is None:
        return {"ok": None, "contract": "unknown",
                "status": 0, "error": "no canonical device on hub yet"}
    from services.hubitat_admin_client import get_client
    return get_client(hub_ip, hub_name).probe_command_path(did)


def _current_health_row(hub_id: int) -> Dict:
    """Fetch the current hub_health row (for transition detection)."""
    pg = _postgrest_url()
    try:
        r = requests.get(
            f'{pg}/hub_health',
            params={
                'hub_id': f'eq.{hub_id}',
                'select': 'platform_version,command_path_ok',
                'limit': '1',
            },
            timeout=4,
        )
        if r.status_code == 200 and r.json():
            return r.json()[0]
    except Exception as e:
        logger.debug(f"contract_watch: health lookup (hub {hub_id}): {e}")
    return {}


def _persist(hub_id: int, version: Optional[str], version_changed: bool,
             canary: Dict) -> None:
    """PATCH the hub_health row with version + command-path status.

    hub_health rows are seeded for every enabled hub at boot, so a PATCH by
    hub_id lands. Only the contract-watch columns are written — WS/reconcile
    columns are left untouched.
    """
    pg = _postgrest_url()
    now_iso = datetime.now(timezone.utc).isoformat()
    patch: Dict = {
        'command_path_ok': canary.get('ok'),
        'command_path_contract': canary.get('contract'),
        'command_path_checked_at': now_iso,
        'command_path_error': canary.get('error'),
    }
    if version is not None:
        patch['platform_version'] = version
        if version_changed:
            patch['platform_version_seen_at'] = now_iso
    try:
        requests.patch(
            f'{pg}/hub_health',
            params={'hub_id': f'eq.{hub_id}'},
            json=patch,
            headers={'Content-Type': 'application/json'},
            timeout=4,
        )
    except Exception as e:
        logger.warning(f"contract_watch: persist failed (hub {hub_id}): {e}")


def run_watch_pass() -> Dict:
    """One pass over all enabled hubs. Returns a small report for logging."""
    hubs = _enabled_hubs()
    if not hubs:
        return {'status': 'no_hubs'}

    report: Dict = {'status': 'ok', 'hubs': []}
    for hub in hubs:
        hub_id, hub_name, hub_ip = hub['id'], hub['hub_name'], hub['hub_ip']
        prev = _current_health_row(hub_id)

        version = _query_platform_version(hub_ip, hub_name)
        prev_version = prev.get('platform_version')
        version_changed = bool(version) and version != prev_version

        canary = _run_canary(hub_ip, hub_name)
        prev_ok = prev.get('command_path_ok')

        _persist(hub_id, version, version_changed, canary)

        # WARN only on a transition — quiet on steady state.
        if version_changed and prev_version is not None:
            logger.warning(
                f"contract_watch [{hub_name}]: firmware changed "
                f"{prev_version!r} -> {version!r} — admin-API command "
                f"contract may have shifted; canary={canary.get('contract')} "
                f"ok={canary.get('ok')}"
            )
        if canary.get('ok') is False and prev_ok is not False:
            logger.warning(
                f"contract_watch [{hub_name}]: COMMAND PATH BROKEN — "
                f"runmethod rejected (HTTP {canary.get('status')}, "
                f"contract={canary.get('contract')}). Firmware "
                f"{version or prev_version}. Lights/automations will not "
                f"actuate. Body: {canary.get('error')!r}"
            )
        elif canary.get('ok') is True and prev_ok is False:
            logger.warning(
                f"contract_watch [{hub_name}]: command path RECOVERED "
                f"(contract={canary.get('contract')})"
            )
        elif canary.get('contract') == 'form':
            # Healthy but on the legacy contract — worth a one-line note.
            logger.info(
                f"contract_watch [{hub_name}]: command path OK via LEGACY "
                f"form-encoded contract (firmware {version})"
            )

        report['hubs'].append({
            'hub': hub_name,
            'version': version,
            'version_changed': version_changed,
            'command_path_ok': canary.get('ok'),
            'contract': canary.get('contract'),
        })
    return report


def schedule_watch_job(scheduler, interval_seconds: int = 21600) -> str:
    """Register the recurring contract-watch pass with APScheduler.

    Default cadence 6h (21600s). Returns the job id.
    """
    job_id = 'hub_contract_watch'
    scheduler.add_job(
        func=run_watch_pass,
        trigger='interval',
        seconds=interval_seconds,
        id=job_id,
        replace_existing=True,
    )
    logger.info(f"contract_watch: scheduled every {interval_seconds}s")
    return job_id
