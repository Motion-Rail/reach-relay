"""Brunel.3 (relay): the full FMS Test Results Report, the same workbook as the PC tool, run from the app.

POST /api/fullreport/start      {stem, rtuA, rtuB, ribbons, testType, from, to, nominal, viewWl, client?, clientName?,
                                 taskId?, label?}                       -> job (queued or running)
POST /api/fullreport/status     {id}                                    -> job with progress
POST /api/fullreport/list       {}                                      -> this relay's jobs plus the saved Report history
POST /api/fullreport/get        {id, kind: "xlsx"|"log"}                -> the file
POST /api/fullreport/locations  {}                                      -> stored location schedules, by cable
POST /api/fullreport/locations/upload {cable, name, data (base64)}      -> stores one cable's Distances workbook
POST /api/fullreport/locations/get    {cable}                           -> that workbook

The engine (fms_report/fms_pull.py and bidir_report.py, copied unchanged from the PC tool) runs in its own process
(report_job.py), one report at a time; the others queue. It signs in with the user's own app sign in, asked for each
time it needs a token. Finished reports and their run logs are kept in LOG_REPO under reports/, with an index, so
Report history survives relay restarts. Location schedules live in LOG_REPO under config/locations/; the NRS-304
Distances workbook that ships with the PC tool is the fallback for F-RGAC-SNBC. Remedial tracking history
(remedial_history.json) is kept in config/ so NEW / EXISTING / IMPROVED works across runs as on the PC.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import requests
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from relay_continuity import LOG_API, LOG_REPO, LOG_TOKEN

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "fms_report")
WORK = os.path.join(tempfile.gettempdir(), "rft-reports")
FMS_BASE = os.environ.get("FMS_HOST") or os.environ.get("EXFO_AUTH_BASE") or "https://raman.ems.exfo-fms.com"
SEED_LOC = {"F-RGAC-SNBC": os.path.join(ENGINE, "Distances.xlsx")}   # ships with the PC tool (NRS-304)
JOBS: dict[str, dict] = {}
QUEUE: list[str] = []
LOCK = threading.Lock()
KEEP_S = 6 * 3600
MAX_CLIENT = 15 * 1024 * 1024


class StartIn(BaseModel):
    stem: str
    rtuA: str
    rtuB: str = ""
    ribbons: str = ""
    testType: str = "auto"
    dfrom: str = ""
    dto: str = ""
    nominal: float = 0.02
    viewWl: str = "1550"
    client: str = ""            # base64 of the EXFO customer report, for Client Verify
    clientName: str = ""
    taskId: str = ""
    label: str = ""


class IdIn(BaseModel):
    id: str
    kind: str = "xlsx"


class LocUp(BaseModel):
    cable: str
    name: str
    data: str


class LocGet(BaseModel):
    cable: str


# ── LOG_REPO helpers (binary safe; GitHub only returns the raw body above 1 MB) ──
def _h(raw=False):
    h = {"Authorization": "Bearer " + LOG_TOKEN, "X-GitHub-Api-Version": "2022-11-28"}
    h["Accept"] = "application/vnd.github.raw" if raw else "application/vnd.github+json"
    return h


def repo_on() -> bool:
    return bool(LOG_REPO and LOG_TOKEN)


def gh_get_bytes(path: str) -> bytes | None:
    if not repo_on():
        return None
    r = requests.get(f"{LOG_API}/repos/{LOG_REPO}/contents/{path}", headers=_h(raw=True), timeout=60)
    if not r.ok:
        return None
    if "json" in (r.headers.get("content-type") or ""):
        try:
            j = r.json()
        except ValueError:
            return r.content
        if isinstance(j, dict) and j.get("content"):
            return base64.b64decode(j["content"])
        if isinstance(j, dict):
            return None
    return r.content


def gh_put_bytes(path: str, data: bytes, message: str) -> bool:
    if not repo_on():
        return False
    url = f"{LOG_API}/repos/{LOG_REPO}/contents/{path}"
    sha = None
    g = requests.get(url, headers=_h(), timeout=30)
    if g.ok:
        try:
            sha = (g.json() or {}).get("sha")
        except ValueError:
            sha = None
    body = {"message": message, "content": base64.b64encode(data).decode()}
    if sha:
        body["sha"] = sha
    r = requests.put(url, headers=_h(), json=body, timeout=120)
    return r.ok


def gh_get_json(path: str, default):
    b = gh_get_bytes(path)
    if not b:
        return default
    try:
        return json.loads(b.decode("utf-8"))
    except Exception:                       # noqa: BLE001
        return default


def cable_key(stem: str) -> str:
    """F-RGAC-SNBC-A-R432 -> F-RGAC-SNBC"""
    m = re.match(r"^(F-[A-Z0-9]+-[A-Z0-9]+)", str(stem or "").upper())
    return m.group(1) if m else str(stem or "").upper()


def ribbon_text(spec: str) -> str:
    s = (spec or "").strip()
    return "all ribbons" if not s or s.lower() in ("all", "*") else "R" + s.replace(",", ", R")


def public(j: dict) -> dict:
    tot = sum(j["totals"].values()) or 0
    ends = 2 if j["rtuB"] else 1
    if len(j["totals"]) < ends and j["totals"]:
        tot = max(j["totals"].values()) * ends
    done = sum(j["done"].values())
    pos = QUEUE.index(j["id"]) + 1 if j["id"] in QUEUE else 0
    return {k: j.get(k) for k in ("id", "state", "stem", "rtuA", "rtuB", "ribbons", "testType", "dfrom", "dto", "nominal",
                                  "user", "label", "taskId", "started", "ended", "fileName", "logName", "error", "note",
                                  "size", "saved", "clientName")} | {
        "done": done, "total": tot, "pct": round(100 * done / tot) if tot else 0, "queuePos": pos,
        "stage": j.get("stage", ""), "tail": j.get("tail", [])[-8:]}


def _tidy():
    now = time.time()
    for k, j in list(JOBS.items()):
        if j.get("ended") and now - j["ended"] > KEEP_S:
            shutil.rmtree(j.get("dir") or "", ignore_errors=True)
            JOBS.pop(k, None)


def _locations_for(stem: str, folder: str) -> str:
    key = cable_key(stem)
    b = gh_get_bytes(f"config/locations/{key}.xlsx")
    if b:
        p = os.path.join(folder, f"{key} locations.xlsx")
        with open(p, "wb") as fh:
            fh.write(b)
        return p
    return SEED_LOC.get(key, "") if os.path.exists(SEED_LOC.get(key, "") or "/nonexistent") else ""


def _run(j: dict, token_fn):
    """One report, start to finish, in its own process."""
    folder = j["dir"]
    try:
        j["state"], j["note"], j["startedRun"] = "running", "Getting ready…", time.time()
        hist = gh_get_bytes("config/remedial_history.json")
        if hist:
            with open(os.path.join(ENGINE, "remedial_history.json"), "wb") as fh:
                fh.write(hist)
        loc = _locations_for(j["stem"], folder)
        j["locations"] = os.path.basename(loc) if loc else ""
        verify = ""
        if j.get("clientPath"):
            verify = j["clientPath"]
        cfg = {"rtuA": j["rtuA"], "rtuB": j["rtuB"], "ribbons": j["ribbons"], "testType": j["testType"],
               "from": j["dfrom"], "to": j["dto"], "nominal": j["nominal"], "viewWl": j["viewWl"],
               "locations": loc, "verify": verify, "user": j["user"], "out": os.path.join(folder, "out"),
               "fmsBase": FMS_BASE}
        env = {**os.environ, "REPORT_JOB": json.dumps(cfg), "PYTHONUNBUFFERED": "1", "MALLOC_ARENA_MAX": "2"}
        p = subprocess.Popen([sys.executable, os.path.join(HERE, "report_job.py")], cwd=HERE, env=env, text=True,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
        j["pid"] = p.pid
        j["note"] = "Signing in to FMS…"
        files = []
        for line in p.stdout:
            line = line.rstrip("\n")
            if j.get("cancel"):
                p.kill()
                break
            if line == "NEED_TOKEN":
                try:
                    tok = token_fn()
                except Exception as e:      # noqa: BLE001
                    tok = "ERROR " + str(e)[:80]
                p.stdin.write(tok + "\n")
                p.stdin.flush()
                continue
            if line.startswith("STAGE|pull|"):
                _, _, end, n = line.split("|")
                j["totals"][end] = int(n)
                j["done"].setdefault(end, 0)
                j["stage"] = f"Pulling results from {end}"
                j["note"] = f"Pulling {n} fibres from {end}"
            elif line.startswith("PROG|"):
                _, end, d, n = line.split("|")
                j["done"][end] = int(d)
                j["totals"][end] = int(n)
                j["note"] = f"{end}: fibre {d} of {n}"
            elif line.startswith("DONE|"):
                files = json.loads(line[5:]).get("files") or []
            elif line.startswith("ERROR|"):
                j["error"] = line[6:]
            elif line.strip():
                j["tail"].append(line.strip()[:200])
                del j["tail"][:-60]
                if "Workbook written" in line or "Building" in line:
                    j["stage"], j["note"] = "Building the workbook", "Building the workbook…"
        p.wait()
        if j.get("cancel"):
            j["state"], j["note"] = "cancelled", "Cancelled"
            return
        out = os.path.join(folder, "out")
        if p.returncode != 0 or not files:
            j["state"] = "error"
            j["error"] = j.get("error") or "The report did not finish. " + " / ".join(j["tail"][-3:])
            j["note"] = "Failed"
            return
        xl = os.path.join(out, files[0])
        log = xl[:-5] + "_runlog.txt"
        j["path"], j["fileName"], j["size"] = xl, files[0], os.path.getsize(xl)
        if os.path.exists(log):
            j["logPath"], j["logName"] = log, os.path.basename(log)
        j["state"], j["note"], j["ended"] = "done", "Ready", time.time()
        # keep it: the workbook, its run log, the index, and the remedial history
        try:
            mon = time.strftime("%Y-%m", time.gmtime())
            j["repoPath"] = f"reports/{mon}/{j['fileName']}"
            ok = gh_put_bytes(j["repoPath"], open(xl, "rb").read(), f"report {j['fileName']}")
            if j.get("logPath"):
                gh_put_bytes(f"reports/{mon}/{j['logName']}", open(j["logPath"], "rb").read(), f"run log {j['logName']}")
            hp = os.path.join(ENGINE, "remedial_history.json")
            if os.path.exists(hp):
                gh_put_bytes("config/remedial_history.json", open(hp, "rb").read(), "remedial history")
            if ok:
                idx = gh_get_json("reports/index.json", [])
                idx.insert(0, {k: j.get(k) for k in ("id", "stem", "rtuA", "rtuB", "ribbons", "testType", "dfrom", "dto",
                                                      "nominal", "user", "label", "taskId", "fileName", "logName", "size",
                                                      "repoPath", "ended", "clientName", "locations")})
                gh_put_bytes("reports/index.json", json.dumps(idx[:500], indent=1).encode(), "report index")
                j["saved"] = True
        except Exception as e:              # noqa: BLE001
            j["saveError"] = str(e)[:200]
    except Exception as e:                  # noqa: BLE001
        j["state"], j["error"], j["note"] = "error", str(e)[:400], "Failed"
    finally:
        j["ended"] = j.get("ended") or time.time()
        with LOCK:
            if j["id"] in QUEUE:
                QUEUE.remove(j["id"])
        _next()


RUNNERS: dict[str, object] = {}


def _next():
    with LOCK:
        if any(x["state"] == "running" for x in JOBS.values()):
            return
        nxt = next((JOBS[i] for i in QUEUE if JOBS.get(i, {}).get("state") == "queued"), None)
        if not nxt:
            return
        nxt["state"] = "running"
    threading.Thread(target=_run, args=(nxt, RUNNERS[nxt["id"]]), daemon=True).start()


def make_router(valid_token, check_key, sessions: dict) -> APIRouter:
    router = APIRouter()
    from relay_bulk import require_desktop

    @router.post("/api/fullreport/start")
    async def start(body: StartIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        require_desktop(sessions, x_session)
        stem = body.stem.strip().upper()
        if not re.match(r"^F-[A-Z0-9]+-[A-Z0-9]+-[A-Z](-R\d+)?$", stem):
            raise HTTPException(400, "Not a cable name, for example F-RGAC-SNBC-A-R432")
        for r in (body.rtuA, body.rtuB):
            if r and not re.fullmatch(r"RTU\d*-[A-Z0-9]+-\d+", r.strip().upper()):
                raise HTTPException(400, f"Not an RTU name: {r}")
        if not body.rtuA:
            raise HTTPException(400, "Pick at least one end")
        spec = body.ribbons.strip()
        if spec and spec.lower() not in ("all", "*"):
            if not re.fullmatch(r"\d{1,2}(-\d{1,2})?(,\s*\d{1,2}(-\d{1,2})?)*", spec):
                raise HTTPException(400, "Ribbons like 1-2 or 3,5,8-11")
            nums = [int(x) for x in re.findall(r"\d+", spec)]
            if any(not 1 <= x <= 36 for x in nums):
                raise HTTPException(400, "Ribbons are 1 to 36")
        if body.testType not in ("auto", "iOLM", "OTDR"):
            raise HTTPException(400, "Test type is auto, iOLM or OTDR")
        for d in (body.dfrom, body.dto):
            if d and not re.fullmatch(r"\d{4}-\d\d-\d\d", d):
                raise HTTPException(400, "Dates as YYYY-MM-DD")
        if not 0 <= body.nominal <= 1:
            raise HTTPException(400, "Splice nominal is 0 to 1 dB")
        if not re.fullmatch(r"\d{4}(,\d{4})*", body.viewWl or "1550"):
            raise HTTPException(400, "Wavelengths like 1550 or 1310,1550")
        _tidy()
        user = (sessions.get(x_session) or {}).get("user", "")
        jid = uuid.uuid4().hex[:12]
        folder = os.path.join(WORK, jid)
        os.makedirs(folder, exist_ok=True)
        j = {"id": jid, "state": "queued", "stem": stem, "rtuA": body.rtuA.strip().upper(), "rtuB": body.rtuB.strip().upper(),
             "ribbons": spec if spec.lower() not in ("all", "*") else "", "testType": body.testType, "dfrom": body.dfrom,
             "dto": body.dto, "nominal": body.nominal, "viewWl": body.viewWl or "1550", "user": user,
             "label": body.label.strip()[:120], "taskId": body.taskId.strip()[:40], "started": time.time(), "ended": None,
             "dir": folder, "totals": {}, "done": {}, "tail": [], "note": "Waiting for the report before it", "error": "",
             "fileName": "", "logName": "", "size": 0, "saved": False, "session": x_session, "clientName": ""}
        if body.client:
            raw = base64.b64decode(body.client)
            if len(raw) > MAX_CLIENT:
                raise HTTPException(413, "The customer report is over 15 MB")
            j["clientPath"] = os.path.join(folder, "client.xlsx")
            with open(j["clientPath"], "wb") as fh:
                fh.write(raw)
            j["clientName"] = body.clientName[:120] or "customer report.xlsx"
        loop = asyncio.get_event_loop()

        def token_fn():
            return asyncio.run_coroutine_threadsafe(valid_token(j["session"]), loop).result(30)
        RUNNERS[jid] = token_fn
        JOBS[jid] = j
        with LOCK:
            QUEUE.append(jid)
        _next()
        return public(j)

    @router.post("/api/fullreport/status")
    async def status(body: IdIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        j = JOBS.get(body.id)
        if not j:
            raise HTTPException(404, "That report has gone from the relay; open it from Report history.")
        return public(j)

    @router.post("/api/fullreport/cancel")
    async def cancel(body: IdIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        require_desktop(sessions, x_session)
        j = JOBS.get(body.id)
        if not j:
            raise HTTPException(404, "No such report")
        if j["state"] == "queued":
            j["state"], j["note"], j["ended"] = "cancelled", "Cancelled", time.time()
            with LOCK:
                if body.id in QUEUE:
                    QUEUE.remove(body.id)
        elif j["state"] == "running":
            j["cancel"] = True
        return public(j)

    @router.post("/api/fullreport/list")
    async def lst(x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        require_desktop(sessions, x_session)
        live = [public(j) for j in sorted(JOBS.values(), key=lambda x: x["started"], reverse=True)]
        saved = await asyncio.to_thread(gh_get_json, "reports/index.json", [])
        ids = {x["id"] for x in live}
        return {"live": live, "saved": [x for x in saved if x.get("id") not in ids][:200], "repo": repo_on()}

    @router.post("/api/fullreport/get")
    async def get(body: IdIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        require_desktop(sessions, x_session)
        j = JOBS.get(body.id)
        want_log = body.kind == "log"
        if j and j.get("state") == "done":
            p = j.get("logPath") if want_log else j.get("path")
            if p and os.path.exists(p):
                return FileResponse(p, filename=os.path.basename(p),
                                    media_type="text/plain" if want_log else
                                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        idx = await asyncio.to_thread(gh_get_json, "reports/index.json", [])
        x = next((r for r in idx if r.get("id") == body.id), None)
        if not x:
            raise HTTPException(404, "That report was not found")
        name = x.get("logName") if want_log else x.get("fileName")
        if not name:
            raise HTTPException(404, "No run log was kept for that report")
        path = x["repoPath"].rsplit("/", 1)[0] + "/" + name
        data = await asyncio.to_thread(gh_get_bytes, path)
        if not data:
            raise HTTPException(404, "That report file was not found")
        return Response(data, media_type="text/plain" if want_log else
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @router.post("/api/fullreport/locations")
    async def locs(x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        require_desktop(sessions, x_session)
        idx = await asyncio.to_thread(gh_get_json, "config/locations/index.json", {})
        out = {}
        for k, p in SEED_LOC.items():
            if os.path.exists(p):
                out[k] = {"cable": k, "name": "Distances.xlsx (from the PC tool, NRS-304)", "by": "", "t": os.path.getmtime(p), "seed": True}
        out.update({k: {**v, "cable": k, "seed": False} for k, v in idx.items()})
        return {"items": list(out.values()), "repo": repo_on()}

    @router.post("/api/fullreport/locations/upload")
    async def loc_up(body: LocUp, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        require_desktop(sessions, x_session)
        key = cable_key(body.cable)
        if not re.fullmatch(r"F-[A-Z0-9]+-[A-Z0-9]+", key):
            raise HTTPException(400, "Pick a cable")
        raw = base64.b64decode(body.data)
        if len(raw) > MAX_CLIENT or raw[:2] != b"PK":
            raise HTTPException(400, "That is not an Excel .xlsx workbook")
        # check the puller can read it before keeping it
        tmp = os.path.join(tempfile.gettempdir(), f"loc-{uuid.uuid4().hex[:8]}.xlsx")
        with open(tmp, "wb") as fh:
            fh.write(raw)
        try:
            sys.path.insert(0, ENGINE)
            import fms_pull
            sheets = fms_pull.location_sheets(tmp)
            rows = sum(len(fms_pull.load_locations(tmp, sheet=s, verbose=False) or []) for s in sheets) if sheets else 0
        except Exception as e:              # noqa: BLE001
            raise HTTPException(400, f"The report tool could not read that workbook: {str(e)[:160]}")
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if not rows:
            raise HTTPException(400, "No locations found. The workbook needs the Distances layout: a header row with "
                                     "Location and Leg, and distance columns headed by ribbons, e.g. R3-17 Actual.")
        if not repo_on():
            raise HTTPException(409, "Report storage is off on the relay")
        ok = await asyncio.to_thread(gh_put_bytes, f"config/locations/{key}.xlsx", raw, f"locations {key}")
        if not ok:
            raise HTTPException(502, "Could not save the workbook")
        idx = await asyncio.to_thread(gh_get_json, "config/locations/index.json", {})
        idx[key] = {"name": body.name[:120], "by": (sessions.get(x_session) or {}).get("user", ""), "t": time.time(),
                    "sheets": sheets, "rows": rows}
        await asyncio.to_thread(gh_put_bytes, "config/locations/index.json", json.dumps(idx, indent=1).encode(), "locations index")
        return {"ok": True, "cable": key, "sheets": sheets, "rows": rows}

    @router.post("/api/fullreport/locations/get")
    async def loc_get(body: LocGet, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        require_desktop(sessions, x_session)
        key = cable_key(body.cable)
        data = await asyncio.to_thread(gh_get_bytes, f"config/locations/{key}.xlsx")
        if not data and os.path.exists(SEED_LOC.get(key, "") or "/x"):
            data = open(SEED_LOC[key], "rb").read()
        if not data:
            raise HTTPException(404, "No locations stored for that cable")
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{key} locations.xlsx"'})

    return router
