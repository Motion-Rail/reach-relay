"""Cable Insight pull engine (Phase 1).

Pulls fibre by fibre with fms_pull's own functions, so the results are the same ones the
client workbooks are built from. Every result is kept in the raw store:

  data/<section>/raw/<A|B>/<fibre>.json   newest result used for that fibre, with the
                                          list of results FMS held when it was checked
  data/<section>/raw/<A|B>/results/<id>.json   every result ever pulled, never changed

Before fetching a fibre's full result, a light query lists the results FMS holds for it.
If that list matches what the raw store already has, the stored result is reused, so a
second pull of the same cable only fetches what is new. A ribbon pull fills every other
fibre from the raw store, so the dataset is always the whole cable.
"""
import collections
import hashlib
import json
import os
import threading
import time

import ci_store

STATES = ("wait", "check", "pull", "new", "same", "kept", "none", "err")


class Progress:
    """Shared, thread safe view of a pull for the page to draw."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.phase = "Starting"
            self.ends = {}          # end -> {"label":..., "fibres": {fno: state}, "dates": {fno: date}}
            self.current = None
            self.summary = None

    def set_end(self, end, label, fnos):
        with self.lock:
            self.ends[end] = {"label": label, "fibres": {n: "wait" for n in fnos}, "dates": {}}

    def mark(self, end, fno, state, date=None):
        with self.lock:
            e = self.ends.get(end)
            if e is not None:
                e["fibres"][fno] = state
                if date:
                    e["dates"][fno] = date[:16].replace("T", " ")
            if state in ("check", "pull"):
                self.current = f"End {end} F{fno:03d}"

    def snapshot(self):
        with self.lock:
            ends = {}
            for k, e in self.ends.items():
                counts = {s: 0 for s in STATES}
                for s in e["fibres"].values():
                    counts[s] += 1
                ends[k] = {"label": e["label"], "fibres": dict(e["fibres"]),
                           "dates": dict(e["dates"]), "counts": counts}
            return {"phase": self.phase, "current": self.current, "ends": ends,
                    "summary": self.summary}


def _row(fp, n, det, end_label):
    """The same row build_end_rows makes."""
    return {"fid": n.get("name"), "date": det.get("testtime"),
            "loss": det.get("w1550"), "length": det.get("length"),
            "star": det.get("star"), "w1310": det.get("w1310"),
            "w1550": det.get("w1550"), "w1625": det.get("w1625"),
            "verdict": det.get("verdict"), "_key": fp.fiber_key(n.get("name")),
            "events": det.get("events") or [], "sw_loss": det.get("sw_loss"), "_end": end_label,
            "desc": fp.desc_of(n), "newer_no_events": det.get("newer_no_events"),
            "newer_count": det.get("newer_count"), "latest": det.get("latest"),
            "src_type": det.get("src_type")}


def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    return o


class RawStore:
    def __init__(self, folder, end):
        self.dir = os.path.join(folder, "raw", end)
        os.makedirs(os.path.join(self.dir, "results"), exist_ok=True)

    def path(self, fno):
        return os.path.join(self.dir, f"F{fno:03d}.json")

    def get(self, fno):
        try:
            with open(self.path(fno), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def put(self, fno, rec):
        tmp = self.path(fno) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_jsonable(rec), fh)
        os.replace(tmp, self.path(fno))
        rid = (rec.get("det") or {}).get("resultid")
        if rid:
            keep = os.path.join(self.dir, "results", f"{rid}.json")
            if not os.path.exists(keep):
                with open(keep, "w", encoding="utf-8") as fh:
                    json.dump(_jsonable(rec.get("det")), fh)

    def all(self):
        out = {}
        for name in os.listdir(self.dir):
            if name.startswith("F") and name.endswith(".json"):
                try:
                    with open(os.path.join(self.dir, name), encoding="utf-8") as fh:
                        out[int(name[1:4])] = json.load(fh)
                except (OSError, ValueError):
                    pass
        return out


def raw_status(store, section):
    """What the raw store already holds for a cable, per end per fibre."""
    folder = store.folder(section)
    out = {}
    for end in ("A", "B"):
        d = os.path.join(folder, "raw", end)
        if not os.path.isdir(d):
            continue
        recs = RawStore(folder, end).all()
        out[end] = {"label": next((r.get("end_label") for r in recs.values() if r.get("end_label")), end),
                    "fibres": {n: ("have" if (r.get("det") or {}).get("testtime") else "none")
                               for n, r in recs.items()},
                    "dates": {n: ((r.get("det") or {}).get("testtime") or "")[:16].replace("T", " ")
                              for n, r in recs.items()},
                    "checked": {n: r.get("checked_at", "") for n, r in recs.items()}}
    return out


def _signature(results, test_type, dfrom, dto):
    ids = []
    for r in results:
        md = r.get("metadata") or {}
        ids.append(f"{r.get('resultid')}|{md.get('TestType')}|{(md.get('TestTime') or '')[:19]}")
    return hashlib.sha1(f"{test_type}|{dfrom}|{dto}|".encode() + "\n".join(ids).encode()).hexdigest()


def run_pull(store, fms_dir, form, password, user, progress, log, session_factory=None):
    fp = ci_store.load_fms_pull(fms_dir)
    if fp is None:
        raise RuntimeError(f"fms_pull.py not found in {fms_dir}")
    section = form.get("section") or ""
    test_type = form.get("test_type") or "iOLM"
    dfrom = fp.norm_date(form.get("from") or "") if form.get("from") else None
    dto = fp.norm_date(form.get("to") or "", end=True) if form.get("to") else None
    ribbons = (form.get("ribbons") or "").strip()

    progress.phase = "Signing in to FMS"
    log(f"Signing in to FMS as {user}")
    try:
        session = (session_factory or fp.FmsSession)(user, password, verbose=False, retry_password=False)
    except SystemExit as exc:
        raise RuntimeError(str(exc).splitlines()[0])
    password = None

    ends = []
    for end, token in (("A", form.get("rtu_a")), ("B", form.get("rtu_b"))):
        if not token:
            raise RuntimeError(f"End {end} RTU is blank")
        progress.phase = f"Finding the fibres on End {end} ({token})"
        log(progress.phase)
        try:
            nodes, rtu = fp.resolve_end(session, token)
        except SystemExit as exc:
            raise RuntimeError(str(exc).splitlines()[0])
        all_nodes = sorted(nodes, key=lambda n: fp.fibre_num(n.get("name") or "") or 0)
        pick = fp.filter_ribbons(all_nodes, ribbons, verbose=False) if ribbons else all_nodes
        label = rtu.get("site") or rtu.get("name")
        ends.append((end, all_nodes, pick, label))
        log(f"  {len(all_nodes)} fibres on {label}" + (f", pulling {len(pick)} (ribbons {ribbons})" if ribbons else ""))

    # an RTU can carry routes for more than one section: keep only the one being pulled
    secs = collections.Counter(ci_store.section_of(n.get("name")) for n in ends[0][1])
    secs.pop("UNKNOWN", None)
    if section and section in secs:
        sec_guess = section
    elif secs:
        sec_guess = secs.most_common(1)[0][0]
    else:
        sec_guess = section or "UNKNOWN"
    if len(secs) > 1:
        kept = []
        for end, all_nodes, pick, label in ends:
            a2 = [n for n in all_nodes if ci_store.section_of(n.get("name")) == sec_guess]
            p2 = [n for n in pick if ci_store.section_of(n.get("name")) == sec_guess]
            log(f"  End {end}: keeping {len(a2)} routes of {sec_guess}, leaving {len(all_nodes) - len(a2)} other routes")
            kept.append((end, a2, p2, label))
        ends = kept
    folder = store.folder(sec_guess)
    for end, all_nodes, pick, label in ends:
        progress.set_end(end, label, [fp.fibre_num(n.get("name")) for n in all_nodes])

    rows = {}
    tally = {"new": 0, "same": 0, "kept": 0, "none": 0, "err": 0}
    for end, all_nodes, pick, label in ends:
        raw = RawStore(folder, end)
        picked = {id(n) for n in pick}
        out = []
        progress.phase = f"Pulling End {end} ({label})"
        log(progress.phase)
        for n in all_nodes:
            fno = fp.fibre_num(n.get("name"))
            have = raw.get(fno)
            if id(n) not in picked:
                if have and have.get("det") is not None:
                    progress.mark(end, fno, "kept", (have["det"] or {}).get("testtime"))
                    tally["kept"] += 1
                    out.append(_row(fp, n, have["det"], label))
                else:
                    progress.mark(end, fno, "none")
                continue
            progress.mark(end, fno, "check")
            try:
                data = session.rest_get(fp.RESULTS_URL, params={
                    "$filter": fp.adhoc_filter(n.get("id")), "$orderby": "metadata/TestTime desc",
                    "$top": 30, "$skip": 0, "$select": "resultid,metadata"})
                sig = _signature(data.get("results") or data.get("value") or [], test_type, dfrom, dto)
                if have and have.get("sig") == sig and have.get("det") is not None:
                    det, state = have["det"], "same"
                    have["checked_at"] = time.strftime("%Y-%m-%d %H:%M")
                    have["desc"] = fp.desc_of(n)
                    raw.put(fno, have)
                else:
                    progress.mark(end, fno, "pull")
                    det = fp.fetch_iolm(session, n.get("id"), dfrom, dto, test_type)
                    same_result = have and (have.get("det") or {}).get("resultid") == det.get("resultid") \
                        and det.get("resultid")
                    state = "same" if same_result else ("new" if det.get("testtime") else "none")
                    raw.put(fno, {"fid": n.get("name"), "asset": n.get("id"), "desc": fp.desc_of(n), "sig": sig, "det": det,
                                  "end_label": label, "checked_at": time.strftime("%Y-%m-%d %H:%M"),
                                  "window": [dfrom, dto], "test_type": test_type})
            except Exception as exc:
                tally["err"] += 1
                progress.mark(end, fno, "err")
                log(f"  F{fno:03d} End {end}: {exc}")
                if have and have.get("det") is not None:
                    out.append(_row(fp, n, have["det"], label))
                continue
            tally[state] += 1
            progress.mark(end, fno, state, det.get("testtime"))
            out.append(_row(fp, n, det, label))
        rows[end] = (out, label)

    return build_dataset(store, fp, fms_dir, rows, tally, form, ribbons, dfrom, dto, test_type, progress, log)


def _node(rec):
    return {"name": rec.get("fid"), "id": rec.get("asset"), "description": rec.get("desc") or ""}


def rebuild_from_raw(store, fms_dir, section, log=print, progress=None):
    """Build a dataset from the raw store alone, with no FMS calls (after a restart, or
    when the settings for naming locations change)."""
    fp = ci_store.load_fms_pull(fms_dir)
    if fp is None:
        raise RuntimeError(f"fms_pull.py not found in {fms_dir}")
    folder = store.folder(section)
    rows = {}
    for end in ("A", "B"):
        recs = RawStore(folder, end).all()
        label = next((r.get("end_label") for r in recs.values() if r.get("end_label")), end)
        rows[end] = ([_row(fp, _node(r), r["det"], label) for n, r in sorted(recs.items())
                      if r.get("det") is not None and r.get("fid")], label)
    if not rows["A"][0] and not rows["B"][0]:
        raise RuntimeError("Nothing in the raw store for this cable yet")
    progress = progress or Progress()
    tally = {"new": 0, "same": 0, "kept": len(rows["A"][0]) + len(rows["B"][0]), "none": 0, "err": 0}
    return build_dataset(store, fp, fms_dir, rows, tally, {"label": ""}, "", None, None, "iOLM", progress, log)


def odf_rows(rows, end):
    """The ODF connector at this end: the first real event after the launch (position over 0) on
    this end's own trace, the same rule as fms_pull.build_odf."""
    out = []
    for r in rows:
        fno = ci_store.fibre_no(r.get("fid"))
        if fno is None or not r.get("date"):
            continue
        evs = [e for e in (r.get("events") or []) if e.get("position") is not None and e["position"] > 0]
        e = min(evs, key=lambda x: x["position"]) if evs else None
        if e is None:
            continue
        out.append({"fno": fno, "end": end, "pos": e.get("position"), "type": e.get("type"),
                    "refl": e.get("r1550"), "w1310": e.get("w1310"), "w1550": e.get("w1550"),
                    "w1625": e.get("w1625"), "sw": r.get("sw_loss")})
    return out


