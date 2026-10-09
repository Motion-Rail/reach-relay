"""
v31 (relay): the Fibres screen. Every fibre of one cable with its latest result of each kind, from both ends,
plus what is being tested on the cable right now.

  POST /api/fibres {stem, live?, refresh?}   grid data; live=true returns only the "right now" part (cheap, for polling)
  POST /api/fibre  {stem, fibre}             one fibre: full history and the stored FMS results from both ends

Where the answers come from
  - E2E runs: the run logs in LOG_REPO (runs/<day>/<...>_<stem>_R..json), results per fibre (straight, cross, dis, unres)
  - Uni-dir: the run logs (kind "uni"): confirmed fibres (doneList, sent by apps from v37), DIS and crossed fibres
  - OTDR and iOLM: FMS bulk Tasks (Conductor workflows), each route's output {status, linkLength, linkLoss, testTime}
  - Detail only: the FMS results API for both route ids (ad hoc results, newest first), with per wavelength loss
  - Right now: E2E runs on the relay (JOBS), Uni-dir progress from phones and the desktop (UNI_LIVE), running Tasks

Finished Tasks and log files never change, so they are cached for the life of the relay. Settings:
FIBRES_DAYS (how far back to look, default 90), FIBRES_MAX_TASKS (most Tasks read per scan, default 300).
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import time
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
import os

import fms_continuity as fc

DAYS = max(1, int(os.getenv("FIBRES_DAYS", "90")))
MAX_TASKS = max(20, int(os.getenv("FIBRES_MAX_TASKS", "300")))
BASE_TTL = 60            # seconds the grid is kept before the logs and Tasks are read again
LIVE_TTL = 4             # running Tasks are read at most this often

WF_PARSED: dict[str, dict] = {}       # finished workflow id -> parsed Task (never changes); routes kept compact (v34)
LOG_FILES: dict[str, dict] = {}       # file sha -> parsed log (never changes)
DIRS: dict[str, dict] = {}            # dir path -> {"t", "items"}
BASE: dict[str, dict] = {}            # stem -> {"t", "data"}
SCAN = {"busy": False, "t": 0.0, "note": "", "seen": 0, "lock": threading.Lock()}
LIVE_TASKS = {"t": 0.0, "data": None}


def _num(v):
    try:
        x = float(v)
        return None if x != x else x
    except Exception:                                   # noqa: BLE001
        return None


def _when(v) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else float(v)
    s = str(v)
    if s.isdigit():
        return _when(int(s))
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:                                   # noqa: BLE001
        return 0.0


# ── v34: memory. 300 Tasks of 432 fibres as plain dicts took ~100 MB on a 512 MB relay, so routes are
#    kept as small tuples read by key, and the other caches are capped. ──
_RK = {k: i for i, k in enumerate(("fibre", "stem", "rtuId", "routeId", "status", "len", "loss", "t", "message"))}


class _Route(tuple):
    __slots__ = ()

    def __getitem__(self, k):
        return tuple.__getitem__(self, _RK[k] if isinstance(k, str) else k)

    def get(self, k, d=None):
        i = _RK.get(k)
        return d if i is None else tuple.__getitem__(self, i)


def _compact(p: dict) -> dict:
    p = dict(p)
    p["routes"] = [_Route((r["fibre"], sys.intern(r["stem"]), sys.intern(r["rtuId"]), sys.intern(r["routeId"]),
                           sys.intern(r["status"]), r["len"], r["loss"], r["t"], r["message"] or ""))
                   for r in p.get("routes", [])]
    return p


def _cap(d: dict, n: int):
    """Drop the oldest entries (insertion order) so a cache never holds more than n."""
    while len(d) > n:
        d.pop(next(iter(d)), None)


def _fib(name: str) -> int:
    m = re.search(r"-F(\d{1,3})$", str(name or "").upper())
    return int(m.group(1)) if m else 0


def _stem(name: str) -> str:
    return re.sub(r"-F\d{1,3}$", "", str(name or "").upper())


def _fnum(text) -> int:
    m = re.search(r"F?(\d{1,3})", str(text or "").upper())
    return int(m.group(1)) if m else 0


# ── FMS bulk Tasks ───────────────────────────────────────────────────────────────
def parse_wf(wf: dict) -> dict | None:
    """One bulk Task: kind, wavelengths, and per route {fibre, stem, rtuId, routeId, status, len, loss, t}."""
    from relay_team import _owner, _wf_input
    inp = _wf_input(wf)
    sub = str(inp.get("subType") or "")
    kind = "OTDR" if sub.endswith("OTDR") else "iOLM" if sub.endswith("IOLM") else ""
    ins = inp.get("subworkflowInputs") or {}
    if not kind or not ins:
        return None
    out = wf.get("output") or {}
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except Exception:                               # noqa: BLE001
            out = {}
    order = [s.get("taskReferenceName") for s in (inp.get("subworkflows") or [])]
    subs = sorted((t for t in wf.get("tasks", []) if t.get("taskType") == "SUB_WORKFLOW"),
                  key=lambda t: t.get("seq") or t.get("startTime") or 0)
    routes = []
    for i, (ref, si) in enumerate(ins.items()):
        name = str(si.get("OpticalRouteName") or "")
        o = out.get(ref) if isinstance(out, dict) else None
        if isinstance(o, str):
            try:
                o = json.loads(o)
            except Exception:                           # noqa: BLE001
                o = None
        res = (o or {}).get("result") or {}
        if isinstance(res, str):
            try:
                res = json.loads(res)
            except Exception:                           # noqa: BLE001
                res = {}
        try:
            k = order.index(ref)
        except ValueError:
            k = i
        tstate = (subs[k] if k < len(subs) else {}).get("status") or ""
        st = str(res.get("status") or tstate or "WAITING").upper()
        if kind == "OTDR":
            wls = [round(float(((si.get("Payload") or {}).get("payLoad") or {}).get("wavelength") or 0) * 1e9)]
        else:
            wls = [round(float(w) * 1e9) for w in (si.get("WavelengthsUsed") or [])]
        routes.append({"fibre": _fib(name), "stem": _stem(name), "rtuId": str(si.get("RtuId") or ""),
                       "routeId": str(si.get("OpticalRouteId") or ""), "status": st,
                       "len": _num(res.get("linkLength")), "loss": _num(res.get("linkLoss")),
                       "t": _when(res.get("testTime")) or ((subs[k].get("endTime") or 0) / 1000 if k < len(subs) else 0)
                            or (wf.get("endTime") or 0) / 1000,
                       "message": str(res.get("message") or "")[:120]})
    return {"id": wf.get("workflowId", ""), "status": wf.get("status"), "kind": kind,
            "wls": [w for w in wls if w] if kind == "OTDR" else
                   sorted({round(float(w) * 1e9) for si in ins.values() for w in (si.get("WavelengthsUsed") or [])}),
            "owner": _owner(inp.get("creatorName") or inp.get("UserName") or ""),
            "started": (wf.get("startTime") or 0) / 1000, "ended": (wf.get("endTime") or 0) / 1000,
            "comment": str(inp.get("comment") or ""), "routes": routes}


def scan_tasks(token: str) -> str:
    """Read finished bulk Tasks newest first until DAYS back. Each Task's detail is fetched once."""
    from relay_team import SEARCH
    import relay_bulk
    fms = relay_bulk.FMS_FOR(token) if relay_bulk.FMS_FOR else fc.Fms(token)
    cutoff = time.time() - DAYS * 86400
    start, read = 0, 0
    base = SEARCH.format(size=50, status="COMPLETED,FAILED,TERMINATED,TIMED_OUT")
    while read < MAX_TASKS:
        r = fms.get(base.replace("start=0", f"start={start}"))
        r.raise_for_status()
        j = r.json()
        rows = j.get("results", j if isinstance(j, list) else [])
        if not rows:
            break
        old = False
        for s in rows:
            wid = s.get("workflowId")
            if not wid:
                continue
            read += 1
            if _when(s.get("startTime")) and _when(s.get("startTime")) < cutoff:
                old = True
                break
            if wid in WF_PARSED:
                continue
            try:
                p = parse_wf(fms.workflow(wid, tasks=True))
            except Exception:                           # noqa: BLE001
                p = None
            WF_PARSED[wid] = _compact(p) if p else {"routes": []}
        if old or len(rows) < 50:
            break
        start += 50
    SCAN["seen"] = read
    return ""


