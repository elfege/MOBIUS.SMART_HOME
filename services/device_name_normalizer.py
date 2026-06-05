"""
Device Name Normalizer
======================

A GLOBAL, system-wide maintenance pass (NOT a per-instance app type) that strips
a trailing " on <hub name>" suffix from device labels — operating directly on the
Hubitat HUBS, not on this app's canonical device table.

Why scan the hubs, not the app DB
---------------------------------
Hubitat Hub Mesh appends " on <SourceHubLocationName>" to a linked device's label
to disambiguate it (e.g. "Coffee Office" shared from the "Home 2" hub appears on
another hub as "Coffee Office on Home 2"). These mesh/linked copies are exactly
what this project's classifier DEDUPLICATES out (is_primary / linkedDevice), so
they never reach the canonical `devices` table. The suffixes therefore only exist
ON THE HUBS — to clean them we must read each hub's live device list and rename
there. (Renaming changes only the label; the device id is untouched, so eventsocket
WS routing — which is keyed by device id — is unaffected.)

Data-driven matching
--------------------
The set of suffix tokens is the set of HUB LOCATION NAMES, fetched live from each
hub (`GET /hub2/hubData` → `locationName`). Nothing is hardcoded — whatever the
user named their hubs is what we match. A label is cleaned only when it ends with
` on <X>` where <X> is one of those location names (case-insensitive, '_'/space
equivalent). The literal ` on ` guard protects legitimate names that merely end in
a digit ("CAM Living 2") — only the trailing " on <hub>" is removed.

Safety: dry-run first
--------------------
Two system settings (both default false):
  - device_name_normalizer_enabled : master on/off for the scan.
  - device_name_normalizer_apply   : when true, actually rename on the hub;
                                      when false, only LOG the proposed renames.
The UI ("Device naming" panel) shows a mandatory preview of every proposed
"old -> new" rename and only enables (both flags) on explicit confirmation.

Scheduling: piggyback
--------------------
`run_normalizer_pass()` is invoked at the end of each ~2-minute device_cache_refresh
cycle. No separate scheduler job.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Dict, List, Optional, Pattern

import requests

logger = logging.getLogger(__name__)

POSTGREST_URL = os.environ.get("POSTGREST_URL", "http://postgrest:3001")

# ANSI colors for log readability.
_C = "\033[96m"   # cyan — labels
_Y = "\033[93m"   # yellow — proposals
_G = "\033[92m"   # green — applied
_R = "\033[0m"    # reset

SETTING_ENABLED = "device_name_normalizer_enabled"
SETTING_APPLY = "device_name_normalizer_apply"

# Seed the two settings once per process so the operator can find/flip them.
_seeded = False
_seed_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Settings seeding
# ---------------------------------------------------------------------------

def _seed_settings_once() -> None:
    """Insert the two control settings if absent (idempotent, value-preserving).

    Both are seeded with ui_exposed=false: they are NOT meant to appear as raw
    true/false rows in the generic settings grid — the dedicated 'Device naming'
    panel (with the mandatory preview-confirm modal) owns them. We also force
    ui_exposed=false on any row that may have been seeded earlier with true.
    """
    global _seeded
    if _seeded:
        return
    with _seed_lock:
        if _seeded:
            return
        rows = [
            {
                "key": SETTING_ENABLED,
                "value": "false",
                "value_type": "bool",
                "description": (
                    "Device Name Normalizer: master on/off. When true, each device "
                    "sync scans the hubs and cleans any trailing ' on <hub name>' "
                    "suffix from device labels."
                ),
                "ui_exposed": False,
            },
            {
                "key": SETTING_APPLY,
                "value": "false",
                "value_type": "bool",
                "description": (
                    "Device Name Normalizer: when true, actually rename devices on the "
                    "hub. When false, only log proposed renames (dry-run). Managed "
                    "together with the enable flag by the Device-naming UI panel."
                ),
                "ui_exposed": False,
            },
        ]
        try:
            r = requests.post(
                f"{POSTGREST_URL}/system_settings",
                params={"on_conflict": "key"},
                headers={
                    "Content-Type": "application/json",
                    # ignore-duplicates: never overwrite an operator's chosen value.
                    "Prefer": "resolution=ignore-duplicates",
                },
                json=rows,
                timeout=5,
            )
            if r.status_code not in (200, 201, 204):
                logger.warning(
                    f"device_name_normalizer: settings seed non-2xx "
                    f"{r.status_code}: {r.text[:160]}"
                )
            # Force-hide from the generic grid even if rows pre-existed with
            # ui_exposed=true. Value is left untouched.
            for k in (SETTING_ENABLED, SETTING_APPLY):
                try:
                    requests.patch(
                        f"{POSTGREST_URL}/system_settings",
                        params={"key": f"eq.{k}"},
                        json={"ui_exposed": False},
                        headers={"Content-Type": "application/json",
                                 "Prefer": "return=minimal"},
                        timeout=5,
                    )
                except Exception:
                    pass
            logger.info("device_name_normalizer: control settings seeded/verified")
            _seeded = True
        except Exception as e:
            logger.warning(f"device_name_normalizer: settings seed failed: {e}")


def seed_settings() -> None:
    """Public, idempotent seeding entrypoint (used by the API endpoints so the
    setting rows exist before set_system is called against them)."""
    _seed_settings_once()


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _build_suffix_pattern(hub_names: List[str]) -> Optional[Pattern]:
    r"""
    Build a compiled regex that matches a trailing ' on <hub name>' suffix,
    where <hub name> is ANY of the names in ``hub_names`` (the live hub names
    fetched from the hubs — nothing here is hardcoded). Returns None when
    ``hub_names`` is empty/blank (there is nothing to match).

    Regex primer for the final pattern, piece by piece:
        \s+        one or more whitespace characters   (the gap before "on")
        on         the literal word "on"
        \s+        one or more whitespace characters   (the gap after "on")
        (?: ... )  a NON-capturing group ("match one of the things inside")
        a|b|c      alternation: match a OR b OR c
        [_\s]+     one or more of (underscore OR whitespace)
        \s*        zero or more whitespace characters
        $          the end of the string
      ...and re.IGNORECASE makes the whole match case-insensitive.

    Each hub name is turned into ONE alternative inside the (?: ... ) group:
      1. lowercase the name and split it into words on runs of '_' or space:
           "Home 2"     -> ["home", "2"]
           "Coffee_Bar" -> ["coffee", "bar"]
           "hub4"       -> ["hub4"]
      2. re.escape() each word, so a name containing regex-special characters
         (".", "+", "(", ...) is matched literally, not as regex syntax.
      3. rejoin the words with "[_\s]+", so the separator is flexible: a name
         stored as "Home 2" still matches a label written "Home 2", "Home_2",
         or "Home  2".

    Example — IF the four hubs happen to be named "Home 1".."Home 4", the
    compiled pattern is:
        \s+on\s+(?:home[_\s]+1|home[_\s]+2|home[_\s]+3|home[_\s]+4)\s*$
    But "home" is NOT hardcoded — those alternatives are whatever ``hub_names``
    holds. If a hub were named "Coffee Bar", its alternative would instead be
    "coffee[_\s]+bar", and the group would contain that.
    """
    parts: List[str] = []
    for hub_name in hub_names:
        # Split this hub name into lowercase words, dropping empties. re.split on
        # "[_\s]+" treats any run of underscores/spaces as a single separator,
        # so "Home 2" and "home_2" both become ["home", "2"].
        words = [w for w in re.split(r"[_\s]+", (hub_name or "").strip().lower()) if w]
        if not words:
            continue
        # re.escape each word (special chars match literally), then glue the
        # words back with "[_\s]+" so '_' and spaces are interchangeable:
        # ["home", "2"] -> "home[_\s]+2".
        parts.append(r"[_\s]+".join(re.escape(w) for w in words))

    # No usable hub names -> no pattern. The caller treats None as "match
    # nothing", so we never strip a suffix when we don't know the hub names.
    if not parts:
        return None

    # Assemble " on <any hub name>" anchored to the END of the label.
    # "|".join(parts) is the DYNAMIC alternation built from the real hub names
    # above (e.g. "home[_\s]+1|home[_\s]+2|..."), never a fixed word.
    return re.compile(r"\s+on\s+(?:" + "|".join(parts) + r")\s*$", re.IGNORECASE)


def _clean_label(label: str, pattern: Pattern) -> Optional[str]:
    """Return the cleaned label if `label` carries the suffix and the result is
    non-empty and actually different; otherwise None."""
    if not label or not pattern.search(label):
        return None
    new = pattern.sub("", label).rstrip()
    if new and new != label:
        return new
    return None


# ---------------------------------------------------------------------------
# Hub access
# ---------------------------------------------------------------------------

def _get_enabled_hubs() -> List[Dict[str, str]]:
    """[{hub_name, hub_ip}, ...] for enabled hubs (empty on failure)."""
    try:
        r = requests.get(
            f"{POSTGREST_URL}/hub_config",
            params={"is_enabled": "eq.true", "select": "hub_name,hub_ip"},
            timeout=5,
        )
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        logger.warning(f"device_name_normalizer: hub_config fetch failed: {e}")
        return []


def _get_location_tokens(hubs: List[Dict[str, str]]) -> List[str]:
    """
    The set of hub LOCATION NAMES (the strings Hub Mesh appends). Fetched live
    from each hub via the admin client — data-driven, whatever the user named
    their hubs. Deduplicated, case-insensitively, preserving order.
    """
    from services.hubitat_admin_client import get_client
    seen = set()
    names: List[str] = []
    for h in hubs:
        ip = h.get("hub_ip")
        if not ip:
            continue
        try:
            client = get_client(ip, h.get("hub_name", ip))
            loc = client.get_location_name()
        except Exception as e:
            logger.debug(f"device_name_normalizer: location name fetch {ip}: {e}")
            loc = None
        if loc and loc.strip():
            key = loc.strip().lower()
            if key not in seen:
                seen.add(key)
                names.append(loc.strip())
    return names


def _scan_hubs(hubs: List[Dict[str, str]], pattern: Pattern) -> List[Dict[str, str]]:
    """
    Scan every enabled hub's live device list and return proposals:
    [{hub_ip, hub_name, device_id (per-hub Hubitat id), old, new}].
    """
    from services.hubitat_admin_client import get_client
    proposals: List[Dict[str, str]] = []
    for h in hubs:
        ip = h.get("hub_ip")
        name = h.get("hub_name", ip)
        if not ip:
            continue
        try:
            client = get_client(ip, name)
            devices = client.get_all_devices()
        except Exception as e:
            logger.warning(
                f"device_name_normalizer: get_all_devices failed for {name} ({ip}): {e}"
            )
            continue
        for d in devices or []:
            label = d.get("label") or d.get("displayName") or d.get("name") or ""
            new = _clean_label(label, pattern)
            if new is not None:
                proposals.append({
                    "hub_ip": ip,
                    "hub_name": name,
                    "device_id": str(d.get("id") or ""),
                    "old": label,
                    "new": new,
                })
    return proposals


# ---------------------------------------------------------------------------
# Public: preview (no-op) and the main pass
# ---------------------------------------------------------------------------

def preview() -> Dict[str, object]:
    """
    NO-OP dry-run used by the UI's mandatory pre-enable confirmation modal.

    Scans the hubs' live device lists and returns the renames that WOULD be
    applied, WITHOUT changing any setting and WITHOUT touching any device.
    Independent of the enabled/apply gates.
    """
    hubs = _get_enabled_hubs()
    tokens = _get_location_tokens(hubs)
    pattern = _build_suffix_pattern(tokens)
    if pattern is None:
        return {"count": 0, "proposals": [], "hubs": tokens}
    proposals = _scan_hubs(hubs, pattern)
    return {"count": len(proposals), "proposals": proposals, "hubs": tokens}


def trigger_pass_background() -> None:
    """Run one normalizer pass off-thread (used right after the user enables the
    feature so the just-previewed renames are applied immediately)."""
    threading.Thread(
        target=run_normalizer_pass, name="dnn-manual-pass", daemon=True
    ).start()


def run_normalizer_pass() -> Dict[str, object]:
    """
    One scan pass over the hubs. Safe to call on every device_cache_refresh cycle.

    Returns a small summary dict. Never raises — all failures are logged and
    swallowed so it can't disrupt the refresh cycle it piggybacks on.
    """
    _seed_settings_once()

    from services.settings_resolver import get_resolver
    resolver = get_resolver()

    if not resolver.get_system(SETTING_ENABLED, False):
        return {"enabled": False, "proposals": 0, "applied": 0}

    apply = bool(resolver.get_system(SETTING_APPLY, False))

    hubs = _get_enabled_hubs()
    tokens = _get_location_tokens(hubs)
    pattern = _build_suffix_pattern(tokens)
    if pattern is None:
        logger.warning(
            "device_name_normalizer: no hub location names available, skipping"
        )
        return {"enabled": True, "apply": apply, "proposals": 0, "applied": 0}

    proposals = _scan_hubs(hubs, pattern)
    if not proposals:
        logger.debug("device_name_normalizer: no hub labels need cleaning")
        return {"enabled": True, "apply": apply, "proposals": 0, "applied": 0}

    tag = "APPLY" if apply else "DRY-RUN"
    logger.info(
        f"device_name_normalizer [{tag}]: {_Y}{len(proposals)}{_R} label(s) to clean "
        f"(matching: {', '.join(tokens)})"
    )
    for p in proposals:
        logger.info(
            f"  [{tag}] {p['hub_name']} #{p['device_id']}: "
            f"{_C}{p['old']!r}{_R} -> {_G}{p['new']!r}{_R}"
        )

    if not apply:
        return {"enabled": True, "apply": False, "proposals": len(proposals), "applied": 0}

    # --- apply path: rename on the owning hub ---
    from services.hubitat_admin_client import get_client
    applied = 0
    for p in proposals:
        if not p["device_id"]:
            logger.warning(f"  [SKIP] {p['hub_name']}: missing device id for {p['old']!r}")
            continue
        client = get_client(p["hub_ip"], p["hub_name"])
        if client.set_device_label(p["device_id"], p["new"]):
            applied += 1
            logger.info(f"  [{_G}APPLIED{_R}] {p['hub_name']} #{p['device_id']}")
        else:
            logger.warning(
                f"  [FAILED] {p['hub_name']} #{p['device_id']} rename did not confirm"
            )

    logger.info(
        f"device_name_normalizer: applied {_G}{applied}{_R}/{len(proposals)}"
    )
    return {"enabled": True, "apply": True, "proposals": len(proposals), "applied": applied}
