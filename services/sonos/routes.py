"""
FastAPI routes for the Sonos subsystem
======================================
Two concerns:
    1. Serve generated/cached clips over PLAIN HTTP (Sonos won't fetch our
       self-signed-nginx HTTPS). Canonical clips are served by the existing
       /static mount; this route serves the runtime-generated cache.
    2. Small operator/test surface: list speakers, fire a manual announcement.

Registered from app.py via ``register_sonos_routes(app)``.
"""

from __future__ import annotations

import logging
import os
import re

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse

from . import get_announce_service, get_sonos
from . import store
from .tts import CACHE_DIR

logger = logging.getLogger(__name__)

_CLIP_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*\.mp3$")  # allow _ (voice separator); no path traversal


def register_sonos_routes(app) -> None:
    """Attach Sonos clip-serving + test routes to the FastAPI ``app``."""

    @app.get("/api/sonos/clip/{name}", include_in_schema=False)
    async def sonos_clip(name: str):
        """Serve a runtime-generated clip as audio/mpeg (Sonos fetches this)."""
        if not _CLIP_NAME.match(name):
            return JSONResponse({"error": "bad clip name"}, status_code=400)
        path = os.path.join(CACHE_DIR, name)
        if not (os.path.isfile(path) and os.path.getsize(path) > 0):
            return JSONResponse({"error": "clip not found"}, status_code=404)
        return FileResponse(path, media_type="audio/mpeg")

    @app.get("/api/sonos/speakers", include_in_schema=False)
    async def sonos_speakers():
        """Discover and list Sonos speakers (ip -> room)."""
        try:
            return JSONResponse({"speakers": get_sonos().discover()})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/sonos/announce", include_in_schema=False)
    async def sonos_announce(request: Request):
        """Fire a manual announcement. Body: ``{room, text, volume?}``.

        Returns immediately (the announce runs in the background); intended for
        operator testing of the subsystem independent of any app.
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        room = (body or {}).get("room")
        text = (body or {}).get("text")
        if not room or not text:
            return JSONResponse({"error": "room and text are required"}, status_code=400)
        volume = (body or {}).get("volume")
        voice = (body or {}).get("voice")
        get_announce_service().announce_in_background(room, text, volume=volume, voice=voice)
        return JSONResponse({"queued": True, "room": room, "text": text, "voice": voice})

    # ---- driver command surface (the /sonos controller page) -------------
    @app.get("/api/sonos/state", include_in_schema=False)
    async def sonos_state():
        """Per-speaker live state for the driver UI: volume, transport state,
        and the current track title/URI. One coordinator per room."""
        import asyncio
        s = get_sonos()
        speakers = s.discover()
        # De-dupe to one coordinator per room so we don't list bonded satellites.
        seen_coord: dict[str, dict] = {}
        for ip, room in speakers.items():
            coord = s.coordinator_ip_for(ip)
            if coord in seen_coord:
                continue
            seen_coord[coord] = {"ip": coord, "room": room}

        def _probe(entry):
            ip = entry["ip"]
            try:
                media = s.media_info(ip)
                entry["volume"] = s.volume(ip)
                entry["state"] = s.transport_state(ip)
                entry["current_uri"] = media.get("CurrentURI", "") or ""
            except Exception as e:
                entry["error"] = str(e)
            # Persisted state (volume lock) from the DB.
            row = store.get_speaker(ip) or {}
            entry["persist_volume"] = bool(row.get("persist_volume"))
            entry["persisted_level"] = row.get("persisted_level")
            return entry

        entries = await asyncio.gather(
            *[asyncio.to_thread(_probe, e) for e in seen_coord.values()])
        return JSONResponse({"speakers": list(entries)})

    @app.post("/api/sonos/volume", include_in_schema=False)
    async def sonos_volume(request: Request):
        """setVolume command. Body ``{room, volume}``. Remembers the prior
        volume so restoreVolume can undo it."""
        body = await request.json()
        room, vol = (body or {}).get("room"), (body or {}).get("volume")
        if not room or vol is None:
            return JSONResponse({"error": "room and volume required"}, status_code=400)
        s = get_sonos()
        ip = s.target_ip(room)
        if not ip:
            return JSONResponse({"error": "speaker not found"}, status_code=404)
        try:
            store.upsert_speaker(ip, room=room, saved_volume=s.volume(ip))  # DB: remember prior
            s.set_volume(ip, int(vol))
            return JSONResponse({"ok": True, "room": room, "volume": int(vol)})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    @app.post("/api/sonos/restore-volume", include_in_schema=False)
    async def sonos_restore_volume(request: Request):
        """restoreVolume command. Body ``{room}``. Restores the volume saved by
        the last setVolume on this speaker (no-op if none saved)."""
        body = await request.json()
        room = (body or {}).get("room")
        s = get_sonos()
        ip = s.target_ip(room) if room else None
        if not ip:
            return JSONResponse({"error": "speaker not found"}, status_code=404)
        row = store.get_speaker(ip) or {}
        prior = row.get("saved_volume")
        if prior is None:
            return JSONResponse({"ok": False, "error": "no saved volume"}, status_code=200)
        try:
            s.set_volume(ip, int(prior))
            return JSONResponse({"ok": True, "room": room, "volume": int(prior)})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    @app.post("/api/sonos/persist-volume", include_in_schema=False)
    async def sonos_persist_volume(request: Request):
        """Volume-LOCK option. Body ``{room, enabled, level?}``. When enabled,
        the webhook hook re-asserts ``level`` (defaults to the speaker's current
        volume) whenever another system changes this speaker's volume."""
        body = await request.json()
        room, enabled = (body or {}).get("room"), (body or {}).get("enabled")
        if not room or enabled is None:
            return JSONResponse({"error": "room and enabled required"}, status_code=400)
        s = get_sonos()
        ip = s.target_ip(room)
        if not ip:
            return JSONResponse({"error": "speaker not found"}, status_code=404)
        try:
            level = (body or {}).get("level")
            if enabled and level is None:
                level = s.volume(ip)          # lock at the current level by default
            store.upsert_speaker(ip, room=room, persist_volume=bool(enabled),
                                 persisted_level=int(level) if level is not None else None)
            return JSONResponse({"ok": True, "room": room,
                                 "persist_volume": bool(enabled), "persisted_level": level})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    @app.post("/api/sonos/play", include_in_schema=False)
    async def sonos_play(request: Request):
        """play command. Body ``{room, url, volume?}``. Plays an mp3 URL on the
        speaker (no snapshot/restore — a deliberate 'play this now')."""
        body = await request.json()
        room, url = (body or {}).get("room"), (body or {}).get("url")
        if not room or not url:
            return JSONResponse({"error": "room and url required"}, status_code=400)
        s = get_sonos()
        ip = s.target_ip(room)
        if not ip:
            return JSONResponse({"error": "speaker not found"}, status_code=404)
        try:
            vol = (body or {}).get("volume")
            if vol is not None:
                s.set_volume(ip, int(vol))
            s.set_uri(ip, url)
            s.play(ip)
            return JSONResponse({"ok": True, "room": room, "url": url})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    @app.post("/api/sonos/stop", include_in_schema=False)
    async def sonos_stop(request: Request):
        """stop command. Body ``{room}``."""
        body = await request.json()
        room = (body or {}).get("room")
        s = get_sonos()
        ip = s.target_ip(room) if room else None
        if not ip:
            return JSONResponse({"error": "speaker not found"}, status_code=404)
        try:
            s.stop(ip)
            return JSONResponse({"ok": True, "room": room})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    @app.get("/api/sonos/voice-preview", include_in_schema=False)
    async def sonos_voice_preview(voice: str, text: str = "Screen time ends in five minutes"):
        """Audition a voice IN THE BROWSER (no Sonos). Proxies Anamnesis TTS for
        ``voice`` + ``text`` and returns the mp3 so a <audio>/Audio() can play it.
        Powers the per-voice ▶ buttons in the voice picker."""
        import json as _json
        import urllib.request
        from .tts import ClipFactory
        base = ClipFactory().anamnesis_url
        try:
            if voice.startswith("edge:"):
                url, payload = f"{base}/api/avatar/preview-edge", {"voice_id": voice, "text": text}
            elif voice.startswith("xtts:"):
                url = f"{base}/api/avatar/voices/{voice.split(':', 1)[1]}/preview"
                payload = {"text": text}
            else:
                return JSONResponse({"error": "unknown voice id"}, status_code=400)
            req = urllib.request.Request(url, data=_json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            audio_url = _json.loads(urllib.request.urlopen(req, timeout=30).read().decode()).get("audio_url")
            if not audio_url:
                return JSONResponse({"error": "no audio"}, status_code=502)
            full = audio_url if audio_url.startswith("http") else f"{base}{audio_url}"
            data = urllib.request.urlopen(full, timeout=30).read()
            from fastapi.responses import Response as _Resp
            return _Resp(content=data, media_type="audio/mpeg")
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    @app.get("/api/sonos/voices", include_in_schema=False)
    async def sonos_voices():
        """List available TTS voices (proxied from Anamnesis avatar voices) for
        the STP voice-selection UI. Returns Edge neural presets + cloned XTTS
        voices, each as a selectable ``{id, name}`` (id = what STP stores)."""
        import json as _json
        import urllib.request
        from .tts import ClipFactory
        base = ClipFactory().anamnesis_url
        try:
            raw = urllib.request.urlopen(f"{base}/api/avatar/voices", timeout=6).read()
            data = _json.loads(raw)
        except Exception as e:
            return JSONResponse({"error": f"voices unavailable: {e}", "voices": []}, status_code=502)
        voices = [{"id": p["id"], "name": p.get("name", p["id"])} for p in data.get("presets", [])]
        for c in data.get("cloned", []):
            slug = c.get("slug")
            if slug:
                voices.append({"id": f"xtts:{slug}", "name": f"{c.get('name', slug)} (cloned)"})
        return JSONResponse({"voices": voices, "default": data.get("default_voice_id")})

    logger.info("Sonos routes registered (/api/sonos/clip, /speakers, /announce, /voices)")
