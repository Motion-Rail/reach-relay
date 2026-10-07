"""v33 (relay): download a finished FMS Task's result files (.sor) as one zip.

How FMS does it (captured from the FMS web UI, 7 Oct 2026, Task page "Download all"):
  GET {host}/upload/ClientsData/zip?resultIds=<id>,<id>,...   -> a zip built by FMS, at most 20 ids a call
The Task itself does not list its result ids, so each fibre's stored result is found in the results API
(same route, same test type, test time matching the Task's own time for that fibre).

Endpoints (desktop users only):
  POST /api/taskfiles/start {taskId}  -> {id, state, ...}  starts the work in the background
  POST /api/taskfiles/status {id}     -> progress: fibres matched, files fetched, notes
  POST /api/taskfiles/get {id}        -> the zip (application/zip, with a file name)
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import fms_continuity as fc

CHUNK = 20                     # the FMS UI sends 20 result ids per zip call
MATCH_S = 600                  # a stored result counts for a fibre when its test time is within 10 min
KEEP_S = 1800                  # a finished zip is kept for 30 minutes
JOBS: dict[str, dict] = {}
UPLOAD = fc.HOST + "/upload/ClientsData/zip"


class StartIn(BaseModel):
    taskId: str


class IdIn(BaseModel):
    id: str


def _when(v) -> float:
    from relay_fibres import _when as w
    return w(v)


def _short(rtu: str) -> str:
    m = re.match(r"^RTU\d*-([A-Z0-9]+)-\d+$", str(rtu or "").upper())
    return m.group(1) if m else str(rtu or "")


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9 ._-]+", "", str(s)).strip() or "files"


def find_result(fms: fc.Fms, route_id: str, kind: str, t: float) -> dict | None:
    """The stored result of this fibre's test in the Task: same route, same type, nearest test time."""
    params = {"$filter": f"metadata/AssetId eq {int(route_id)} and metadata/TestCategory eq 'Adhoc'",
              "$orderby": "metadata/TestTime desc", "$top": "20", "$skip": "0", "$select": "resultid,metadata"}
    r = fms.get(fc.RESULTS_URL, params=params)
    r.raise_for_status()
    j = r.json()
    best, gap = None, MATCH_S + 1
    for x in j.get("results", j if isinstance(j, list) else []):
        md = x.get("metadata") or {}
        if str(md.get("TestType") or "").lower() != kind.lower():
            continue
        g = abs(_when(md.get("TestTime")) - t) if t else 0
        if g < gap:
            best, gap = x, g
    return best if best and gap <= MATCH_S else None