def running_tasks(token: str) -> list[dict]:
    """Running bulk Tasks with per fibre state, cached a few seconds (shared by everyone polling)."""
    if LIVE_TASKS["data"] is not None and time.time() - LIVE_TASKS["t"] < LIVE_TTL:
        return LIVE_TASKS["data"]
    import relay_bulk
    data = []
    try:
        if relay_bulk.READ_TASKS:
            data = relay_bulk.READ_TASKS(token, 0).get("running", [])
    except Exception:                                   # noqa: BLE001
        data = []
    LIVE_TASKS.update(t=time.time(), data=data)
    return data


# ── run logs (E2E and Uni-dir) ──────────────────────────────────────────────────
def _log_on() -> bool:
    from relay_continuity import LOG_REPO, LOG_TOKEN
    return bool(LOG_REPO and LOG_TOKEN)


def _list(path: str, ttl: float) -> list[dict]:
    from relay_continuity import _gh_list_sync
    c = DIRS.get(path)
    if c and time.time() - c["t"] < ttl:
        return c["items"]
    items = _gh_list_sync(path)
    DIRS[path] = {"t": time.time(), "items": items}
    _cap(DIRS, 300)
    return items


def _read(item: dict) -> dict | None:
    from relay_continuity import _gh_get_sync
    sha = item.get("sha", "")
    if sha and sha in LOG_FILES:
        return LOG_FILES[sha]
    rep, _ = _gh_get_sync(item["path"])
    if rep is not None and sha and rep.get("state") not in ("running", "paused"):
        LOG_FILES[sha] = rep
        _cap(LOG_FILES, 800)
    return rep


