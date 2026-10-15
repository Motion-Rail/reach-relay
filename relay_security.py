"""Brunel.6 (relay): security hardening with no effect on test speed.

What it adds
- Idle timeout. A session nobody has used for IDLE_S (8 h) is dropped, or KEEP_IDLE_S (7 days) when
  "keep me signed in" was ticked. A session that owns a running or paused E2E run is never dropped,
  so a long run is not cut off.
- Sign in limit. After LOGIN_MAX_FAILS (5) failed sign ins in LOGIN_WINDOW_S (15 min) from one IP
  address, or for one username, further tries are refused for the rest of the window (HTTP 429).
  A good sign in clears the count. Normal use never meets it.
- Sign out everywhere. An admin (ADMIN_USERS, default Alkis) can end every session at once. Every
  resume code issued before that moment is refused from then on, even after a relay restart, because
  the moment is saved in the log repo (config/revoke.json) and read back at start up.
- Security headers on every relay reply (no sniffing, no framing, no referrer, HTTPS only) and
  no-store on /api replies, so a shared browser does not keep test data in its cache.

Each check is a dictionary look up or a clock comparison, measured in microseconds, beside FMS calls
that take seconds. Nothing here touches the test path.
"""
from __future__ import annotations

import os
import threading
import time

from fastapi import APIRouter, Header, HTTPException, Request

IDLE_S = int(os.getenv("IDLE_S", str(8 * 3600)))
KEEP_IDLE_S = int(os.getenv("KEEP_IDLE_S", str(7 * 86400)))
LOGIN_MAX_FAILS = int(os.getenv("LOGIN_MAX_FAILS", "5"))
LOGIN_WINDOW_S = int(os.getenv("LOGIN_WINDOW_S", str(15 * 60)))
ADMIN_USERS = {u.strip().lower() for u in os.getenv("ADMIN_USERS", "alkis.kardasopoulos@motionrail.co.uk").split(",")
               if u.strip()}
REVOKE_PATH = "config/revoke.json"

STATE = {"revoke_before": float(os.getenv("REVOKE_BEFORE", "0") or 0), "loaded": False, "note": "",
         "last_sweep": 0.0, "dropped_idle": 0, "blocked_logins": 0}
_FAILS: dict[str, list[float]] = {}      # "ip:1.2.3.4" or "user:name" -> failure times


# ── sign in limit ─────────────────────────────────────────────────────────────
def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _recent(key: str, now: float) -> list[float]:
    times = [t for t in _FAILS.get(key, []) if now - t < LOGIN_WINDOW_S]
    if times:
        _FAILS[key] = times
    else:
        _FAILS.pop(key, None)
    return times


def login_blocked(ip: str, user: str) -> int:
    """Seconds until another sign in may be tried, or 0 when it may be tried now."""
    now = time.time()
    worst = 0
    for key in (f"ip:{ip}", f"user:{user.strip().lower()}"):
        times = _recent(key, now)
        if len(times) >= LOGIN_MAX_FAILS:
            worst = max(worst, int(LOGIN_WINDOW_S - (now - times[0])) + 1)
    if worst:
        STATE["blocked_logins"] += 1
    return worst


def login_failed(ip: str, user: str) -> None:
    now = time.time()
    for key in (f"ip:{ip}", f"user:{user.strip().lower()}"):
        _FAILS.setdefault(key, []).append(now)
    if len(_FAILS) > 5000:                               # keep memory bounded
        for k in list(_FAILS)[:1000]:
            _FAILS.pop(k, None)


def login_ok(ip: str, user: str) -> None:
    _FAILS.pop(f"ip:{ip}", None)
    _FAILS.pop(f"user:{user.strip().lower()}", None)


# ── idle timeout ─────────────────────────────────────────────────────────────
def touch(sessions: dict, sid: str) -> None:
    """A request from the person. If the session had already gone idle it is ended now, before the
    request is handled, so the person signs in again; otherwise it counts as activity."""
    s = sessions.get(sid)
    if s is None:
        return
    now = time.time()
    limit = KEEP_IDLE_S if s.get("keep") else IDLE_S
    if now - float(s.get("seen") or s.get("born") or now) > limit and \
            str(s.get("user", "")).strip().lower() not in _busy_users():
        sessions.pop(sid, None)
        STATE["dropped_idle"] += 1
        return
    s["seen"] = now


def _busy_users() -> set[str]:
    try:
        from relay_continuity import JOBS
        return {str(j.get("user") or "").strip().lower() for j in JOBS.values()
                if j.get("state") in ("running", "paused")}
    except Exception:                                   # noqa: BLE001
        return set()


