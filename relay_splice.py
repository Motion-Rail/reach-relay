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
POST /api/splice/odf      {stem, view?}                        ODF connector at each end (Brunel 1.1.1)
POST /api/splice/settings {stem, settings}                     save the cable's rules for everyone

`view` is this viewer's filters (pass limit, split at 0.10, one end rule, S-ODF limit, NR ribbons); they
change the colours and counts only for the person looking. The saved settings are the cable's defaults.

Storage: the raw store is saved to LOG_REPO splice/<section>/raw.tgz after each update and the cable's
settings to splice/<section>/settings.json. After a relay restart the store is rebuilt from raw.tgz with
no FMS calls. Working files are in the temp folder (SPLICE_WORK).
"""
from __future__ import annotations

import asyncio
import calendar
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

from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
import ci_pull  # noqa: E402
import ci_store  # noqa: E402

WORK = os.environ.get("SPLICE_WORK") or os.path.join(tempfile.gettempdir(), "rft-splice")
FMS_BASE = os.environ.get("FMS_HOST") or os.environ.get("EXFO_AUTH_BASE") or "https://raman.ems.exfo-fms.com"
_LEARNED = {}


def learned_for(cable: str):
    """Brunel 1.0's learned locations for a cable with no Distances schedule, so the Splice loss
    view names its joints the same way (cached a few minutes; None when there are none)."""
    try:
        import learned_locations as ll
        import relay_assets as ra
        key = ra.key_of(cable)
        hit = _LEARNED.get(key)
        if hit and time.time() - hit[0] < 300:
            return hit[1]
        st, ok = ra.read(key)
        out = None
        if ok and st:
            out = [{"name": ll.label(L), "distance_m": L["distance_m"], "tolerance_m": L.get("tolerance_m"),
                    "type": L.get("type") or ""} for L in ll.active(st)
                   if L.get("status") != "notjoint" and L.get("distance_m") is not None] or None
        if ok:
            _LEARNED[key] = (time.time(), out)
        return out
    except Exception:                       # noqa: BLE001
        return None


ci_store.LEARNED_FOR = learned_for


def _names_to_repo(section: str, names: list):
    """Brunel 1.1.3: joints named in the app are kept in the log repo, so a relay restart keeps them."""
    fr = _repo()
    if fr.repo_on():
        fr.gh_put_bytes(f"splice/{section}/names.json", json.dumps({"names": names}, indent=1).encode(),
                        f"Joint names for {section}")


NAMES_READY: set = set()
HIST_LOCK = threading.Lock()


def _keep_pull(section: str, ds: int) -> bool:
    """Brunel 1.1.3: every pull is kept in the log repo (splice/<section>/history/), so the Over time and
    Compare views have every earlier test even after a relay restart, when only the latest results of each
    fibre are rebuilt from raw.tgz."""
    fr = _repo()
    if not fr.repo_on():
        return False
    d = next((x for x in STORE.datasets(section) if x["id"] == ds), None)
    if not d:
        return False
    fname = f"{(d.get('t_to') or 'unknown')[:10]}_{(d.get('content_sig') or str(ds))[:10]}.json.gz"
    with HIST_LOCK:
        idx = fr.gh_get_json(f"splice/{section}/history/index.json", None) or []
        if any(x.get("file") == fname for x in idx):
            return True
        if not fr.gh_put_bytes(f"splice/{section}/history/{fname}", STORE.export_pull(section, ds),
                               f"Splice loss pull {d.get('label')} for {section}"):
            return False
        idx.append({"file": fname, "label": d.get("label"), "t_from": d.get("t_from"), "t_to": d.get("t_to"),
                    "sig": d.get("content_sig"), "kept": time.strftime("%Y-%m-%d %H:%M")})
        idx.sort(key=lambda x: (x.get("t_to") or "", x.get("kept") or ""))
        return fr.gh_put_bytes(f"splice/{section}/history/index.json", json.dumps(idx, indent=1).encode(),
                               f"Splice loss history index for {section}")


def _history_from_repo(section: str) -> int:
    """After a restart: the kept pulls back into the store, oldest first, before the latest is rebuilt."""
    fr = _repo()
    if not fr.repo_on():
        return 0
    idx = fr.gh_get_json(f"splice/{section}/history/index.json", None) or []
    n = 0
    for x in sorted(idx, key=lambda x: (x.get("t_to") or "", x.get("kept") or "")):
        f = str(x.get("file") or "")
        if not re.fullmatch(r"[0-9A-Za-z_\-]+\.json\.gz", f):
            continue
        b = fr.gh_get_bytes(f"splice/{section}/history/{f}")
        if not b:
            continue
        try:
            if STORE.import_pull(section, b):
                n += 1
        except Exception:                   # noqa: BLE001
            continue
    return n


def _names_from_repo(section: str):
    """After a restart the work folder is empty: bring the cable's names back before any pull is built."""
    if section in NAMES_READY:
        return
    path = os.path.join(STORE.folder(section), "names.json")
    if not os.path.exists(path):
        fr = _repo()
        got = fr.gh_get_json(f"splice/{section}/names.json", None) if fr.repo_on() else None
        if got and isinstance(got.get("names"), list):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"names": got["names"]}, fh, indent=1)
    NAMES_READY.add(section)


