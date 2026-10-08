"""
v32 (relay): Live OTDR. One fibre, from one RTU, a short OTDR again and again so an engineer can watch the trace
change while they handle the fibre (bends, joints, patching).

  POST /api/otdr/live/start  {stem, fibre, rtuId, rtuName?, minutes?}   -> session
  POST /api/otdr/live/status {id}                                       -> session with the reference and latest trace
  POST /api/otdr/live/stop   {id}
  POST /api/otdr/live/reference {id}                                    -> make the latest trace the reference
  POST /api/otdr/live/list                                              -> live sessions now (any cable)

Each shot is the direct ad hoc OTDR the FMS screen uses (no Task): 1550 nm, FMS automatic settings, 5 s
(LIVE_OTDR_S). The stored result is read back with its trace. Traces are reduced to about 1500 points.
The first trace is the reference; every later trace is compared with it and the biggest new loss step is
reported with its distance from the RTU.

One live session per RTU; it refuses an RTU with a Task, an E2E run or a tone on it, and stops by itself after
LIVE_OTDR_MINUTES (default 10). While it runs the RTU shows as testing (purple) everywhere.
How the trace numbers are stored is set by TRACE_FORMAT (uint16le, uint16be, int16le, float32le) and
TRACE_SCALE (dB per unit, default 0.001) until the probe on a real result confirms it.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import struct
import threading
import time
import uuid

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

import fms_continuity as fc

SHOT_S = max(1, int(os.getenv("LIVE_OTDR_S", "3")))           # v34: 3 s by default (was 5); 1 to 10 per session
MINUTES = max(1, int(os.getenv("LIVE_OTDR_MINUTES", "10")))
TRACE_FORMAT = os.getenv("TRACE_FORMAT", "uint16le").strip().lower()
TRACE_SCALE = float(os.getenv("TRACE_SCALE", "0.001"))
BINS = 1500
STEP_DB = float(os.getenv("LIVE_OTDR_STEP_DB", "0.15"))     # smallest new loss worth marking
POLL_S = float(os.getenv("LIVE_OTDR_POLL_S", "1"))          # results list check while waiting (the push is quicker)
SHOT_CHOICES = (1, 2, 3, 5, 10)

SESSIONS_L: dict[str, dict] = {}     # session id -> session


def _num(v, d=None):
    try:
        x = float(v)
        return d if x != x else x
    except Exception:                                   # noqa: BLE001
        return d


def decode_points(points_b64: str, fmt: str = "", scale: float | None = None) -> list[float]:
    fmt = fmt or TRACE_FORMAT
    scale = TRACE_SCALE if scale is None else scale
    raw = base64.b64decode(points_b64 or "")
    codes = {"uint16le": ("<", "H", 2), "uint16be": (">", "H", 2), "int16le": ("<", "h", 2), "int16be": (">", "h", 2),
             "float32le": ("<", "f", 4), "float32be": (">", "f", 4)}
    e, c, w = codes.get(fmt, codes["uint16le"])
    n = len(raw) // w
    return [v * scale for v in struct.unpack(f"{e}{n}{c}", raw[: n * w])]


def reduce_trace(vals: list[float], res_m: float, first_m: float, until_m: float | None) -> dict:
    """About BINS points (mean per bin) from the start to a little past the fibre end."""
    if not vals:
        return {"x0": 0, "dx": 1, "y": []}
    n_keep = len(vals)
    if until_m and res_m:
        n_keep = min(len(vals), int((until_m * 1.08 - first_m) / res_m) + 1)
    vals = vals[: max(10, n_keep)]
    k = max(1, len(vals) // BINS)
    y = [round(sum(vals[i:i + k]) / len(vals[i:i + k]), 4) for i in range(0, len(vals), k)]
    return {"x0": round(first_m, 2), "dx": round(res_m * k, 4), "y": y}


def compare(ref: dict, new: dict, link_m: float | None) -> list[dict]:
    """New loss steps: where the latest trace drops away from the reference and stays lower after."""
    if not ref.get("y") or not new.get("y") or abs(ref["dx"] - new["dx"]) > 1e-6:
        return []
    n = min(len(ref["y"]), len(new["y"]))
    end = n
    if link_m:
        end = min(n, int((link_m - ref["x0"]) / ref["dx"]) - 3)
    d = [ref["y"][i] - new["y"][i] for i in range(n)]          # positive: more loss now
    w = 6
    best = []
    i = w + 2
    while i < end - w - 2:
        before = sum(d[i - w - 2:i - 2]) / w
        after = sum(d[i + 2:i + w + 2]) / w
        step = after - before
        if step >= STEP_DB:
            j = i
            while j + 1 < end - w - 2:                          # walk to the sharpest point of this step
                b2 = sum(d[j + 1 - w - 2:j - 1]) / w
                a2 = sum(d[j + 3:j + w + 3]) / w
                if a2 - b2 < step:
                    break
                step, j = a2 - b2, j + 1
            best.append({"m": round(ref["x0"] + j * ref["dx"]), "db": round(step, 2)})
            i = j + w * 2
        else:
            i += 1
    return sorted(best, key=lambda b: -b["db"])[:5]


def _busy(rtu_id: str, rtu_name: str) -> str:
    """Why this RTU cannot run a live OTDR now, or ''."""
    import relay_bulk
    from relay_continuity import JOBS
    for s in SESSIONS_L.values():
        if s["state"] == "running" and s["rtuId"] == str(rtu_id):
            return f"{s['owner']} is already running a live OTDR from this RTU"
    for j in JOBS.values():
        if j.get("state") in ("running", "paused") and rtu_name and rtu_name in (j.get("toneRtu"), j.get("testRtu")):
            return "An E2E run is using this RTU"
    for t in relay_bulk.toning_now():
        if str(t.get("rtuId")) == str(rtu_id):
            return "A Uni-dir tone is running from this RTU"
    try:
        import relay_fibres
        for t in (relay_fibres.LIVE_TASKS.get("data") or []):
            if str(t.get("rtuId")) == str(rtu_id):
                return f"An FMS Task by {t.get('owner') or 'someone'} is running on this RTU"
    except Exception:                                   # noqa: BLE001
        pass
    return ""


def _fire(fms, s: dict) -> dict:
    """Start one OTDR. Returns {pid, t0}. Subscribes the push channel first when it is open."""
    url = fc.ADHOC_URL.format(rtu=int(s["rtuId"]), route=int(s["routeId"]))
    t0 = time.time()
    r = fms.post(url, json=fc.adhoc_payload(int(s.get("shotS") or SHOT_S)))
    if r.status_code == 409 or "AlreadyScheduled" in r.text:
        raise RuntimeError("The RTU is busy with another test. Waiting.")
    if not r.ok:
        raise RuntimeError(f"FMS refused the OTDR ({r.status_code}): {r.text[:160]}")
    pid = r.text.strip().strip('"')
    w = s.get("_watch")
    if w:
        try:
            w.subscribe(fc.ADHOC_TOPIC.format(route=int(s["routeId"]), promise=pid))
        except Exception:                               # noqa: BLE001
            s["_watch"] = None
    s.setdefault("pids", []).append(pid)
    del s["pids"][:-400]
    return {"pid": pid, "t0": t0}


def _fire_iolm(fms, s: dict, setup_id: int, mode: str, wls: list[int]) -> dict:
    """v37: start one ad hoc iOLM the way the FMS screen does (no Task). Returns {pid, t0}."""
    r = fms.get(fc.TESTCONFIG_URL.format(id=int(setup_id)))
    if not r.ok:
        raise RuntimeError(f"FMS would not give that iOLM test setup ({r.status_code}).")
    body = fc.iolm_body(r.json(), mode, wls)
    t0 = time.time()
    r = fms.post(fc.IOLM_URL.format(rtu=int(s["rtuId"]), route=int(s["routeId"])), json=body)
    if r.status_code == 409 or "AlreadyScheduled" in r.text:
        raise RuntimeError("The RTU is busy with another test. Try again when it is free.")
    if not r.ok:
        raise RuntimeError(f"FMS refused the iOLM ({r.status_code}): {r.text[:160]}")
    pid = r.text.strip().strip('"')
    w = s.get("_watch")
    if w:
        try:
            w.subscribe(fc.ADHOC_TOPIC.format(route=int(s["routeId"]), promise=pid))
        except Exception:                               # noqa: BLE001
            s["_watch"] = None
    return {"pid": pid, "t0": t0}


def _await(fms, s: dict, shot: dict, kind: str = "OTDR", limit: float = 90) -> str:
    """Wait for that OTDR's stored result id: the push says it at once; the results list is checked every second too."""
    pid, t0 = shot["pid"], shot["t0"]
    nxt = t0 + max(1.0, int(s.get("shotS") or SHOT_S) - 0.5)   # nothing can be ready before the shot ends
    what = "iOLM" if kind == "iOLM" else "OTDR"
    while time.time() - t0 < limit and s["state"] == "running":
        w = s.get("_watch")
        if w:
            msg = w.next_message(timeout=0.5)
            if msg is not None and str(msg.get("promiseId") or "") == pid:
                if msg.get("isError") or msg.get("error"):
                    why = msg.get("body") if isinstance(msg.get("body"), str) else str(msg.get("body") or "")
                    if fc.LIVE_PATTERNS.search(why or ""):
                        raise RuntimeError(f"Live light on the fibre: the RTU will not fire an {what} into it.")
                    raise RuntimeError(f"The {what} failed: " + (why[:160] or "no reason given"))
                if msg.get("lastTestResultId"):
                    s["pushOk"] = True
                    return str(msg["lastTestResultId"])
        else:
            time.sleep(0.3)
        if time.time() >= nxt:
            nxt = time.time() + POLL_S
            for res in fms.adhoc_results(int(s["routeId"]), top=3, kind=kind):
                md = res.get("metadata") or {}
                if md.get("PromiseId") == pid:
                    if md.get("HasError"):
                        why = " ".join(fc._strings(res, keys=("message", "messageKey", "error", "errorMessage")))[:200]
                        if fc.LIVE_PATTERNS.search(why):
                            raise RuntimeError(f"Live light on the fibre: the RTU will not fire an {what} into it.")
                        raise RuntimeError(f"The {what} failed: " + (why or "no reason given"))
                    return str(res.get("resultid"))
    if s["state"] != "running":
        return ""
    raise RuntimeError(f"No result after {int(limit)} s. Light on the fibre (a tone or traffic) stops an {what}.")