def log_reports(stem: str) -> list[dict]:
    """E2E and Uni-dir reports for this cable, from the file names, so other cables are never opened."""
    if not _log_on():
        return []
    key = re.sub(r"[^A-Za-z0-9-]", "", stem.upper())
    today = time.strftime("%Y-%m-%d", time.gmtime())
    days = {i.get("name") for i in _list("runs", 60)}
    out = []
    for d in range(DAYS):
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - d * 86400))
        if day not in days:
            continue
        for item in _list(f"runs/{day}", 60 if day == today else 3600):
            nm = str(item.get("name", ""))
            if nm.endswith(".json") and key in nm.upper():
                try:
                    rep = _read(item)
                except Exception:                       # noqa: BLE001
                    rep = None
                if rep:
                    out.append(rep)
    return out


# ── putting a cable together ─────────────────────────────────────────────────────
def ends_for(stem: str) -> list[dict]:
    main = sys.modules.get("main")
    cache = getattr(main, "ROUTE_ID_CACHE", {}) if main else {}
    by: dict[str, dict] = {}
    for k, v in list(cache.items()):
        name = k.split("|", 1)[1] if "|" in k else ""
        if _stem(name) == stem.upper() and v.get("rtuId"):
            e = by.setdefault(v["rtuId"], {"rtu": v.get("rtuName", ""), "rtuId": v["rtuId"], "site": v.get("site", ""), "routes": {}})
            e["routes"][str(_fib(name))] = v.get("id", "")
    first = (stem.split("-") + ["", ""])[1].upper()
    return sorted(by.values(), key=lambda e: (first not in e["rtu"].upper(), e["rtu"]))


async def ensure_ends(token: str, stem: str) -> list[dict]:
    ends = ends_for(stem)
    if len(ends) >= 2:
        return ends
    main = sys.modules.get("main")
    crawl = getattr(main, "_crawl", None) if main else None
    if crawl:
        for code in [c for c in stem.split("-")[1:3] if c]:
            try:
                await crawl(token, code)
            except Exception:                           # noqa: BLE001
                pass
    return ends_for(stem)


def _end_of(ends: list[dict], rtu_id: str = "", rtu_name: str = "") -> str:
    for i, e in enumerate(ends[:2]):
        if (rtu_id and e["rtuId"] == str(rtu_id)) or (rtu_name and e["rtu"].upper() == str(rtu_name).upper()):
            return "ab"[i]
    return ""


