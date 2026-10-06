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

SHOT_S = max(1, int(os.getenv("LIVE_OTDR_S", "5")))
MINUTES = max(1, int(os.getenv("LIVE_OTDR_MINUTES", "10")))
TRACE_FORMAT = os.getenv("TRACE_FORMAT", "uint16le").strip().lower()
TRACE_SCALE = float(os.getenv("TRACE_SCALE", "0.001"))
BINS = 1500
STEP_DB = float(os.getenv("LIVE_OTDR_STEP_DB", "0.15"))     # smallest new loss worth marking
GAP_S = float(os.getenv("LIVE_OTDR_GAP_S", "1"))            # pause between shots

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


def _shot(fms, s: dict) -> dict:
    """One OTDR: start it, wait for the stored result, read the trace."""
    t0 = time.time()
    url = fc.ADHOC_URL.format(rtu=int(s["rtuId"]), route=int(s["routeId"]))
    r = fms.post(url, json=fc.adhoc_payload(SHOT_S))
    if r.status_code == 409 or "AlreadyScheduled" in r.text:
        raise RuntimeError("The RTU is busy with another test. Waiting.")
    if not r.ok:
        raise RuntimeError(f"FMS refused the OTDR ({r.status_code}): {r.text[:160]}")
    pid = r.text.strip().strip('"')
    found = None
    while time.time() - t0 < 90 and s["state"] == "running":
        time.sleep(2)
        for res in fms.adhoc_results(int(s["routeId"]), top=3):
            if (res.get("metadata") or {}).get("PromiseId") == pid:
                found = res
                break
        if found:
            break
    if not found:
        raise RuntimeError("No result after 90 s. Light on the fibre (a tone or traffic) stops an OTDR.")
    md = found.get("metadata") or {}
    if md.get("HasError"):
        why = " ".join(fc._strings(found, keys=("message", "messageKey", "error", "errorMessage")))[:200]
        if fc.LIVE_PATTERNS.search(why):
            raise RuntimeError("Live light on the fibre: the RTU will not fire an OTDR into it.")
        raise RuntimeError("The OTDR failed: " + (why or "no reason given"))
    rid = found.get("resultid")
    params = {"$filter": f"metadata/AssetId eq {int(s['routeId'])} and metadata/TestCategory eq 'Adhoc' and metadata/TestType eq 'OTDR'",
              "$orderby": "metadata/TestTime desc", "$top": "3", "$skip": "0",
              "$select": "resultid,metadata,brief/LinkResults,brief/Measurement/OtdrMeasurements"}
    rr = fms.get(fc.RESULTS_URL, params=params)
    rr.raise_for_status()
    j = rr.json()
    full = next((x for x in j.get("results", j if isinstance(j, list) else []) if x.get("resultid") == rid), None)
    if not full:
        raise RuntimeError("The trace could not be read back from FMS.")
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


def _loop(s: dict, token_fn):
    import relay_bulk
    fms = fc.Fms.from_token_provider(token_fn, s["user"])
    while s["state"] == "running":
        if time.time() > s["until"]:
            s.update(state="done", note="Stopped after the time limit.")
            break
        try:
            relay_bulk.mark_toning(s["rtuId"], SHOT_S + 30, s["user"], s["fibreName"])   # shows as testing everywhere
            shot = _shot(fms, s)
            s["error"] = ""
            if s["ref"] is None:
                s["ref"] = shot
            shot["steps"] = compare(s["ref"]["trace"], shot["trace"], s["ref"].get("len") or shot.get("len"))
            prev = s["latest"]
            shot["new"] = compare(prev["trace"], shot["trace"], shot.get("len")) if prev else []
            s["latest"] = shot
            s["shots"].append({k: shot[k] for k in ("t", "secs", "len", "resultId")} | {"steps": shot["steps"][:2]})
            s["count"] += 1
        except Exception as e:                          # noqa: BLE001
            s["error"] = str(e)[:240]
            time.sleep(8)
        time.sleep(GAP_S)
    relay_bulk.TONING.pop(str(s["rtuId"]), None)
    s["ended"] = time.time()


def public(s: dict, full: bool = True) -> dict:
    out = {k: s[k] for k in ("id", "state", "owner", "stem", "fibre", "rtuId", "rtuName", "started", "until", "count", "error", "note")}
    out["shotS"] = SHOT_S
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


class IdIn(BaseModel):
    id: str


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
             "session": x_session}
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
        return public(s)

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