def _read(fms, rid: str, t0: float) -> dict:
    """The trace of one stored result, asked for by its id (one small query, not the last three traces)."""
    params = {"$filter": f"resultid eq {rid}", "$top": "1", "$skip": "0",
              "$select": "resultid,metadata,brief/LinkResults,brief/Measurement/OtdrMeasurements"}
    full = None
    for _ in range(3):                                  # the push can be a moment ahead of the store
        rr = fms.get(fc.RESULTS_URL, params=params)
        rr.raise_for_status()
        j = rr.json()
        full = next(iter(j.get("results", j if isinstance(j, list) else [])), None)
        if full:
            break
        time.sleep(0.7)
    if not full:
        raise RuntimeError("The trace could not be read back from FMS.")
    md = full.get("metadata") or {}
    om = (((full.get("brief") or {}).get("Measurement") or {}).get("OtdrMeasurements") or [{}])[0]
    dp = om.get("DataPoints") or {}
    link = _num(((full.get("brief") or {}).get("LinkResults") or {}).get("Length"))
    npts = int(_num(dp.get("NumberOfPoints"), 0) or 0)
    res_m = _num(dp.get("Resolution")) or ((_num((om.get("Parameters") or {}).get("Range"), 0) / npts) if npts else 1.0)
    first = _num(dp.get("FirstPointPosition"), 0.0) or 0.0
    vals = decode_points(dp.get("Points") or "")
    events = event_table(om.get("Events") or [], vals, res_m, first, link)
    return {"t": time.time(), "secs": round(time.time() - t0, 1), "resultId": rid, "testTime": md.get("TestTime"),
            "len": link, "trace": reduce_trace(vals, res_m, first, link), "events": events,
            "wl": _wl(om.get("Wavelength"))}