def build(stem: str, ends: list[dict]) -> dict:
    from relay_team import _owner
    stem = stem.upper()
    rows = {f: {"f": f} for f in range(1, 433)}
    hist: dict[int, list] = {}

    def note(f, item):
        hist.setdefault(f, []).append(item)

    for rep in log_reports(stem):
        if rep.get("kind") == "uni":
            t = rep.get("ended") or rep.get("started") or 0
            who, loc = _owner(rep.get("name") or rep.get("user", "")), rep.get("location", "")
            end = rep.get("rtu", "")
            marks = {}
            for f in rep.get("doneList") or []:
                marks[int(f)] = {"state": "confirmed"}
            for x in rep.get("dis") or []:
                marks[_fnum(x)] = {"state": "dis"}
            for x in rep.get("cross") or []:
                a, _, b = str(x).partition(" with ")
                marks[_fnum(a)] = {"state": "cross", "found": _fnum(b) or None}
            for f, m in marks.items():
                if not 1 <= f <= 432:
                    continue
                item = {**m, "t": t, "by": who, "location": loc, "rtu": end}
                if (rows[f].get("uni") or {}).get("t", 0) <= t:
                    rows[f]["uni"] = item
                note(f, {"t": t, "type": "Uni-dir", "end": end,
                         "summary": {"confirmed": "confirmed", "dis": "Dis.", "cross": "crossed"}[m["state"]]
                         + (f" to F{m['found']:03d}" if m.get("found") else "") + (f" at {loc}" if loc else ""), "by": who})
        elif rep.get("stem", "").upper() == stem:
            t = rep.get("ended") or rep.get("started") or 0
            who = _owner(rep.get("user") or (rep.get("settings") or {}).get("user", ""))
            for k, v in (rep.get("results") or {}).items():
                f = int(k)
                if not 1 <= f <= 432:
                    continue
                item = {"state": v.get("state"), "found": v.get("found") or None, "t": t, "by": who,
                        "loc": ((v.get("loc") or {}).get("text") or "")}
                if (rows[f].get("e2e") or {}).get("t", 0) <= t:
                    rows[f]["e2e"] = item
                word = {"straight": "straight", "cross": f"crossed to F{int(v.get('found') or 0):03d}",
                        "flip": f"flipped ribbon, lands on F{int(v.get('found') or 0):03d}",
                        "dis": "Dis." + (f", {item['loc']}" if item["loc"] else ""), "unres": "not found"}.get(v.get("state"), v.get("state"))
                note(f, {"t": t, "type": "E2E", "end": rep.get("toneRtu", ""), "summary": word, "by": who})
    for p in list(WF_PARSED.values()):
        for r in p.get("routes", []):
            if r["stem"] != stem or not 1 <= r["fibre"] <= 432 or r["status"] not in ("COMPLETED", "FAILED"):
                continue
            end = _end_of(ends, r["rtuId"])
            if not end:
                continue
            f, kind = r["fibre"], p["kind"].lower()
            item = {"len": r["len"], "t": r["t"], "status": r["status"], "task": p["id"], "by": p["owner"]}
            if kind == "otdr":
                item.update(loss=r["loss"], wl=(p["wls"] or [None])[0])
            else:
                item.update(loss=({str(p["wls"][0]): r["loss"]} if len(p["wls"]) == 1 and r["loss"] is not None else {}),
                            wls=p["wls"], linkLoss=r["loss"], verdict="Failed" if r["status"] == "FAILED" else "")
            cur = (rows[f].get(kind) or {}).get(end)
            if r["status"] == "COMPLETED" and (not cur or cur["t"] <= item["t"]):
                rows[f].setdefault(kind, {})[end] = item
            words = (f"{r['len'] / 1000:.3f} km" if r["len"] else "") + (f", {r['loss']:.2f} dB" if r["loss"] is not None else "")
            note(f, {"t": item["t"], "type": p["kind"], "end": ends[0 if end == "a" else 1]["rtu"],
                     "summary": (words or r["status"].lower()) + (f" @{'/'.join(map(str, p['wls']))}" if p["wls"] else "")
                     + ("" if r["status"] == "COMPLETED" else f" ({r['message'] or r['status'].lower()})"), "by": p["owner"]})
    for f in hist:
        hist[f].sort(key=lambda x: x["t"] or 0, reverse=True)
    return {"rows": [rows[f] for f in range(1, 433)], "hist": hist}


