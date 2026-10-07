"""
v32 (relay): Live OTDR. One fibre, from one RTU, a short OTDR again and again so an engineer can watch the trace
change while they handle the fibre (bends, joints, patching).

  POST /api/otdr/live/start  {stem, fibre, rtuId, rtuName?, minutes?}   -> session
  POST /api/otdr/live/status {id}                                       -> session with the reference and latest trace
  POST /api/otdr/live/stop   {id}
  POST /api/otdr/live/reference {id}                                    -> make the latest trace the reference
  POST /api/otdr/live/list                                              -> live sessions now (any cable)

Each shot is the direct ad hoc OTDR the FMS screen uses (no Task): 1550 nm, FMS automatic settings, 5 s
(LIVE_OTDR_S). The stored result is read back with its trace. Traces are reduced to about 1500 points.
The first trace is the reference; every later trace is compared with it and the biggest new loss step is
reported with its distance from the RTU.

One live session per RTU; it refuses an RTU with a Task, an E2E run or a tone on it, and stops by itself after
LIVE_OTDR_MINUTES (default 10). While it runs the RTU shows as testing (purple) everywhere.
How the trace numbers are stored is set by TRACE_FORMAT (uint16le, uint16be, int16le, float32le) and
TRACE_SCALE (dB per unit, default 0.001) until the probe on a real result confirms it.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import struct
import threading
import time
import uuid

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import fms_continuity as fc

SHOT_S = max(1, int(os.getenv("LIVE_OTDR_S", "3")))           # v34: 3 s by default (was 5); 1 to 10 per session
MINUTES = max(1, int(os.getenv("LIVE_OTDR_MINUTES", "10")))
TRACE_FORMAT = os.getenv("TRACE_FORMAT", "uint16le").strip().lower()
TRACE_SCALE = float(os.getenv("TRACE_SCALE", "0.001"))
BINS = 1500
STEP_DB = float(os.getenv("LIVE_OTDR_STEP_DB", "0.15"))     # smallest new loss worth marking
POLL_S = float(os.getenv("LIVE_OTDR_POLL_S", "1"))          # results list check while waiting (the push is quicker)
SHOT_CHOICES = (1, 2, 3, 5, 10)

SESSIONS_L: dict[str, dict] = {}     # session id -> session


def _num(v, d=None):
    try:
        x = float(v)
        return d if x != x else x
    except Exception:                                   # noqa: BLE001
        return d


def decode_points(points_b64: str, fmt: str = "", scale: float | None = None) -> list[float]:
    fmt = fmt or TRACE_FORMAT
    scale = TRACE_SCALE if scale is None else scale
    raw = base64.b64decode(points_b64 or "")
    codes = {"uint16le": ("<", "H", 2), "uint16be": (">", "H", 2), "int16le": ("<", "h", 2), "int16be": (">", "h", 2),
             "float32le": ("<", "f", 4), "float32be": (">", "f", 4)}
    e, c, w = codes.get(fmt, codes["uint16le"])
    n = len(raw) // w
    return [v * scale for v in struct.unpack(f"{e}{n}{c}", raw[: n * w])]


def reduce_trace(vals: list[float], res_m: float, first_m: float, until_m: float | None) -> dict:
    """About BINS points (mean per bin) from the start to a little past the fibre end."""
    if not vals:
        return {"x0": 0, "dx": 1, "y": []}
    n_keep = len(vals)
    if until_m and res_m:
        n_keep = min(len(vals), int((until_m * 1.08 - first_m) / res_m) + 1)
    vals = vals[: max(10, n_keep)]
    k = max(1, len(vals) // BINS)
    y = [round(sum(vals[i:i + k]) / len(vals[i:i + k]), 4) for i in range(0, len(vals), k)]
    return {"x0": round(first_m, 2), "dx": round(res_m * k, 4), "y": y}


def compare(ref: dict, new: dict, link_m: float | None) -> list[dict]:
    """New loss steps: where the latest trace drops away from the reference and stays lower after."""
    if not ref.get("y") or not new.get("y") or abs(ref["dx"] - new["dx"]) > 1e-6:
        return []
    n = min(len(ref["y"]), len(new["y"]))
    end = n
    if link_m:
        end = min(n, int((link_m - ref["x0"]) / ref["dx"]) - 3)
    d = [ref["y"][i] - new["y"][i] for i in range(n)]          # positive: more loss now
    w = 6
    best = []
    i = w + 2
    while i < end - w - 2:
        before = sum(d[i - w - 2:i - 2]) / w
        after = sum(d[i + 2:i + w + 2]) / w
        step = after - before
        if step >= STEP_DB:
            j = i
            while j + 1 < end - w - 2:                          # walk to the sharpest point of this step
                b2 = sum(d[j + 1 - w - 2:j - 1]) / w
                a2 = sum(d[j + 3:j + w + 3]) / w
                if a2 - b2 < step:
                    break
                step, j = a2 - b2, j + 1
            best.append({"m": round(ref["x0"] + j * ref["dx"]), "db": round(step, 2)})
            i = j + w * 2
        else:
            i += 1
    return sorted(best, key=lambda b: -b["db"])[:5]


def _busy(rtu_id: str, rtu_name: str) -> str:
    """Why this RTU cannot run a live OTDR now, or ''."""
    import relay_bulk
    from relay_continuity import JOBS
    for s in SESSIONS_L.values():
        if s["state"] == "running" and s["rtuId"] == str(rtu_id):
            return f"{s['owner']} is already running a live OTDR from this RTU"
    for j in JOBS.values():
        if j.get("state") in ("running", "paused") and rtu_name and rtu_name in (j.get("toneRtu"), j.get("testRtu")):
            return "An E2E run is using this RTU"
    for t in relay_bulk.toning_now():
        if str(t.get("rtuId")) == str(rtu_id):
            return "A Uni-dir tone is running from this RTU"
    try:
        import relay_fibres
        for t in (relay_fibres.LIVE_TASKS.get("data") or []):
            if str(t.get("rtuId")) == str(rtu_id):
                return f"An FMS Task by {t.get('owner') or 'someone'} is running on this RTU"
    except Exception:                                   # noqa: BLE001
        pass
    return ""


def _fire(fms, s: dict) -> dict:
    """Start one OTDR. Returns {pid, t0}. Subscribes the push channel first when it is open."""
    url = fc.ADHOC_URL.format(rtu=int(s["rtuId"]), route=int(s["routeId"]))
    t0 = time.time()
    r = fms.post(url, json=fc.adhoc_payload(int(s.get("shotS") or SHOT_S)))
    if r.status_code == 409 or "AlreadyScheduled" in r.text:
        raise RuntimeError("The RTU is busy with another test. Waiting.")
    if not r.ok:
        raise RuntimeError(f"FMS refused the OTDR ({r.status_code}): {r.text[:160]}")
    pid = r.text.strip().strip('"')
    w = s.get("_watch")
    if w:
        try:
            w.subscribe(fc.ADHOC_TOPIC.format(route=int(s["routeId"]), promise=pid))
        except Exception:                               # noqa: BLE001
            s["_watch"] = None
    s.setdefault("pids", []).append(pid)
    del s["pids"][:-400]
    return {"pid": pid, "t0": t0}


def _await(fms, s: dict, shot: dict) -> str:
    """Wait for that OTDR's stored result id: the push says it at once; the results list is checked every second too."""
    pid, t0 = shot["pid"], shot["t0"]
    nxt = t0 + max(1.0, int(s.get("shotS") or SHOT_S) - 0.5)   # nothing can be ready before the shot ends
    while time.time() - t0 < 90 and s["state"] == "running":
        w = s.get("_watch")
        if w:
            msg = w.next_message(timeout=0.5)
            if msg is not None and str(msg.get("promiseId") or "") == pid:
                if msg.get("isError") or msg.get("error"):
                    why = msg.get("body") if isinstance(msg.get("body"), str) else str(msg.get("body") or "")
                    if fc.LIVE_PATTERNS.search(why or ""):
                        raise RuntimeError("Live light on the fibre: the RTU will not fire an OTDR into it.")
                    raise RuntimeError("The OTDR failed: " + (why[:160] or "no reason given"))
                if msg.get("lastTestResultId"):
                    s["pushOk"] = True
                    return str(msg["lastTestResultId"])
        else:
            time.sleep(0.3)
        if time.time() >= nxt:
            nxt = time.time() + POLL_S
            for res in fms.adhoc_results(int(s["routeId"]), top=3):
                md = res.get("metadata") or {}
                if md.get("PromiseId") == pid:
                    if md.get("HasError"):
                        why = " ".join(fc._strings(res, keys=("message", "messageKey", "error", "errorMessage")))[:200]
                        if fc.LIVE_PATTERNS.search(why):
                            raise RuntimeError("Live light on the fibre: the RTU will not fire an OTDR into it.")
                        raise RuntimeError("The OTDR failed: " + (why or "no reason given"))
                    return str(res.get("resultid"))
    if s["state"] != "running":
        return ""
    raise RuntimeError("No result after 90 s. Light on the fibre (a tone or traffic) stops an OTDR.")