def build_dataset(store, fp, fms_dir, rows, tally, form, ribbons, dfrom, dto, test_type, progress, log):
    progress.phase = "Pairing both ends"
    rowsA, labelA = rows["A"]
    rowsB, labelB = rows["B"]
    mains, loops, npair = fp.split_legs_same_name(rowsA, verbose=False)
    if npair:
        rowsA, rowsB = mains, loops
    else:
        rowsA = fp.dedupe_routes(rowsA, verbose=False)
        rowsB = fp.dedupe_routes(rowsB, verbose=False)
    events = fp.all_events_table(rowsA, rowsB, single=False, tol=150.0, wl=1550)

    def e2e(rs):
        out = {}
        for r in rs:
            fno = ci_store.fibre_no(r.get("fid"))
            if fno is None or not r.get("date"):
                continue
            out[fno] = {"fid": r["fid"], "date": r["date"], "l1550": r.get("w1550"),
                        "len": r.get("length"), "l1310": r.get("w1310"), "l1625": r.get("w1625")}
        return out
    rawA, rawB = e2e(rowsA), e2e(rowsB)
    ev = [{"fid": e["fid"], "fno": ci_store.fibre_no(e["fid"]), "position": e.get("position"),
           "type": e.get("type"), "ab": e.get("a1550"), "ba": e.get("b1550"), "dirs": e.get("dirs")}
          for e in events if ci_store.fibre_no(e.get("fid"))]
    data = ci_store.finish_data(rawA, rawB, ev, labelA, labelB)
    data["odf"] = odf_rows(rowsA, "A") + odf_rows(rowsB, "B")
    sig = ci_store.content_sig(rawA, rawB)
    newer = sum(1 for r in rowsA + rowsB if r.get("newer_no_events"))
    both = len(set(rawA) & set(rawB))
    summary = {"pulled": tally, "both_ends": both, "a_only": len(set(rawA) - set(rawB)),
               "b_only": len(set(rawB) - set(rawA)), "newer_no_events": newer,
               "fibres": len(set(rawA) | set(rawB))}
    same = store.find_same(data["section"], sig)
    if same:
        store.backfill_odf(data["section"], same["id"], data["odf"])
        summary["same_as"] = same["label"]
        progress.summary = summary
        progress.phase = "Done"
        log(f"Same results as {same['label']}: nothing new to store")
        return {"section": data["section"], "ds": same["id"], "new": False, "same_as": same["label"],
                "summary": summary}
    progress.phase = "Naming locations and storing"
    label = form.get("label") or ""
    if not label:
        label = ci_store.tested_label(data["t_from"], data["t_to"])
        if ribbons:
            label += f" (R{ribbons} updated)"
    ds = store.store_data(data, f"FMS pull {time.strftime('%Y-%m-%d %H:%M')}", "", sig, fms_dir,
                          label, log, notes={"pull": summary, "ribbons": ribbons,
                                             "window": [dfrom, dto], "test_type": test_type})
    progress.summary = summary
    progress.phase = "Done"
    log(f"Done: {tally['new']} new, {tally['same']} already had, {tally['kept']} kept from the store, "
        f"{tally['none']} no result, {tally['err']} errors. Both ends: {both} of {summary['fibres']}")
    return {"section": data["section"], "ds": ds, "new": True, "summary": summary}


