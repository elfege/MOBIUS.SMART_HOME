"""
Encrypted vault for Matter device setup codes (AES-256-GCM).

WHY THIS EXISTS (see docs/plans/matter_reclaim_as_primary_...md):
A commissioned Matter device stores only the SPAKE2+ *verifier*, never the
passcode — no cluster exposes the setup code, so retrieval is cryptographically
impossible. The ONLY way to have a device's FACTORY setup code at "reclaim as
primary" time (wipe fabrics -> re-commission from factory advertising) is to
have captured it at first commission and stored it encrypted. This module is
that store's crypto boundary.

SECURITY POSTURE (RULE 13):
- Codes are encrypted at rest with AES-256-GCM (authenticated). Plaintext codes
  never touch the DB and never leave this process except through decrypt().
- The key comes ONLY from the environment (`MATTER_CODE_ENC_KEY`, injected from
  the SMARTHOME AWS secret via start.sh) — NEVER hardcoded, NEVER a default.
- FAIL-CLOSED: if the `cryptography` library is missing OR the key is
  absent/malformed, the vault reports unavailable and callers MUST skip capture.
  There is deliberately no plaintext fallback.
- The `matter_device_codes` table is SERVER-SIDE ONLY — it is never exposed
  through PostgREST/the `api` view schema (even ciphertext should not be a
  public REST resource).

KEY FORMAT: `MATTER_CODE_ENC_KEY` is a base64- or hex-encoded 32-byte (256-bit)
key. Generate one with, e.g.:  `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`
"""

import base64
import binascii
import hashlib
import logging
import os
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_ENV_KEY = "MATTER_CODE_ENC_KEY"
_KEY_LEN = 32          # AES-256
_NONCE_LEN = 12        # GCM standard nonce


def _load_key() -> Optional[bytes]:
    """Decode the 32-byte key from the env var (base64 first, then hex).
    Returns None (with a one-time-ish warning) if absent or malformed."""
    raw = os.environ.get(_ENV_KEY, "").strip()
    if not raw:
        return None
    for decoder in (base64.b64decode, bytes.fromhex):
        try:
            key = decoder(raw)
            if len(key) == _KEY_LEN:
                return key
        except (binascii.Error, ValueError):
            continue
    logger.warning(
        f"{_ENV_KEY} is set but not a valid base64/hex 32-byte key — "
        f"setup-code vault DISABLED (fail-closed)."
    )
    return None


def _aesgcm(key: bytes):
    """Lazy-import the AESGCM primitive so the app still boots when the
    `cryptography` wheel isn't in the image yet (fail-closed, not fail-crash)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key)


def is_available() -> bool:
    """True only if BOTH the crypto lib is importable AND a valid key is present.
    Callers gate capture on this — never store a code when it's False."""
    if _load_key() is None:
        return False
    try:
        import cryptography  # noqa: F401
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        return True
    except ImportError:
        logger.warning(
            "cryptography library not installed — setup-code vault DISABLED "
            "(add `cryptography` to requirements.txt + ./deploy.sh to enable)."
        )
        return False


def key_fingerprint() -> Optional[str]:
    """Short non-secret id of the active key (sha256 prefix) for rotation
    tracking. Never reveals the key. None if no key."""
    key = _load_key()
    return hashlib.sha256(key).hexdigest()[:12] if key else None


def encrypt(plaintext: str) -> Optional[Tuple[bytes, bytes]]:
    """
    Encrypt a setup code. Returns (ciphertext, nonce) or None if the vault is
    unavailable (fail-closed) or plaintext is empty. Never raises to the caller.
    """
    if not plaintext:
        return None
    key = _load_key()
    if key is None:
        return None
    try:
        nonce = os.urandom(_NONCE_LEN)
        ct = _aesgcm(key).encrypt(nonce, plaintext.encode("utf-8"), None)
        return ct, nonce
    except ImportError:
        return None
    except Exception as e:  # noqa: BLE001 — vault must never break the caller
        logger.warning(f"setup-code encrypt failed (fail-closed): {e}")
        return None


def decrypt(ciphertext: bytes, nonce: bytes) -> Optional[str]:
    """
    Decrypt a stored setup code. Returns the plaintext, or None if the vault is
    unavailable or the ciphertext fails authentication (tamper/wrong key).
    """
    key = _load_key()
    if key is None or not ciphertext or not nonce:
        return None
    try:
        pt = _aesgcm(key).decrypt(bytes(nonce), bytes(ciphertext), None)
        return pt.decode("utf-8")
    except ImportError:
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"setup-code decrypt failed (bad key or tampered ciphertext): {e}")
        return None
