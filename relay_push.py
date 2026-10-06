"""
v30 (relay): phone and browser notifications (Web Push), so a user hears about things with the app closed.

  GET  /api/push/key          {enabled, key}             public VAPID key the app subscribes with
  POST /api/push/subscribe    {sub, device}              this device, for the signed in user
  POST /api/push/unsubscribe  {endpoint}
  POST /api/push/test                                    a test message to all of my devices
  POST /api/push/watch        {rtuId, rtuName}           tell me when this busy RTU is free
  POST /api/push/unwatch      {rtuId}
  POST /api/push/status                                  {enabled, devices, watches}

What is sent (only to the person it is about):
  - my E2E run ends (done, stopped, failed)                  hook in relay_continuity.notify
  - my FMS bulk Task ends (completed, failed, cancelled)     background watcher, Tasks matched by creator
  - an RTU I asked about is free (no Task, E2E or tone)      background watcher
  - FMS stops answering during my run, and when it is back   background watcher

Web Push is done here with `cryptography` only (RFC 8291 aes128gcm, RFC 8292 VAPID), no pywebpush.
Settings: VAPID_PRIVATE_KEY (base64url of the 32 byte P-256 private key; push is off without it),
VAPID_SUBJECT (default mailto:alkis.kardasopoulos@motionrail.co.uk), PUSH_EVERY (watcher seconds, default 30).
Devices are kept in config/push-devices.json in the run log repo (LOG_REPO), so a relay restart keeps them.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import threading
import time
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:alkis.kardasopoulos@motionrail.co.uk")
PUSH_EVERY = max(1.0, float(os.getenv("PUSH_EVERY", "30")))
STORE_PATH = "config/push-devices.json"
WATCH_HOURS = 12


def _b64d(s: str) -> bytes:
    s = str(s or "").strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _load_key():
    raw = os.getenv("VAPID_PRIVATE_KEY", "").strip()
    if not raw:
        return None, ""
    try:
        if "BEGIN" in raw:
            key = serialization.load_pem_private_key(raw.encode(), password=None)
        else:
            key = ec.derive_private_key(int.from_bytes(_b64d(raw), "big"), ec.SECP256R1())
        pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return key, _b64e(pub)
    except Exception:                                   # noqa: BLE001
        return None, ""


VAPID_KEY, VAPID_PUBLIC = _load_key()


def enabled() -> bool:
    return VAPID_KEY is not None


# ── Web Push encryption and VAPID ─────────────────────────────────────────────────
def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """RFC 8291: one aes128gcm record for the subscription's keys."""
    ua_pub = _b64d(p256dh)
    secret = _b64d(auth)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    eph = ec.generate_private_key(ec.SECP256R1())
    as_pub = eph.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = eph.exchange(ec.ECDH(), ua_key)
    ikm = HKDF(hashes.SHA256(), 32, secret, b"WebPush: info\x00" + ua_pub + as_pub).derive(shared)
    salt = os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!IB", 4096, len(as_pub)) + as_pub + body


