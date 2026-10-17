"""Brunel 1.1 (relay): the Splice loss view in Cable View, built on the Cable Insight engine.

The latest result of every fibre from both ends is kept per cable (the raw store). An update
checks each fibre with a light FMS query and only fetches what is new, so when a ribbon is tested
again its new figures replace the old ones and everything else stays. The analysis (pass/fail,
S-ODF limit, fibre groups, spurs, compare) is the same code as Cable Insight on the PC.

POST /api/splice/state    {stem}                               raw store per end, latest pull, job, settings
POST /api/splice/update   {stem, rtuA, rtuB, ribbons}          start an update (one at a time on the relay)
POST /api/splice/job      {id}                                 progress with every fibre's state
POST /api/splice/cable    {stem, view?, group?}                ribbon by location grid of the latest pull
POST /api/splice/location {stem, loc, ribbon?, view?}          every fibre at one location
POST /api/splice/ribbon   {stem, ribbon, view?}                one ribbon across the cable
POST /api/splice/compare  {stem, base, new, group?, view?}     solved, remaining, new between two pulls
POST /api/splice/settings {stem, settings}                     save the cable's rules for everyone

`view` is this viewer's filters (pass limit, split at 0.10, one end rule, S-ODF limit, NR ribbons); they
change the colours and counts only for the person looking. The saved settings are the cable's defaults.

Storage: the raw store is saved to LOG_REPO splice/<section>/raw.tgz after each update and the cable's
settings to splice/<section>/settings.json. After a relay restart the store is rebuilt from raw.tgz with
no FMS calls. Working files are in the temp folder (SPLICE_WORK).
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
import ci_pull  # noqa: E402
import ci_store  # noqa: E402

WORK = os.environ.get("SPLICE_WORK") or os.path.join(tempfile.gettempdir(), "rft-splice")
FMS_BASE = os.environ.get("FMS_HOST") or os.environ.get("EXFO_AUTH_BASE") or "https://raman.ems.exfo-fms.com"
STORE = ci_store.Store(WORK)
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
PREP: dict[str, dict] = {}            # section -> {"state": "running"|"done"|"error"|"none", "note"}
SETTINGS: dict[str, dict] = {}        # section -> saved settings (cache of LOG_REPO)
RULE_KEYS = ("one_end", "nominal", "threshold", "split", "sodf_limit", "nr_ribbons", "spur_gap", "group_tol")
JOB_SCRIPT = os.path.join(HERE, "splice_job.py")
STEM_RE = r"^F-[A-Z0-9]+-[A-Z0-9]+-[A-Z](-R\d+)?$"


class StemIn(BaseModel):
    stem: str


class UpdateIn(BaseModel):
    stem: str
    rtuA: str
    rtuB: str
    ribbons: str = ""
    testType: str = "iOLM"


class IdIn(BaseModel):
    id: str


class ViewIn(BaseModel):
    stem: str
    view: dict = {}
    group: str = ""
    ds: int = 0


class LocIn(ViewIn):
    loc: str
    ribbon: int = 0


class RibbonIn(ViewIn):
    ribbon: int


class CompareIn(ViewIn):
    base: int
    new: int


class SettingsIn(BaseModel):
    stem: str
    settings: dict


def section_of(stem: str) -> str:
    s = stem.strip().upper()
    if not re.match(STEM_RE, s):
        raise HTTPException(400, "Not a cable name, for example F-RGAC-SNBC-A-R432")
    return s[2:]


def clean(new: dict, base: dict) -> dict:
    st = {k: base.get(k, ci_store.DEFAULT_SETTINGS[k]) for k in RULE_KEYS}
    if new.get("one_end") in ("nominal", "auto", "exclude"):
        st["one_end"] = new["one_end"]
    for k, lo, hi in (("nominal", 0.0, 1.0), ("threshold", 0.01, 2.0), ("sodf_limit", 0.01, 3.0),
                      ("spur_gap", 50, 20000), ("group_tol", 5, 5000)):
        if k in new:
            try:
                v = float(new[k])
                if lo <= v <= hi:
                    st[k] = round(v, 3)
            except (TypeError, ValueError):
                pass
    if "split" in new:
        st["split"] = bool(new["split"])
    if "nr_ribbons" in new:
        st["nr_ribbons"] = re.sub(r"[^0-9,\-]", "", str(new["nr_ribbons"]))[:40]
    return st


# ── LOG_REPO ──────────────────────────────────────────────────────────────────────────────
def _repo():
    import relay_fullreport as fr
    return fr


def saved_settings(section: str) -> dict:
    if section not in SETTINGS:
        fr = _repo()
        got = fr.gh_get_json(f"splice/{section}/settings.json", None) if fr.repo_on() else None
        SETTINGS[section] = clean(got or {}, dict(ci_store.DEFAULT_SETTINGS))
    return dict(SETTINGS[section])


def settings_for(section: str, view: dict | None) -> dict:
    return clean(view or {}, saved_settings(section))


def _raw_tgz(section: str) -> bytes:
    """The current result of every fibre at both ends (not the results archive)."""
    buf = io.BytesIO()
    folder = STORE.folder(section)
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for end in ("A", "B"):
            d = os.path.join(folder, "raw", end)
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if name.startswith("F") and name.endswith(".json"):
                    tf.add(os.path.join(d, name), arcname=f"{end}/{name}")
    return buf.getvalue()


def _save_raw(section: str) -> bool:
    fr = _repo()
    if not fr.repo_on():
        return False
    return fr.gh_put_bytes(f"splice/{section}/raw.tgz", _raw_tgz(section), f"Splice loss: {section} latest results")


def _restore_raw(section: str) -> bool:
    fr = _repo()
    if not fr.repo_on():
        return False
    b = fr.gh_get_bytes(f"splice/{section}/raw.tgz")
    if not b:
        return False
    folder = STORE.folder(section)
    with tarfile.open(fileobj=io.BytesIO(b), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile() or not re.fullmatch(r"[AB]/F\d{3}\.json", m.name):
                continue                                # only our own files, never a path outside the folder
            dest = os.path.join(folder, "raw", m.name)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(tf.extractfile(m).read())
    return True


def latest_ds(section: str) -> dict | None:
    if not os.path.exists(os.path.join(STORE.folder(section), "store.sqlite")):
        return None
    ds = STORE.datasets(section)
    return max(ds, key=lambda d: d["id"]) if ds else None


# ── jobs ──────────────────────────────────────────────────────────────────────────────────
def public(j: dict) -> dict:
    return {k: j.get(k) for k in ("id", "kind", "state", "stem", "section", "ribbons", "user", "started", "ended",
                                  "phase", "error", "result", "saved")} | {
        "ends": {e: {"label": v["label"], "n": v["n"], "fibres": v["fibres"], "dates": v["dates"]}
                 for e, v in j["ends"].items()}, "log": j["log"][-12:]}


def _dist_for(stem: str, folder: str) -> str:
    try:
        return _repo()._locations_for(stem, folder) or ""
    except Exception:                       # noqa: BLE001
        return ""


def _run(j: dict, token_fn):
    folder = os.path.join(WORK, "_jobs", j["id"])
    os.makedirs(folder, exist_ok=True)
    try:
        j["state"], j["phase"] = "running", "Getting ready"
        cfg = {"mode": j["kind"], "section": j["section"], "rtuA": j.get("rtuA", ""), "rtuB": j.get("rtuB", ""),
               "ribbons": j.get("ribbons", ""), "testType": j.get("testType", "iOLM"), "data": WORK,
               "dist": _dist_for(j["stem"], folder), "user": j.get("user", ""), "fmsBase": FMS_BASE}
        env = {**os.environ, "SPLICE_JOB": json.dumps(cfg), "PYTHONUNBUFFERED": "1", "MALLOC_ARENA_MAX": "2"}
        p = subprocess.Popen([sys.executable, JOB_SCRIPT], cwd=HERE, env=env, text=True,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
        res = None
        for line in p.stdout:
            line = line.rstrip("\n")
            if line == "NEED_TOKEN":
                try:
                    tok = token_fn() if token_fn else "ERROR no sign in"
                except Exception as e:      # noqa: BLE001
                    tok = "ERROR " + str(e)[:80]
                p.stdin.write(tok + "\n")
                p.stdin.flush()
            elif line.startswith("SETEND|"):
                _, end, label, n = line.split("|", 3)
                j["ends"][end] = {"label": label, "n": int(n), "fibres": {}, "dates": {}}
            elif line.startswith("MARK|"):
                _, end, fno, state, date = (line.split("|", 4) + [""])[:5]
                e = j["ends"].get(end)
                if e is not None:
                    e["fibres"][fno] = state
                    if date:
                        e["dates"][fno] = date
            elif line.startswith("PHASE|"):
                j["phase"] = line[6:]
            elif line.startswith("DONE|"):
                res = json.loads(line[5:])
            elif line.startswith("ERROR|"):
                j["error"] = line[6:]
            elif line.startswith("LOG|"):
                j["log"].append(line[4:200])
                del j["log"][:-60]
            elif line.strip():
                j["log"].append(line.strip()[:200])
                del j["log"][:-60]
        p.wait()
        if p.returncode != 0 or res is None:
            j["state"] = "error"
            j["error"] = j.get("error") or "The update did not finish. " + " / ".join(j["log"][-3:])
            return
        j["result"], j["state"], j["phase"] = res, "done", "Done"
        if j["kind"] == "pull":
            j["saved"] = _save_raw(j["section"])
    except Exception as e:                  # noqa: BLE001
        j["state"], j["error"] = "error", str(e)[:300]
    finally:
        j["ended"] = time.time()
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
        if j["kind"] == "rebuild":
            PREP[j["section"]] = {"state": "done" if j["state"] == "done" else "error",
                                  "note": j.get("error") or ""}


def start_job(kind: str, stem: str, section: str, user: str = "", token_fn=None, **kw) -> dict:
    with LOCK:
        busy = next((x for x in JOBS.values() if x["state"] in ("queued", "running")), None)
        if busy:
            if busy["section"] == section:
                return busy
            raise HTTPException(409, f"An update of {busy['stem']} is running; try again when it finishes.")
        jid = uuid.uuid4().hex[:12]
        j = {"id": jid, "kind": kind, "state": "queued", "stem": stem, "section": section, "user": user,
             "started": time.time(), "ended": None, "phase": "Queued", "error": "", "result": None, "saved": False,
             "ends": {}, "log": [], **kw}
        JOBS[jid] = j
        for k in [k for k, v in JOBS.items() if v.get("ended") and time.time() - v["ended"] > 6 * 3600]:
            JOBS.pop(k, None)
    threading.Thread(target=_run, args=(j, token_fn), daemon=True).start()
    return j


def ensure_ready(stem: str, section: str) -> dict:
    """The store has this cable, or a rebuild from the saved raw store is on its way."""
    if latest_ds(section):
        return {"state": "ready"}
    p = PREP.get(section)
    if p and p["state"] == "running":
        return {"state": "preparing"}
    if p and p["state"] in ("none", "error") and time.time() - p.get("t", 0) < 120:
        return {"state": p["state"], "note": p.get("note", "")}
    if not os.path.isdir(os.path.join(STORE.folder(section), "raw")) and not _restore_raw(section):
        PREP[section] = {"state": "none", "t": time.time()}
        return {"state": "none"}
    PREP[section] = {"state": "running", "t": time.time()}
    try:
        start_job("rebuild", stem, section)
    except HTTPException:
        PREP[section] = {"state": "none", "t": time.time(), "note": "waiting for another update"}
        return {"state": "none", "note": "waiting for another update"}
    return {"state": "preparing"}


def make_router(valid_token, check_key, sessions: dict) -> APIRouter:
    router = APIRouter()
    from relay_bulk import require_desktop

    async def guard(x_app_key, x_session):
        check_key(x_app_key)
        await valid_token(x_session)
        require_desktop(sessions, x_session)

    @router.post("/api/splice/state")
    async def state(body: StemIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        await guard(x_app_key, x_session)
        section = section_of(body.stem)
        ready = await asyncio.to_thread(ensure_ready, body.stem.upper(), section)
        raw = await asyncio.to_thread(ci_pull.raw_status, STORE, section)
        job = next((public(j) for j in sorted(JOBS.values(), key=lambda x: -x["started"])
                    if j["section"] == section and j["kind"] == "pull"), None)
        ds = STORE.datasets(section) if latest_ds(section) else []
        return {"ok": True, "section": section, "ready": ready, "raw": raw, "job": job,
                "datasets": [{k: d.get(k) for k in ("id", "label", "t_from", "t_to", "n_fibres", "both_ends", "imported_at",
                                                    "schedule")} for d in ds],
                "settings": await asyncio.to_thread(saved_settings, section)}

    @router.post("/api/splice/update")
    async def update(body: UpdateIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        await guard(x_app_key, x_session)
        section = section_of(body.stem)
        for r in (body.rtuA, body.rtuB):
            if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{2,60}", (r or "").strip().upper()):
                raise HTTPException(400, f"Not an RTU name: {r}")
        spec = body.ribbons.strip()
        if spec and not re.fullmatch(r"\d{1,2}(-\d{1,2})?(,\s*\d{1,2}(-\d{1,2})?)*", spec):
            raise HTTPException(400, "Ribbons like 7 or 3-6 or 1,4,9")
        if body.testType not in ("iOLM", "auto", "OTDR"):
            raise HTTPException(400, "Test type is iOLM, auto or OTDR")
        if not latest_ds(section):
            await asyncio.to_thread(ensure_ready, body.stem.upper(), section)
            if PREP.get(section, {}).get("state") == "running":
                raise HTTPException(409, "Getting the saved results ready; try again in a minute.")
        loop = asyncio.get_event_loop()

        def token_fn():
            return asyncio.run_coroutine_threadsafe(valid_token(x_session), loop).result(30)
        user = (sessions.get(x_session) or {}).get("user", "")
        j = start_job("pull", body.stem.upper(), section, user, token_fn, rtuA=body.rtuA.strip().upper(),
                      rtuB=body.rtuB.strip().upper(), ribbons=spec, testType=body.testType)
        return public(j)

    @router.post("/api/splice/job")
    async def job(body: IdIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        await guard(x_app_key, x_session)
        j = JOBS.get(body.id)
        if not j:
            raise HTTPException(404, "That update has gone from the relay")
        return public(j)

    def _ds(section: str, want: int) -> int:
        d = latest_ds(section)
        if not d:
            raise HTTPException(409, "No results for this cable yet. Press Update.")
        return want or d["id"]

    @router.post("/api/splice/cable")
    async def cable(body: ViewIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        await guard(x_app_key, x_session)
        section = section_of(body.stem)
        st = await asyncio.to_thread(settings_for, section, body.view)
        return await asyncio.to_thread(STORE.summary, section, _ds(section, body.ds), st, body.group or None)

    @router.post("/api/splice/location")
    async def location(body: LocIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        await guard(x_app_key, x_session)
        section = section_of(body.stem)
        st = await asyncio.to_thread(settings_for, section, body.view)
        return await asyncio.to_thread(STORE.location, section, _ds(section, body.ds), body.loc[:120],
                                       body.ribbon or None, st)

    @router.post("/api/splice/ribbon")
    async def ribbon(body: RibbonIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        await guard(x_app_key, x_session)
        section = section_of(body.stem)
        if not 1 <= body.ribbon <= 36:
            raise HTTPException(400, "Ribbons are 1 to 36")
        st = await asyncio.to_thread(settings_for, section, body.view)
        return await asyncio.to_thread(STORE.ribbon, section, _ds(section, body.ds), body.ribbon, st)

    @router.post("/api/splice/compare")
    async def compare(body: CompareIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        await guard(x_app_key, x_session)
        section = section_of(body.stem)
        _ds(section, 0)
        st = await asyncio.to_thread(settings_for, section, body.view)
        try:
            return await asyncio.to_thread(STORE.compare, section, body.base, body.new, st, body.group or None)
        except KeyError:
            raise HTTPException(404, "One of those pulls is not on the relay any more")

    @router.post("/api/splice/settings")
    async def settings(body: SettingsIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        await guard(x_app_key, x_session)
        section = section_of(body.stem)
        st = clean(body.settings or {}, await asyncio.to_thread(saved_settings, section))
        SETTINGS[section] = st
        fr = _repo()
        ok = await asyncio.to_thread(fr.gh_put_bytes, f"splice/{section}/settings.json",
                                     json.dumps(st, indent=1).encode(), f"Splice loss settings for {section}") \
            if fr.repo_on() else False
        return {"ok": True, "settings": st, "saved": ok}

    return router
