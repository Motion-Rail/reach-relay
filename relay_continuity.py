"""
relay_continuity.py  -  RTU to RTU continuity, run on the relay (v12).

Method, proven on RGAC2 -> SNBC on 28 Sep 2026: tone fibre n at one end, order an
AdHoc OTDR on fibre n at the other. The far RTU refuses with 112_LiveFiberDetected
when the light is on that fibre (PASS). A completed OTDR means the tone is elsewhere.

The sweep runs here as a background job, not in the browser, so a phone can lock
or a laptop can close and the run carries on. Any signed-in device can watch it.
Jobs live in memory: a relay restart ends them (results so far are in the last
status the app saved).

Routes (all POST, X-App-Key + X-Session as for /api/tone):
  /api/continuity/start    {toneRtu, testRtu, stem, ribbons[], toneS?, otdrS?, leadS?,
                            wavelengthNm?, freqHz?, simulate?}      -> {jobId}
  /api/continuity/status   {jobId}                                    -> job snapshot
  /api/continuity/control  {jobId, action: pause|resume|stop}
  /api/continuity/jobs     {}                                         -> recent jobs
  /api/continuity/test     one tone + one OTDR, for checks by hand
  /api/otdr                one OTDR on one fibre
  /api/continuity/pace     measured timings

Safety: fibres in CONTINUITY_EXCLUDE (route names or ribbon ranges, e.g. R35-36)
are never toned or tested. Live runs refuse to start when LIVE_TONE is off, because
every fibre would read "not found".
"""
from __future__ import annotations

import asyncio
import os
import re
import time
import uuid

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel

import base64
import json

import requests

import break_locator as bl
import continuity_engine as ce
from fms_continuity import Fms, StartRefused

PACE = {                              # measured on RGAC2 -> SNBC, 28 Sep 2026
    "clash_s": 10,                    # live fibre refused: the PASS
    "clean_s": 35,                    # dark fibre, full OTDR runs
    "first_test_s": 53,
    "default_otdr_s": 3,
    # v15, measured on R1/R2 28 Sep 2026: straight cycle 12.8 s at tone 6 or 10 (lead 2);
    # the tone only adds time above ~11 s. Tone 10 gives more cover for slow FMS starts.
    # Dark OTDR: 3 s -> 17.4-18.5 s; 5 s -> 17.5-18.5 s; 1 s -> 14-35 s (erratic).
    # Lead 1 s saved ~0.9 s a fibre but missed the first fibre once; kept at 2 s.
    "default_tone_s": 10,
    "default_lead_s": 2,
    "max_tone_s": 20,
    "cover_tone_s": 20,               # first test of a run, and a retry after a miss
    "slow_cycle_s": 25,               # a miss slower than this is an FMS delay, not a short tone
    "late_margin_s": 25,              # v20 fallback only, when FMS task times are missing
    "acq_margin_s": 1,
    "dis_otdr_s": 30,                 # v22: one long OTDR on each DIS fibre, for an accurate distance
    "outage_wait_s": 60,              # v22: FMS not answering: wait, then retry the same test
    "outage_limit_s": 7200,           #      give up (run fails, resumable) after 2 h                # v20: acquisition must start at least this long before the tone ends              # v18: a dark result later than tone start + tone + this is rechecked
}
RELAY_VERSION = "v30"   # kept in step with main.py
JOBS: dict[str, dict] = {}
LOCKS: dict[str, asyncio.Lock] = {}
ROUTES: dict[str, dict[str, dict]] = {}          # rtuName -> {routeName: node}


def _excluded() -> tuple[set, list]:
    names, ribbons = set(), []
    for tok in filter(None, (t.strip() for t in os.environ.get("CONTINUITY_EXCLUDE", "").split(","))):
        m = re.fullmatch(r"R(\d+)(?:-(\d+))?", tok, re.I)
        if m:
            a = int(m.group(1)); ribbons.append((a, int(m.group(2) or a)))
        else:
            names.add(tok.upper())
    return names, ribbons


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


def is_excluded(route_name: str) -> bool:
    names, ribbons = _excluded()
    if route_name.upper() in names:
        return True
    m = re.search(r"F(\d{3})$", route_name)
    if m:
        r = (int(m.group(1)) - 1) // 12 + 1
        return any(a <= r <= b for a, b in ribbons)
    return False


class OtdrReq(BaseModel):
    rtuName: str
    fibre: str
    durationS: int = PACE["default_otdr_s"]
    rangeM: int = 80000
    comment: str = "continuity"


class TestReq(BaseModel):
    toneRtu: str
    testRtu: str
    source: int
    candidate: int
    stem: str = "F-RGAC-SNBC-A-R432"
    toneS: int = PACE["default_tone_s"]
    wavelengthNm: int = 1550
    freqHz: int = 0
    durationS: int = PACE["default_otdr_s"]


class StartReq(BaseModel):
    toneRtu: str
    testRtu: str
    stem: str
    ribbons: list[int]
    toneRtuId: str | None = None
    toneS: int = PACE["default_tone_s"]
    otdrS: int = PACE["default_otdr_s"]
    leadS: float = PACE["default_lead_s"]
    autoPace: bool = True             # lengthen the tone if the expected fibre misses
    wavelengthNm: int = 1550
    freqHz: int = 0
    simulate: str | None = None       # testing only; not offered in the app since v14
    appVersion: str = ""
    prior: dict[str, dict] | None = None   # v17 resume: {"12": {"state": "straight", "found": 12}}
    user: str = ""
    resumeOf: str | None = None