ci_store.NAMES_SAVED = _names_to_repo


def _snaps_to_repo(section: str, snaps: list):
    fr = _repo()
    if fr.repo_on():
        fr.gh_put_bytes(f"splice/{section}/snapshots.json", json.dumps({"snapshots": snaps}, indent=1).encode(),
                        f"Splice loss snapshots for {section}")


SNAPS_READY: set = set()


def _snaps_from_repo(section: str):
    if section in SNAPS_READY:
        return
    path = os.path.join(STORE.folder(section), "snapshots.json")
    if not os.path.exists(path):
        fr = _repo()
        got = fr.gh_get_json(f"splice/{section}/snapshots.json", None) if fr.repo_on() else None
        if got and isinstance(got.get("snapshots"), list):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"snapshots": got["snapshots"]}, fh, indent=1)
    SNAPS_READY.add(section)


ci_store.SNAPS_SAVED = _snaps_to_repo
STORE = ci_store.Store(WORK)
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
PREP: dict[str, dict] = {}            # section -> {"state": "running"|"done"|"error"|"none", "note"}
SETTINGS: dict[str, dict] = {}        # section -> saved settings (cache of LOG_REPO)
RULE_KEYS = ("one_end", "nominal", "threshold", "split", "sodf_limit", "nr_ribbons", "spur_gap", "group_tol", "odf_limit")
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


class TrackIn(ViewIn):
    ids: list[int] = []
    frozen: bool = True


class SnapIn(ViewIn):
    remove: bool = False
    note: str = ""
    checks: dict = {}


class NameIn(BaseModel):
    stem: str
    loc: str
    name: str = ""
    type: str = ""
    ds: int = 0


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
                      ("spur_gap", 50, 20000), ("group_tol", 5, 5000), ("odf_limit", 0.05, 5.0)):
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


LOSS_CACHE: dict[str, dict] = {}


def latest_loss(stem: str) -> dict:
    """Brunel 1.1.1: the latest stored iOLM of every fibre at each end (any age), for Cable View's loss and
    length when no recent FMS Task tested the cable. {fibre: {"a": {t, loss, len, verdict, src}, "b": ...}}.
    End A is the raw store's A (the cable's first RTU, as Cable View's end a)."""
    section = stem.strip().upper()[2:]
    folder = STORE.folder(section)
    stamp = 0.0
    for end in ("A", "B"):
        d = os.path.join(folder, "raw", end)
        if os.path.isdir(d):
            stamp = max(stamp, os.path.getmtime(d))
    if not stamp:
        return {}
    c = LOSS_CACHE.get(section)
    if c and c["stamp"] == stamp:
        return c["data"]
    out: dict[int, dict] = {}
    for end, k in (("A", "a"), ("B", "b")):
        for fno, rec in ci_pull.RawStore(folder, end).all().items():
            det = rec.get("det") or {}
            t = det.get("testtime")
            if not t:
                continue
            try:
                ts = calendar.timegm(time.strptime(t[:19], "%Y-%m-%dT%H:%M:%S"))     # FMS times are UTC
            except ValueError:
                continue
            loss = {str(w): det.get(f"w{w}") for w in (1310, 1550, 1625) if det.get(f"w{w}") is not None}
            out.setdefault(fno, {})[k] = {"t": ts, "loss": loss, "len": det.get("length"),
                                          "verdict": det.get("verdict") if det.get("verdict") in ("Pass", "Fail") else "",
                                          "src": "FMS result"}
    LOSS_CACHE[section] = {"stamp": stamp, "data": out}
    return out