# --------------------------------------------------------------------------- cable list
CABLE_CACHE = "fms_cables.json"


def cable_list(fp, nodes, rtus):
    """Every cable FMS knows, with the RTU at each end (End A east, as fms_pull orders them)
    and the section its routes carry."""
    rtu_of = lambda n: (n.get("rtu") or {}).get("name") or n.get("rtuName")
    secs = {}
    for n in nodes:
        s = ci_store.section_of(n.get("name"))
        if s != "UNKNOWN":
            secs.setdefault(rtu_of(n), collections.Counter())[s] += 1
    out = []
    for key, ends in fp.cables_from_rtus(rtus).items():
        a, b = fp.order_ends(key, ends)
        c = secs.get(a["name"]) or collections.Counter()
        out.append({"key": key, "cable": key[2:] if key.startswith("F-") else key,
                    "section": c.most_common(1)[0][0] if c else key,
                    "rtu_a": a["name"], "site_a": a.get("site") or "",
                    "rtu_b": b["name"] if b else "", "site_b": (b.get("site") or "") if b else "",
                    "two_ended": bool(b), "routes": a.get("count")})
    return sorted(out, key=lambda x: x["cable"])


def load_cable_list(store, fms_dir):
    """The cable list saved by the last refresh, or fms_pull's own RTU cache if there is one."""
    import json
    mine = os.path.join(store.data_dir, CABLE_CACHE)
    try:
        with open(mine, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        pass
    fp = ci_store.load_fms_pull(fms_dir)
    theirs = os.path.join(fms_dir or "", ".cache_rtus_RTU.json")
    if fp and os.path.exists(theirs):
        try:
            with open(theirs, encoding="utf-8") as fh:
                c = json.load(fh)
            return {"at": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(theirs))),
                    "source": "fms_pull cache", "cables": cable_list(fp, c["nodes"], c["rtus"])}
        except Exception:
            pass
    return {"at": None, "source": None, "cables": []}