def live_part(stem: str, ends: list[dict], token: str | None) -> dict:
    """What is happening on this cable now, from the relay's memory. Cheap enough to poll every few seconds."""
    from relay_continuity import JOBS
    from relay_team import UNI_LIVE, UNI_LIVE_TTL, USER_NAMES, _owner
    stem = stem.upper()
    now = time.time()
    runs, uni, tasks = [], [], []
    for j in list(JOBS.values()):
        if j.get("state") not in ("running", "paused") or str(j.get("stem", "")).upper() != stem:
            continue
        eng = j.get("engine")
        res = getattr(eng, "results", {}) or {}
        runs.append({"id": j["id"], "state": j["state"], "owner": _owner(j.get("user", "")), "ribbons": j.get("ribbons", []),
                     "current": getattr(eng, "current", None), "toneRtu": j.get("toneRtu"),
                     "results": {str(f): {"state": r.state, "found": r.found or None} for f, r in res.items()}})
    key = re.sub(r"-R\d+$", "", stem)
    for v in list(UNI_LIVE.values()):
        if now - v.get("seen", 0) > UNI_LIVE_TTL or str(v.get("cable", "")).upper() != key:
            continue
        uni.append({"owner": _owner(USER_NAMES.get(str(v.get("user", "")).lower(), v.get("name") or v.get("user", ""))),
                    "rtu": v.get("rtu", ""), "fibre": v.get("fibre"), "toning": bool(v.get("toning")),
                    "doneList": v.get("doneList") or [], "disList": v.get("disList") or [], "crossList": v.get("crossList") or [],
                    "location": v.get("location", "")})
    if token:
        for t in running_tasks(token):
            fib = [(_fib(x.get("name")), x.get("state")) for x in t.get("fibres", []) if _stem(x.get("name")) == stem]
            if not fib:
                continue
            tasks.append({"id": t.get("id"), "kind": t.get("kind"), "owner": t.get("owner"), "rtuId": t.get("rtuId"),
                          "end": _end_of(ends, t.get("rtuId")), "done": t.get("done"), "total": t.get("total"),
                          "testing": [f for f, s in fib if s in ("IN_PROGRESS", "SCHEDULED")],
                          "finished": [f for f, s in fib if s in ("COMPLETED", "FAILED")]})
    otdr = []
    try:                                                # v32: live OTDR sessions on this cable
        from relay_otdr import SESSIONS_L
        for o in list(SESSIONS_L.values()):
            if o["state"] == "running" and o["stem"] == stem:
                otdr.append({"id": o["id"], "owner": o["owner"], "fibre": o["fibre"], "rtuId": o["rtuId"],
                             "end": _end_of(ends, o["rtuId"]), "count": o["count"]})
    except Exception:                                   # noqa: BLE001
        pass
    return {"t": now, "runs": runs, "uni": uni, "tasks": tasks, "otdr": otdr}


class FibresIn(BaseModel):
    stem: str
    live: bool = False
    refresh: bool = False


class FibreIn(BaseModel):
    stem: str
    fibre: int


