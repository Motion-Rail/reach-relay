"""
relay_bulk.py  -  v26: bulk tests, RTU Tasks and cancel, for the desktop app.

RTU TASKS  (POST /api/tasks)
  Every FMS bulk Task RUNNING now (from anyone: FMS screen, this app, scripts), grouped by
  RTU and ordered by start time, so a second Task on a busy RTU shows as queued with its
  position. Per Task: creator, comment, kind, settings, fibres done / testing / waiting and
  each fibre's state. Plus the recent finished Tasks and the Tasks cancelled through the relay.

CANCEL  (POST /api/tasks/cancel)
  Standard Conductor terminate, DELETE /workflow/{id}?reason=..., proven 5 Oct 2026.
  Finished fibres keep their results; the rest never run. FMS's Tasks page hides TERMINATED
  Tasks, so the relay records each cancel in memory and in the run log store.
  CANCEL_POLICY: "confirm" (default) = anyone may cancel, but cancelling someone else's Task
  needs confirmOwner = that Task's creator name (the app's confirm box sends it); "own" =
  only your own Tasks.

BULK START  (POST /api/bulk/plan, POST /api/bulk/start)
  Builds the same Task input the FMS screen sends (captured 3 and 5 Oct 2026):
    OTDR: one Task per wavelength (FMS fires an OTDR at one wavelength at a time).
          Payload.payLoad = thresholds, wavelength, duration, autoSettings, and pulse and
          range (m) when autoSettings is off.
    iOLM: one Task. TestConfigId (test limit), TestConfigName "iOLM test parameters",
          MeasurementType "Standard iOLM" | "FastOvwNode" (Fast F1) |
          "FastOvwNode;RequiredDynamicRange_dB=14" (RTU Connection), WavelengthsUsed list.
  /plan returns what would be sent and the time estimate without starting anything.
"""
from __future__ import annotations

import asyncio
import os
import re
import time

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import fms_continuity as fc
from relay_continuity import LOG_REPO, LOG_TOKEN, _push_log_sync
from relay_team import SEARCH, USER_NAMES, _claims, _owner, _wf_input

CANCEL_POLICY = os.getenv("CANCEL_POLICY", "confirm").strip().lower()
WAVELENGTHS = {1310: "0.00000131", 1550: "0.00000155", 1625: "0.000001625"}
PULSES_NS = [5, 10, 30, 50, 100, 275, 500, 1000, 2500, 5000, 10000, 20000]   # the FMS OTDR dialog list
IOLM_MODES = {"standard": "Standard iOLM", "fast": "FastOvwNode",
              "rtu": "FastOvwNode;RequiredDynamicRange_dB=14"}
IOLM_MODE_LABELS = {"standard": "Standard iOLM", "fast": "Fast F1 (27dB)", "rtu": "RTU Connection"}
OTDR_OVERHEAD_S, IOLM_PER_FIBRE_S = 17, 73     # measured 2 and 3 Oct 2026
DONE = ("COMPLETED", "FAILED", "TIMED_OUT", "CANCELED", "SKIPPED", "COMPLETED_WITH_ERRORS")

CANCELLED: list[dict] = []                      # cancels through this relay, newest first
_ROUTES: dict[str, dict] = {}                   # rtu name -> {"t": ts, "routes": [...]}
_TASKS = {"t": 0.0, "data": None}
_RTU_NAME: dict[str, str] = {}                 # RtuId -> RTU name


class OtdrSettings(BaseModel):
    wavelengths: list[int] = [1550]
    duration: int = 5
    auto: bool = True
    pulseNs: float | None = None
    rangeKm: float | None = None
    spliceLoss: float = 0.02
    reflectance: float = -72
    endOfFibre: float = 4


class IolmSettings(BaseModel):
    wavelengths: list[int] = [1550]
    testConfigId: int = 28898                   # iOLM Motion NRS-304
    mode: str = "standard"                      # standard | fast | rtu


class BulkIn(BaseModel):
    rtu: str                                    # RTU name, e.g. RTU2-RGAC2-1991133
    cable: str                                  # route stem, e.g. F-RGAC-SNBC-A-R432
    fibres: list[int]
    kind: str = "otdr"                          # otdr | iolm
    otdr: OtdrSettings = OtdrSettings()
    iolm: IolmSettings = IolmSettings()
    comment: str = ""


