"""v36 (relay): stay signed in through relay restarts and deploys.

Sessions live in memory, so a restart used to sign everyone out. Now the relay gives each app an
encrypted "resume" code (AES-GCM) holding the session id, the user and the current FMS refresh token.
The app sends it back in the X-Resume header. When a request arrives with a session id the relay
does not know (it restarted) and a valid resume code for that same id, the relay renews the FMS token
with the refresh token and carries on under the same session id. Nothing is stored on the relay's
disk or in GitHub; the code is useless without the relay's key, and the FMS still decides how long a
refresh token lives (if it has expired, the person signs in as before).

Key: env SESSION_KEY, else derived from VAPID_PRIVATE_KEY (already set on the relay). No key, no resume.
"""
from __future__ import annotations

import base64
import json
import os
import time

MAX_AGE_S = int(os.getenv("RESUME_MAX_AGE_S", str(12 * 3600)))


def _key() -> bytes | None:
    raw = os.getenv("SESSION_KEY") or os.getenv("VAPID_PRIVATE_KEY") or ""
    if not raw:
        return None
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"reach-fibre-tester", info=b"session resume v1").derive(raw.encode())


def resume_blob(sid: str, sess: dict | None) -> str:
    """Encrypted resume code for this session, or '' when resume is off or there is no refresh token."""
    k = _key()
    if not k or not sid or not sess or not sess.get("refresh"):
        return ""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    body = json.dumps({"sid": sid, "user": sess.get("user", ""), "refresh": sess["refresh"], "t": int(time.time())},
                      separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(nonce + AESGCM(k).encrypt(nonce, body, sid.encode())).decode().rstrip("=")


def read_blob(blob: str, sid: str) -> dict | None:
    """The resume record if the code is genuine, for this session id, and not too old."""
    k = _key()
    if not k or not blob or not sid or len(blob) > 8000:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))
        rec = json.loads(AESGCM(k).decrypt(raw[:12], raw[12:], sid.encode()))
    except Exception:                                   # noqa: BLE001
        return None
    if rec.get("sid") != sid or not rec.get("refresh") or time.time() - float(rec.get("t", 0)) > MAX_AGE_S:
        return None
    return rec
