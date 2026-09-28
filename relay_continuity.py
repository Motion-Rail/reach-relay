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
}
RELAY_VERSION = "v17"
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
    resumeOf: str | None = None


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
                wid = await asyncio.to_thread(fms.start_otdr, node["rtuId"], node["id"], name,
                                              duration=dur, range_m=80000, comment=comment)
            except StartRefused as e:
                return {"verdict": "start_refused", "detail": str(e), "seconds": round(time.time() - t0, 1)}
            out = await asyncio.to_thread(fms.wait, wid)
        return {"verdict": out.verdict, "detail": out.detail[:600], "seconds": round(out.seconds, 1),
                "workflowId": wid}

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

        eng = ce.Engine()
        for k, v in (req.prior or {}).items():          # v17: resume carries finished fibres over
            try:
                f, st = int(k), v.get("state")
            except Exception:                           # noqa: BLE001
                continue
            if st in ("straight", "cross") and f in targets:
                found = int(v.get("found") or f)
                eng.results[f] = ce.Result(st, found, int(v.get("tests") or 0),
                                           (v.get("why") or "") + " (earlier run)")
                eng.used_far.add(found)
        jid = uuid.uuid4().hex
        job = {"id": jid, "state": "running", "simulate": req.simulate or "", "stem": req.stem,
               "toneRtu": req.toneRtu, "testRtu": req.testRtu, "ribbons": sorted(set(req.ribbons)),
               "targets": len(targets), "started": time.time(), "ended": None, "rtuSeconds": 0.0,
               "error": "", "settings": req.model_dump(exclude={"prior"}), "engine": eng, "truthNotes": [],
               "user": "", "pauseEvt": asyncio.Event(), "testLog": [],
               "pace": {"toneS": req.toneS, "leadS": req.leadS, "auto": req.autoPace, "changes": []},
               "appVersion": req.appVersion, "resumeOf": req.resumeOf or "", "carried": len(eng.results)}
        job["pauseEvt"].set()
        JOBS[jid] = job

        async def control() -> bool:
            await job["pauseEvt"].wait()
            return job["state"] != "stopped"

        if req.simulate:
            truth, notes = ce.planted_truth(req.simulate)
            job["truthNotes"] = notes
            base = ce.sim_tester(truth, miss_rate=0.05, delay=0.25)

            async def test(src, cand):
                r = await base(src, cand)
                job["rtuSeconds"] += PACE["clash_s"] if r == "clash" else PACE["clean_s"]
                job["testLog"].append({"t": round(time.time(), 1), "src": src, "cand": cand, "verdict": r})
                return r
        else:
            fms = await fms_for(x_session)
            await routes(fms, req.toneRtu)
            await routes(fms, req.testRtu)
            tone_state = {"src": 0, "until": 0.0}
            missed = set()                                  # sources whose own fibre read clean once
            pace = job["pace"]

            async def test(src, cand):
                # v17: nothing inside one test may end the run. Any fault is logged as an
                # error and the engine retries, so a 401 or a network blip costs one test.
                try:
                    return await test_once(src, cand)
                except Exception as e:                  # noqa: BLE001
                    msg = getattr(e, "detail", None) or str(e)
                    job["testLog"].append({"t": round(time.time(), 1), "src": src, "cand": cand,
                                           "verdict": "error", "detail": str(msg)[:300]})
                    job["errors"] = job.get("errors", 0) + 1
                    eng.say(f"  {ce.fname(cand)}: error, will retry: {str(msg)[:160]}")
                    await asyncio.sleep(5)
                    return "error"

            async def test_once(src, cand):
                await fms._refresh()
                src_name = f"{req.stem}-{ce.fname(src)}"
                cand_name = f"{req.stem}-{ce.fname(cand)}"
                t0 = time.time()
                tone_s, lead_s = pace["toneS"], pace["leadS"]
                # v15: the first test of a run and the retry of a fibre that missed get a long
                # tone. Field runs: every miss so far followed a slow FMS start (cycle 36-66 s),
                # so the extra cover goes where the risk is, not on every fibre.
                if not job["testLog"] or (cand == src and src in missed):
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
                        eng.say(f"  tone failed on {ce.fname(src)}: {e.detail}")
                        await asyncio.sleep(5)
                        return "error"
                    if t.get("simulated"):
                        raise RuntimeError("Relay tone is simulated (LIVE_TONE=0)")
                    tone_state.update(src=src, until=time.time() + tone_s)
                    await asyncio.sleep(lead_s)
                res = await run_otdr(fms, req.testRtu, cand_name, req.otdrS,
                                     f"continuity {ce.fname(src)} to {ce.fname(cand)}")
                job["rtuSeconds"] += res.get("seconds", 0)
                v = res["verdict"]
                job["testLog"].append({"t": round(t0, 1), "src": src, "cand": cand, "verdict": v,
                                       "otdrS": res.get("seconds"), "cycleS": round(time.time() - t0, 1),
                                       "toneS": tone_s, "leadS": lead_s, "workflow": res.get("workflowId", ""),
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

        job["task"] = asyncio.create_task(runner())
        return {"ok": True, "jobId": jid}

    def snap(job: dict, full: bool = True) -> dict:
        eng: ce.Engine = job["engine"]
        res = eng.results
        counts = {"straight": sum(r.state == "straight" for r in res.values()),
                  "cross": sum(r.state == "cross" for r in res.values()),
                  "unres": sum(r.state == "unres" for r in res.values())}
        out = {k: job[k] for k in ("id", "state", "simulate", "stem", "toneRtu", "testRtu", "ribbons",
                                   "targets", "started", "ended", "error", "truthNotes")}
        out.update(pace={k: job["pace"][k] for k in ("toneS", "leadS", "auto")},
                   appVersion=job.get("appVersion", ""), relayVersion=RELAY_VERSION)
        out.update(done=len(res), counts=counts, tests=eng.tests, rtuSeconds=round(job["rtuSeconds"]),
                   current=eng.current, candidate=eng.candidate, now=time.time(),
                   resumeOf=job.get("resumeOf", ""), carried=job.get("carried", 0),
                   testErrors=job.get("errors", 0))
        if full:
            out.update(eng.snapshot())
            out["settings"] = job["settings"]
        return out

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

    @router.post("/api/continuity/debug")
    async def debug(req: JobReq, x_app_key: str | None = Header(default=None),
                    x_session: str | None = Header(default=None)):
        """Everything needed to diagnose a run: every test with timings, pace changes, full log."""
        check_key(x_app_key)
        job = JOBS.get(req.jobId)
        if not job:
            raise HTTPException(404, "No such run")
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
        return out

    @router.post("/api/continuity/jobs")
    async def jobs(x_app_key: str | None = Header(default=None)):
        check_key(x_app_key)
        lst = sorted(JOBS.values(), key=lambda j: j["started"], reverse=True)[:20]
        return {"jobs": [snap(j, False) for j in lst]}

    return router