def _read(fms, rid: str, t0: float) -> dict:
    """The trace of one stored result, asked for by its id (one small query, not the last three traces)."""
    params = {"$filter": f"resultid eq {rid}", "$top": "1", "$skip": "0",
              "$select": "resultid,metadata,brief/LinkResults,brief/Measurement/OtdrMeasurements"}
    full = None
    for _ in range(3):                                  # the push can be a moment ahead of the store
        rr = fms.get(fc.RESULTS_URL, params=params)
        rr.raise_for_status()
        j = rr.json()
        full = next(iter(j.get("results", j if isinstance(j, list) else [])), None)
        if full:
            break
        time.sleep(0.7)
    if not full:
        raise RuntimeError("The trace could not be read back from FMS.")
    md = full.get("metadata") or {}
    om = (((full.get("brief") or {}).get("Measurement") or {}).get("OtdrMeasurements") or [{}])[0]
    dp = om.get("DataPoints") or {}
    link = _num(((full.get("brief") or {}).get("LinkResults") or {}).get("Length"))
    npts = int(_num(dp.get("NumberOfPoints"), 0) or 0)
    res_m = _num(dp.get("Resolution")) or ((_num((om.get("Parameters") or {}).get("Range"), 0) / npts) if npts else 1.0)
    first = _num(dp.get("FirstPointPosition"), 0.0) or 0.0
    vals = decode_points(dp.get("Points") or "")
    events = [{"m": round(_num(e.get("Position"), 0)), "loss": _num(e.get("Loss")), "type": e.get("Type") or ""}
              for e in (om.get("Events") or []) if _num(e.get("Position")) is not None]
    return {"t": time.time(), "secs": round(time.time() - t0, 1), "resultId": rid, "testTime": md.get("TestTime"),
            "len": link, "trace": reduce_trace(vals, res_m, first, link), "events": events[:40]}