def _when(v) -> float:
    from relay_fibres import _when as w
    return w(v)


def _live_pids() -> set:
    return {p for s in SESSIONS_L.values() for p in s.get("pids", [])}



def _pick(d: dict, *names):
    """First of these fields that holds a number (FMS sends numbers as text, NaN included)."""
    for n in names:
        v = _num(d.get(n))
        if v is not None and v == v:
            return v
    return None


def _slope(vals: list[float], res_m: float, first_m: float, a_m: float, b_m: float) -> float | None:
    """Fibre attenuation in dB/km between two distances, by a straight line fit through the trace,
    keeping clear of the events at each end (the dead zones)."""
    if not vals or not res_m or b_m - a_m < 400:
        return None
    pad = min(250.0, (b_m - a_m) * 0.15)
    i = max(0, int((a_m + pad - first_m) / res_m))
    j = min(len(vals) - 1, int((b_m - pad - first_m) / res_m))
    if j - i < 20:
        return None
    step = max(1, (j - i) // 4000)
    xs = [first_m + k * res_m for k in range(i, j + 1, step)]
    ys = [vals[k] for k in range(i, j + 1, step)]
    n = len(xs); mx = sum(xs) / n; my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if not sxx:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    att = round(-b * 1000, 3)
    return att if 0 <= att < 5 else None


def event_table(raw: list[dict], vals: list[float], res_m: float, first_m: float, link_m: float | None) -> list[dict]:
    """v38: the OTDR event table the way the instrument shows it: number, type, distance, section before,
    event loss, reflectance, section attenuation and cumulative loss from the launch."""
    rows = []
    for e in raw or []:
        m = _pick(e, "Position")
        if m is None:
            continue
        status = str(e.get("Status") or "")
        rows.append({"m": round(m, 1), "loss": _pick(e, "Loss", "SpliceLoss", "EventLoss"),
                     "refl": _pick(e, "Reflectance"), "type": e.get("Type") or e.get("TypeCode") or "",
                     "code": str(e.get("TypeCode") or ""), "status": status,
                     "att": _pick(e, "Attenuation", "SectionAttenuation"), "cumFms": _pick(e, "CumulativeLoss", "Cumulative")})
    rows.sort(key=lambda r: r["m"])
    cum, prev = 0.0, None
    for n, r in enumerate(rows, 1):
        r["n"] = n
        if prev is None:
            r["sec"] = None
        else:
            r["sec"] = round(r["m"] - prev["m"], 1)
            if r["att"] is None:
                r["att"] = _slope(vals, res_m, first_m, prev["m"], r["m"])
            if r["att"] is not None:
                cum += r["att"] * r["sec"] / 1000
        launch = "Launch" in str(r["type"]) or "SpanStart" in r["status"]
        end = "End" in str(r["type"]) and "Fib" in str(r["type"]) or "SpanEnd" in r["status"]
        if r["loss"] is not None and not launch and not end:
            cum += r["loss"]
        r["cum"] = r.pop("cumFms") if r.get("cumFms") is not None else (round(cum, 3) if prev is not None else 0.0)
        r["launch"], r["end"] = launch, bool(end)
        prev = r
    return rows[:80]


def _wl(v) -> str:
    """A wavelength as nm text: FMS gives 1550, "1550" or 1.55e-06 (metres)."""
    x = _num(v)
    if x is None or x != x or x <= 0:
        return "1550"
    return str(round(x * 1e9) if x < 1e-3 else round(x))


def _m(v):
    """iOLM distances come in km on this FMS; anything under 1000 is km."""
    x = _num(v)
    if x is None or x != x:
        return None
    return round(x * 1000, 1) if x < 1000 else round(x, 1)


def link_view(full: dict) -> dict:
    """v38: an iOLM result as EXFO's Link View: the elements in order (with the fibre section before each),
    per wavelength loss and reflectance, verdicts, and the link summary."""
    b = full.get("brief") or {}
    lr = b.get("LinkResults") or {}
    wls = [str(x.get("Wavelength")) for x in (lr.get("Results") or []) if x.get("Wavelength")]
    summary = [{"wl": str(x.get("Wavelength")), "loss": _pick(x, "Loss"), "orl": _pick(x, "Orl", "ORL")}
               for x in (lr.get("Results") or [])]
    els = []
    for el in (b.get("Measurement") or {}).get("Elements") or []:
        res = {}
        for q in el.get("Results") or []:
            w = str(q.get("Wavelength") or "")
            if w:
                res[w] = {"loss": _pick(q, "Loss"), "refl": _pick(q, "Reflectance"),
                          "verdict": q.get("Verdict") or q.get("ElementVerdict") or ""}
                if w not in wls:
                    wls.append(w)
        sec = el.get("PreviousFiberSection") or {}
        sres = {}
        for q in sec.get("Results") or []:
            w = str(q.get("Wavelength") or "")
            if w:
                sres[w] = {"att": _pick(q, "Attenuation"), "loss": _pick(q, "Loss")}
        subs = el.get("SubElements") or []
        els.append({"type": el.get("Type") or (el.get("CustomElementName") or "Element"),
                    "name": el.get("CustomElementName") or "", "status": el.get("Status") or "",
                    "verdict": el.get("ElementVerdict") or el.get("Verdict") or "",
                    "m": _m(el.get("Position")), "res": res, "sub": len(subs),
                    "subTypes": [str(x.get("Type") or "") for x in subs][:6],
                    "sec": {"m": _m(sec.get("Length")), "res": sres} if sec else None})
    els.sort(key=lambda e: (e["m"] is None, e["m"] or 0))
    for n, e in enumerate(els, 1):
        e["n"] = n
    return {"len": _num(lr.get("Length")), "verdict": b.get("GlobalVerdict") or "", "wls": wls,
            "summary": summary, "elements": els[:120]}

def _one(fms, rid: str, select: str) -> dict | None:
    r = fms.get(fc.RESULTS_URL, params={"$filter": f"resultid eq {rid}", "$top": "1", "$skip": "0", "$select": select})
    r.raise_for_status()
    j = r.json()
    return next(iter(j.get("results", j if isinstance(j, list) else [])), None)


def _related_ids(md: dict) -> list[str]:
    out = []
    for x in md.get("RelatedResults") or []:
        v = x if isinstance(x, str) else (x.get("ResultId") or x.get("resultid") or x.get("resultId") or x.get("Id") or "") if isinstance(x, dict) else ""
        if v:
            out.append(str(v))
    return out


def _read_any(fms, rid: str) -> dict:
    """v36: an OTDR result gives its own trace. An iOLM result has no trace of its own: FMS can extract the OTDR
    traces it was built from (POST .../otdr/extract, then metadata.RelatedResults); the 1550 one is drawn, and the
    iOLM link elements become the event list. If FMS gives no trace, the events are still returned."""
    head = _one(fms, rid, "resultid,metadata,brief/LinkResults")
    if not head:
        raise RuntimeError("That result was not found in FMS.")
    md = head.get("metadata") or {}
    if str(md.get("TestType") or "").upper() != "IOLM":
        return {**_read(fms, rid, time.time()), "kind": "OTDR"}
    full = _one(fms, rid, "resultid,metadata,brief/GlobalVerdict,brief/LinkResults,brief/Measurement/Elements") or head
    lr = (full.get("brief") or {}).get("LinkResults") or {}
    length = _num(lr.get("Length"))
    events = []
    for el in ((full.get("brief") or {}).get("Measurement") or {}).get("Elements") or []:
        if str(el.get("Status") or "") in ("LinkStart", "LinkEnd"):
            continue
        pos = _num(el.get("Position"))
        if pos is None:
            continue
        m = pos * 1000 if pos < 1000 else pos                   # iOLM positions come in km
        rs = el.get("Results") or []
        q = next((x for x in rs if str(x.get("Wavelength")) == "1550"), rs[0] if rs else {})
        events.append({"m": round(m), "loss": _num(q.get("Loss")), "type": el.get("Type") or ""})
    rel = _related_ids(md)
    if not rel:
        try:
            fms.post(fc.RESULTS_URL.rstrip("/") + f"/{rid}/otdr/extract", json={})
            again = _one(fms, rid, "resultid,metadata")
            rel = _related_ids((again or {}).get("metadata") or {})
        except Exception:                               # noqa: BLE001
            rel = []
    best, note = None, ""
    for r2 in rel[:4]:
        try:
            t = _read(fms, r2, time.time())
        except Exception:                               # noqa: BLE001
            continue
        wl = str(((_one(fms, r2, "resultid,brief/LinkResults") or {}).get("brief") or {}).get("LinkResults", {}).get("Results", [{}])[0].get("Wavelength") or "")
        if best is None or wl == "1550":
            best = {**t, "wl": wl}
        if wl == "1550":
            break
    if not best:
        note = "FMS gave no OTDR trace for this iOLM result, so only its events are shown."
    return {"resultId": rid, "testTime": md.get("TestTime"), "len": length or (best or {}).get("len"),
            "trace": (best or {}).get("trace"), "events": events[:60], "kind": "iOLM", "note": note,
            "traceOf": (best or {}).get("resultId", ""), "link": link_view(full),
            "traceEvents": (best or {}).get("events") or [], "wl": (best or {}).get("wl") or ""}


def _take(s: dict, shot: dict):
    if s["ref"] is None:
        s["ref"] = shot
    shot["steps"] = compare(s["ref"]["trace"], shot["trace"], s["ref"].get("len") or shot.get("len"))
    prev = s["latest"]
    shot["new"] = compare(prev["trace"], shot["trace"], shot.get("len")) if prev else []
    s["latest"] = shot
    s["shots"].append({k: shot[k] for k in ("t", "secs", "len", "resultId")} | {"steps": shot["steps"][:2], "shotS": shot.get("shotS")})
    s["count"] += 1


def _loop(s: dict, token_fn):
    """v34: the next OTDR starts as soon as FMS has stored the last one; its trace is read while the next one runs."""
    import relay_bulk
    fms = fc.Fms.from_token_provider(token_fn, s["user"])
    s["_watch"] = fc.StompWatch.open(fms)
    s["push"] = bool(s["_watch"])
    pending = None                                      # the OTDR now running on the RTU
    while s["state"] == "running":
        try:
            if time.time() > s["until"]:
                s.update(state="done", note="Stopped after the time limit.")
                break
            relay_bulk.mark_toning(s["rtuId"], int(s.get("shotS") or SHOT_S) + 30, s["user"], s["fibreName"])
            if pending is None:
                pending = _fire(fms, s)
            rid = _await(fms, s, pending)
            if not rid:
                break
            done, pending = pending, None
            shot_s = int(s.get("shotS") or SHOT_S)
            if s["state"] == "running" and time.time() + shot_s < s["until"]:
                pending = _fire(fms, s)                 # the RTU is busy again while we read the trace
            shot = _read(fms, rid, done["t0"])
            shot["shotS"] = shot_s
            _take(s, shot)
            s["error"] = ""
        except Exception as e:                          # noqa: BLE001
            s["error"] = str(e)[:240]
            pending = None
            time.sleep(6)
            if s.get("_watch") is None and s.get("push"):
                s["_watch"] = fc.StompWatch.open(fms)
    if s.get("_watch"):
        s["_watch"].close()
    s["_watch"] = None
    relay_bulk.TONING.pop(str(s["rtuId"]), None)
    s["ended"] = time.time()


def public(s: dict, full: bool = True) -> dict:
    out = {k: s[k] for k in ("id", "state", "owner", "stem", "fibre", "rtuId", "rtuName", "started", "until", "count", "error", "note")}
    out["shotS"] = int(s.get("shotS") or SHOT_S)
    out["push"] = bool(s.get("pushOk"))
    out["shots"] = s["shots"][-30:]
    if full:
        out["ref"] = s["ref"] and {k: s["ref"][k] for k in ("t", "len", "trace", "events", "resultId")}
        out["latest"] = s["latest"] and {k: s["latest"][k] for k in ("t", "len", "trace", "events", "steps", "new", "resultId")}
    return out


class StartIn(BaseModel):
    stem: str
    fibre: int
    rtuId: str
    rtuName: str = ""
    minutes: int = 0
    seconds: int = 0


class IdIn(BaseModel):
    id: str
    have: int = -1          # v34: the trace count the page already has; traces are only sent when it changed
    seconds: int = 0


class FibreIn(BaseModel):
    stem: str
    fibre: int


class TraceIn(BaseModel):
    resultId: str


class OnceIn(BaseModel):
    stem: str
    fibre: int
    rtuId: str
    seconds: int = 10
    kind: str = "otdr"            # v37: "otdr" or "iolm"
    setupId: int = 28898          # iOLM test setup (28898 = iOLM Motion NRS-304, 1 = AdHoc iOLM)
    mode: str = "standard"        # iOLM acquisition: standard, fast, rtu
    wavelengths: list[int] = [1550]


ONCE_CHOICES = (5, 10, 15, 30, 60)
IOLM_WLS = (1310, 1550, 1625)
IOLM_WAIT_S = 300                     # one fibre, Standard iOLM: 25 to 75 s seen; three wavelengths take longer


TRACES: dict[str, dict] = {}          # result id -> reduced trace (stored results never change)


def _secs(v: int) -> int:
    return int(v) if int(v or 0) in SHOT_CHOICES else SHOT_S


def make_router(valid_token, check_key, sessions: dict) -> APIRouter:
    router = APIRouter()
    from relay_bulk import require_desktop
    from relay_team import _owner

    def mine(x_session) -> tuple[str, str]:
        u = str((sessions.get(x_session or "") or {}).get("user", ""))
        return u, _owner(u)

    @router.post("/api/otdr/live/start")
    async def start(body: StartIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        user, owner = mine(x_session)
        stem = body.stem.strip().upper()
        if not 1 <= body.fibre <= 432:
            raise HTTPException(400, "Fibre must be 1 to 432")
        import relay_fibres
        ends = await relay_fibres.ensure_ends(await valid_token(x_session), stem)
        end = next((e for e in ends if e["rtuId"] == str(body.rtuId)), None)
        if not end or not end["routes"].get(str(body.fibre)):
            raise HTTPException(404, "That fibre was not found on that RTU in FMS")
        why = _busy(end["rtuId"], end["rtu"])
        if why:
            raise HTTPException(409, why + ". Try again when it is free.")
        sid = uuid.uuid4().hex[:12]
        mins = max(1, min(int(body.minutes or MINUTES), 30))
        s = {"id": sid, "state": "running", "user": user, "owner": owner, "stem": stem, "fibre": body.fibre,
             "fibreName": f"{stem}-F{body.fibre:03d}", "rtuId": end["rtuId"], "rtuName": end["rtu"],
             "routeId": end["routes"][str(body.fibre)], "started": time.time(), "until": time.time() + mins * 60,
             "count": 0, "error": "", "note": "", "ref": None, "latest": None, "shots": [], "ended": None,
             "session": x_session, "shotS": _secs(body.seconds)}
        for k, o in list(SESSIONS_L.items()):           # v34: forget sessions that ended over an hour ago
            if o.get("ended") and time.time() - o["ended"] > 3600:
                SESSIONS_L.pop(k, None)
        SESSIONS_L[sid] = s
        loop = asyncio.get_event_loop()

        def token_fn():
            return asyncio.run_coroutine_threadsafe(valid_token(s["session"]), loop).result(30)
        threading.Thread(target=_loop, args=(s, token_fn), daemon=True).start()
        return public(s)

    @router.post("/api/otdr/live/status")
    async def status(body: IdIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        s = SESSIONS_L.get(body.id)
        if not s:
            raise HTTPException(404, "That live OTDR has ended (the service restarted)")
        return public(s, full=body.have != s["count"])

    # ── v41: stored OTDR traces of one fibre (both ends), and one trace to view ──
    @router.post("/api/otdr/traces")
    async def traces(body: FibreIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        token = await valid_token(x_session)
        require_desktop(sessions, x_session)
        stem = body.stem.strip().upper()
        if not 1 <= body.fibre <= 432:
            raise HTTPException(400, "Fibre must be 1 to 432")
        import relay_fibres
        ends = await relay_fibres.ensure_ends(token, stem)

        def fetch():
            import relay_bulk
            fms = relay_bulk.FMS_FOR(token) if relay_bulk.FMS_FOR else fc.Fms(token)
            rows, pids = [], _live_pids()
            for e in ends[:2]:
                rid = e["routes"].get(str(body.fibre))
                if not rid:
                    continue
                params = {"$filter": f"metadata/AssetId eq {int(rid)} and metadata/TestCategory eq 'Adhoc'",   # v36: OTDR and iOLM
                          "$orderby": "metadata/TestTime desc", "$top": "50", "$skip": "0", "$select": "resultid,metadata,brief/LinkResults"}
                r = fms.get(fc.RESULTS_URL, params=params)
                r.raise_for_status()
                j = r.json()
                for x in j.get("results", j if isinstance(j, list) else []):
                    md = x.get("metadata") or {}
                    kind = str(md.get("TestType") or "").upper()
                    if md.get("HasError") or kind not in ("OTDR", "IOLM"):
                        continue
                    lr = (x.get("brief") or {}).get("LinkResults") or {}
                    rs = lr.get("Results") or [{}]
                    pick = next((q for q in rs if str(q.get("Wavelength")) == "1550"), rs[0])
                    rows.append({"resultId": x.get("resultid"), "t": _when(md.get("TestTime")), "rtuId": e["rtuId"], "rtu": e["rtu"],
                                 "kind": "iOLM" if kind == "IOLM" else "OTDR",
                                 "len": _num(lr.get("Length")), "loss": _num(pick.get("Loss")), "wl": pick.get("Wavelength") or "",
                                 "wls": [str(q.get("Wavelength")) for q in rs if q.get("Wavelength")],
                                 "live": bool(md.get("PromiseId")) and md.get("PromiseId") in pids})
            rows.sort(key=lambda r: -r["t"])
            return rows
        try:
            rows = await asyncio.to_thread(fetch)
        except Exception as e:                          # noqa: BLE001
            raise HTTPException(502, "FMS traces could not be read: " + str(e)[:120]) from None
        return {"stem": stem, "fibre": body.fibre, "traces": rows}

    # ── v36: one OTDR on a fibre now (direct ad hoc call, no FMS Task), the trace comes straight back ──
    @router.post("/api/otdr/once")
    async def once(body: OnceIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        token = await valid_token(x_session)
        require_desktop(sessions, x_session)
        user, owner = mine(x_session)
        stem = body.stem.strip().upper()
        if not 1 <= body.fibre <= 432:
            raise HTTPException(400, "Fibre must be 1 to 432")
        kind = (body.kind or "otdr").lower()
        if kind not in ("otdr", "iolm"):
            raise HTTPException(400, "Test must be otdr or iolm")
        if kind == "otdr" and body.seconds not in ONCE_CHOICES:
            raise HTTPException(400, "OTDR length must be one of " + ", ".join(map(str, ONCE_CHOICES)) + " s")
        if kind == "iolm":
            from relay_bulk import IOLM_MODES
            wls = sorted(set(body.wavelengths or []))
            if not wls or any(w not in IOLM_WLS for w in wls):
                raise HTTPException(400, "iOLM wavelengths must be from 1310, 1550, 1625")
            if body.mode not in IOLM_MODES:
                raise HTTPException(400, "iOLM mode must be standard, fast or rtu")
            if body.setupId <= 0:
                raise HTTPException(400, "That is not a test setup")
        import relay_fibres
        import relay_bulk
        ends = await relay_fibres.ensure_ends(token, stem)
        end = next((e for e in ends if e["rtuId"] == str(body.rtuId)), None)
        if not end or not end["routes"].get(str(body.fibre)):
            raise HTTPException(404, "That fibre was not found on that RTU in FMS")
        why = _busy(end["rtuId"], end["rtu"])
        if why:
            raise HTTPException(409, why + ". Try again when it is free.")
        s = {"state": "running", "rtuId": end["rtuId"], "routeId": end["routes"][str(body.fibre)], "shotS": body.seconds,
             "user": user, "fibreName": f"{stem}-F{body.fibre:03d}"}
        fms = relay_bulk.FMS_FOR(token) if relay_bulk.FMS_FOR else fc.Fms(token)

        def go():
            if kind == "iolm":
                from relay_bulk import IOLM_MODES
                s["shotS"] = 20                         # nothing is stored sooner than this
                relay_bulk.mark_toning(s["rtuId"], IOLM_WAIT_S + 30, user, s["fibreName"])
                try:
                    shot = _fire_iolm(fms, s, body.setupId, IOLM_MODES[body.mode], wls)
                    rid = _await(fms, s, shot, kind="iOLM", limit=IOLM_WAIT_S)
                    t = _read_any(fms, rid)
                    return {**t, "secs": time.time() - shot["t0"]}
                finally:
                    relay_bulk.TONING.pop(str(s["rtuId"]), None)
            relay_bulk.mark_toning(s["rtuId"], body.seconds + 60, user, s["fibreName"])
            try:
                shot = _fire(fms, s)
                rid = _await(fms, s, shot)
                return {**_read(fms, rid, shot["t0"]), "kind": "OTDR"}
            finally:
                relay_bulk.TONING.pop(str(s["rtuId"]), None)
        try:
            t = await asyncio.to_thread(go)
        except RuntimeError as e:
            raise HTTPException(502, str(e)[:200]) from None
        TRACES[t["resultId"]] = {k: t.get(k) for k in ("resultId", "testTime", "len", "trace", "events", "kind", "note", "traceOf", "link", "traceEvents", "wl")}
        if len(TRACES) > 60:
            TRACES.pop(next(iter(TRACES)))
        return {**TRACES[t["resultId"]], "secs": t["secs"], "rtu": end["rtu"], "rtuId": end["rtuId"], "seconds": body.seconds,
                "wavelengths": wls if kind == "iolm" else [1550], "by": owner, "t": time.time()}

    @router.post("/api/otdr/trace")
    async def trace(body: TraceIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        token = await valid_token(x_session)
        require_desktop(sessions, x_session)
        rid = body.resultId.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", rid):
            raise HTTPException(400, "That is not a result id")
        if rid not in TRACES:
            import relay_bulk
            fms = relay_bulk.FMS_FOR(token) if relay_bulk.FMS_FOR else fc.Fms(token)
            try:
                t = await asyncio.to_thread(_read_any, fms, rid)
            except Exception as e:                      # noqa: BLE001
                raise HTTPException(502, str(e)[:160]) from None
            if len(TRACES) > 60:
                TRACES.pop(next(iter(TRACES)))
            TRACES[rid] = {k: t.get(k) for k in ("resultId", "testTime", "len", "trace", "events", "kind", "note", "traceOf", "link", "traceEvents", "wl")}
        return TRACES[rid]

    @router.post("/api/otdr/live/settings")
    async def settings(body: IdIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        s = SESSIONS_L.get(body.id)
        if not s:
            raise HTTPException(404, "That live OTDR has ended")
        if body.seconds not in SHOT_CHOICES:
            raise HTTPException(400, "Shot length must be one of " + ", ".join(map(str, SHOT_CHOICES)) + " s")
        s["shotS"] = body.seconds                       # used from the next shot
        return public(s, full=False)

    @router.post("/api/otdr/live/stop")
    async def stop(body: IdIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        s = SESSIONS_L.get(body.id)
        if s and s["state"] == "running":
            s.update(state="stopped", note=f"Stopped by {mine(x_session)[1]}.")
        return {"ok": True}

    @router.post("/api/otdr/live/reference")
    async def reference(body: IdIn, x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        s = SESSIONS_L.get(body.id)
        if not s or not s["latest"]:
            raise HTTPException(409, "No trace yet")
        s["ref"] = s["latest"]
        s["latest"] = {**s["latest"], "steps": [], "new": []}
        return public(s)

    @router.post("/api/otdr/live/list")
    async def lst(x_app_key: str | None = Header(default=None), x_session: str | None = Header(default=None)):
        check_key(x_app_key)
        await valid_token(x_session)
        now = time.time()
        return {"sessions": [public(s, False) for s in SESSIONS_L.values()
                             if s["state"] == "running" or now - (s["ended"] or now) < 300]}

    return router