def refresh_cable_list(store, fms_dir, password, user, progress, log, session_factory=None):
    import json
    fp = ci_store.load_fms_pull(fms_dir)
    if fp is None:
        raise RuntimeError(f"fms_pull.py not found in {fms_dir}")
    progress.phase = "Signing in to FMS"
    log(f"Signing in to FMS as {user}")
    try:
        session = (session_factory or fp.FmsSession)(user, password, verbose=False, retry_password=False)
    except SystemExit as exc:
        raise RuntimeError(str(exc).splitlines()[0])
    password = None
    progress.phase = "Listing every RTU and cable in FMS"
    log(progress.phase + " (about a minute)")
    nodes = fp.search_by_rtu(session, "RTU", verbose=False)
    rtus = fp.collect_rtus(nodes)
    try:
        fp.cache_save("rtus_RTU", {"nodes": nodes, "rtus": rtus})     # fms_pull's menu reuses it
    except Exception:
        pass
    out = {"at": time.strftime("%Y-%m-%d %H:%M"), "source": "FMS", "cables": cable_list(fp, nodes, rtus)}
    with open(os.path.join(store.data_dir, CABLE_CACHE), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    progress.phase = "Done"
    log(f"Done: {len(out['cables'])} cables on {len(rtus)} RTUs")
    return {"cables": len(out["cables"]), "rtus": len(rtus)}
