"""Brunel 1.0 (relay): learned locations and the asset inventory per cable.

The learning itself is in learned_locations.py and runs inside each report (report_job.py). This module keeps each
cable's learned state in LOG_REPO at config/learned/<F-XXXX-YYYY>.json and serves the Locations screen:

POST /api/locations/list      {cables: [..]}                       -> per cable: mode, counts, build end
POST /api/locations/get       {cable}                              -> state, inventory, whether a schedule exists
POST /api/locations/name      {cable, id, name, type?, chainage?}  -> name an unnamed (or rename a named) location
POST /api/locations/notjoint  {cable, id, undo?}                   -> mark a cluster as not a joint (or restore it)
POST /api/locations/split     {cable, id, at: [m, m]}              -> two joints too close to tell apart
POST /api/locations/settings  {cable, mode?, minShare?, tol?, seed?} -> learned or schedule; share of fibres; window
POST /api/locations/inventory {cable}                              -> the asset inventory as an Excel workbook

A report's learning is merged into the stored state when it finishes (merge_run), so names given while a report
was running are never lost. One write at a time (LOCK).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import threading
import uuid

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

import learned_locations as ll
import relay_fullreport as fr

LOCK = threading.Lock()
MEMO: dict[str, dict] = {}            # cable -> last state read or written, used when the store is off


class CableIn(BaseModel):
    cable: str


class ListIn(BaseModel):
    cables: list[str] = []


class NameIn(BaseModel):
    cable: str
    id: str
    name: str
    type: str = ""
    chainage: float | None = None


class NjIn(BaseModel):
    cable: str
    id: str
    undo: bool = False


class SplitIn(BaseModel):
    cable: str
    id: str
    at: list[float]


class SetIn(BaseModel):
    cable: str
    mode: str | None = None
    minShare: float | None = None
    tol: float | None = None
    seed: bool = False


def key_of(cable: str) -> str:
    k = fr.cable_key(cable)
    if not re.fullmatch(r"F-[A-Z0-9]+-[A-Z0-9]+", k):
        raise HTTPException(400, "Pick a cable")
    return k


def path_of(key: str) -> str:
    return f"config/learned/{key}.json"


def has_schedule(key: str) -> bool:
    idx = fr.gh_get_json("config/locations/index.json", {}) if fr.repo_on() else {}
    return key in idx or os.path.exists(fr.SEED_LOC.get(key, "") or "/nonexistent")


def read(key: str) -> tuple[dict | None, bool]:
    """(state or None when there is none yet, read_ok). read_ok False never leads to a write."""
    if not fr.repo_on():
        return (MEMO.get(key), True)
    b = fr.gh_get_bytes(path_of(key))
    if b is None:
        # None is both "not found" and "could not read"; look at the folder listing to tell them apart
        try:
            r = fr.requests.get(f"{fr.LOG_API}/repos/{fr.LOG_REPO}/contents/{path_of(key)}", headers=fr._h(), timeout=30)
            if r.status_code == 404:
                return (None, True)
        except Exception:                   # noqa: BLE001
            pass
        return (None, False)
    try:
        return (ll.fix_state(json.loads(b.decode("utf-8")), key), True)
    except ValueError:
        return (None, False)


def write(key: str, st: dict) -> bool:
    MEMO[key] = st
    if not fr.repo_on():
        return True
    return fr.gh_put_bytes(path_of(key), json.dumps(st, indent=1).encode(), f"learned locations {key}")


def state_for(key: str) -> dict:
    """The cable's state, or a new one: learned when the cable has no Distances schedule, schedule when it has."""
    st, ok = read(key)
    if not ok:
        raise HTTPException(503, "Could not read the learned locations just now. Try again in a moment.")
    return st or ll.new_state(key, "schedule" if has_schedule(key) else "learned")


def mode_for(key: str) -> tuple[str, dict | None]:
    """Used when a report starts: ('learned', state) or ('schedule', None)."""
    st, ok = read(key)
    if not ok:
        st = MEMO.get(key)
        if st is None:
            return ("schedule", None) if has_schedule(key) else ("learned", ll.new_state(key))
    if st is None:
        return ("schedule", None) if has_schedule(key) else ("learned", ll.new_state(key))
    return (st["settings"].get("mode") or "learned", st if st["settings"].get("mode") != "schedule" else None)


def change(key: str, fn) -> dict:
    """Read the stored state fresh, change it, write it back, one change at a time."""
    with LOCK:
        st = state_for(key)
        res = fn(st)
        if not write(key, st):
            raise HTTPException(502, "Could not save the change. Try again in a moment.")
        return {"state": st, "result": res}


def merge_run(key: str, run_state_path: str) -> bool:
    """A finished report's learning into the stored state (names given meanwhile are kept)."""
    try:
        with open(run_state_path, encoding="utf-8") as fh:
            run = ll.fix_state(json.load(fh), key)
    except (OSError, ValueError):
        return False
    with LOCK:
        cur, ok = read(key)
        if not ok:
            return False
        merged = ll.merge(cur, run) if cur else run
        return write(key, merged)