def make_router(valid_token, check_key, sessions: dict) -> APIRouter:
    router = APIRouter()
    from relay_bulk import require_desktop

    def bg_scan(token: str):
        if SCAN["busy"]:
            return
        SCAN["busy"] = True

        def go():
            try:
                SCAN["note"] = scan_tasks(token)
            except Exception as e:                      # noqa: BLE001
                SCAN["note"] = "FMS Tasks could not be read: " + str(e)[:120]
            finally:
                SCAN.update(busy=False, t=time.time())
                BASE.clear()                            # rebuild with what was found
        threading.Thread(target=go, daemon=True).start()

    @router.post("/api/fibres")
    async def fibres(body: FibresIn, x_app_key: str | None = Header(default=None),
                     x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        token = await valid_token(x_session)
        require_desktop(sessions, x_session)
        stem = body.stem.strip().upper()
        if not re.match(r"^F-[A-Z0-9]+-[A-Z0-9]+-[A-Z](-R\d+)?$", stem):
            raise HTTPException(400, "Not a cable name, for example F-RGAC-SNBC-A-R432")
        ends = await ensure_ends(token, stem)
        live = await asyncio.to_thread(live_part, stem, ends, token)
        if body.live:
            return {"ok": True, "stem": stem, "live": live}
        first = SCAN["t"] == 0
        if body.refresh or time.time() - SCAN["t"] > 300:
            bg_scan(token)
        if first:                                       # the very first look: give the Task scan a few seconds
            for _ in range(16):
                if not SCAN["busy"]:
                    break
                await asyncio.sleep(0.5)
        b = BASE.get(stem)
        if body.refresh or not b or time.time() - b["t"] > BASE_TTL:
            data = await asyncio.to_thread(build, stem, ends)
            b = BASE[stem] = {"t": time.time(), "data": data}
            _cap(BASE, 6)
        notes = []
        if not _log_on():
            notes.append("E2E and Uni-dir results need the run logs (LOG_REPO) on the relay.")
        if SCAN["busy"]:
            notes.append("Still reading older FMS Tasks; more results will appear.")
        if SCAN["note"]:
            notes.append(SCAN["note"])
        if len(ends) < 2:
            notes.append("Only one end of this cable was found in FMS.")
        return {"ok": True, "stem": stem, "ends": [{k: e[k] for k in ("rtu", "rtuId", "site")} for e in ends[:2]],
                "fibres": b["data"]["rows"], "updated": b["t"], "notes": notes, "live": live,
                "scan": {"busy": SCAN["busy"], "tasks": SCAN["seen"], "days": DAYS}}

    @router.post("/api/fibre")
    async def fibre(body: FibreIn, x_app_key: str | None = Header(default=None),
                    x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        token = await valid_token(x_session)
        require_desktop(sessions, x_session)
        stem = body.stem.strip().upper()
        f = int(body.fibre)
        if not 1 <= f <= 432:
            raise HTTPException(400, "Fibre must be 1 to 432")
        ends = await ensure_ends(token, stem)
        b = BASE.get(stem)
        if not b:
            b = BASE[stem] = {"t": time.time(), "data": await asyncio.to_thread(build, stem, ends)}
        hist = list(b["data"]["hist"].get(f, []))
        results, iolm, note = {}, {}, ""

        def fetch():
            import relay_bulk
            fms = relay_bulk.FMS_FOR(token) if relay_bulk.FMS_FOR else fc.Fms(token)
            out = {}
            for i, e in enumerate(ends[:2]):
                rid = e["routes"].get(str(f))
                if not rid:
                    continue
                params = {"$filter": f"metadata/AssetId eq {int(rid)} and metadata/TestCategory eq 'Adhoc'",
                          "$orderby": "metadata/TestTime desc", "$top": "12", "$skip": "0",
                          "$select": "resultid,brief/LinkResults,brief/GlobalVerdict,metadata"}
                r = fms.get(fc.RESULTS_URL, params=params)
                r.raise_for_status()
                j = r.json()
                out["ab"[i]] = j.get("results", j if isinstance(j, list) else [])
            return out
        try:
            raw = await asyncio.to_thread(fetch)
        except Exception as e:                          # noqa: BLE001
            raw, note = {}, "FMS results could not be read: " + str(e)[:120]
        for end, items in raw.items():
            name = ends[0 if end == "a" else 1]["rtu"]
            for res in items:
                md = res.get("metadata") or {}
                link = ((res.get("brief") or {}).get("LinkResults") or {})
                ttype = str(md.get("TestType") or "")
                losses = {str(x.get("Wavelength")): _num(x.get("Loss")) for x in (link.get("Results") or []) if x.get("Wavelength")}
                t = _when(md.get("TestTime"))
                item = {"t": t, "type": ttype or "Result", "len": _num(link.get("Length")), "loss": losses,
                        "verdict": str((res.get("brief") or {}).get("GlobalVerdict") or "")}
                results.setdefault(end, []).append(item)
                if ttype.lower() == "iolm" and end not in iolm:
                    iolm[end] = {"loss": {k: v for k, v in losses.items() if v is not None}, "len": item["len"],
                                 "verdict": item["verdict"] if item["verdict"] in ("Pass", "Fail") else "", "t": t}
                words = (f"{item['len'] / 1000:.3f} km" if item["len"] else "") + \
                        "".join(f", {v:.2f} dB @{k}" for k, v in losses.items() if v is not None)
                hist.append({"t": t, "type": (ttype or "Result") + " (FMS result)", "end": name,
                             "summary": (words.lstrip(", ") or "stored result") + (f", {item['verdict']}" if item["verdict"] not in ("", "Unknown") else "")})
        # a bulk Task also stores an FMS result: keep one line per test
        tasks_at = [(h["type"], h.get("end"), h["t"]) for h in hist if h["type"] in ("OTDR", "iOLM")]
        hist = [h for h in hist if not (h["type"].endswith("(FMS result)") and any(
            h["type"].split(" ")[0].lower() == k.lower() and h.get("end") == e and abs((h["t"] or 0) - (t or 0)) < 300
            for k, e, t in tasks_at))]
        hist.sort(key=lambda x: x["t"] or 0, reverse=True)
        return {"ok": True, "stem": stem, "fibre": f, "history": hist[:60], "iolm": iolm, "note": note}

    return router
