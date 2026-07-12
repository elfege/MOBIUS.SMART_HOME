"""
ClipFactory — local, cached audio clips for Sonos announcements
===============================================================
Sonos has no "speak text" command: you hand it a URL to an audio file and it
plays it. This module turns announcement text into a playable mp3 URL, fully
locally, with three layers (first hit wins):

    1. CANONICAL clips  — pre-recorded mp3s tracked in static/audio/sonos/.
       Served by the existing /static mount. Always present; no runtime TTS
       needed. This is the "pre-recorded fixed clips to start" decision.
    2. GENERATED cache  — if a phrase has no canonical clip and a local TTS
       backend (Piper, then espeak-ng) is installed, synthesize once into
       state/sonos_clips/ and reuse. Drop-in path for dynamic text later.
    3. ALERT fallback   — a synthesized two-tone chime (ffmpeg), so an
       announcement is still AUDIBLE even with no speech backend. Logged loudly
       so the silent-failure mode never hides.

Clip integrity guard (the Anamnesis 0-byte-MP3 lesson): every generated clip is
written to a temp file, checked for non-zero size, then atomically renamed —
Sonos is never handed a half-written or empty file.

LAN-only. No cloud TTS (gTTS/Polly are intentionally excluded).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Canonical (pre-recorded, tracked) clips — read-only, served via /static.
CANONICAL_DIR = os.path.join(_ROOT, "static", "audio", "sonos")
# Runtime-generated cache. Must be writable by the CONTAINER user (uid 999),
# which neither ./state (root-owned) nor ./static (host uid 1000) are — so it
# defaults to a tmp dir. Ephemeral is fine: it's a regenerable cache. Override
# with SONOS_CLIP_CACHE_DIR to make it persistent on a writable volume.
CACHE_DIR = os.environ.get("SONOS_CLIP_CACHE_DIR", "/tmp/sonos_clips")

# Public route paths (must match the FastAPI routes registered in app.py).
_CANONICAL_URL = "/static/audio/sonos"   # served by StaticFiles
_CACHE_URL = "/api/sonos/clip"           # served by services.sonos.routes
_ALERT_NAME = "alert"                     # generic chime, used as last resort


def slugify(text: str) -> str:
    """Stable filesystem-safe name for a phrase (also the cache key)."""
    s = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return s or "clip"


class ClipFactory:
    """Resolves announcement text -> a LAN-reachable clip URL, generating and
    caching on demand. Stateless apart from the on-disk cache."""

    def __init__(self, base_url: str | None = None) -> None:
        """``base_url`` is the http origin Sonos will fetch from, e.g.
        ``http://<LAN_IP>:5001``. Defaults from SERVER_IP/APP_EXTERNAL_PORT
        (the app's DIRECT http port — clips must NOT go through the self-signed
        nginx, which Sonos won't fetch)."""
        ip = os.environ.get("SERVER_IP", "<LAN_IP>")
        port = os.environ.get("APP_EXTERNAL_PORT", "5001")
        self.base_url = (base_url or f"http://{ip}:{port}").rstrip("/")
        # Anamnesis avatar TTS — the GOOD voices (Edge neural + cloned XTTS).
        # Edge voices are CPU-side (no GPU → safe under the office-GPU isolation
        # rule); XTTS cloned voices need a worker. Default to a warm Edge voice.
        self.anamnesis_url = os.environ.get("ANAMNESIS_URL", "http://<LAN_IP>:3010").rstrip("/")
        self.default_voice = os.environ.get("SONOS_TTS_VOICE", "edge:en-US-AvaNeural")
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
        except OSError as e:
            # Non-fatal: canonical clips (served from /static) still work; only
            # the runtime-generated cache is unavailable.
            logger.warning("Sonos clip cache dir %s not creatable: %s", CACHE_DIR, e)

    def clip_url(self, text: str, voice: str | None = None) -> str | None:
        """Return a fully-qualified clip URL for ``text`` in ``voice``.

        ``voice`` is a voice id: ``edge:<MsVoice>`` or ``xtts:<slug>`` (both via
        Anamnesis), ``piper:<model>``, or ``espeak``; None uses the default
        (a warm Edge neural voice). Resolution order:
          1. per-voice runtime cache (state)            → reuse
          2. synthesize via the requested voice's engine → cache + serve
          3. voice-agnostic canonical clip (static)      → pre-recorded fallback
          4. alert chime                                 → never-silent last resort
        Returns None only if even the chime can't be produced.
        """
        voice = voice or self.default_voice
        name = f"{slugify(text)}__{slugify(voice)}"

        cached = os.path.join(CACHE_DIR, f"{name}.mp3")
        if _nonempty(cached):
            return f"{self.base_url}{_CACHE_URL}/{name}.mp3"

        if self._synthesize(text, voice, cached):
            return f"{self.base_url}{_CACHE_URL}/{name}.mp3"

        # Fallback: a voice-agnostic pre-recorded clip for the standard phrases.
        base_name = slugify(text)
        if _nonempty(os.path.join(CANONICAL_DIR, f"{base_name}.mp3")):
            logger.warning("Sonos: voice %r unavailable for %r — using canonical clip", voice, text)
            return f"{self.base_url}{_CANONICAL_URL}/{base_name}.mp3"

        logger.warning(
            "Sonos: no voice and no canonical clip for %r — using alert chime.", text)
        if self._ensure_alert():
            return f"{self.base_url}{_CACHE_URL}/{_ALERT_NAME}.mp3"
        return None

    # ---- generation backends --------------------------------------------
    def _synthesize(self, text: str, voice: str, dest: str) -> bool:
        """Synthesize ``text`` to ``dest`` (mp3) using the engine for ``voice``.
        Returns True only if the REQUESTED voice produced a verified file (so
        clip_url can fall back to a canonical clip rather than caching a wrong
        voice under this name). Never raises."""
        try:
            if voice.startswith(("edge:", "xtts:")):
                return self._anamnesis(text, voice, dest)
            if voice.startswith("piper:"):
                return self._piper(text, dest)
            if voice.startswith("espeak"):
                return self._espeak(text, dest)
            # Unknown id: try the default Edge voice, then local espeak.
            return self._anamnesis(text, self.default_voice, dest) or self._espeak(text, dest)
        except Exception as e:  # never let TTS crash an announcement
            logger.warning("Sonos TTS for voice %r failed: %s", voice, e)
            return False

    def _anamnesis(self, text: str, voice: str, dest: str) -> bool:
        """Synthesize via Anamnesis avatar TTS (Edge neural or cloned XTTS),
        download the resulting mp3, and cache it locally (integrity-guarded).

        ``edge:<id>`` → POST /api/avatar/preview-edge (CPU, no GPU).
        ``xtts:<slug>`` → POST /api/avatar/voices/{slug}/preview (needs a worker).
        """
        import json as _json
        import urllib.request
        if voice.startswith("edge:"):
            url = f"{self.anamnesis_url}/api/avatar/preview-edge"
            payload = {"voice_id": voice, "text": text}
            timeout = 30
        else:  # xtts:<slug>
            slug = voice.split(":", 1)[1]
            url = f"{self.anamnesis_url}/api/avatar/voices/{slug}/preview"
            payload = {"text": text}
            timeout = 60
        req = urllib.request.Request(
            url, data=_json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        resp = _json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
        audio_url = resp.get("audio_url")
        if not audio_url:
            logger.warning("Sonos: Anamnesis TTS returned no audio_url for %r", voice)
            return False
        full = audio_url if audio_url.startswith("http") else f"{self.anamnesis_url}{audio_url}"
        data = urllib.request.urlopen(full, timeout=timeout).read()
        tmp = dest + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        return _commit(tmp, dest)

    def _piper(self, text: str, dest: str) -> bool:
        piper = shutil.which("piper")
        model = os.environ.get("SONOS_PIPER_MODEL")
        if not piper or not model or not os.path.isfile(model):
            return False
        wav = dest + ".wav"
        subprocess.run([piper, "-m", model, "-f", wav], input=text.encode(),
                       check=True, capture_output=True, timeout=30)
        return self._to_mp3(wav, dest)

    def _espeak(self, text: str, dest: str) -> bool:
        espeak = shutil.which("espeak-ng") or shutil.which("espeak")
        if not espeak:
            return False
        wav = dest + ".wav"
        subprocess.run([espeak, "-w", wav, text], check=True, capture_output=True, timeout=30)
        return self._to_mp3(wav, dest)

    def _ensure_alert(self) -> bool:
        """Lazily create the generic two-tone chime fallback (ffmpeg)."""
        dest = os.path.join(CACHE_DIR, f"{_ALERT_NAME}.mp3")
        if _nonempty(dest):
            return True
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return False
        tmp = dest + ".tmp"
        try:
            subprocess.run(
                [ffmpeg, "-y",
                 "-f", "lavfi", "-i", "sine=frequency=784:duration=0.4",
                 "-f", "lavfi", "-i", "sine=frequency=988:duration=0.4",
                 "-filter_complex", "[0][1]concat=n=2:v=0:a=1",
                 "-codec:a", "libmp3lame", "-q:a", "4", "-f", "mp3", tmp],  # -f mp3: .tmp ext
                check=True, capture_output=True, timeout=15)
            return _commit(tmp, dest)
        except Exception as e:
            logger.warning("Sonos alert-chime generation failed: %s", e)
            return False

    @staticmethod
    def _to_mp3(wav: str, dest: str) -> bool:
        """Convert a wav to mp3 with the integrity guard (temp -> verify -> rename)."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg or not _nonempty(wav):
            return False
        tmp = dest + ".tmp"
        try:
            # -f mp3 is REQUIRED: ffmpeg infers the muxer from the output
            # extension, and we write to a ".tmp" file (atomic-rename guard),
            # which it can't infer → "no suitable output format" (exit 234).
            subprocess.run([ffmpeg, "-y", "-i", wav, "-codec:a", "libmp3lame",
                            "-q:a", "4", "-f", "mp3", tmp],
                           check=True, capture_output=True, timeout=20)
            return _commit(tmp, dest)
        finally:
            try:
                os.remove(wav)
            except OSError:
                pass


def _nonempty(path: str) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _commit(tmp: str, dest: str) -> bool:
    """Atomically publish ``tmp`` as ``dest`` only if it is non-empty."""
    if not _nonempty(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    os.replace(tmp, dest)
    return True