def latest_ds(section: str) -> dict | None:
    if not os.path.exists(os.path.join(STORE.folder(section), "store.sqlite")):
        return None
    ds = STORE.datasets(section)
    return max(ds, key=lambda d: d["id"]) if ds else None


# ── jobs ──────────────────────────────────────────────────────────────────────────────────
def public(j: dict) -> dict:
    """A copy of the job for the page. The job thread keeps adding fibres while this is read, so the
    dicts are copied (and the copy retried if one changed under it) rather than handed out live."""
    for _ in range(5):
        try:
            ends = {e: {"label": v["label"], "n": v["n"], "fibres": dict(v["fibres"]), "dates": dict(v["dates"])}
                    for e, v in list(j["ends"].items())}
            break
        except RuntimeError:
            time.sleep(0.01)
    else:
        ends = {}
    return {k: j.get(k) for k in ("id", "kind", "state", "stem", "section", "ribbons", "user", "started", "ended",
                                  "phase", "error", "result", "saved")} | {"ends": ends, "log": list(j["log"])[-12:]}


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
        if not cfg["dist"]:
            cfg["learned"] = learned_for(j["stem"])
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
        try:                                # Brunel 1.1.3: keep every pull, so results can be compared over time
            d = latest_ds(j["section"])
            if d:
                _keep_pull(j["section"], d["id"])
        except Exception:                   # noqa: BLE001
            pass
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
    for fn in (_names_from_repo, _snaps_from_repo):
        try:
            fn(section)
        except Exception:                   # noqa: BLE001
            pass
    p = PREP.get(section)
    if p and p["state"] == "running":
        return {"state": "preparing"}       # kept pulls may be back already; the latest is still being rebuilt
    if latest_ds(section):
        return {"state": "ready"}
    if p and p["state"] in ("none", "error") and time.time() - p.get("t", 0) < 120:
        return {"state": p["state"], "note": p.get("note", "")}
    has_raw = os.path.isdir(os.path.join(STORE.folder(section), "raw")) or _restore_raw(section)
    try:
        kept = _history_from_repo(section)   # earlier pulls first, so the rebuilt latest lands on top
    except Exception:                       # noqa: BLE001
        kept = 0
    if not has_raw:
        PREP[section] = {"state": "none", "t": time.time()}
        return {"state": "ready"} if kept else {"state": "none"}
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
        snaps = {x["sig"]: x for x in STORE.snapshots(section)}
        return {"ok": True, "section": section, "ready": ready, "raw": raw, "job": job,
                "datasets": [{k: d.get(k) for k in ("id", "label", "t_from", "t_to", "n_fibres", "both_ends", "imported_at",
                                                    "schedule")} | {"baseline": bool((d.get("notes") or {}).get("baseline")),
                                                                    "snapshot": snaps.get(d.get("content_sig"))}
                             for d in ds],
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

    @router.post("/api/splice/odf")
    async def odf(body: ViewIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        await guard(x_app_key, x_session)
        section = section_of(body.stem)
        st = await asyncio.to_thread(settings_for, section, body.view)
        return await asyncio.to_thread(STORE.odf, section, _ds(section, body.ds), st)

    @router.post("/api/splice/trend")
    async def trend(body: ViewIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        """Brunel 1.1.3: fails at every joint in every kept pull, oldest first."""
        await guard(x_app_key, x_session)
        section = section_of(body.stem)
        _ds(section, 0)
        st = await asyncio.to_thread(settings_for, section, body.view)
        return await asyncio.to_thread(STORE.trend, section, st, body.group or None)

    @router.post("/api/splice/track")
    async def track(body: TrackIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        """Brunel 1.1.3: as many kept tests as wanted, every splice followed across them."""
        await guard(x_app_key, x_session)
        section = section_of(body.stem)
        _ds(section, 0)
        st = await asyncio.to_thread(settings_for, section, body.view)
        return await asyncio.to_thread(STORE.track, section, body.ids[:200], st, body.group or None, body.frozen)

    @router.post("/api/splice/track.xlsx")
    async def track_xlsx(body: TrackIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        await guard(x_app_key, x_session)
        section = section_of(body.stem)
        _ds(section, 0)
        st = await asyncio.to_thread(settings_for, section, body.view)
        t = await asyncio.to_thread(STORE.track, section, body.ids[:200], st, body.group or None, body.frozen)
        import ci_export
        data = await asyncio.to_thread(ci_export.track_book, t, body.stem.upper())
        fn = f"{body.stem.upper()} over time, {len(t['pulls'])} tests.xlsx"
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fn)}"})

    @router.post("/api/splice/review")
    async def review(body: ViewIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        """Brunel 1.1.3: what to check before a test becomes a snapshot (end ODFs, ODFs along the route,
        joints and spurs, criteria, line-up with the snapshot before)."""
        await guard(x_app_key, x_session)
        section = section_of(body.stem)
        await asyncio.to_thread(_snaps_from_repo, section)
        st = await asyncio.to_thread(settings_for, section, body.view)
        try:
            return await asyncio.to_thread(STORE.review, section, _ds(section, body.ds), st)
        except KeyError:
            raise HTTPException(404, "That test is not on the relay any more")

    @router.post("/api/splice/snapshot")
    async def snapshot(body: SnapIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        """Save a checked test as a snapshot with the criteria in force now (or take it off)."""
        await guard(x_app_key, x_session)
        section = section_of(body.stem)
        user = str((sessions.get(x_session or "") or {}).get("user", "")).strip().lower()
        await asyncio.to_thread(_snaps_from_repo, section)
        st = await asyncio.to_thread(settings_for, section, body.view)
        try:
            return await asyncio.to_thread(STORE.set_snapshot, section, _ds(section, body.ds), st, user, body.note,
                                           body.remove, {k: bool(v) for k, v in (body.checks or {}).items()})
        except KeyError:
            raise HTTPException(404, "That test is not on the relay any more")

    @router.post("/api/splice/lineup")
    async def lineup(body: CompareIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        await guard(x_app_key, x_session)
        section = section_of(body.stem)
        st = await asyncio.to_thread(settings_for, section, body.view)
        try:
            return await asyncio.to_thread(STORE.lineup, section, body.base, body.new, st)
        except KeyError:
            raise HTTPException(404, "One of those tests is not on the relay any more")

    @router.post("/api/splice/remedials")
    async def remedials(body: ViewIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        """Brunel 1.1.4: the remedial lists for one test, splice remedials and ODF remedials kept apart."""
        await guard(x_app_key, x_session)
        section = section_of(body.stem)
        st = await asyncio.to_thread(settings_for, section, body.view)
        try:
            return await asyncio.to_thread(STORE.remedials, section, _ds(section, body.ds), st, getattr(body, "group", None) or None)
        except KeyError:
            raise HTTPException(404, "That test is not on the relay any more")

    @router.post("/api/splice/remedials.xlsx")
    async def remedials_xlsx(body: ViewIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        await guard(x_app_key, x_session)
        section = section_of(body.stem)
        st = await asyncio.to_thread(settings_for, section, body.view)
        ds = _ds(section, body.ds)
        g = getattr(body, "group", None) or None
        try:
            rm = await asyncio.to_thread(STORE.remedials, section, ds, st, g)
            sm = await asyncio.to_thread(STORE.summary, section, ds, st, g)
        except KeyError:
            raise HTTPException(404, "That test is not on the relay any more")
        import ci_export
        data = await asyncio.to_thread(ci_export.remedial_book, rm, body.stem.upper(), sm)
        fn = f"{body.stem.upper()} remedials {rm['dataset'].get('label') or ds}.xlsx".replace("/", "-")
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fn)}"})

    @router.post("/api/splice/compare.xlsx")
    async def compare_xlsx(body: CompareIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        await guard(x_app_key, x_session)
        section = section_of(body.stem)
        st = await asyncio.to_thread(settings_for, section, body.view)
        try:
            c = await asyncio.to_thread(STORE.compare, section, body.base, body.new, st, body.group or None)
        except KeyError:
            raise HTTPException(404, "One of those pulls is not on the relay any more")
        import ci_export
        data = await asyncio.to_thread(ci_export.compare_book, c, body.stem.upper())
        fn = f"{body.stem.upper()} compare {c['base']['label']} to {c['new']['label']}.xlsx".replace("/", "-")
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fn)}"})

    @router.post("/api/splice/name")
    async def name(body: NameIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        """Name a joint found from the results (or one the schedule does not list). Saved for the cable and
        matched by place, so every later test, and the pulls already stored, take the name."""
        await guard(x_app_key, x_session)
        section = section_of(body.stem)
        user = str((sessions.get(x_session or "") or {}).get("user", "")).strip().lower()
        await asyncio.to_thread(_names_from_repo, section)
        try:
            res = await asyncio.to_thread(STORE.set_name, section, _ds(section, body.ds), body.loc[:120],
                                          body.name, body.type, user)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, **res, "types": ci_store.JOINT_TYPES}

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
