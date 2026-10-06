"""
relay_team.py  -  v25: who is online, and the run history.

WHO IS ONLINE  (POST /api/presence)
  Every open app sends a heartbeat every 30 s with what it is doing (screen, cable, RTU).
  The reply lists:
    * people signed in to the app and seen in the last PRESENCE_TTL seconds,
    * E2E runs in progress on this relay,
    * FMS bulk Tasks RUNNING on FMS from anyone (FMS UI or another tool), so a clash on an
      RTU can be seen before it happens. Ad hoc single tests started in the FMS UI create
      no Task and cannot be seen; FMS answers those with "already scheduled".

RUN HISTORY  (POST /api/history, POST /api/history/add)
  Rows come from two places:
    * the relay's own run logs in LOG_REPO (runs/<day>/*.json): E2E runs (written since
      v20) and Uni-dir sessions (written by the app at FINISH since v28),
    * FMS bulk Tasks (iOLM / OTDR) from the FMS workflow search, newest first.
  Each row: type, owner, RTU, cable, scope, result, summary, start, end. The app filters by
  type, RTU, owner and result. File contents and finished FMS Tasks are cached, so only new
  items are fetched after the first load.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time

import requests
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import fms_continuity as fc
from relay_continuity import JOBS, LOG_REPO, LOG_TOKEN, _gh_get_sync, _gh_list_sync, _push_log_sync

PRESENCE_TTL = 120            # seconds since the last heartbeat before someone drops off
PRESENCE: dict[str, dict] = {}
# v29: live Uni-dir progress sent by the phone every few seconds while a tone session is open
UNI_LIVE: dict[str, dict] = {}            # session id -> latest progress
UNI_LIVE_TTL = 90
_FMS_RUNNING = {"t": 0.0, "rows": []}
_FILE_CACHE: dict[str, dict] = {}     # sha -> parsed row
_DIR_CACHE: dict[str, dict] = {}      # path -> {"t": ts, "items": [...]}
_WF_CACHE: dict[str, dict] = {}       # workflowId -> row (finished Tasks only)
_RTU_NAMES: dict[str, str] = {}
USER_NAMES: dict[str, str] = {}       # email (lower case) -> name from the FMS token, learnt at sign in
SEARCH = (fc.WF_BASE + "/workflow/search?start=0&size={size}&sort=startTime:DESC&freeText=*"
          "&query=workflowType%20IN%20(" + fc.WF_NAME + ")%20AND%20status%20IN%20({status})")


class PresenceIn(BaseModel):
    screen: str = ""          # mode | uni | e2e | history | login
    cable: str = ""
    rtu: str = ""
    detail: str = ""
    appVersion: str = ""


class UniLiveIn(BaseModel):
    id: str = ""
    cable: str = ""
    rtu: str = ""
    rtuId: str = ""
    fibre: int = 0            # 1 to 432, the fibre on screen
    toning: bool = False
    confirmed: int = 0
    dis: int = 0
    cross: int = 0
    inScope: int = 432
    location: str = ""
    started: float = 0
    ended: bool = False


class HistoryIn(BaseModel):
    days: int = 30
    fms: bool = True


class UniIn(BaseModel):
    id: str
    cable: str
    rtu: str = ""
    site: str = ""
    location: str = ""
    scope: str = ""
    inScope: int = 0
    confirmed: int = 0
    dis: list[str] = []
    cross: list[str] = []
    issues: int = 0
    started: float = 0
    ended: float = 0
    appVersion: str = ""


def _claims(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:                                   # noqa: BLE001
        return {}


def _display_name(sess: dict) -> str:
    c = _claims(sess.get("access", ""))
    name = (str(c.get("given_name", "")) + " " + str(c.get("family_name", ""))).strip()
    return name or sess.get("user", "") or "?"


def _node(rtu: str) -> str:
    m = re.match(r"^RTU\d*-([^-]+)-", str(rtu or ""), re.I)
    return m.group(1) if m else str(rtu or "")


def _cable(name: str) -> str:
    return re.sub(r"-F\d+$", "", str(name or ""), flags=re.I)


def _wf_input(wf: dict) -> dict:
    i = wf.get("input")
    if isinstance(i, str):
        try:
            i = json.loads(i)
        except Exception:                               # noqa: BLE001
            i = {}
    return i or {}


def _owner(x: str) -> str:
    """One spelling per person, whether the source holds an email or an FMS creator name."""
    x = str(x or "").strip()
    if x.lower() in USER_NAMES:
        return USER_NAMES[x.lower()]
    if "@" in x:
        x = re.sub(r"[._-]+", " ", x.split("@")[0])
    return " ".join(w[:1].upper() + w[1:].lower() for w in x.split())


def _ribbon_text(rs) -> str:
    rs = sorted({int(r) for r in rs or []})
    out, i = [], 0
    while i < len(rs):
        j = i
        while j + 1 < len(rs) and rs[j + 1] == rs[j] + 1:
            j += 1
        out.append(f"R{rs[i]}" if i == j else f"R{rs[i]}-{rs[j]}")
        i = j + 1
    return ", ".join(out)


def make_router(valid_token, check_key, sessions: dict, rtu_index: dict, relay_version: str) -> APIRouter:
    router = APIRouter()

    def fms_for(token: str) -> fc.Fms:
        return fc.Fms.from_token_provider(lambda: token)

    def rtu_name(fms: fc.Fms, rtu_id) -> str:
        k = str(rtu_id or "")
        if not k:
            return ""
        if k in rtu_index and rtu_index[k].get("rtuName"):
            return rtu_index[k]["rtuName"]
        if k in _RTU_NAMES:
            return _RTU_NAMES[k]
        name = f"RTU {k}"
        try:
            r = fms.get(fc.HOST + f"/api/topology/remotetestunits/{k}")
            if r.ok:
                j = r.json()
                name = j.get("name") or j.get("rtuName") or j.get("Name") or name
        except Exception:                               # noqa: BLE001
            pass
        _RTU_NAMES[k] = name
        return name

    def task_row(fms: fc.Fms, wf: dict) -> dict | None:
        inp = _wf_input(wf)
        sub = str(inp.get("subType") or "")
        if "ADHOC_TEST" not in sub:
            return None
        kind = "OTDR" if sub.endswith("OTDR") else "iOLM" if sub.endswith("IOLM") else sub.split(".")[-1]
        ins = list((inp.get("subworkflowInputs") or {}).values())
        first = ins[0] if ins else {}
        n = int(inp.get("totalOrsCount") or len(ins) or 0)
        out = wf.get("output") or {}
        if isinstance(out, str):
            try:
                out = json.loads(out)
            except Exception:                           # noqa: BLE001
                out = {}
        ok = failed = 0
        for v in out.values():
            if isinstance(v, str):
                try:
                    v = json.loads(v)
                except Exception:                       # noqa: BLE001
                    continue
            st = ((v or {}).get("result") or {}).get("status")
            if st == "COMPLETED":
                ok += 1
            elif st:
                failed += 1
        status = str(wf.get("status") or "")
        if status == "RUNNING":
            result = "Running"
        elif status == "TERMINATED":                # v27: cancelled (FMS's Tasks page hides these)
            result = "Cancelled"
        elif status in ("FAILED", "TIMED_OUT") and not ok:
            result = "Failed"
        elif failed:
            result = "Issues"
        else:
            result = "Pass"
        names = sorted(str(e.get("OpticalRouteName") or "") for e in ins)
        fibres = [m.group(1) for m in (re.search(r"F(\d+)$", x) for x in names) if m]
        nums = sorted(int(f) for f in fibres)
        whole = nums and nums == list(range(nums[0], nums[-1] + 1)) and nums[0] % 12 == 1 and len(nums) % 12 == 0
        if whole:                                   # whole ribbons read like the app: R3, R3-5
            a, b = (nums[0] - 1) // 12 + 1, nums[-1] // 12
            scope = f"R{a}" if a == b else f"R{a}-{b}"
        else:
            scope = (f"F{fibres[0]} to F{fibres[-1]}" if len(fibres) > 1 else f"F{fibres[0]}" if fibres else "") + f" ({n})"
        payload = ((first.get("Payload") or {}).get("payLoad") or {})
        setting = (f"{payload.get('duration')} s" if payload.get("duration") else "") + \
                  (", auto" if payload.get("autoSettings") else "") if kind == "OTDR" else \
                  str(first.get("TestConfigName") or "")
        return {"source": "fms", "id": wf.get("workflowId", ""), "type": "FMS " + kind,
                "owner": _owner(inp.get("creatorName") or inp.get("UserName") or ""),
                "rtu": rtu_name(fms, first.get("RtuId")), "rtuId": str(first.get("RtuId") or ""),
                "cable": re.sub(r"-R\d+$", "", _cable(names[0])) if names else "",
                "scope": scope, "result": result,
                "summary": (f"{n} fibres, still running" if status == "RUNNING" else
                            f"Cancelled after {ok} of {n}" if status == "TERMINATED" else
                            f"{ok} of {n} completed" + (f", {failed} failed" if failed else ""))
                           + (f". {setting}" if setting else "") + (f". {inp.get('comment')}" if inp.get("comment") else ""),
                "started": (wf.get("startTime") or 0) / 1000, "ended": (wf.get("endTime") or 0) / 1000 or None,
                "status": status}

    def fms_running(token: str) -> list[dict]:
        if time.time() - _FMS_RUNNING["t"] < 30:
            return _FMS_RUNNING["rows"]
        rows = []
        try:
            fms = fms_for(token)
            r = fms.get(SEARCH.format(size=20, status="RUNNING"))
            if r.ok:
                j = r.json()
                for s in j.get("results", j if isinstance(j, list) else []):
                    wid = s.get("workflowId")
                    if not wid:
                        continue
                    wf = fms.workflow(wid, tasks=True)
                    row = task_row(fms, wf)
                    if row:
                        done = sum(1 for t in wf.get("tasks", [])
                                   if t.get("taskType") == "SUB_WORKFLOW" and t.get("status") in ("COMPLETED", "FAILED"))
                        n = int(_wf_input(wf).get("totalOrsCount") or 0)
                        row["progress"] = f"{done} of {n}"
                        rows.append(row)
        except Exception as e:                          # noqa: BLE001
            rows = [{"error": "FMS Tasks could not be read: " + str(e)[:120]}]
        _FMS_RUNNING.update(t=time.time(), rows=rows)
        return rows

    def app_runs() -> list[dict]:
        out = []
        for j in JOBS.values():
            if j.get("state") not in ("running", "paused") or j.get("simulate"):
                continue
            eng = j.get("engine")
            done = len(getattr(eng, "results", {}) or {})
            out.append({"type": "E2E", "owner": _owner(j.get("user", "")), "cable": re.sub(r"-R\d+$", "", j.get("stem", "")),
                        "rtu": f"{_node(j.get('toneRtu'))} to {_node(j.get('testRtu'))}",
                        "rtus": [j.get("toneRtu"), j.get("testRtu")], "scope": _ribbon_text(j.get("ribbons")),
                        "progress": f"{done} of {j.get('targets')}", "state": j.get("state"),
                        "started": j.get("started")})
        return out

    @router.post("/api/presence")
    async def presence(body: PresenceIn, x_app_key: str | None = Header(default=None),
                       x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        sess = sessions.get(x_session or "")
        if not sess:
            raise HTTPException(401, "No session — sign in again")
        now = time.time()
        prev = PRESENCE.get(x_session, {})
        if sess.get("user") and _display_name(sess) != sess.get("user"):
            USER_NAMES[sess["user"].lower()] = _owner(_display_name(sess))
        PRESENCE[x_session] = {"user": sess.get("user", ""), "name": _display_name(sess),
                               "screen": body.screen[:20], "cable": body.cable[:60], "rtu": body.rtu[:60],
                               "detail": body.detail[:120], "app": body.appVersion[:10],
                               "seen": now, "since": prev.get("since", now)}
        for k in [k for k, v in PRESENCE.items() if now - v["seen"] > PRESENCE_TTL * 5]:
            PRESENCE.pop(k, None)
        latest: dict[str, dict] = {}
        for k, v in PRESENCE.items():
            if now - v["seen"] <= PRESENCE_TTL:
                u = v["user"].lower()
                if u not in latest or v["seen"] > latest[u]["seen"]:
                    latest[u] = {**v, "you": k == x_session or u == sess.get("user", "").lower()}
        people = sorted(latest.values(), key=lambda v: (not v["you"], v["name"].lower()))
        token = await valid_token(x_session)
        tasks = await asyncio.to_thread(fms_running, token)
        return {"ok": True, "now": now, "ttl": PRESENCE_TTL, "people": people,
                "runs": app_runs(), "fmsTasks": tasks, "relayVersion": relay_version}

    @router.post("/api/uni/live")
    async def uni_live(body: UniLiveIn, x_app_key: str | None = Header(default=None),
                       x_session: str | None = Header(default=None)):
        """v29: the phone reports its Uni-dir session (fibre, counts, toning) for the desktop Live screen."""
        check_key(x_app_key)
        sess = sessions.get(x_session or "")
        if not sess:
            raise HTTPException(401, "No session — sign in again")
        if body.ended:
            UNI_LIVE.pop(x_session, None)
            return {"ok": True}
        UNI_LIVE[x_session] = {**body.model_dump(), "user": sess.get("user", ""), "name": _display_name(sess),
                               "cable": re.sub(r"-R\d+$", "", body.cable)[:60], "location": body.location[:80],
                               "seen": time.time()}
        return {"ok": True}

    @router.post("/api/presence/leave")
    async def leave(x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        PRESENCE.pop(x_session or "", None)
        UNI_LIVE.pop(x_session or "", None)
        return {"ok": True}

    # ── history ──────────────────────────────────────────────────────────────
    def log_row(rep: dict, path: str) -> dict | None:
        if rep.get("kind") == "uni":
            dis, cross = rep.get("dis") or [], rep.get("cross") or []
            part = rep.get("confirmed", 0) + len(dis) + len(cross) < rep.get("inScope", 0)
            result = "Issues" if (dis or cross) else "Part done" if part else "Pass"
            summ = f"{rep.get('confirmed', 0)} confirmed, {len(dis)} DIS, {len(cross)} crossed of {rep.get('inScope', 0)}"
            if dis:
                summ += ". DIS " + ", ".join(dis[:10]) + ("…" if len(dis) > 10 else "")
            if cross:
                summ += ". Crossed " + ", ".join(cross[:10]) + ("…" if len(cross) > 10 else "")
            return {"source": "log", "id": rep.get("id", ""), "type": "Uni-dir", "owner": _owner(rep.get("name") or rep.get("user", "")), "ownerRaw": rep.get("user", ""),
                    "rtu": rep.get("rtu", ""), "cable": re.sub(r"-R\d+$", "", rep.get("cable", "")),
                    "scope": rep.get("scope", ""), "result": result, "summary": summ,
                    "started": rep.get("started") or None, "ended": rep.get("ended") or None, "path": path,
                    "location": rep.get("location", "")}
        if not rep.get("stem"):
            return None
        c = rep.get("counts") or {}
        state = rep.get("state", "")
        issues = c.get("cross", 0) + c.get("dis", 0) + c.get("unres", 0)
        result = {"running": "Running", "paused": "Running", "stopped": "Stopped", "error": "Failed",
                  "interrupted": "Stopped", "resumed": "Stopped"}.get(state, "Issues" if issues else "Pass")
        summ = (f"{c.get('straight', 0)} straight, {c.get('cross', 0)} crossed, {c.get('dis', 0)} DIS"
                + (f", {c.get('unres')} not found" if c.get("unres") else "")
                + f" of {rep.get('targets')}. {rep.get('tests', 0)} tests")
        crosses = [f"F{int(k):03d}→F{int(v.get('found') or 0):03d}" for k, v in (rep.get("results") or {}).items()
                   if v.get("state") == "cross"]
        if crosses:
            summ += ". Crossed " + ", ".join(crosses[:8]) + ("…" if len(crosses) > 8 else "")
        fb = rep.get("feedback") or {}
        if fb.get("verdict"):
            summ += f". Site check: {fb['verdict']}"
        via = sorted({t.get("via") for t in rep.get("testLog") or [] if t.get("via")})
        return {"source": "log", "id": rep.get("id", ""), "type": "E2E",
                "owner": _owner(rep.get("user") or (rep.get("settings") or {}).get("user", "")),
                "ownerRaw": rep.get("user") or (rep.get("settings") or {}).get("user", ""),
                "rtu": f"{_node(rep.get('toneRtu'))} to {_node(rep.get('testRtu'))}",
                "rtuFull": [rep.get("toneRtu"), rep.get("testRtu")],
                "cable": re.sub(r"-R\d+$", "", rep.get("stem", "")), "scope": _ribbon_text(rep.get("ribbons")),
                "result": result, "summary": summ, "started": rep.get("started"), "ended": rep.get("ended"),
                "path": path, "app": rep.get("appVersion", ""), "relay": rep.get("relayVersion", ""), "via": via}

    def list_dir(path: str, today: bool) -> list[dict]:
        c = _DIR_CACHE.get(path)
        if c and time.time() - c["t"] < (60 if today else 900):
            return c["items"]
        items = _gh_list_sync(path)
        _DIR_CACHE[path] = {"t": time.time(), "items": items}
        return items

    def read_file(item: dict) -> dict | None:
        sha = item.get("sha", "")
        if sha in _FILE_CACHE:
            return _FILE_CACHE[sha]
        rep, _ = _gh_get_sync(item["path"])
        row = log_row(rep, item["path"]) if rep else None
        if row and row["result"] != "Running":
            _FILE_CACHE[sha] = row
        return row

    def log_rows(days: int) -> tuple[list[dict], str]:
        if not (LOG_REPO and LOG_TOKEN):
            return [], "Run logs are off on the relay (LOG_REPO / LOG_TOKEN not set)."
        rows = []
        today = time.strftime("%Y-%m-%d", time.gmtime())
        dayset = [time.strftime("%Y-%m-%d", time.gmtime(time.time() - d * 86400)) for d in range(days)]
        top = {i.get("name") for i in list_dir("runs", True)}
        for day in dayset:
            if day not in top:
                continue
            for item in list_dir(f"runs/{day}", day == today):
                if item.get("name", "").endswith(".json"):
                    try:
                        row = read_file(item)
                    except Exception:                   # noqa: BLE001
                        row = None
                    if row:
                        rows.append(row)
        return rows, ""

    def _search_time(v) -> float:
        """Search startTime is ISO text on live FMS, epoch ms on some builds. 0 when unknown."""
        try:
            if isinstance(v, (int, float)):
                return v / 1000 if v > 1e11 else float(v)
            if isinstance(v, str) and v:
                if v.isdigit():
                    return _search_time(int(v))
                from datetime import datetime
                return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except Exception:                               # noqa: BLE001
            pass
        return 0.0

    def fms_rows(token: str, days: int) -> tuple[list[dict], str]:
        rows, cutoff = [], time.time() - days * 86400
        try:
            fms = fms_for(token)
            size = 20 if days <= 1 else 40 if days <= 7 else 60   # v28: smaller search for short periods
            r = fms.get(SEARCH.format(size=size, status="RUNNING,COMPLETED,FAILED,TERMINATED,TIMED_OUT"))
            r.raise_for_status()
            j = r.json()
            for s in j.get("results", j if isinstance(j, list) else []):
                wid = s.get("workflowId")
                if not wid:
                    continue
                st0 = _search_time(s.get("startTime"))
                if st0 and st0 < cutoff - 3600:          # v28: sorted newest first, so stop before old detail calls
                    break
                row = _WF_CACHE.get(wid)
                if not row:
                    wf = fms.workflow(wid)
                    row = task_row(fms, wf)
                    if not row:
                        continue
                    if row["status"] != "RUNNING":
                        _WF_CACHE[wid] = row
                if row["started"] and row["started"] < cutoff:
                    continue
                if row["type"] == "FMS OTDR" and "continuity" in row["summary"].lower() and row["scope"].endswith("(1)"):
                    continue                            # pre v24 continuity tests, one Task per fibre
                rows.append(row)
            return rows, ""
        except Exception as e:                          # noqa: BLE001
            return rows, "FMS Tasks could not be read: " + str(e)[:160]

    @router.post("/api/history")
    async def history(body: HistoryIn, x_app_key: str | None = Header(default=None),
                      x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        token = await valid_token(x_session)
        days = max(1, min(int(body.days or 30), 90))
        (logs, note1), (tasks, note2) = await asyncio.gather(
            asyncio.to_thread(log_rows, days),
            asyncio.to_thread(fms_rows, token, days) if body.fms else asyncio.sleep(0, result=([], "")))
        rows = sorted(logs + tasks, key=lambda r: r.get("started") or 0, reverse=True)
        rows = [{**r, "owner": USER_NAMES.get(str(r.get("ownerRaw") or "").lower(), r.get("owner", ""))} for r in rows]
        return {"ok": True, "rows": rows, "notes": [n for n in (note1, note2) if n], "days": days}

    @router.post("/api/history/add")
    async def history_add(body: UniIn, x_app_key: str | None = Header(default=None),
                          x_session: str | None = Header(default=None)):
        """v28 app: a Uni-dir session is logged when the tester presses FINISH. Same id = same file."""
        check_key(x_app_key)
        sess = sessions.get(x_session or "")
        if not sess:
            raise HTTPException(401, "No session — sign in again")
        if not (LOG_REPO and LOG_TOKEN):
            return {"ok": False, "logSaved": "off"}
        rid = re.sub(r"[^A-Za-z0-9]", "", body.id)[:16] or "x"
        start = body.started or time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime(start))
        path = f"runs/{day}/{day}_{time.strftime('%H%M', time.gmtime(start))}_{re.sub(r'[^A-Za-z0-9-]', '', body.cable)}_UNI_{rid}.json"
        rec = {"kind": "uni", **body.model_dump(), "user": sess.get("user", ""), "name": _display_name(sess),
               "relayVersion": relay_version, "saved": time.time()}
        old, sha = await asyncio.to_thread(_gh_get_sync, path)
        sha2, st = await asyncio.to_thread(_push_log_sync, path, rec, sha, f"uni-dir: {body.cable}")
        for k in [k for k in _DIR_CACHE if k.startswith("runs")]:
            _DIR_CACHE.pop(k, None)
        return {"ok": st == "saved", "logSaved": st, "path": path}

    return router