def vapid_header(endpoint: str) -> str:
    u = urlparse(endpoint)
    head = _b64e(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = _b64e(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600,
                               "sub": VAPID_SUBJECT}, separators=(",", ":")).encode())
    r, s = decode_dss_signature(VAPID_KEY.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    sig = _b64e(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{claims}.{sig}, k={VAPID_PUBLIC}"


# ── devices and watches ───────────────────────────────────────────────────────────
DEVICES: dict[str, dict] = {}      # endpoint -> {"sub": {...}, "user": email, "device": str, "added": t}
WATCHES: dict[str, dict] = {}      # "user|rtuId" -> {"user", "rtuId", "rtuName", "added", "sawBusy"}
SENT: list[dict] = []              # last 50 sends, for /api/push/status and the checks
_STORE = {"sha": None, "loaded": False, "dirty": False, "lock": threading.Lock()}


def _store_on() -> bool:
    try:
        from relay_continuity import LOG_REPO, LOG_TOKEN
        return bool(LOG_REPO and LOG_TOKEN)
    except Exception:                                   # noqa: BLE001
        return False


def _load_sync():
    if _STORE["loaded"]:
        return
    _STORE["loaded"] = True
    if not _store_on():
        return
    try:
        from relay_continuity import _gh_get_sync
        data, sha = _gh_get_sync(STORE_PATH)
        _STORE["sha"] = sha
        for d in (data or {}).get("devices", []):
            if d.get("sub", {}).get("endpoint"):
                DEVICES.setdefault(d["sub"]["endpoint"], d)
        for w in (data or {}).get("watches", []):
            if time.time() - w.get("added", 0) < WATCH_HOURS * 3600:
                WATCHES.setdefault(f"{w['user']}|{w['rtuId']}", w)
    except Exception:                                   # noqa: BLE001
        pass


def _save_sync():
    if not _store_on():
        return
    with _STORE["lock"]:
        try:
            from relay_continuity import _push_log_sync
            body = {"devices": list(DEVICES.values()), "watches": list(WATCHES.values()), "saved": time.time()}
            _STORE["sha"], _ = _push_log_sync(STORE_PATH, body, _STORE["sha"], "push devices")
        except Exception:                                   # noqa: BLE001
            pass


def _save_soon():
    threading.Thread(target=_save_sync, daemon=True).start()


def _send_sync(dev: dict, msg: dict) -> int:
    sub = dev["sub"]
    keys = sub.get("keys") or {}
    body = encrypt(json.dumps(msg).encode(), keys.get("p256dh", ""), keys.get("auth", ""))
    r = requests.post(sub["endpoint"], data=body, timeout=20, headers={
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": "86400",
        "Urgency": "high", "Authorization": vapid_header(sub["endpoint"])})
    return r.status_code


def push_user_sync(user: str, title: str, body: str, tag: str = "", url: str = "", kind: str = "") -> int:
    """Send to every device of one user. Gone devices (404 / 410) are dropped. Returns how many took it."""
    if not enabled() or not user:
        return 0
    _load_sync()
    u = str(user).strip().lower()
    msg = {"title": title, "body": body, "tag": tag or kind or "rft", "url": url or "/", "kind": kind, "t": time.time()}
    ok, gone = 0, []
    for ep, dev in list(DEVICES.items()):
        if dev.get("user", "").lower() != u:
            continue
        try:
            code = _send_sync(dev, msg)
        except Exception as e:                          # noqa: BLE001
            code = "error " + str(e)[:80]
        if code in (404, 410):
            gone.append(ep)
        elif isinstance(code, int) and code < 300:
            ok += 1
        SENT.append({"user": u, "title": title, "body": body, "kind": kind, "status": code, "t": time.time(),
                     "device": dev.get("device", "")})
    del SENT[:-50]
    if gone:
        for ep in gone:
            DEVICES.pop(ep, None)
        _save_soon()
    return ok


async def push_user(user: str, title: str, body: str, tag: str = "", url: str = "", kind: str = "") -> int:
    return await asyncio.to_thread(push_user_sync, user, title, body, tag, url, kind)


# ── the watcher: FMS Tasks, free RTUs, FMS down and back ──────────────────────────
W = {"task": None, "tasks": {}, "fms_ok": True, "warned": set(), "t": 0.0, "note": ""}


def _same(a: str, b: str) -> bool:
    from relay_team import _owner
    a, b = str(a or "").strip().lower(), str(b or "").strip().lower()
    return bool(a and b) and (a == b or _owner(a).lower() == _owner(b).lower())


def _short(name: str) -> str:
    """RTU2-RGAC-2026963 style names to RGAC2, as the app shows them."""
    import re
    m = re.match(r"RTU(\d+)-([A-Z]+)", str(name or ""))
    return f"{m.group(2)}{m.group(1)}" if m else str(name or "RTU")


NAMES: dict[str, str] = {}         # rtuId -> full RTU name, learnt from watches and the relay's route cache


def _rtu_label(rtu_id, name: str = "") -> str:
    """Live FMS gives a Task only its RtuId, so look the name up the way the app does."""
    k = str(rtu_id or "")
    if name and not name.startswith("RTU ") and "-" in name:
        return _short(name)
    import sys
    main = sys.modules.get("main")
    idx = getattr(main, "RTU_INDEX", {}) if main else {}
    full = (idx.get(k) or {}).get("rtuName") or NAMES.get(k, "")
    return _short(full) if full else (name or f"RTU {k}")


def _task_text(t: dict) -> str:
    return f"{t.get('kind', 'Task')} on {_rtu_label(t.get('rtuId'), t.get('rtuName', ''))}"


async def _token(valid_token, sessions: dict) -> str | None:
    """Any signed in session will do for reading FMS; people with devices first."""
    users = {d.get("user", "").lower() for d in DEVICES.values()}
    order = sorted(sessions.items(), key=lambda kv: (str(kv[1].get("user", "")).lower() not in users, -kv[1].get("exp", 0)))
    for sid, _ in order:
        try:
            return await valid_token(sid)
        except Exception:                               # noqa: BLE001
            continue
    return None


def _busy_ids(running: list[dict]) -> tuple[set, set]:
    from relay_bulk import toning_now
    from relay_continuity import JOBS
    ids = {str(t.get("rtuId")) for t in running} | {str(t["rtuId"]) for t in toning_now()}
    names = set()
    for j in JOBS.values():
        if j.get("state") in ("running", "paused"):
            names |= {str(j.get("toneRtu") or ""), str(j.get("testRtu") or "")}
            ids |= {str(j.get("toneRtuId") or ""), str(j.get("testRtuId") or "")}
    ids.discard("")
    names.discard("")
    return ids, names


async def _tick(valid_token, sessions: dict):
    import relay_bulk
    from relay_continuity import FMS_STATUS, JOBS
    _load_sync()
    now = time.time()
    # FMS down / back, to the people with a run going
    ok = bool(FMS_STATUS.get("ok", True))
    if ok != W["fms_ok"]:
        W["fms_ok"] = ok
        owners = {j.get("user", "") for j in JOBS.values() if j.get("state") in ("running", "paused") and j.get("user")}
        for u in owners:
            if ok:
                await push_user(u, "FMS is back", "Your E2E run carries on.", tag="fms", kind="fms")
            else:
                await push_user(u, "FMS not responding", "Your E2E run is waiting for FMS. Nothing is lost.",
                                tag="fms", kind="fms")
    for k in [k for k, w in WATCHES.items() if now - w["added"] > WATCH_HOURS * 3600]:
        WATCHES.pop(k, None)
    task_users = {d.get("user", "").lower() for d in DEVICES.values()}
    if not (WATCHES or task_users) or relay_bulk.READ_TASKS is None:
        W["note"] = "nothing to watch"
        return
    token = await _token(valid_token, sessions)
    if not token:
        W["note"] = "no one signed in, FMS not read"
        return
    data = await asyncio.to_thread(relay_bulk.READ_TASKS, token, 0)
    running = data.get("running", [])
    W["t"], W["note"] = now, f"{len(running)} Tasks running"
    # my Task ended: it was running last time and is not now
    seen = {t["id"]: t for t in running if t.get("id")}
    for tid, old in list(W["tasks"].items()):
        if tid in seen:
            continue
        W["tasks"].pop(tid, None)
        who = next((d["user"] for d in DEVICES.values() if _same(d.get("user"), old.get("creator") or old.get("owner"))), None)
        if not who:
            continue
        status, done, total = "", old.get("done", 0), old.get("total", 0)
        try:
            fms = relay_bulk.FMS_FOR(token)
            d = await asyncio.to_thread(lambda: relay_bulk.TASK_DETAIL(fms, fms.workflow(tid, tasks=True)))
            status, done, total = d.get("status", ""), d.get("done", done), d.get("total", total)
        except Exception:                               # noqa: BLE001
            pass
        word = {"COMPLETED": "finished", "FAILED": "failed", "TERMINATED": "cancelled",
                "TIMED_OUT": "timed out"}.get(status, "ended")
        await push_user(who, f"{_task_text(old)} {word}", f"{done} of {total} fibres tested."
                        + (f" {old['comment']}" if old.get("comment") else ""), tag="task-" + tid, kind="task")
    for tid, t in seen.items():
        W["tasks"][tid] = {k: t.get(k) for k in ("kind", "rtuName", "rtuId", "creator", "owner", "comment", "done", "total")}
    # an RTU I asked about is free
    ids, names = _busy_ids(running)
    changed = False
    for k, w in list(WATCHES.items()):
        busy = str(w["rtuId"]) in ids or (w.get("rtuName") and w["rtuName"] in names)
        if busy:
            if not w.get("sawBusy"):
                w["sawBusy"] = True
            continue
        WATCHES.pop(k, None)
        changed = True
        await push_user(w["user"], f"{_rtu_label(w['rtuId'], w.get('rtuName', ''))} is free", "No Task, E2E run or tone on it now.",
                        tag="free-" + str(w["rtuId"]), kind="free")
    if changed:
        _save_soon()


async def _loop(valid_token, sessions: dict):
    while True:
        try:
            await _tick(valid_token, sessions)
        except Exception as e:                          # noqa: BLE001
            W["note"] = "watcher error: " + str(e)[:120]
        await asyncio.sleep(PUSH_EVERY)


# ── endpoints ─────────────────────────────────────────────────────────────────────
class SubIn(BaseModel):
    sub: dict
    device: str = ""


class EndIn(BaseModel):
    endpoint: str


class WatchIn(BaseModel):
    rtuId: str
    rtuName: str = ""


def make_router(valid_token, check_key, sessions: dict) -> APIRouter:
    router = APIRouter()

    def start():
        if enabled() and (W["task"] is None or W["task"].done()):
            W["task"] = asyncio.get_event_loop().create_task(_loop(valid_token, sessions))

    async def who(x_session) -> str:
        await valid_token(x_session)                   # 401 when the session is gone
        user = str((sessions.get(x_session or "") or {}).get("user", "")).strip().lower()
        if not user:
            raise HTTPException(401, "Sign in again")
        return user

    @router.on_event("startup")
    async def _startup():
        if enabled():
            await asyncio.to_thread(_load_sync)
            start()

    @router.get("/api/push/key")
    async def key(x_app_key: str | None = Header(default=None)):
        check_key(x_app_key)
        start()
        return {"enabled": enabled(), "key": VAPID_PUBLIC}

    @router.post("/api/push/subscribe")
    async def subscribe(body: SubIn, x_app_key: str | None = Header(default=None),
                        x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        user = await who(x_session)
        if not enabled():
            raise HTTPException(503, "Notifications are not set up on the relay")
        ep = str(body.sub.get("endpoint", ""))
        keys = body.sub.get("keys") or {}
        if not ep.startswith("http") or not keys.get("p256dh") or not keys.get("auth"):
            raise HTTPException(400, "Not a push subscription")
        await asyncio.to_thread(_load_sync)
        old = DEVICES.get(ep)
        DEVICES[ep] = {"sub": {"endpoint": ep, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}},
                       "user": user, "device": body.device[:80], "added": (old or {}).get("added", time.time())}
        if not old or old.get("user") != user or old.get("sub", {}).get("keys") != DEVICES[ep]["sub"]["keys"]:
            _save_soon()
        start()
        return {"ok": True, "devices": sum(1 for d in DEVICES.values() if d["user"] == user)}

    @router.post("/api/push/unsubscribe")
    async def unsubscribe(body: EndIn, x_app_key: str | None = Header(default=None),
                          x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        user = await who(x_session)
        d = DEVICES.get(body.endpoint)
        if d and d["user"] == user:
            DEVICES.pop(body.endpoint, None)
            _save_soon()
        return {"ok": True}

    @router.post("/api/push/test")
    async def test(x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        user = await who(x_session)
        n = await push_user(user, "Notifications are on", "You will hear when your runs and Tasks end.",
                            tag="test", kind="test")
        if not n:
            raise HTTPException(404, "No device took the test. Turn notifications off and on again.")
        return {"ok": True, "sent": n}

    @router.post("/api/push/watch")
    async def watch(body: WatchIn, x_app_key: str | None = Header(default=None),
                    x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        user = await who(x_session)
        if not enabled():
            raise HTTPException(503, "Notifications are not set up on the relay")
        if not any(d["user"] == user for d in DEVICES.values()):
            raise HTTPException(409, "Turn on notifications on this device first")
        if body.rtuName:
            NAMES[str(body.rtuId)] = body.rtuName[:80]
        WATCHES[f"{user}|{body.rtuId}"] = {"user": user, "rtuId": str(body.rtuId), "rtuName": body.rtuName[:80],
                                           "added": time.time(), "sawBusy": False}
        _save_soon()
        start()
        return {"ok": True}

    @router.post("/api/push/unwatch")
    async def unwatch(body: WatchIn, x_app_key: str | None = Header(default=None),
                      x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        user = await who(x_session)
        if WATCHES.pop(f"{user}|{body.rtuId}", None):
            _save_soon()
        return {"ok": True}

    @router.post("/api/push/status")
    async def status(x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        user = await who(x_session)
        await asyncio.to_thread(_load_sync)
        return {"enabled": enabled(), "devices": sum(1 for d in DEVICES.values() if d["user"] == user),
                "watches": [{"rtuId": w["rtuId"], "rtuName": w["rtuName"], "added": w["added"]}
                            for w in WATCHES.values() if w["user"] == user],
                "watcher": W["note"], "sent": [s for s in SENT if s["user"] == user][-10:]}

    return router