def _merge(src: bytes, out: zipfile.ZipFile, seen: set) -> int:
    n = 0
    with zipfile.ZipFile(io.BytesIO(src)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/").split("/")[-1] or "file"
            base, ext = os.path.splitext(name)
            k = 2
            while name in seen:
                name = f"{base} ({k}){ext}"
                k += 1
            seen.add(name)
            out.writestr(name, z.read(info))
            n += 1
    return n


def run(job: dict, token_fn, loop=None) -> None:
    import relay_bulk
    from relay_fibres import parse_wf
    try:
        fms = relay_bulk.FMS_FOR(token_fn()) if relay_bulk.FMS_FOR else fc.Fms(token_fn())
        job["note"] = "Reading the Task…"
        wf = fms.workflow(job["taskId"], tasks=True)
        t = parse_wf(wf)
        if not t:
            raise RuntimeError("That is not a bulk OTDR or iOLM Task.")
        if wf.get("status") == "RUNNING":
            raise RuntimeError("The Task is still running. Download its files when it has finished.")
        routes = [r for r in t["routes"] if r.get("routeId") and r.get("status") == "COMPLETED"]
        name = ""
        try:
            name = relay_bulk.TASK_DETAIL(fms, wf).get("rtuName", "") if relay_bulk.TASK_DETAIL else ""
        except Exception:                               # noqa: BLE001
            pass
        stem = routes[0]["stem"] if routes else (t["routes"][0]["stem"] if t["routes"] else "")
        rid = (routes or t["routes"] or [{}])[0].get("rtuId", "")
        if not name or name.startswith("RTU ") and name[4:].isdigit():   # live FMS gives a Task no RTU name
            import relay_fibres
            ends = relay_fibres.ends_for(stem) if stem else []
            if len(ends) < 2 and stem and loop:
                try:
                    ends = asyncio.run_coroutine_threadsafe(relay_fibres.ensure_ends(token_fn(), stem), loop).result(60)
                except Exception:                       # noqa: BLE001
                    pass
            name = next((e["rtu"] for e in ends if str(e.get("rtuId")) == str(rid)), name)
        from datetime import datetime
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Europe/London")              # the relay runs on UTC; names use UK time
        except Exception:                               # noqa: BLE001
            tz = None
        day = datetime.fromtimestamp(t.get("started") or time.time(), tz).strftime("%Y-%m-%d %H%M")
        job.update(kind=t["kind"], fibres=len(t["routes"]), tested=len(routes), rtuName=name,
                   fileName=_safe(f"{stem} {_short(name)} {t['kind']} {day}") + ".zip")
        if not routes:
            raise RuntimeError("No fibre in this Task finished a test, so there are no files.")
        job["note"] = "Finding each fibre's result in FMS…"
        found: list[tuple[int, str]] = []

        def one(r):
            hit = find_result(fms, r["routeId"], t["kind"], r.get("t") or 0)
            job["matched"] += 1
            return (r["fibre"], hit.get("resultid")) if hit and hit.get("resultid") else (r["fibre"], "")
        with ThreadPoolExecutor(6) as ex:
            for f, rid in ex.map(one, routes):
                if rid:
                    found.append((f, rid))
                else:
                    job["missing"].append(f)
        found.sort()
        if not found:
            raise RuntimeError("FMS has no stored result for the fibres in this Task.")
        job["note"] = "Fetching the files from FMS…"
        fms.timeout = 180                               # FMS builds each zip itself, 20 files can take a while
        fd, path = tempfile.mkstemp(suffix=".zip", prefix="taskfiles-")
        os.close(fd)
        seen: set = set()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
            for i in range(0, len(found), CHUNK):
                if job["state"] != "running":
                    break
                ids = [rid for _, rid in found[i:i + CHUNK]]
                r = fms.get(UPLOAD, params={"resultIds": ",".join(ids)})
                if not r.ok:
                    raise RuntimeError(f"FMS would not give the files ({r.status_code}).")
                try:
                    job["files"] += _merge(r.content, out, seen)
                except zipfile.BadZipFile:
                    raise RuntimeError("FMS sent something that is not a zip.") from None
                job["chunks"] += 1
        if job["state"] != "running":
            os.remove(path)
            return
        job.update(path=path, size=os.path.getsize(path), state="ready", ended=time.time(),
                   note=f"{job['files']} file{'s' if job['files'] != 1 else ''} ready."
                        + (f" No stored result for {len(job['missing'])} fibre{'s' if len(job['missing']) != 1 else ''}." if job["missing"] else ""))
    except Exception as e:                              # noqa: BLE001
        job.update(state="failed", ended=time.time(), note=str(e)[:200] or "The download failed.")


def _tidy():
    now = time.time()
    for k, j in list(JOBS.items()):
        if j.get("ended") and now - j["ended"] > KEEP_S:
            if j.get("path"):
                try:
                    os.remove(j["path"])
                except OSError:
                    pass
            JOBS.pop(k, None)


def public(j: dict) -> dict:
    return {k: j.get(k) for k in ("id", "taskId", "state", "note", "kind", "fibres", "tested", "matched", "files",
                                  "chunks", "missing", "fileName", "size", "rtuName", "started", "ended")}


def make_router(valid_token, check_key, sessions: dict) -> APIRouter:
    router = APIRouter()
    from relay_bulk import require_desktop

    @router.post("/api/taskfiles/start")
    async def start(body: StartIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        require_desktop(sessions, x_session)
        tid = body.taskId.strip()
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", tid):
            raise HTTPException(400, "That is not a Task id")
        _tidy()
        for j in JOBS.values():                         # the same Task already on its way or ready: reuse it
            if j["taskId"] == tid and j["state"] in ("running", "ready"):
                return public(j)
        jid = uuid.uuid4().hex[:12]
        j = {"id": jid, "taskId": tid, "state": "running", "note": "Starting…", "kind": "", "fibres": 0, "tested": 0,
             "matched": 0, "files": 0, "chunks": 0, "missing": [], "fileName": "", "size": 0, "rtuName": "",
             "started": time.time(), "ended": None, "path": "", "session": x_session}
        JOBS[jid] = j
        loop = asyncio.get_event_loop()

        def token_fn():
            return asyncio.run_coroutine_threadsafe(valid_token(j["session"]), loop).result(30)
        threading.Thread(target=run, args=(j, token_fn, loop), daemon=True).start()
        return public(j)

    @router.post("/api/taskfiles/status")
    async def status(body: IdIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        j = JOBS.get(body.id)
        if not j:
            raise HTTPException(404, "That download has expired. Start it again.")
        return public(j)

    @router.post("/api/taskfiles/get")
    async def get(body: IdIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        j = JOBS.get(body.id)
        if not j or j["state"] != "ready" or not os.path.exists(j.get("path") or ""):
            raise HTTPException(404, "That download is not ready or has expired.")
        return FileResponse(j["path"], media_type="application/zip", filename=j["fileName"] or "task-files.zip")

    return router