class FmsDown(Exception):
    """v22: FMS itself is not answering (5xx, hung workflows). Not a fibre result."""


# v22: last known state of FMS, from real tone / OTDR calls. Shown on /health and in the app.
FMS_STATUS = {"ok": True, "t": 0.0, "detail": "", "since": 0.0}


def fms_ok():
    if not FMS_STATUS["ok"]:
        FMS_STATUS.update(ok=True, detail="", since=0.0)
    FMS_STATUS["t"] = time.time()


def fms_bad(detail: str):
    if FMS_STATUS["ok"]:
        FMS_STATUS["since"] = time.time()
    FMS_STATUS.update(ok=False, t=time.time(), detail=detail[:200])


def _is_outage(text: str) -> bool:
    return bool(re.search(r"\b50[0234]\b|Service Temporarily Unavailable|Bad Gateway|Gateway Time|timed? ?out|"
                          r"Connection (?:reset|refused|aborted)|RemoteDisconnected|Max retries", str(text), re.I))


def _num(v):
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


class FeedbackReq(BaseModel):
    jobId: str
    verdict: str                      # correct | partly | wrong
    notes: str = ""
    user: str = ""
    appVersion: str = ""


# v20: every finished run (and any feedback on it) is written to a private GitHub repo, so runs
# can be reviewed and the search tuned. Off unless both are set on the relay.
LOG_REPO = os.environ.get("LOG_REPO", "")            # e.g. Motion-Rail/relay-logs
LOG_TOKEN = os.environ.get("LOG_TOKEN", "")          # fine grained token, contents read/write on LOG_REPO only
LOG_API = os.environ.get("LOG_API", "https://api.github.com").rstrip("/")


def _push_log_sync(path: str, body: dict, sha: str | None, message: str) -> tuple[str | None, str]:
    url = f"{LOG_API}/repos/{LOG_REPO}/contents/{path}"
    h = {"Authorization": "Bearer " + LOG_TOKEN, "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    data = {"message": message,
            "content": base64.b64encode(json.dumps(body, indent=1, default=str).encode()).decode()}
    if sha:
        data["sha"] = sha
    r = requests.put(url, headers=h, json=data, timeout=30)
    if r.status_code == 409 or (r.status_code == 422 and not sha):     # stale or unknown sha: fetch and retry once
        g = requests.get(url, headers=h, timeout=30)
        if g.ok:
            data["sha"] = g.json().get("sha")
            r = requests.put(url, headers=h, json=data, timeout=30)
    if r.ok:
        return r.json().get("content", {}).get("sha"), "saved"
    return sha, f"{r.status_code} {r.text[:160]}"


def _gh_headers() -> dict:
    return {"Authorization": "Bearer " + LOG_TOKEN, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _gh_list_sync(path: str) -> list[dict]:
    r = requests.get(f"{LOG_API}/repos/{LOG_REPO}/contents/{path}", headers=_gh_headers(), timeout=30)
    return r.json() if r.ok and isinstance(r.json(), list) else []


def _gh_get_sync(path: str) -> tuple[dict | None, str | None]:
    r = requests.get(f"{LOG_API}/repos/{LOG_REPO}/contents/{path}", headers=_gh_headers(), timeout=30)
    if not r.ok:
        return None, None
    j = r.json()
    try:
        return json.loads(base64.b64decode(j.get("content", ""))), j.get("sha")
    except Exception:                                   # noqa: BLE001
        return None, j.get("sha")


# v21: a short message to a Teams channel when a run finishes. TEAMS_WEBHOOK is the URL of a
# Teams Workflows "post to a channel when a webhook request is received" flow. Off when unset.
TEAMS_WEBHOOK = os.environ.get("TEAMS_WEBHOOK", "")
APP_URL = os.environ.get("APP_URL", "https://motionrail.onrender.com")


def _teams_sync(title: str, lines: list[str]) -> str:
    body = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True}]
    body += [{"type": "TextBlock", "text": t, "wrap": True, "spacing": "Small"} for t in lines]
    card = {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
                    "version": "1.4", "body": body,
                    "actions": [{"type": "Action.OpenUrl", "title": "Open Reach Fibre Tester", "url": APP_URL}]}}]}
    r = requests.post(TEAMS_WEBHOOK, json=card, timeout=20)
    return "sent" if r.status_code < 300 else f"{r.status_code} {r.text[:120]}"


class JobReq(BaseModel):
    jobId: str
    action: str | None = None