def sweep(sessions: dict, force: bool = False) -> int:
    """Drop idle sessions, at most once a minute. Returns how many were dropped."""
    now = time.time()
    if not force and now - STATE["last_sweep"] < 60:
        return 0
    STATE["last_sweep"] = now
    busy = _busy_users()
    gone = 0
    for sid, s in list(sessions.items()):
        limit = KEEP_IDLE_S if s.get("keep") else IDLE_S
        seen = float(s.get("seen") or s.get("born") or now)
        if now - seen > limit and str(s.get("user", "")).strip().lower() not in busy:
            sessions.pop(sid, None)
            gone += 1
    STATE["dropped_idle"] += gone
    return gone


# ── sign out everywhere ─────────────────────────────────────────────────────
def resume_allowed(rec: dict) -> bool:
    """A resume code issued before the last 'sign out everywhere' is refused."""
    return float(rec.get("t", 0)) > STATE["revoke_before"]


def load_revoke_async() -> None:
    def run():
        try:
            from relay_continuity import LOG_REPO, LOG_TOKEN, _gh_get_sync
            if not (LOG_REPO and LOG_TOKEN):
                STATE["note"] = "log repo not set, sign out everywhere lasts until the next restart"
                return
            data, _sha = _gh_get_sync(REVOKE_PATH)
            if data and float(data.get("revokeBefore", 0)) > STATE["revoke_before"]:
                STATE["revoke_before"] = float(data["revokeBefore"])
            STATE["note"] = "read " + REVOKE_PATH if data else REVOKE_PATH + " not found (nothing revoked)"
        except Exception as e:                          # noqa: BLE001
            STATE["note"] = "could not read " + REVOKE_PATH + ": " + str(e)[:80]
        finally:
            STATE["loaded"] = True
    threading.Thread(target=run, daemon=True).start()


def _save_revoke_sync(by: str) -> str:
    try:
        from relay_continuity import LOG_REPO, LOG_TOKEN, _gh_get_sync, _push_log_sync
        if not (LOG_REPO and LOG_TOKEN):
            return "not saved (log repo not set)"
        _old, sha = _gh_get_sync(REVOKE_PATH)
        _sha, status = _push_log_sync(REVOKE_PATH, {"revokeBefore": STATE["revoke_before"], "by": by,
                                                    "at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())},
                                      sha, f"Sign out everywhere by {by}")
        return status
    except Exception as e:                              # noqa: BLE001
        return "not saved: " + str(e)[:80]


# ── security headers ─────────────────────────────────────────────────────────
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def add_headers(request: Request, response) -> None:
    for k, v in HEADERS.items():
        response.headers.setdefault(k, v)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"


# ── admin endpoints ─────────────────────────────────────────────────────────
def make_router(check_key, sessions: dict, logout_fms):
    router = APIRouter()

    def admin_user(x_session) -> str:
        user = str((sessions.get(x_session or "") or {}).get("user", "")).strip().lower()
        if not user:
            raise HTTPException(401, "No session — sign in again")
        if user not in ADMIN_USERS:
            raise HTTPException(403, "Only an admin can do this")
        return user

    @router.post("/api/admin/security")
    async def security_status(x_app_key: str | None = Header(default=None),
                              x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        admin_user(x_session)
        now = time.time()
        people: dict[str, int] = {}
        for s in sessions.values():
            people[str(s.get("user", ""))] = people.get(str(s.get("user", "")), 0) + 1
        return {"ok": True, "sessions": len(sessions), "people": people,
                "idleHours": IDLE_S / 3600, "keepIdleDays": KEEP_IDLE_S / 86400,
                "loginLimit": f"{LOGIN_MAX_FAILS} fails in {LOGIN_WINDOW_S // 60} min",
                "lockedOut": sum(1 for k in list(_FAILS) if len(_recent(k, now)) >= LOGIN_MAX_FAILS),
                "droppedIdle": STATE["dropped_idle"], "blockedLogins": STATE["blocked_logins"],
                "revokedBefore": STATE["revoke_before"], "note": STATE["note"]}

    @router.post("/api/admin/signout-all")
    async def signout_all(x_app_key: str | None = Header(default=None),
                          x_session: str | None = Header(default=None)):
        """End every session, including the caller's, and refuse every resume code issued before now."""
        check_key(x_app_key)
        by = admin_user(x_session)
        STATE["revoke_before"] = time.time()
        ended = list(sessions.values())
        sessions.clear()
        for s in ended:                                 # end the FMS sign ins too (best effort)
            try:
                await logout_fms(s)
            except Exception:                           # noqa: BLE001
                pass
        import asyncio
        saved = await asyncio.to_thread(_save_revoke_sync, by)
        return {"ok": True, "ended": len(ended), "saved": saved}

    return router