def _seed_from_schedule(key: str, st: dict, by: str) -> int:
    """Start the learned list from the cable's Distances workbook."""
    folder = tempfile.mkdtemp(prefix="seed-")
    p = fr._locations_for(key + "-A", folder)
    if not p:
        return 0
    sys.path.insert(0, fr.ENGINE)
    import fms_pull
    nm, _ent = fms_pull.pick_sheet_for_cable(p, key, verbose=False)
    if not nm:
        sheets = fms_pull.location_sheets(p) or [None]
        nm = sheets[0]
    sched = fms_pull.load_locations(p, sheet=nm, verbose=False)
    return ll.seed(st, sched, by=by)


def summary(key: str, st: dict | None, sched: bool) -> dict:
    if not st:
        return {"cable": key, "mode": "schedule" if sched else "learned", "locations": 0, "unnamed": 0,
                "named": 0, "buildEnd": None, "schedule": sched, "runs": 0}
    act = [x for x in ll.active(st) if x["status"] != "notjoint"]
    return {"cable": key, "mode": st["settings"].get("mode"), "locations": len(act),
            "unnamed": sum(1 for x in act if x["status"] == "unnamed"), "named": sum(1 for x in act if x["status"] == "named"),
            "buildEnd": st.get("buildEnd"), "schedule": sched, "runs": len(st.get("runs") or []),
            "lastRun": (st.get("runs") or [{}])[-1].get("at")}


def full(key: str, st: dict) -> dict:
    return {"state": st, "inventory": ll.inventory(st), "schedule": has_schedule(key), "types": ll.TYPES,
            "repo": fr.repo_on()}


def inventory_xlsx(st: dict) -> bytes:
    sys.path.insert(0, fr.ENGINE)
    import openpyxl
    import bidir_report
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    bidir_report._add_asset_inventory(wb, ll.inventory(st), {"cable": st["cable"]})
    tmp = os.path.join(tempfile.gettempdir(), f"inv-{uuid.uuid4().hex[:8]}.xlsx")
    wb.save(tmp)
    try:
        return open(tmp, "rb").read()
    finally:
        os.remove(tmp)


def make_router(valid_token, check_key, sessions: dict) -> APIRouter:
    router = APIRouter()
    from relay_bulk import require_desktop

    async def gate(x_app_key, x_session) -> str:
        check_key(x_app_key)
        await valid_token(x_session)
        require_desktop(sessions, x_session)
        return (sessions.get(x_session) or {}).get("user", "")

    def bad(e: Exception):
        if isinstance(e, HTTPException):
            raise e
        if isinstance(e, KeyError):
            raise HTTPException(404, "That location is not on this cable any more. Refresh the list.")
        raise HTTPException(400, str(e)[:200])

    @router.post("/api/locations/list")
    async def lst(body: ListIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        await gate(x_app_key, x_session)
        idx = await asyncio.to_thread(fr.gh_get_json, "config/locations/index.json", {}) if fr.repo_on() else {}
        out = []
        for c in body.cables[:12]:
            k = key_of(c)
            st, _ok = await asyncio.to_thread(read, k)
            sched = k in idx or os.path.exists(fr.SEED_LOC.get(k, "") or "/nonexistent")
            out.append(summary(k, st, sched))
        return {"items": out, "repo": fr.repo_on()}

    @router.post("/api/locations/get")
    async def get(body: CableIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        await gate(x_app_key, x_session)
        k = key_of(body.cable)
        st = await asyncio.to_thread(state_for, k)
        return await asyncio.to_thread(full, k, st)

    @router.post("/api/locations/name")
    async def name(body: NameIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        by = await gate(x_app_key, x_session)
        k = key_of(body.cable)
        try:
            r = await asyncio.to_thread(change, k, lambda st: ll.name(st, body.id, body.name, body.type, body.chainage, by))
        except Exception as e:              # noqa: BLE001
            bad(e)
        return await asyncio.to_thread(full, k, r["state"])

    @router.post("/api/locations/notjoint")
    async def nj(body: NjIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        by = await gate(x_app_key, x_session)
        k = key_of(body.cable)
        try:
            r = await asyncio.to_thread(change, k, lambda st: ll.not_joint(st, body.id, by, body.undo))
        except Exception as e:              # noqa: BLE001
            bad(e)
        return await asyncio.to_thread(full, k, r["state"])

    @router.post("/api/locations/split")
    async def split(body: SplitIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        by = await gate(x_app_key, x_session)
        k = key_of(body.cable)
        try:
            r = await asyncio.to_thread(change, k, lambda st: ll.split(st, body.id, body.at, by))
        except Exception as e:              # noqa: BLE001
            bad(e)
        return await asyncio.to_thread(full, k, r["state"])

    @router.post("/api/locations/settings")
    async def settings(body: SetIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        by = await gate(x_app_key, x_session)
        k = key_of(body.cable)

        def fn(st):
            first_learned = body.mode == "learned" and st["settings"].get("mode") != "learned"
            ll.set_settings(st, by, mode=body.mode, minShare=body.minShare, tol=body.tol)
            if first_learned and body.seed and not st["locations"]:
                return _seed_from_schedule(k, st, by)
            return 0
        try:
            r = await asyncio.to_thread(change, k, fn)
        except Exception as e:              # noqa: BLE001
            bad(e)
        return await asyncio.to_thread(full, k, r["state"]) | {"seeded": r["result"]}

    @router.post("/api/locations/inventory")
    async def inv(body: CableIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        await gate(x_app_key, x_session)
        k = key_of(body.cable)
        st = await asyncio.to_thread(state_for, k)
        data = await asyncio.to_thread(inventory_xlsx, st)
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{k} asset inventory.xlsx"'})

    return router