def _when(v) -> float:
    from relay_fibres import _when as w
    return w(v)


def _live_pids() -> set:
    return {p for s in SESSIONS_L.values() for p in s.get("pids", [])}


def _take(s: dict, shot: dict):
    if s["ref"] is None:
        s["ref"] = shot
    shot["steps"] = compare(s["ref"]["trace"], shot["trace"], s["ref"].get("len") or shot.get("len"))
    prev = s["latest"]
    shot["new"] = compare(prev["trace"], shot["trace"], shot.get("len")) if prev else []
    s["latest"] = shot
    s["shots"].append({k: shot[k] for k in ("t", "secs", "len", "resultId")} | {"steps": shot["steps"][:2], "shotS": shot.get("shotS")})
    s["count"] += 1


def _loop(s: dict, token_fn):
    """v34: the next OTDR starts as soon as FMS has stored the last one; its trace is read while the next one runs."""
    import relay_bulk
    fms = fc.Fms.from_token_provider(token_fn, s["user"])
    s["_watch"] = fc.StompWatch.open(fms)
    s["push"] = bool(s["_watch"])
    pending = None                                      # the OTDR now running on the RTU
    while s["state"] == "running":
        try:
            if time.time() > s["until"]:
                s.update(state="done", note="Stopped after the time limit.")
                break
            relay_bulk.mark_toning(s["rtuId"], int(s.get("shotS") or SHOT_S) + 30, s["user"], s["fibreName"])
            if pending is None:
                pending = _fire(fms, s)
            rid = _await(fms, s, pending)
            if not rid:
                break
            done, pending = pending, None
            shot_s = int(s.get("shotS") or SHOT_S)
            if s["state"] == "running" and time.time() + shot_s < s["until"]:
                pending = _fire(fms, s)                 # the RTU is busy again while we read the trace
            shot = _read(fms, rid, done["t0"])
            shot["shotS"] = shot_s
            _take(s, shot)
            s["error"] = ""
        except Exception as e:                          # noqa: BLE001
            s["error"] = str(e)[:240]
            pending = None
            time.sleep(6)
            if s.get("_watch") is None and s.get("push"):
                s["_watch"] = fc.StompWatch.open(fms)
    if s.get("_watch"):
        s["_watch"].close()
    s["_watch"] = None
    relay_bulk.TONING.pop(str(s["rtuId"]), None)
    s["ended"] = time.time()