class TasksIn(BaseModel):
    rtu: str = ""                               # optional: only this RTU (name)
    recent: int = 10                            # finished Tasks to include


class CancelIn(BaseModel):
    id: str
    reason: str = ""
    confirmOwner: str = ""


def otdr_payload(s: OtdrSettings, wl: int) -> dict:
    p = {"spliceLossThreshold": s.spliceLoss, "reflectanceThreshold": s.reflectance,
         "endOfFiberThreshold": s.endOfFibre, "wavelength": WAVELENGTHS[wl],
         "duration": int(s.duration), "autoSettings": bool(s.auto)}
    if not s.auto:
        p["pulse"] = f"{float(s.pulseNs) * 1e-9:g}"
        p["range"] = int(round(float(s.rangeKm) * 1000))
    return p


def check_settings(b: BulkIn):
    if b.kind not in ("otdr", "iolm"):
        raise HTTPException(400, "kind must be otdr or iolm")
    if not b.fibres:
        raise HTTPException(400, "No fibres chosen")
    s = b.otdr if b.kind == "otdr" else b.iolm
    wls = sorted(set(s.wavelengths))
    if not wls or any(w not in WAVELENGTHS for w in wls):
        raise HTTPException(400, "Wavelengths must be from 1310, 1550, 1625")
    if b.kind == "otdr":
        if not 1 <= int(b.otdr.duration) <= 600:
            raise HTTPException(400, "Duration must be 1 to 600 s")
        if not b.otdr.auto:
            if b.otdr.pulseNs not in PULSES_NS:
                raise HTTPException(400, "Pulse must be one FMS offers: " + ", ".join(map(str, PULSES_NS)) + " ns")
            if not b.otdr.rangeKm or not 0.1 <= float(b.otdr.rangeKm) <= 400:
                raise HTTPException(400, "Range must be 0.1 to 400 km")
    elif b.iolm.mode not in IOLM_MODES:
        raise HTTPException(400, "iOLM mode must be standard, fast or rtu")
    return wls


def build_inputs(b: BulkIn, routes: list[dict], user: dict) -> list[dict]:
    """The Task input(s) exactly as the FMS screen builds them. OTDR: one per wavelength."""
    wls = check_settings(b)
    rtu_id = int(routes[0]["rtuId"])

    def task(sub_type: str, per_route, label: str) -> dict:
        subs, ins = [], {}
        for r in routes:
            ref = f"Rtu_{rtu_id}_OR_{int(r['id'])}"
            subs.append({"subWorkflowParam": {"name": "postAdhocTest_On_Input_OR"},
                         "type": "SUB_WORKFLOW", "taskReferenceName": ref})
            ins[ref] = {"RtuId": rtu_id, "OpticalRouteId": int(r["id"]), "OpticalRouteName": r["name"],
                        **per_route}
        comment = (b.comment.strip() + " · " if b.comment.strip() else "") + label
        return {"subworkflows": subs, "type": "BULK_TEST.OPTICAL_TEST", "subType": sub_type,
                "totalOrsCount": len(routes), "comment": comment[:200], "subworkflowInputs": ins, **user}

    if b.kind == "otdr":
        return [task("BULK_TEST.ADHOC_TEST_OTDR",
                     {"AdhocTestType": "OTDR", "Payload": {"validInput": True, "payLoad": otdr_payload(b.otdr, wl)}},
                     f"Reach Fibre Tester OTDR {wl} nm {b.otdr.duration} s")
                for wl in wls]
    return [task("BULK_TEST.ADHOC_TEST_IOLM",
                 {"TestConfigName": "iOLM test parameters", "AdhocTestType": "iOLM",
                  "TestConfigId": int(b.iolm.testConfigId), "MeasurementType": IOLM_MODES[b.iolm.mode],
                  "WavelengthsUsed": [float(WAVELENGTHS[w]) for w in wls]},
                 f"Reach Fibre Tester iOLM {IOLM_MODE_LABELS[b.iolm.mode]} {'/'.join(map(str, wls))}")]


def estimate_s(b: BulkIn) -> int:
    n = len(b.fibres)
    if b.kind == "otdr":
        return n * (int(b.otdr.duration) + OTDR_OVERHEAD_S) * len(set(b.otdr.wavelengths))
    return n * IOLM_PER_FIBRE_S