def make_router(valid_token, check_key, tone, live_tone: bool) -> APIRouter:
    """valid_token: async (session_id) -> bearer   (main._valid_token)
       check_key:   (x_app_key) -> None raises 401 (main._check_key)
       tone:        async (session_id, fibre, rtuId, wl, dur, hz) -> dict  (wraps main.tone)
    """
    router = APIRouter()

    async def fms_for(sid: str) -> Fms:
        tok = await valid_token(sid)
        box = {"t": tok, "at": time.time()}
        loop = asyncio.get_running_loop()

        def fetch():
            # v17: FMS calls run in worker threads, so they can ask the event loop for a
            # current token. Before v17 the token was only renewed between tests, and a long
            # OTDR poll ran past its expiry (401 on the workflow status, run killed at F126).
            try:
                asyncio.get_running_loop()
                return                              # on the loop thread: keep the cached one
            except RuntimeError:
                pass
            try:
                box["t"] = asyncio.run_coroutine_threadsafe(valid_token(sid), loop).result(30)
                box["at"] = time.time()
            except Exception:                       # noqa: BLE001
                pass

        def provider():
            if time.time() - box["at"] > 20:
                fetch()
            return box["t"]

        f = Fms.from_token_provider(provider)
        f._force = fetch

        async def refresh():                      # called by the job between tests
            box["t"] = await valid_token(sid)
            box["at"] = time.time()
        f._refresh = refresh
        return f

    async def routes(fms: Fms, rtu: str) -> dict[str, dict]:
        if rtu not in ROUTES:
            nodes = await asyncio.to_thread(fms.routes_for_rtu, rtu)
            if not nodes:
                raise HTTPException(404, f"No routes on {rtu}")
            ROUTES[rtu] = {n["name"].upper(): n for n in nodes}
        return ROUTES[rtu]

    async def run_otdr(fms: Fms, rtu: str, name: str, dur: int, comment: str) -> dict:
        if is_excluded(name):
            raise HTTPException(423, f"{name} is excluded from continuity testing")
        node = (await routes(fms, rtu)).get(name.upper())
        if not node:
            raise HTTPException(404, f"{name} not on {rtu}")
        async with LOCKS.setdefault(rtu, asyncio.Lock()):
            t0 = time.time()
            try:
                posted = time.time()
                wid = await asyncio.to_thread(fms.start_otdr, node["rtuId"], node["id"], name,
                                              duration=dur, range_m=80000, comment=comment)
            except StartRefused as e:
                return {"verdict": "start_refused", "detail": str(e), "seconds": round(time.time() - t0, 1)}
            via = "adhoc" if wid in getattr(fms, "_adhoc", {}) else "workflow"
            out = await asyncio.to_thread(fms.wait, wid, posted=posted)
        return {"verdict": out.verdict, "detail": out.detail[:600], "seconds": round(out.seconds, 1),
                "acqStart": (out.raw or {}).get("acqStart"), "timeline": (out.raw or {}).get("timeline", []),
                "posted": posted, "lenM": _num((out.raw or {}).get("linkLength")),
                "workflowId": wid, "via": via}

    # ---------------- single calls ----------------
    @router.post("/api/continuity/pace")
    async def pace(x_app_key: str | None = Header(default=None)):
        check_key(x_app_key)
        return {**PACE, "live_tone": live_tone}

    @router.post("/api/otdr")
    async def otdr(req: OtdrReq, x_app_key: str | None = Header(default=None),
                   x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        fms = await fms_for(x_session)
        return await run_otdr(fms, req.rtuName, req.fibre, req.durationS, req.comment)

    @router.post("/api/continuity/test")
    async def cont_test(req: TestReq, x_app_key: str | None = Header(default=None),
                        x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        fms = await fms_for(x_session)
        src = f"{req.stem}-F{req.source:03d}"
        cand = f"{req.stem}-F{req.candidate:03d}"
        if is_excluded(src):
            raise HTTPException(423, f"{src} is excluded from continuity testing")
        tix = await routes(fms, req.toneRtu)
        if src.upper() not in tix:
            raise HTTPException(404, f"{src} not on {req.toneRtu}")
        t = await tone(x_session, src, str(tix[src.upper()]["rtuId"]), req.wavelengthNm, req.toneS, req.freqHz)
        await asyncio.sleep(PACE["default_lead_s"])
        res = await run_otdr(fms, req.testRtu, cand, req.durationS, f"continuity {src[-4:]} to {cand[-4:]}")
        return {**res, "source": req.source, "candidate": req.candidate, "tone": t}

    # ---------------- background sweep ----------------
    @router.post("/api/continuity/start")
    async def start(req: StartReq, x_app_key: str | None = Header(default=None),
                    x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        for j in JOBS.values():
            if j["state"] in ("running", "paused") and not j["simulate"] and not req.simulate and \
                    {j["toneRtu"], j["testRtu"]} & {req.toneRtu, req.testRtu}:
                raise HTTPException(409, f"A continuity run is already using {j['toneRtu']} / {j['testRtu']} "
                                         f"(job {j['id'][:8]}). Stop it first.")
        if not req.simulate and not live_tone:
            raise HTTPException(409, "The relay has LIVE_TONE off, so no light would reach the fibre. "
                                     "Set LIVE_TONE=1 on the relay, or run a simulation.")
        targets = [f for f in ce.ribbons_to_targets(req.ribbons)
                   if not is_excluded(f"{req.stem}-{ce.fname(f)}")]
        if not targets:
            raise HTTPException(400, "No fibres to test (none selected, or all excluded)")

        fms = None
        if not req.simulate:                    # v24: check the session before anything is created
            fms = await fms_for(x_session)
            await routes(fms, req.toneRtu)
            await routes(fms, req.testRtu)
        old = JOBS.get(req.resumeOf or "")
        if old and old["state"] == "interrupted":
            old["state"] = "resumed"
            old["resumedAsPending"] = True
        eng = ce.Engine()
        carried_locs: dict[int, dict] = {}
        for k, v in (req.prior or {}).items():          # v17: resume carries finished fibres over
            try:
                f, st = int(k), v.get("state")
            except Exception:                           # noqa: BLE001
                continue
            if st in ("straight", "cross", "dis") and f in targets:
                found = int(v.get("found") or 0) if st == "dis" else int(v.get("found") or f)
                eng.results[f] = ce.Result(st, found, int(v.get("tests") or 0),
                                           (v.get("why") or "").replace(" (earlier run)", "") + " (earlier run)")
                if v.get("loc"):
                    carried_locs[f] = v["loc"]
                if found:
                    eng.used_far.add(found)
        jid = uuid.uuid4().hex
        job = {"id": jid, "state": "running", "simulate": req.simulate or "", "stem": req.stem,
               "toneRtu": req.toneRtu, "testRtu": req.testRtu, "ribbons": sorted(set(req.ribbons)),
               "targets": len(targets), "started": time.time(), "ended": None, "rtuSeconds": 0.0,
               "error": "", "settings": req.model_dump(exclude={"prior"}), "engine": eng, "truthNotes": [],
               "pauseEvt": asyncio.Event(), "testLog": [],
               "pace": {"toneS": req.toneS, "leadS": req.leadS, "auto": req.autoPace, "changes": []},
               "appVersion": req.appVersion, "resumeOf": req.resumeOf or "", "carried": len(eng.results),
               "locs": carried_locs, "user": req.user or ""}
        job["pauseEvt"].set()
        JOBS[jid] = job
        if old and old.pop("resumedAsPending", False):
            old["resumedAs"] = jid
            asyncio.create_task(save_log(old, "resumed"))

        async def control() -> bool:
            await job["pauseEvt"].wait()
            return job["state"] != "stopped"

        if req.simulate:
            truth, notes = ce.planted_truth(req.simulate)
            job["truthNotes"] = notes
            base = ce.sim_tester(truth, miss_rate=0.05, delay=0.25)

            async def test(src, cand, long=False):
                r = await base(src, cand, long=long)
                job["rtuSeconds"] += PACE["clash_s"] if r == "clash" else PACE["clean_s"]
                job["testLog"].append({"t": round(time.time(), 1), "src": src, "cand": cand, "verdict": r})
                return r
        else:
            tone_state = {"src": 0, "until": 0.0}
            missed = set()                                  # sources whose own fibre read clean once
            pace = job["pace"]

            async def measure_dis(f):
                """v22: one long OTDR on a DIS fibre for an accurate distance to its open end."""
                name = f"{req.stem}-{ce.fname(f)}"
                try:
                    eng.say(f"  {ce.fname(f)}: measuring where it stops ({PACE['dis_otdr_s']} s OTDR)")
                    res = await run_otdr(fms, req.testRtu, name, PACE["dis_otdr_s"], f"continuity DIS distance {ce.fname(f)}")
                    job["testLog"].append({"t": round(time.time(), 1), "src": f, "cand": f, "verdict": "dis-" + res["verdict"],
                                           "otdrS": res.get("seconds"), "lenM": res.get("lenM"),
                                           "workflow": res.get("workflowId", ""), "detail": res.get("detail", "")[:200]})
                    if res["verdict"] == "clean" and res.get("lenM"):
                        job.setdefault("disLen", {})[f] = res["lenM"]
                        fms_ok()
                except Exception as e:                      # noqa: BLE001
                    eng.say(f"  {ce.fname(f)}: distance measurement failed ({str(e)[:100]})")
            eng.after_dis = measure_dis

            async def test(src, cand, long=False):
                # v17: nothing inside one test may end the run. Any fault is logged as an
                # error and the engine retries, so a 401 or a network blip costs one test.
                # v22: when FMS itself is down the run waits and retries the same test, so an
                # outage never turns into dark readings and false DIS results.
                try:
                    while True:
                        try:
                            try:
                                r = await test_once(src, cand, long)
                            except (FmsDown, RuntimeError):
                                raise
                            except Exception as e2:     # noqa: BLE001
                                if _is_outage(getattr(e2, "detail", None) or str(e2)):
                                    raise FmsDown(str(getattr(e2, "detail", None) or e2)[:160])
                                raise
                            if job.get("fmsDown"):
                                eng.say(f"FMS answering again after {round((time.time() - job['fmsDown']['since']) / 60)} min; carrying on.")
                                job["fmsDown"] = None
                            return r
                        except FmsDown as e:
                            fms_bad(str(e))
                            if not job.get("fmsDown"):
                                job["fmsDown"] = {"since": time.time(), "reason": str(e)[:200], "retries": 0}
                                eng.say(f"FMS not responding ({str(e)[:100]}). Retrying in 15 s, then every "
                                        f"{PACE['outage_wait_s']} s; nothing is marked until it answers.")
                            job["fmsDown"]["retries"] += 1
                            job["fmsDown"]["reason"] = str(e)[:200]
                            if time.time() - job["fmsDown"]["since"] > PACE["outage_limit_s"]:
                                raise RuntimeError("FMS has not answered for 2 hours. Resume the run when it is back.")
                            for _ in range(15 if job["fmsDown"]["retries"] == 1 else PACE["outage_wait_s"]):
                                if job["state"] == "stopped":
                                    return "error"
                                await asyncio.sleep(1)
                            tone_state["until"] = 0
                except RuntimeError:
                    raise
                except Exception as e:                  # noqa: BLE001
                    msg = getattr(e, "detail", None) or str(e)
                    job["testLog"].append({"t": round(time.time(), 1), "src": src, "cand": cand,
                                           "verdict": "error", "detail": str(msg)[:300]})
                    job["errors"] = job.get("errors", 0) + 1
                    eng.say(f"  {ce.fname(cand)}: error, will retry: {str(msg)[:160]}")
                    await asyncio.sleep(5)
                    return "error"

            async def test_once(src, cand, long=False, recheck=False):
                await fms._refresh()
                src_name = f"{req.stem}-{ce.fname(src)}"
                cand_name = f"{req.stem}-{ce.fname(cand)}"
                t0 = time.time()
                tone_s, lead_s = pace["toneS"], pace["leadS"]
                # v15: the first test of a run and the retry of a fibre that missed get a long
                # tone. Field runs: every miss so far followed a slow FMS start (cycle 36-66 s),
                # so the extra cover goes where the risk is, not on every fibre.
                if long or not job["testLog"] or (cand == src and src in missed):
                    tone_s = max(tone_s, PACE["cover_tone_s"])
                need = max(1, min(13, tone_s - lead_s - 1))  # tone must still be on at the live check
                if tone_state["src"] != src or tone_state["until"] - time.time() < need:
                    wait = tone_state["until"] - time.time()
                    if wait > 0:                            # one source per RTU
                        await asyncio.sleep(wait + 0.5)
                    node = ROUTES[req.toneRtu].get(src_name.upper())
                    try:
                        t = await tone(x_session, src_name, str(node["rtuId"]),
                                       req.wavelengthNm, tone_s, req.freqHz, str(node["id"]))
                    except HTTPException as e:
                        if _is_outage(e.detail) or e.status_code >= 500:
                            raise FmsDown(f"tone refused by FMS: {str(e.detail)[:120]}")
                        eng.say(f"  tone failed on {ce.fname(src)}: {e.detail}")
                        await asyncio.sleep(5)
                        return "error"
                    if t.get("simulated"):
                        raise RuntimeError("Relay tone is simulated (LIVE_TONE=0)")
                    tone_state.update(src=src, until=time.time() + tone_s, start=time.time(), len=tone_s)
                    await asyncio.sleep(lead_s)
                res = await run_otdr(fms, req.testRtu, cand_name, req.otdrS,
                                     f"continuity {ce.fname(src)} to {ce.fname(cand)}")
                job["rtuSeconds"] += res.get("seconds", 0)
                v = res["verdict"]
                # v22: an OTDR that hangs, gives no output, or a 5xx on start is FMS failing, not a fibre result
                if v in ("timeout", "unknown") or (v == "start_refused" and _is_outage(res.get("detail", ""))):
                    job["testLog"].append({"t": round(t0, 1), "src": src, "cand": cand, "verdict": "fms-" + v,
                                           "otdrS": res.get("seconds"), "cycleS": round(time.time() - t0, 1),
                                           "workflow": res.get("workflowId", ""), "detail": res.get("detail", "")[:200]})
                    raise FmsDown(f"OTDR {v}: {res.get('detail', '')[:120]}")
                if v in ("clash", "clean"):
                    fms_ok()
                # v18: a dark reading only counts if the tone was still on when the OTDR started.
                # FMS sometimes starts late; then the tone has ended and "clean" proves nothing.
                # v20: judge by when FMS started the acquisition (from its task times), not by when
                # the result came back. A dark OTDR that is merely slow to finish is fine.
                tone_end = tone_state.get("start", t0) + tone_state.get("len", tone_s)
                acq = res.get("acqStart")
                if acq:
                    late = acq > tone_end - PACE["acq_margin_s"]
                else:
                    late = time.time() - tone_state.get("start", t0) > tone_state.get("len", tone_s) + PACE["late_margin_s"]
                if v == "clean" and late and not recheck:
                    job["testLog"].append({"t": round(t0, 1), "src": src, "cand": cand, "verdict": "late",
                                           "otdrS": res.get("seconds"), "cycleS": round(time.time() - t0, 1),
                                           "toneS": tone_s, "leadS": lead_s, "workflow": res.get("workflowId", ""),
                                           "detail": "dark after a late FMS start, rechecking with a long tone"})
                    job["lateRechecks"] = job.get("lateRechecks", 0) + 1
                    eng.say(f"  {ce.fname(src)} on {ce.fname(cand)}: dark but FMS started late "
                            f"({round(time.time() - t0)} s), rechecking with a {PACE['cover_tone_s']} s tone")
                    tone_state["until"] = 0                 # force a fresh tone
                    return await test_once(src, cand, True, True)
                job["testLog"].append({"t": round(t0, 1), "src": src, "cand": cand, "verdict": v,
                                       "otdrS": res.get("seconds"), "cycleS": round(time.time() - t0, 1),
                                       "toneS": tone_s, "leadS": lead_s, "workflow": res.get("workflowId", ""),
                                       "lenM": res.get("lenM"), "via": res.get("via"),
                                       "acqAfterToneS": (round(res["acqStart"] - tone_state.get("start", t0), 1)
                                                         if res.get("acqStart") else None),
                                       "timeline": res.get("timeline", [])[:8] if len(job["testLog"]) < 60 else [],
                                       "detail": ("" if v in ("clash", "clean") else res.get("detail", "")[:300])})
                del job["testLog"][:-5000]
                # Auto pacing: the expected fibre read clean, then passed on the retry. The tone
                # was probably too short, so give it 2 s more (and 1 s more lead) from now on.
                if cand == src and v == "clean":
                    missed.add(src)
                    slow_miss = (time.time() - t0) > PACE["slow_cycle_s"]
                    job.setdefault("missNotes", []).append({"fibre": src, "cycleS": round(time.time() - t0, 1), "slow": slow_miss})
                    if slow_miss:
                        eng.say(f"  {ce.fname(src)} read dark after a slow FMS start ({round(time.time() - t0)} s); "
                                f"retrying with a {PACE['cover_tone_s']} s tone, pace unchanged")
                elif cand == src and v == "clash" and src in missed and pace["auto"] \
                        and not job["missNotes"][-1]["slow"] \
                        and pace["toneS"] < PACE["max_tone_s"]:
                    pace["toneS"] = min(PACE["max_tone_s"], pace["toneS"] + 2)
                    pace["leadS"] = min(4, pace["leadS"] + 1)
                    pace["changes"].append({"t": round(time.time(), 1), "fibre": src,
                                            "toneS": pace["toneS"], "leadS": pace["leadS"]})
                    eng.say(f"  auto pace: {ce.fname(src)} passed only on the retry, tone now "
                            f"{pace['toneS']} s, lead {pace['leadS']} s")
                if v in ("clash", "clean"):
                    return v
                eng.say(f"  {ce.fname(cand)}: {v} {res.get('detail', '')[:160]}")
                if v == "start_refused":
                    await asyncio.sleep(15)                 # someone else is using the RTU
                return "error"

        async def runner():
            try:
                eng.say(f"Run started: {len(targets)} fibres, tone {req.toneRtu}, OTDR {req.testRtu}"
                        + (f", SIMULATION ({req.simulate})" if req.simulate else ""))
                done = await eng.run(targets, test, control)
                job["state"] = "done" if done else "stopped"
                eng.say("Run complete." if done else "Stopped.")
            except Exception as e:                          # noqa: BLE001
                job["state"] = "error"
                job["error"] = str(e)[:300]
                eng.say("Run failed: " + job["error"])
            finally:
                job["ended"] = time.time()
                await save_log(job, "run " + job["state"])
                await notify(job)

        job["task"] = asyncio.create_task(runner())
        job["ckpt"] = asyncio.create_task(checkpointer(job))
        return {"ok": True, "jobId": jid}

    def snap(job: dict, full: bool = True) -> dict:
        eng: ce.Engine = job["engine"]
        res = eng.results
        counts = {"straight": sum(r.state == "straight" for r in res.values()),
                  "cross": sum(r.state == "cross" for r in res.values()),
                  "dis": sum(r.state == "dis" for r in res.values()),
                  "unres": sum(r.state == "unres" for r in res.values())}
        out = {k: job[k] for k in ("id", "state", "simulate", "stem", "toneRtu", "testRtu", "ribbons",
                                   "targets", "started", "ended", "error", "truthNotes")}
        out.update(pace={k: job["pace"][k] for k in ("toneS", "leadS", "auto")},
                   appVersion=job.get("appVersion", ""), relayVersion=RELAY_VERSION)
        out.update(done=len(res), counts=counts, tests=eng.tests, rtuSeconds=round(job["rtuSeconds"]),
                   current=eng.current, candidate=eng.candidate, now=time.time(),
                   resumeOf=job.get("resumeOf", ""), carried=job.get("carried", 0),
                   testErrors=job.get("errors", 0), lateRechecks=job.get("lateRechecks", 0),
                   logSaved=job.get("logSaved", ""), feedback=job.get("feedback"),
                   notified=job.get("notified", ""), restored=bool(job.get("restored")),
                   resumedAs=job.get("resumedAs", ""), fmsDown=job.get("fmsDown"),
                   fms={k: FMS_STATUS[k] for k in ("ok", "since", "detail")})
        if full:
            out.update(eng.snapshot())
            out["settings"] = job["settings"]
            for k, r in out.get("results", {}).items():         # v21: where each DIS fibre stops
                if r.get("state") == "dis":
                    loc = dis_location(job, int(k))
                    if loc:
                        r["loc"] = loc
        return out

    def dis_location(job: dict, f: int) -> dict | None:
        if f in job.get("locs", {}):
            return job["locs"][f]
        if f in job.get("disLen", {}):                       # v22: the long OTDR reading
            loc = bl.locate(job["stem"], job["testRtu"], f, job["disLen"][f])
            loc["method"] = f"{PACE['dis_otdr_s']} s OTDR"
            return loc
        lens = sorted(t["lenM"] for t in job["testLog"]
                      if t.get("src") == f and t.get("cand") == f and t.get("verdict") == "clean" and t.get("lenM"))
        if not lens:
            return None
        loc = bl.locate(job["stem"], job["testRtu"], f, lens[len(lens) // 2], approx=True)
        loc["readings"] = len(lens)
        loc["method"] = "3 s OTDR"
        return loc

    @router.post("/api/continuity/status")
    async def status(req: JobReq, x_app_key: str | None = Header(default=None),
                     x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        job = JOBS.get(req.jobId)
        if not job:
            raise HTTPException(404, "No such run on the relay (it may have restarted)")
        return snap(job)

    @router.post("/api/continuity/control")
    async def control_ep(req: JobReq, x_app_key: str | None = Header(default=None),
                         x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        job = JOBS.get(req.jobId)
        if not job:
            raise HTTPException(404, "No such run")
        if job["state"] not in ("running", "paused"):
            return snap(job, False)
        if req.action == "pause":
            job["state"] = "paused"; job["pauseEvt"].clear()
        elif req.action == "resume":
            job["state"] = "running"; job["pauseEvt"].set()
        elif req.action == "stop":
            job["state"] = "stopped"; job["pauseEvt"].set()
        else:
            raise HTTPException(400, "action must be pause, resume or stop")
        return snap(job, False)

    def report(job: dict) -> dict:
        """Everything needed to diagnose a run: every test with timings, pace changes, full log."""
        eng: ce.Engine = job["engine"]
        out = snap(job)
        out["log"] = eng.log[-400:]
        out["testLog"] = job["testLog"]
        out["paceChanges"] = job["pace"]["changes"]
        cyc = [t["cycleS"] for t in job["testLog"] if t.get("cycleS")]
        out["timing"] = {"tests": len(job["testLog"]),
                         "avgCycleS": round(sum(cyc) / len(cyc), 1) if cyc else None,
                         "clashAvgS": _avg([t["otdrS"] for t in job["testLog"] if t["verdict"] == "clash" and t.get("otdrS")]),
                         "cleanAvgS": _avg([t["otdrS"] for t in job["testLog"] if t["verdict"] == "clean" and t.get("otdrS")]),
                         "elapsedS": round((job["ended"] or time.time()) - job["started"])}
        out["excluded"] = os.environ.get("CONTINUITY_EXCLUDE", "")
        out["feedback"] = job.get("feedback")
        return out

    async def save_log(job: dict, why: str):
        if not (LOG_REPO and LOG_TOKEN) or job.get("simulate"):
            job["logSaved"] = "off" if not (LOG_REPO and LOG_TOKEN) else "simulation, not saved"
            return
        if not job.get("logPath"):
            day = time.strftime("%Y-%m-%d", time.gmtime(job["started"]))
            rib = "R" + "-".join(str(r) for r in job["ribbons"][:6]) + ("+" if len(job["ribbons"]) > 6 else "")
            job["logPath"] = f"runs/{day}/{day}_{time.strftime('%H%M', time.gmtime(job['started']))}_{job['stem']}_{rib}_{job['id'][:8]}.json"
        try:
            sha, st = await asyncio.to_thread(_push_log_sync, job["logPath"], report(job), job.get("logSha"),
                                              f"{why}: {job['stem']} {job['state']}")
            job["logSha"], job["logSaved"] = sha, st
        except Exception as e:                              # noqa: BLE001
            job["logSaved"] = "error: " + str(e)[:160]

    def summary_lines(job: dict) -> tuple[str, list[str]]:
        s = snap(job)
        c, res = s["counts"], s.get("results", {})
        cable = re.sub(r"-R\d+$", "", job["stem"])
        rib = ", ".join(f"R{r}" for r in job["ribbons"][:8]) + ("…" if len(job["ribbons"]) > 8 else "")
        word = {"done": "complete", "stopped": "stopped", "error": "failed"}.get(job["state"], job["state"])
        title = f"E2E {cable} {rib}: {word}"
        mins = round(((job["ended"] or time.time()) - job["started"]) / 60)
        lines = [f"{c['straight']} straight, {c['cross']} crossed, {c.get('dis', 0)} DIS"
                 + (f", {c['unres']} not found" if c.get("unres") else "")
                 + f" of {job['targets']} fibres. {s['tests']} tests, {mins} min."]
        crosses = [f"F{int(k):03d} → F{v['found']:03d}" for k, v in res.items() if v["state"] == "cross"]
        if crosses:
            lines.append("Crossed: " + ", ".join(crosses[:12]) + ("…" if len(crosses) > 12 else ""))
        for k, v in res.items():
            if v["state"] == "dis":
                lines.append(f"DIS F{int(k):03d}" + (f": {v['loc']['text']}" if v.get("loc") else ""))
        if job.get("error"):
            lines.append("Error: " + job["error"][:200])
        if job.get("user"):
            lines.append("Run by " + job["user"])
        return title, lines[:20]

    async def notify(job: dict):
        if job["state"] not in ("done", "stopped", "error"):
            return
        try:                                            # v30: a phone or browser notification to whoever started it
            from relay_push import push_user
            title, lines = summary_lines(job)
            job["pushed"] = await push_user(job.get("user", ""), title, lines[0] if lines else "",
                                            tag="run-" + job["id"], kind="e2e")
        except Exception as e:                          # noqa: BLE001
            job["pushed"] = "error: " + str(e)[:120]
        if not TEAMS_WEBHOOK or job.get("simulate"):
            return
        try:
            title, lines = summary_lines(job)
            job["notified"] = await asyncio.to_thread(_teams_sync, title, lines)
        except Exception as e:                              # noqa: BLE001
            job["notified"] = "error: " + str(e)[:120]

    async def checkpointer(job: dict):
        """v21: save the run to the log store every minute, so a relay restart loses nothing."""
        while job["state"] in ("running", "paused"):
            await asyncio.sleep(60)
            if job["state"] in ("running", "paused"):
                await save_log(job, "checkpoint")

    async def restore():
        """v21: on start up, bring back runs that were in progress when the relay stopped. They come
        back as 'interrupted' with every finished fibre; the app resumes them after sign in."""
        if not (LOG_REPO and LOG_TOKEN):
            return
        try:
            days = {time.strftime("%Y-%m-%d", time.gmtime(time.time() - d * 86400)) for d in (0, 1)}
            for day in sorted(days):
                for f in await asyncio.to_thread(_gh_list_sync, f"runs/{day}"):
                    if not f.get("name", "").endswith(".json"):
                        continue
                    rep_, sha = await asyncio.to_thread(_gh_get_sync, f["path"])
                    if not rep_ or rep_.get("state") not in ("running", "paused", "interrupted") or rep_.get("id") in JOBS:
                        continue
                    eng = ce.Engine()
                    locs = {}
                    for k, v in (rep_.get("results") or {}).items():
                        eng.results[int(k)] = ce.Result(v["state"], v.get("found") or 0, v.get("tests") or 0, v.get("why") or "")
                        if v.get("found"):
                            eng.used_far.add(v["found"])
                        if v.get("loc"):
                            locs[int(k)] = v["loc"]
                    eng.tests = rep_.get("tests") or 0
                    eng.log = list(rep_.get("log") or [])[-80:]
                    eng.say("Relay restarted. Run interrupted; resume carries every finished fibre over.")
                    st = rep_.get("settings") or {}
                    job = {"id": rep_["id"], "state": "interrupted", "simulate": "", "stem": rep_["stem"],
                           "toneRtu": rep_["toneRtu"], "testRtu": rep_["testRtu"], "ribbons": rep_["ribbons"],
                           "targets": rep_["targets"], "started": rep_["started"], "ended": time.time(),
                           "rtuSeconds": rep_.get("rtuSeconds") or 0.0, "error": "The relay restarted during the run.",
                           "settings": st, "engine": eng, "truthNotes": [], "pauseEvt": asyncio.Event(),
                           "testLog": [], "pace": {**{"toneS": 10, "leadS": 2, "auto": True}, **(rep_.get("pace") or {}), "changes": []},
                           "appVersion": rep_.get("appVersion", ""), "resumeOf": rep_.get("resumeOf", ""),
                           "carried": rep_.get("carried", 0), "locs": locs, "user": st.get("user", ""),
                           "logPath": f["path"], "logSha": sha, "restored": True}
                    JOBS[job["id"]] = job
                    await save_log(job, "restored after relay restart")
        except Exception as e:                              # noqa: BLE001
            print("restore failed:", e)

    async def save_all():
        for job in list(JOBS.values()):
            if job["state"] in ("running", "paused"):
                await save_log(job, "relay shutting down")

    router.add_event_handler("startup", restore)
    router.add_event_handler("shutdown", save_all)

    @router.post("/api/continuity/debug")
    async def debug(req: JobReq, x_app_key: str | None = Header(default=None),
                    x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        job = JOBS.get(req.jobId)
        if not job:
            raise HTTPException(404, "No such run")
        return report(job)

    @router.post("/api/continuity/feedback")
    async def feedback(req: FeedbackReq, x_app_key: str | None = Header(default=None),
                       x_session: str | None = Header(default=None)):
        """v20: the tester says whether the result matched what is on site."""
        check_key(x_app_key)
        job = JOBS.get(req.jobId)
        if not job:
            raise HTTPException(404, "That run is no longer on the relay")
        if req.verdict not in ("correct", "partly", "wrong"):
            raise HTTPException(400, "verdict must be correct, partly or wrong")
        job["feedback"] = {"verdict": req.verdict, "notes": req.notes[:2000], "user": req.user[:120],
                           "appVersion": req.appVersion, "t": round(time.time(), 1)}
        await save_log(job, "feedback")
        return {"ok": True, "logSaved": job.get("logSaved", "")}

    @router.post("/api/continuity/jobs")
    async def jobs(x_app_key: str | None = Header(default=None)):
        check_key(x_app_key)
        lst = sorted(JOBS.values(), key=lambda j: j["started"], reverse=True)[:20]
        return {"jobs": [snap(j, False) for j in lst]}

    return router