def public(s: dict, full: bool = True) -> dict:
    out = {k: s[k] for k in ("id", "state", "owner", "stem", "fibre", "rtuId", "rtuName", "started", "until", "count", "error", "note")}
    out["shotS"] = int(s.get("shotS") or SHOT_S)
    out["push"] = bool(s.get("pushOk"))
    out["shots"] = s["shots"][-30:]
    if full:
        out["ref"] = s["ref"] and {k: s["ref"][k] for k in ("t", "len", "trace", "events", "resultId")}
        out["latest"] = s["latest"] and {k: s["latest"][k] for k in ("t", "len", "trace", "events", "steps", "new", "resultId")}
    return out


class StartIn(BaseModel):
    stem: str
    fibre: int
    rtuId: str
    rtuName: str = ""
    minutes: int = 0
    seconds: int = 0


class IdIn(BaseModel):
    id: str
    have: int = -1          # v34: the trace count the page already has; traces are only sent when it changed
    seconds: int = 0


class FibreIn(BaseModel):
    stem: str
    fibre: int


class TraceIn(BaseModel):
    resultId: str


TRACES: dict[str, dict] = {}          # result id -> reduced trace (stored results never change)


def _secs(v: int) -> int:
    return int(v) if int(v or 0) in SHOT_CHOICES else SHOT_S