def make_router(valid_token, check_key, sessions: dict, relay_version: str) -> APIRouter:
    router = APIRouter()

    def fms_for(token: str) -> fc.Fms:
        return fc.Fms.from_token_provider(lambda: token)

    def me(x_session) -> dict:
        sess = sessions.get(x_session or "") or {}
        c = _claims(sess.get("access", ""))
        name = (str(c.get("given_name", "")) + " " + str(c.get("family_name", ""))).strip()
        return {"email": sess.get("user", ""), "name": name or _owner(sess.get("user", ""))}

    def routes_for(fms: fc.Fms, rtu: str) -> list[dict]:
        hit = _ROUTES.get(rtu)
        if hit and time.time() - hit["t"] < 600:
            return hit["routes"]
        routes = fms.routes_for_rtu(rtu)
        _ROUTES[rtu] = {"t": time.time(), "routes": routes}
        return routes

    def pick_routes(fms: fc.Fms, b: BulkIn) -> list[dict]:
        want = {fc.fibre_name(b.cable, int(n)): int(n) for n in b.fibres}
        found = [r for r in routes_for(fms, b.rtu) if r["name"] in want]
        found.sort(key=lambda r: r["name"])
        missing = sorted(set(want) - {r["name"] for r in found})
        if missing:
            raise HTTPException(400, f"Not on {b.rtu}: " + ", ".join(missing[:8]) + (" …" if len(missing) > 8 else ""))
        return found

    def rtu_name(fms: fc.Fms, rtu_id) -> str:
        k = str(rtu_id or "")
        if not k:
            return ""
        if k in _RTU_NAME:
            return _RTU_NAME[k]
        for hit in _ROUTES.values():
            for r in hit["routes"][:1]:
                _RTU_NAME[str(r.get("rtuId"))] = r.get("rtuName") or ""
        if _RTU_NAME.get(k):
            return _RTU_NAME[k]
        name = ""
        try:
            r = fms.get(fc.HOST + f"/api/topology/remotetestunits/{k}")
            if r.ok:
                j = r.json()
                name = j.get("name") or j.get("rtuName") or j.get("Name") or ""
        except Exception:                               # noqa: BLE001
            pass
        _RTU_NAME[k] = name or f"RTU {k}"
        return _RTU_NAME[k]

    def task_detail(fms: fc.Fms, wf: dict) -> dict:
        inp = _wf_input(wf)
        sub_type = str(inp.get("subType") or "")
        kind = "OTDR" if sub_type.endswith("OTDR") else "iOLM" if sub_type.endswith("IOLM") else sub_type
        order = [s.get("taskReferenceName") for s in (inp.get("subworkflows") or [])]
        ins = inp.get("subworkflowInputs") or {}
        first = ins.get(order[0], {}) if order else (next(iter(ins.values()), {}) if ins else {})
        subs = sorted((t for t in wf.get("tasks", []) if t.get("taskType") == "SUB_WORKFLOW"),
                      key=lambda t: t.get("seq") or t.get("startTime") or 0)
        fibres = []
        for i, ref in enumerate(order):
            t = subs[i] if i < len(subs) else None
            st = (t or {}).get("status") or "WAITING"
            fibres.append({"name": (ins.get(ref) or {}).get("OpticalRouteName", ref), "state": st})
        done = sum(1 for f in fibres if f["state"] in DONE)
        testing = sum(1 for f in fibres if f["state"] in ("IN_PROGRESS", "SCHEDULED"))
        n = int(inp.get("totalOrsCount") or len(order) or 0)
        pl = ((first.get("Payload") or {}).get("payLoad") or {})
        if kind == "OTDR":
            wl = {v: k for k, v in WAVELENGTHS.items()}.get(str(pl.get("wavelength")), pl.get("wavelength"))
            setting = f"{wl} nm, {pl.get('duration')} s, " + ("auto" if pl.get("autoSettings") else
                                                              f"pulse {pl.get('pulse')}, range {pl.get('range')} m")
        else:
            mode = {v: IOLM_MODE_LABELS[k] for k, v in IOLM_MODES.items()}.get(first.get("MeasurementType"),
                                                                              first.get("MeasurementType"))
            wls = "/".join(str(round(float(w) * 1e9)) for w in first.get("WavelengthsUsed") or [])
            setting = f"{mode}, {wls} nm, setup {first.get('TestConfigId')}"
        return {"id": wf.get("workflowId", ""), "status": wf.get("status"), "kind": kind, "setting": setting,
                "rtuId": str(first.get("RtuId") or ""), "rtuName": rtu_name(fms, first.get("RtuId")), "creator": inp.get("creatorName") or "",
                "owner": _owner(inp.get("creatorName") or inp.get("UserName") or ""),
                "comment": inp.get("comment") or "", "total": n, "done": done, "testing": testing,
                "waiting": max(0, n - done - testing), "fibres": fibres,
                "started": (wf.get("startTime") or 0) / 1000, "ended": (wf.get("endTime") or 0) / 1000 or None}

    def read_tasks(token: str, recent: int) -> dict:
        fms = fms_for(token)
        r = fms.get(SEARCH.format(size=25, status="RUNNING"))
        r.raise_for_status()
        j = r.json()
        running = []
        for s in j.get("results", j if isinstance(j, list) else []):
            if s.get("workflowId"):
                running.append(task_detail(fms, fms.workflow(s["workflowId"], tasks=True)))
        by_rtu: dict[str, list] = {}
        for t in sorted(running, key=lambda t: t["started"]):
            by_rtu.setdefault(t["rtuId"], []).append(t)
        for rtu_id, ts in by_rtu.items():
            for i, t in enumerate(ts):
                t["position"] = i + 1
                t["state"] = "testing" if t["testing"] else ("queued" if i else "starting")
        finished = []
        if recent:
            r2 = fms.get(SEARCH.format(size=int(recent), status="COMPLETED,FAILED"))
            if r2.ok:
                j2 = r2.json()
                for s in j2.get("results", j2 if isinstance(j2, list) else []):
                    finished.append({"id": s.get("workflowId"), "status": s.get("status"),
                                     "started": s.get("startTime"), "ended": s.get("endTime")})
        return {"running": running, "byRtu": by_rtu, "finished": finished}

    @router.post("/api/tasks")
    async def tasks(body: TasksIn, x_app_key: str | None = Header(default=None),
                    x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        token = await valid_token(x_session)
        data = await asyncio.to_thread(read_tasks, token, max(0, min(int(body.recent or 0), 30)))
        if body.rtu:
            rid = next((r.get("rtuId") for r in _ROUTES.get(body.rtu, {}).get("routes", [])[:1]), None)
            if rid is None:
                rows = await asyncio.to_thread(lambda: routes_for(fms_for(token), body.rtu))
                rid = rows[0]["rtuId"] if rows else ""
            data["running"] = [t for t in data["running"] if t["rtuId"] == str(rid)]
            data["byRtu"] = {k: v for k, v in data["byRtu"].items() if k == str(rid)}
        return {"ok": True, "me": me(x_session), "cancelPolicy": CANCEL_POLICY,
                "cancelled": CANCELLED[:20], **data}

    @router.post("/api/tasks/cancel")
    async def cancel(body: CancelIn, x_app_key: str | None = Header(default=None),
                     x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        token = await valid_token(x_session)
        who = me(x_session)
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", body.id or ""):
            raise HTTPException(400, "Not a Task id")

        def run():
            fms = fms_for(token)
            before = task_detail(fms, fms.workflow(body.id, tasks=True))
            if before["status"] != "RUNNING":
                raise HTTPException(409, f"Task is {before['status']}, nothing to cancel")
            mine = before["creator"].strip().lower() in (who["name"].lower(), who["email"].lower()) or \
                _owner(before["creator"]).lower() == _owner(who["email"]).lower()
            if not mine:
                if CANCEL_POLICY == "own":
                    raise HTTPException(403, f"Only {before['owner']} can cancel this Task")
                if body.confirmOwner.strip().lower() != before["creator"].strip().lower():
                    raise HTTPException(428, f"This Task belongs to {before['owner']}. Confirm to cancel it.")
            reason = (body.reason or f"Cancelled by {who['name']} from Reach Fibre Tester")[:200]
            r = fms.s.delete(f"{fc.WF_BASE}/workflow/{body.id}", params={"reason": reason},
                             headers=fms._auth(), timeout=fms.timeout)
            if not r.ok:
                raise HTTPException(502, f"FMS refused the cancel: {r.status_code} {r.text[:200]}")
            after = before
            for _ in range(5):
                time.sleep(1)
                after = task_detail(fms, fms.workflow(body.id, tasks=True))
                if after["status"] != "RUNNING":
                    break
            rec = {"kind": "cancel", "id": body.id, "by": who["name"], "byEmail": who["email"],
                   "owner": before["owner"], "taskKind": before["kind"], "setting": before["setting"],
                   "rtuId": before["rtuId"], "rtuName": before["rtuName"], "total": before["total"], "doneBefore": before["done"],
                   "fibres": after["fibres"], "statusAfter": after["status"], "reason": reason,
                   "started": before["started"], "cancelled": time.time(), "relayVersion": relay_version}
            CANCELLED.insert(0, rec)
            del CANCELLED[50:]
            saved = "off"
            if LOG_REPO and LOG_TOKEN:
                day = time.strftime("%Y-%m-%d", time.gmtime())
                path = f"runs/{day}/{day}_{time.strftime('%H%M', time.gmtime())}_CANCEL_{body.id[:8]}.json"
                _, saved = _push_log_sync(path, rec, None, f"cancel: {body.id[:8]}")
            return {**rec, "logSaved": saved}

        rec = await asyncio.to_thread(run)
        return {"ok": True, **rec}

    def plan(token: str, b: BulkIn, user: dict) -> dict:
        fms = fms_for(token)
        routes = pick_routes(fms, b)
        inputs = build_inputs(b, routes, user)
        busy = []
        try:
            r = fms.get(SEARCH.format(size=25, status="RUNNING"))
            if r.ok:
                j = r.json()
                for s in j.get("results", j if isinstance(j, list) else []):
                    if s.get("workflowId"):
                        t = task_detail(fms, fms.workflow(s["workflowId"], tasks=True))
                        if t["rtuId"] == str(routes[0]["rtuId"]):
                            busy.append({"id": t["id"], "owner": t["owner"], "kind": t["kind"],
                                         "done": t["done"], "total": t["total"]})
        except Exception:                               # noqa: BLE001
            pass
        return {"rtuId": routes[0]["rtuId"], "fibres": len(routes), "tasks": len(inputs),
                "estimateS": estimate_s(b), "busy": busy, "queuePosition": len(busy) + 1,
                "inputs": inputs}

    def user_fields(x_session) -> dict:
        sess = sessions.get(x_session or "") or {}
        return fc.Fms.from_token_provider(lambda: sess.get("access", "")).user_fields()

    @router.post("/api/bulk/plan")
    async def bulk_plan(body: BulkIn, x_app_key: str | None = Header(default=None),
                        x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        token = await valid_token(x_session)
        p = await asyncio.to_thread(plan, token, body, user_fields(x_session))
        for i in p["inputs"]:
            i["UserRoles"] = "[roles from your sign in]"
        return {"ok": True, **p}

    @router.post("/api/bulk/start")
    async def bulk_start(body: BulkIn, x_app_key: str | None = Header(default=None),
                         x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        token = await valid_token(x_session)

        def run():
            p = plan(token, body, user_fields(x_session))
            fms = fms_for(token)
            ids = []
            for inp in p["inputs"]:
                r = fms.post(fc.WF_BASE + "/workflow", json={"name": fc.WF_NAME, "input": inp})
                if not r.ok:
                    raise HTTPException(502, f"FMS refused the start: {r.status_code} {r.text[:200]}"
                                        + (f" (after starting {len(ids)})" if ids else ""))
                ids.append(r.text.strip().strip('"'))
            p.pop("inputs")
            return {**p, "ids": ids}

        res = await asyncio.to_thread(run)
        _TASKS["t"] = 0
        return {"ok": True, **res}

    @router.get("/api/bulk/options")
    async def options():
        return {"wavelengths": sorted(WAVELENGTHS), "pulsesNs": PULSES_NS,
                "iolmModes": IOLM_MODE_LABELS, "otdrOverheadS": OTDR_OVERHEAD_S, "iolmPerFibreS": IOLM_PER_FIBRE_S}

    return router