def make_router(valid_token, check_key, sessions: dict) -> APIRouter:
    router = APIRouter()
    from relay_bulk import require_desktop
    from relay_team import _owner

    def mine(x_session) -> tuple[str, str]:
        u = str((sessions.get(x_session or "") or {}).get("user", ""))
        return u, _owner(u)

    @router.post("/api/otdr/live/start")
    async def start(body: StartIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        user, owner = mine(x_session)
        stem = body.stem.strip().upper()
        if not 1 <= body.fibre <= 432:
            raise HTTPException(400, "Fibre must be 1 to 432")
        import relay_fibres
        ends = await relay_fibres.ensure_ends(await valid_token(x_session), stem)
        end = next((e for e in ends if e["rtuId"] == str(body.rtuId)), None)
        if not end or not end["routes"].get(str(body.fibre)):
            raise HTTPException(404, "That fibre was not found on that RTU in FMS")
        why = _busy(end["rtuId"], end["rtu"])
        if why:
            raise HTTPException(409, why + ". Try again when it is free.")
        sid = uuid.uuid4().hex[:12]
        mins = max(1, min(int(body.minutes or MINUTES), 30))
        s = {"id": sid, "state": "running", "user": user, "owner": owner, "stem": stem, "fibre": body.fibre,
             "fibreName": f"{stem}-F{body.fibre:03d}", "rtuId": end["rtuId"], "rtuName": end["rtu"],
             "routeId": end["routes"][str(body.fibre)], "started": time.time(), "until": time.time() + mins * 60,
             "count": 0, "error": "", "note": "", "ref": None, "latest": None, "shots": [], "ended": None,
             "session": x_session, "shotS": _secs(body.seconds)}
        for k, o in list(SESSIONS_L.items()):           # v34: forget sessions that ended over an hour ago
            if o.get("ended") and time.time() - o["ended"] > 3600:
                SESSIONS_L.pop(k, None)
        SESSIONS_L[sid] = s
        loop = asyncio.get_event_loop()

        def token_fn():
            return asyncio.run_coroutine_threadsafe(valid_token(s["session"]), loop).result(30)
        threading.Thread(target=_loop, args=(s, token_fn), daemon=True).start()
        return public(s)

    @router.post("/api/otdr/live/status")
    async def status(body: IdIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        s = SESSIONS_L.get(body.id)
        if not s:
            raise HTTPException(404, "That live OTDR has ended (the service restarted)")
        return public(s, full=body.have != s["count"])

    # ── v41: stored OTDR traces of one fibre (both ends), and one trace to view ──
    @router.post("/api/otdr/traces")
    async def traces(body: FibreIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        token = await valid_token(x_session)
        require_desktop(sessions, x_session)
        stem = body.stem.strip().upper()
        if not 1 <= body.fibre <= 432:
            raise HTTPException(400, "Fibre must be 1 to 432")
        import relay_fibres
        ends = await relay_fibres.ensure_ends(token, stem)

        def fetch():
            import relay_bulk
            fms = relay_bulk.FMS_FOR(token) if relay_bulk.FMS_FOR else fc.Fms(token)
            rows, pids = [], _live_pids()
            for e in ends[:2]:
                rid = e["routes"].get(str(body.fibre))
                if not rid:
                    continue
                params = {"$filter": f"metadata/AssetId eq {int(rid)} and metadata/TestCategory eq 'Adhoc' and metadata/TestType eq 'OTDR'",
                          "$orderby": "metadata/TestTime desc", "$top": "40", "$skip": "0", "$select": "resultid,metadata,brief/LinkResults"}
                r = fms.get(fc.RESULTS_URL, params=params)
                r.raise_for_status()
                j = r.json()
                for x in j.get("results", j if isinstance(j, list) else []):
                    md = x.get("metadata") or {}
                    if md.get("HasError"):
                        continue
                    lr = (x.get("brief") or {}).get("LinkResults") or {}
                    res = (lr.get("Results") or [{}])[0]
                    rows.append({"resultId": x.get("resultid"), "t": _when(md.get("TestTime")), "rtuId": e["rtuId"], "rtu": e["rtu"],
                                 "len": _num(lr.get("Length")), "loss": _num(res.get("Loss")), "wl": res.get("Wavelength") or "",
                                 "live": bool(md.get("PromiseId")) and md.get("PromiseId") in pids})
            rows.sort(key=lambda r: -r["t"])
            return rows
        try:
            rows = await asyncio.to_thread(fetch)
        except Exception as e:                          # noqa: BLE001
            raise HTTPException(502, "FMS traces could not be read: " + str(e)[:120]) from None
        return {"stem": stem, "fibre": body.fibre, "traces": rows}

    @router.post("/api/otdr/trace")
    async def trace(body: TraceIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        token = await valid_token(x_session)
        require_desktop(sessions, x_session)
        rid = body.resultId.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", rid):
            raise HTTPException(400, "That is not a result id")
        if rid not in TRACES:
            import relay_bulk
            fms = relay_bulk.FMS_FOR(token) if relay_bulk.FMS_FOR else fc.Fms(token)
            try:
                t = await asyncio.to_thread(_read, fms, rid, time.time())
            except Exception as e:                      # noqa: BLE001
                raise HTTPException(502, str(e)[:160]) from None
            if len(TRACES) > 60:
                TRACES.pop(next(iter(TRACES)))
            TRACES[rid] = {k: t[k] for k in ("resultId", "testTime", "len", "trace", "events")}
        return TRACES[rid]

    @router.post("/api/otdr/live/settings")
    async def settings(body: IdIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        s = SESSIONS_L.get(body.id)
        if not s:
            raise HTTPException(404, "That live OTDR has ended")
        if body.seconds not in SHOT_CHOICES:
            raise HTTPException(400, "Shot length must be one of " + ", ".join(map(str, SHOT_CHOICES)) + " s")
        s["shotS"] = body.seconds                       # used from the next shot
        return public(s, full=False)

    @router.post("/api/otdr/live/stop")
    async def stop(body: IdIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        s = SESSIONS_L.get(body.id)
        if s and s["state"] == "running":
            s.update(state="stopped", note=f"Stopped by {mine(x_session)[1]}.")
        return {"ok": True}

    @router.post("/api/otdr/live/reference")
    async def reference(body: IdIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        s = SESSIONS_L.get(body.id)
        if not s or not s["latest"]:
            raise HTTPException(409, "No trace yet")
        s["ref"] = s["latest"]
        s["latest"] = {**s["latest"], "steps": [], "new": []}
        return public(s)

    @router.post("/api/otdr/live/list")
    async def lst(x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        now = time.time()
        return {"sessions": [public(s, False) for s in SESSIONS_L.values()
                             if s["state"] == "running" or now - (s["ended"] or now) < 300]}

    return router
