"""Cable Insight store.

One folder per cable section under data/, each holding:
  pulls/        pull workbooks loaded by hand, exactly as fms_pull wrote them
  raw/A, raw/B  every FMS result pulled, one file per fibre per end (Phase 1)
  store.sqlite  the processed results, one dataset per pull

Locations are named with fms_pull's own schedule matching (Distances.xlsx), so the
names are the ones the client workbooks use.

A splice seen from both ends uses the signed average (a gainer's negative reading is
kept). A splice seen from one end only follows the "one end only" setting:
  nominal  the missing direction is the nominal value (default 0.02 dB)
  auto     the missing direction is the same direction ribbon average at that location
           when at least half the ribbon saw it there, otherwise the nominal value
  exclude  left out of Warn+ and listed as one way only
Filled values are always marked.
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import statistics
import sys
import time

import openpyxl

SPLICE_TYPES = ("Splice", "Group")
END_MARGIN_M = 100.0
MATCH_TOL_M = 30.0
DIST_OVERRIDE = None        # a Distances workbook to use instead of the one next to fms_pull (the relay)
FIBRES_PER_RIBBON = 12
DEFAULT_SETTINGS = {"one_end": "nominal", "nominal": 0.02, "threshold": 0.15, "split": False,
                    "sodf_limit": 0.30, "nr_ribbons": "1-18", "spur_gap": 1500, "group_tol": 100}

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets(
  id INTEGER PRIMARY KEY, label TEXT, source TEXT, sha1 TEXT, imported_at TEXT,
  section TEXT, cable TEXT, site_a TEXT, site_b TEXT, t_from TEXT, t_to TEXT,
  n_fibres INTEGER, n_events INTEGER, schedule TEXT, content_sig TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS fibres(
  ds INTEGER, fno INTEGER, fid TEXT, ribbon INTEGER,
  date_a TEXT, date_b TEXT, len_a REAL, len_b REAL,
  a1310 REAL, a1550 REAL, a1625 REAL, b1310 REAL, b1550 REAL, b1625 REAL);
CREATE TABLE IF NOT EXISTS events(
  ds INTEGER, fno INTEGER, ribbon INTEGER, pos REAL, type TEXT,
  ab REAL, ba REAL, dirs INTEGER, loc TEXT);
CREATE TABLE IF NOT EXISTS locations(
  ds INTEGER, loc TEXT, ord INTEGER, pos REAL, kind TEXT, n_fibres INTEGER,
  scheduled INTEGER);
CREATE INDEX IF NOT EXISTS ev_ds ON events(ds, loc);
CREATE INDEX IF NOT EXISTS fi_ds ON fibres(ds);
"""


# --------------------------------------------------------------------------- helpers
def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def fibre_no(fid):
    m = re.search(r"F(\d+)\s*$", str(fid or ""))
    return int(m.group(1)) if m else None


def ribbon_of(fno):
    return (fno - 1) // FIBRES_PER_RIBBON + 1 if fno else None


def section_of(fid):
    """F-RGAC-SNBC-A-R432-F001 -> RGAC-SNBC-A-R432"""
    m = re.match(r"F-(.+)-F\d+\s*$", str(fid or ""))
    return m.group(1) if m else "UNKNOWN"


def cable_of(fid):
    """F-RGAC-SNBC-A-R432-F001 -> F-RGAC-SNBC (the Distances.xlsx CABLE tag)"""
    m = re.match(r"(F-[A-Z0-9]+-[A-Z0-9]+)", str(fid or ""))
    return m.group(1) if m else ""


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_sig(rawA, rawB):
    """Fingerprint of WHICH tests a pull holds: every fibre's test time at each end.
    Two pulls with the same fingerprint hold the same results, whatever the file."""
    parts = [f"A{n}:{rawA[n]['date'][:19]}" for n in sorted(rawA) if rawA[n].get("date")]
    parts += [f"B{n}:{rawB[n]['date'][:19]}" for n in sorted(rawB) if rawB[n].get("date")]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def is_pull_workbook(path):
    try:
        wb = openpyxl.load_workbook(path, read_only=True)
        ok = "All Events" in wb.sheetnames and "Raw A-B (End A)" in wb.sheetnames
        wb.close()
        return ok
    except Exception:
        return False


def tested_label(t_from, t_to):
    def d(s):
        try:
            return time.strptime(s[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return None
    a, b = d(t_from), d(t_to)
    if not b:
        return "Pull"
    if a and a[:3] != b[:3]:
        if a[:2] == b[:2]:
            return f"Tests {a.tm_mday} to {time.strftime('%d %b %Y', b).lstrip('0')}"
        return f"Tests {time.strftime('%d %b', a).lstrip('0')} to {time.strftime('%d %b %Y', b).lstrip('0')}"
    return f"Tests {time.strftime('%d %b %Y', b).lstrip('0')}"


def splice_value(ab, ba, st, ravg_ab=None, ravg_ba=None):
    """(value, how, gainer). how = both | nominal | ribbon | one_way (left out) | None."""
    if ab is not None and ba is not None:
        return (ab + ba) / 2.0, "both", (ab < 0 or ba < 0)
    if ab is None and ba is None:
        return None, None, False
    mode, nom = st.get("one_end", "nominal"), float(st.get("nominal", 0.02))
    if mode == "exclude":
        return (ab if ab is not None else ba), "one_way", False
    how = "nominal"
    if ab is None:
        fill = ravg_ab if (mode == "auto" and ravg_ab is not None) else nom
        if mode == "auto" and ravg_ab is not None:
            how = "ribbon"
        return (fill + ba) / 2.0, how, False
    fill = ravg_ba if (mode == "auto" and ravg_ba is not None) else nom
    if mode == "auto" and ravg_ba is not None:
        how = "ribbon"
    return (ab + fill) / 2.0, how, False


def ribbon_averages(group, size):
    """Same direction averages at one location for one ribbon, only when at least half
    the ribbon's fibres saw the event in that direction."""
    out = []
    for d in ("ab", "ba"):
        vals = [g[d] for g in group if g[d] is not None]
        n = len({g["fno"] for g in group if g[d] is not None})
        out.append(sum(vals) / len(vals) if vals and n >= size / 2.0 else None)
    return out


# --------------------------------------------------------------------------- reading
def read_pull_workbook(path):
    """Read an fms_pull bidir workbook: both raw E2E sheets and All Events."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)

    def raw(sheet):
        out = {}
        if sheet not in wb.sheetnames:
            return out
        for r in wb[sheet].iter_rows(min_row=3, values_only=True):
            fno = fibre_no(r[0])
            if fno is None:
                continue
            out[fno] = {"fid": r[0], "date": str(r[1] or ""), "l1550": _f(r[6]),
                        "len": _f(r[3]), "l1310": _f(r[5]), "l1625": _f(r[7])}
        return out

    rawA, rawB = raw("Raw A-B (End A)"), raw("Raw B-A (End B)")

    ws = wb["All Events"]
    hdr, events = None, []
    ab_col = ba_col = None
    for r in ws.iter_rows(values_only=True):
        if hdr is None:
            if r and "Fibre" in r and "Position (m)" in r:
                hdr = {str(h).strip(): i for i, h in enumerate(r) if h is not None}
                ab_col = next((k for k in hdr if k.startswith("A-B")), None)
                ba_col = next((k for k in hdr if k.startswith("B-A")), None)
            continue
        fid = r[hdr["Fibre"]]
        fno = fibre_no(fid)
        if fno is None:
            continue
        events.append({
            "fid": fid, "fno": fno, "position": _f(r[hdr["Position (m)"]]),
            "type": r[hdr["Type"]], "ab": _f(r[hdr[ab_col]]) if ab_col else None,
            "ba": _f(r[hdr[ba_col]]) if ba_col else None,
            "dirs": int(r[hdr["Dirs"]] or 0) if "Dirs" in hdr else None})

    site_a = site_b = ""
    if "Bidir E2E Loss Report" in wb.sheetnames:
        top = next(wb["Bidir E2E Loss Report"].iter_rows(max_row=1, values_only=True))
        site_a = str(top[0] or "").split("(")[0].strip()
        site_b = next((str(c).split("(")[0].strip() for c in top[1:]
                       if c and "End B" in str(c)), "")
    wb.close()
    return finish_data(rawA, rawB, events, site_a, site_b)


def finish_data(rawA, rawB, events, site_a, site_b):
    fids = [v["fid"] for v in rawA.values() if v.get("fid")] or [e["fid"] for e in events]
    dates = sorted(d[:19] for d in [v.get("date") for v in rawA.values()] +
                   [v.get("date") for v in rawB.values()] if d)
    return {"rawA": rawA, "rawB": rawB, "events": events, "site_a": site_a,
            "site_b": site_b, "section": section_of(fids[0]) if fids else "UNKNOWN",
            "cable": cable_of(fids[0]) if fids else "",
            "t_from": dates[0] if dates else "", "t_to": dates[-1] if dates else ""}


# --------------------------------------------------------------------------- naming
def _joint_types(dist_path, sheet):
    out = {}
    try:
        wb = openpyxl.load_workbook(dist_path, read_only=True, data_only=True)
        ws = wb[sheet]
        col = None
        for r in ws.iter_rows(values_only=True):
            if col is None:
                if r and "Location" in r and "Joint type" in r:
                    col = list(r).index("Joint type")
                continue
            if r[0]:
                out[str(r[0]).strip()] = str(r[col] or "").strip()
        wb.close()
    except Exception:
        pass
    return out


def load_fms_pull(fms_dir):
    if not fms_dir or not os.path.exists(os.path.join(fms_dir, "fms_pull.py")):
        return None
    if fms_dir not in sys.path:
        sys.path.insert(0, fms_dir)
    import fms_pull
    return fms_pull


def name_locations(events, cable, site_a, fms_dir, log=print):
    """Give each mid-route event its location name using fms_pull's own matching."""
    fp = load_fms_pull(fms_dir)
    if fp is None:
        log("  fms_pull not found: locations are grouped by distance only")
        return _cluster_only(events), {}
    dist = DIST_OVERRIDE or os.path.join(fms_dir, "Distances.xlsx")
    schedule, label, kinds = [], "none", {}
    if os.path.exists(dist):
        sheet, _ = fp.pick_sheet_for_cable(dist, cable, site=site_a, verbose=False)
        if sheet:
            schedule = fp.load_locations(dist, sheet, verbose=False)
            kinds = _joint_types(dist, sheet)
            label = f"Distances.xlsx / {sheet}"
    if schedule:
        fp.map_locations(events, schedule, tol=120.0, verbose=False)
    fp.label_unmapped(events, tol=25.0, min_fibres=3, verbose=False)
    log(f"  locations named from {label}")
    return label, kinds


def _cluster_only(events):
    pts = sorted(e["position"] for e in events)
    if not pts:
        return "distance only"
    run, centres = [pts[0]], []
    for p in pts[1:]:
        if p - run[-1] <= 25.0:
            run.append(p)
        else:
            centres.append(sum(run) / len(run)); run = [p]
    centres.append(sum(run) / len(run))
    for e in events:
        c = round(min(centres, key=lambda x: abs(x - e["position"])))
        e["location"] = f"{c:,} m (unscheduled)"
        e["unscheduled"] = True
    return "distance only"


# --------------------------------------------------------------------------- store
class Store:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

    def sections(self):
        return sorted(d for d in os.listdir(self.data_dir)
                      if os.path.exists(os.path.join(self.data_dir, d, "store.sqlite")))

    def folder(self, section):
        return os.path.join(self.data_dir, section)

    def db(self, section):
        folder = self.folder(section)
        os.makedirs(os.path.join(folder, "pulls"), exist_ok=True)
        con = sqlite3.connect(os.path.join(folder, "store.sqlite"))
        con.row_factory = sqlite3.Row
        con.executescript(SCHEMA)
        cols = {r[1] for r in con.execute("PRAGMA table_info(datasets)")}
        for c in ("content_sig", "notes"):
            if c not in cols:
                con.execute(f"ALTER TABLE datasets ADD COLUMN {c} TEXT")
        # upgrade pulls stored by v0.1: fingerprint them and give them the new names
        for d in con.execute("SELECT id, label, t_from, t_to FROM datasets WHERE content_sig IS NULL").fetchall():
            rows = con.execute("SELECT fno, date_a, date_b FROM fibres WHERE ds=?", (d["id"],)).fetchall()
            sig = content_sig({r["fno"]: {"date": r["date_a"]} for r in rows},
                              {r["fno"]: {"date": r["date_b"]} for r in rows})
            label = d["label"]
            if re.match(r"^Pull \d{1,2} \w{3} \d{4}$", label or ""):
                label = tested_label(d["t_from"], d["t_to"])
            con.execute("UPDATE datasets SET content_sig=?, label=?, notes=COALESCE(notes,'{}') WHERE id=?",
                        (sig, label, d["id"]))
        con.commit()
        return con

    def find_same(self, section, sig):
        if not os.path.exists(os.path.join(self.folder(section), "store.sqlite")):
            return None
        con = self.db(section)
        r = con.execute("SELECT id, label FROM datasets WHERE content_sig=?", (sig,)).fetchone()
        return dict(r) if r else None

    # ---------------------------------------------------------------- import
    def import_workbook(self, path, fms_dir, label="", log=print):
        log(f"Reading {os.path.basename(path)} ...")
        data = read_pull_workbook(path)
        section = data["section"]
        digest = sha1_of(path)
        sig = content_sig(data["rawA"], data["rawB"])
        same = self.find_same(section, sig)
        if same:
            log(f"  same results as {same['label']}, already in the store")
            return {"section": section, "ds": same["id"], "new": False, "same_as": same["label"]}
        keep = os.path.join(self.folder(section), "pulls", os.path.basename(path))
        os.makedirs(os.path.dirname(keep), exist_ok=True)
        if os.path.abspath(keep) != os.path.abspath(path):
            if os.path.exists(keep) and sha1_of(keep) != digest:
                stem, ext = os.path.splitext(keep)
                keep = f"{stem}_{digest[:8]}{ext}"
            if not os.path.exists(keep):
                shutil.copy2(path, keep)
        ds = self.store_data(data, os.path.basename(keep), digest, sig, fms_dir, label, log)
        return {"section": section, "ds": ds, "new": True}

    def store_data(self, data, source, digest, sig, fms_dir, label="", log=print, notes=None):
        section = data["section"]
        con = self.db(section)
        lens = [v["len"] for v in data["rawA"].values() if v.get("len")]
        cable_len = statistics.median(lens) if lens else None
        mid = [e for e in data["events"] if e["position"] is not None and e["position"] > END_MARGIN_M
               and (cable_len is None or e["position"] < cable_len - END_MARGIN_M)
               and e["type"] in ("Splice", "Group", "Connector", "Macrobend")]
        schedule, kinds = name_locations(mid, data["cable"], data["site_a"], fms_dir, log=log)
        label = label or tested_label(data["t_from"], data["t_to"])
        cur = con.execute(
            "INSERT INTO datasets(label,source,sha1,imported_at,section,cable,site_a,site_b,"
            "t_from,t_to,n_fibres,n_events,schedule,content_sig,notes) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (label, source, digest, time.strftime("%Y-%m-%d %H:%M"), section,
             data["cable"], data["site_a"], data["site_b"], data["t_from"], data["t_to"],
             len(set(data["rawA"]) | set(data["rawB"])), len(data["events"]), schedule, sig,
             json.dumps(notes or {})))
        ds = cur.lastrowid
        rows = []
        for n in sorted(set(data["rawA"]) | set(data["rawB"])):
            a, b = data["rawA"].get(n, {}), data["rawB"].get(n, {})
            rows.append((ds, n, a.get("fid") or b.get("fid"), ribbon_of(n), a.get("date"),
                         b.get("date"), a.get("len"), b.get("len"), a.get("l1310"),
                         a.get("l1550"), a.get("l1625"), b.get("l1310"), b.get("l1550"),
                         b.get("l1625")))
        con.executemany("INSERT INTO fibres VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.executemany("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)", [
            (ds, e["fno"], ribbon_of(e["fno"]), e["position"], e["type"], e["ab"], e["ba"],
             e["dirs"], e.get("location")) for e in data["events"]])
        groups = {}
        for e in mid:
            if e.get("location"):
                groups.setdefault(e["location"], []).append(e)
        order = sorted(groups, key=lambda k: statistics.median(x["position"] for x in groups[k]))
        con.executemany("INSERT INTO locations VALUES(?,?,?,?,?,?,?)", [
            (ds, k, i, statistics.median(x["position"] for x in groups[k]),
             kinds.get(k, ""), len({x["fno"] for x in groups[k]}),
             0 if any(x.get("unscheduled") for x in groups[k]) else 1)
            for i, k in enumerate(order)])
        con.commit()
        log(f"  stored: {len(rows)} fibres, {len(data['events'])} events, {len(order)} locations")
        return ds

    # ---------------------------------------------------------------- analysis
    def analysis(self, section, ds, st):
        """Fibre groups, spurs and the per location limit for one pull."""
        con = self.db(section)
        fibres = [dict(r) for r in con.execute("SELECT * FROM fibres WHERE ds=? ORDER BY fno", (ds,))]
        locs = [dict(r) for r in con.execute("SELECT * FROM locations WHERE ds=? ORDER BY ord", (ds,))]
        loc_f = {}
        for r in con.execute("SELECT fno, loc FROM events WHERE ds=? AND loc IS NOT NULL", (ds,)):
            loc_f.setdefault(r["loc"], set()).add(r["fno"])
        groups = refine_groups(fibre_groups(fibres, float(st.get("group_tol", 100))), loc_f)
        spurs = detect_spurs(locs, loc_f, groups, len(fibres), float(st.get("spur_gap", 1500)))
        spur_of = {l: sp["name"] for sp in spurs for l in sp["locations"]}
        nr = parse_ribbons(st.get("nr_ribbons", ""))
        for l in locs:
            l["spur"] = spur_of.get(l["loc"])
            l["sodf"] = is_sodf(l.get("kind"))
            l["fnos"] = loc_f.get(l["loc"], set())
        return {"fibres": fibres, "locs": locs, "groups": groups, "spurs": spurs, "nr": nr}

    def _limit(self, loc_meta, ribbon, st, nr):
        if loc_meta and loc_meta.get("sodf") and (not nr or ribbon in nr):
            return float(st.get("sodf_limit", 0.30))
        return float(st["threshold"])

    def _groups(self, con, ds, extra="", args=()):
        evs = [dict(r) for r in con.execute(
            "SELECT fno, ribbon, loc, ab, ba, type, pos FROM events WHERE ds=? AND loc IS NOT NULL "
            "AND type IN ('Splice','Group')" + extra, (ds,) + tuple(args))]
        sizes = {r["ribbon"]: r["n"] for r in con.execute(
            "SELECT ribbon, COUNT(*) n FROM fibres WHERE ds=? GROUP BY ribbon", (ds,))}
        groups = {}
        for e in evs:
            groups.setdefault((e["ribbon"], e["loc"]), []).append(e)
        ravg = {k: ribbon_averages(g, sizes.get(k[0], FIBRES_PER_RIBBON)) for k, g in groups.items()}
        return groups, ravg

    # ---------------------------------------------------------------- reads
    def datasets(self, section):
        con = self.db(section)
        out = []
        for r in con.execute("SELECT * FROM datasets ORDER BY t_to DESC, id DESC"):
            d = dict(r)
            both = con.execute("SELECT SUM(date_a IS NOT NULL AND date_a<>'' AND date_b IS NOT NULL AND date_b<>''),"
                               " SUM((date_a IS NULL OR date_a='') <> (date_b IS NULL OR date_b=''))"
                               " FROM fibres WHERE ds=?", (d["id"],)).fetchone()
            d["both_ends"], d["one_end"] = both[0] or 0, both[1] or 0
            try:
                d["notes"] = json.loads(d.get("notes") or "{}")
            except ValueError:
                d["notes"] = {}
            out.append(d)
        return out

    def summary(self, section, ds, st, group=None):
        con = self.db(section)
        info = dict(con.execute("SELECT * FROM datasets WHERE id=?", (ds,)).fetchone())
        an = self.analysis(section, ds, st)
        gsel = next((g for g in an["groups"] if g["id"] == group), None)
        keep = gsel["fnos"] if gsel else None
        fibres = [f for f in an["fibres"] if keep is None or f["fno"] in keep]
        meta = {l["loc"]: l for l in an["locs"]}
        groups, ravg = self._groups(con, ds)
        cells = {}
        counts = {"splices": 0, "both": 0, "filled": 0, "filled_ribbon": 0, "fail": 0,
                  "fail_both": 0, "fail_filled": 0, "fail_sodf": 0, "pass": 0, "pass_low": 0,
                  "gainers": 0, "one_way_left_out": 0, "one_way_high": 0, "spur_fail": 0}
        for key, grp in groups.items():
            m = meta.get(key[1])
            for e in grp:
                if keep is not None and e["fno"] not in keep:
                    continue
                v, how, gain = splice_value(e["ab"], e["ba"], st, *ravg[key])
                if v is None:
                    continue
                lim = self._limit(m, key[0], st, an["nr"])
                c = cells.setdefault(key, {"ribbon": key[0], "loc": key[1], "worst": None, "worst_how": None,
                                           "n": 0, "fail": 0, "filled": 0, "gainer": 0, "one_way": 0,
                                           "one_way_high": 0, "limit": lim})
                counts["splices"] += 1
                c["n"] += 1
                if how == "one_way":
                    counts["one_way_left_out"] += 1
                    c["one_way"] += 1
                    if v >= lim:
                        counts["one_way_high"] += 1
                        c["one_way_high"] += 1
                    continue
                if how == "both":
                    counts["both"] += 1
                    counts["gainers"] += gain
                    c["gainer"] += gain
                else:
                    counts["filled"] += 1
                    counts["filled_ribbon"] += how == "ribbon"
                    c["filled"] += 1
                if v >= lim:
                    counts["fail"] += 1
                    c["fail"] += 1
                    counts["fail_both" if how == "both" else "fail_filled"] += 1
                    if m and m.get("spur"):
                        counts["spur_fail"] += 1
                else:
                    counts["pass"] += 1
                    counts["pass_low"] += v < 0.10
                    if m and m.get("sodf") and lim > float(st["threshold"]) and v >= float(st["threshold"]):
                        counts["fail_sodf"] += 1      # would fail at the single splice limit
                if c["worst"] is None or v > c["worst"]:
                    c["worst"], c["worst_how"] = round(v, 3), how
        nr_suggest = suggest_nr(groups, meta)
        e2e = [((f["a1550"] or 0) + (f["b1550"] or 0)) / 2 for f in fibres
               if f["a1550"] is not None and f["b1550"] is not None]
        nmin = max(3, round(len(fibres) * 0.05))
        locs = []
        for l in an["locs"]:
            n = len(l["fnos"] & keep) if keep is not None else len(l["fnos"])
            if n == 0:
                continue
            locs.append({k: v for k, v in l.items() if k != "fnos"} | {"n_group": n, "minor": n < nmin})
        return {"dataset": info, "locations": locs,
                "ribbons": sorted({f["ribbon"] for f in fibres}),
                "fibres_tested": len(fibres), "both_ends": sum(1 for f in fibres if f["date_a"] and f["date_b"]),
                "counts": counts, "settings": st,
                "e2e_avg": round(sum(e2e) / len(e2e), 3) if e2e else None,
                "cells": list(cells.values()),
                "groups": [{k: v for k, v in g.items() if k != "fnos"} for g in an["groups"]],
                "group": group if gsel else None,
                "nr_suggest": nr_suggest,
                "spurs": [sp | {"fail": sum(c["fail"] for c in cells.values() if c["loc"] in sp["locations"])}
                          for sp in an["spurs"]]}

    def location(self, section, ds, loc, ribbon, st):
        con = self.db(section)
        an = self.analysis(section, ds, st)
        meta = next((l for l in an["locs"] if l["loc"] == loc), None)
        q = ("SELECT e.fno, e.ribbon, e.pos, e.type, e.ab, e.ba, f.fid FROM events e "
             "JOIN fibres f ON f.ds=e.ds AND f.fno=e.fno WHERE e.ds=? AND e.loc=?")
        args = [ds, loc]
        if ribbon:
            q += " AND e.ribbon=?"
            args.append(int(ribbon))
        raw = [dict(r) for r in con.execute(q + " ORDER BY e.fno, e.pos", args)]
        _, ravg = self._groups(con, ds, " AND loc=?", (loc,))
        rows = []
        for r in raw:
            sp = r["type"] in SPLICE_TYPES
            v, how, gain = splice_value(r["ab"], r["ba"], st, *ravg.get((r["ribbon"], loc), (None, None)))
            lim = self._limit(meta, r["ribbon"], st, an["nr"])
            rows.append({"fno": r["fno"], "fid": r["fid"], "ribbon": r["ribbon"], "pos": r["pos"],
                         "type": r["type"], "ab": r["ab"], "ba": r["ba"], "limit": lim,
                         "value": None if v is None else round(v, 3), "how": how, "gainer": gain,
                         "splice": sp, "fail": bool(sp and how not in ("one_way", None) and v >= lim)})
        tested = sum(1 for f in an["fibres"] if not ribbon or f["ribbon"] == int(ribbon))
        loc_out = {k: v for k, v in meta.items() if k != "fnos"} if meta else {"loc": loc}
        return {"location": loc_out, "rows": rows, "tested": tested, "settings": st}

    def ribbon(self, section, ds, ribbon, st):
        con = self.db(section)
        ribbon = int(ribbon)
        an = self.analysis(section, ds, st)
        meta = {l["loc"]: l for l in an["locs"]}
        fibres = [f for f in an["fibres"] if f["ribbon"] == ribbon]
        fn = {f["fno"] for f in fibres}
        locs = [{k: v for k, v in l.items() if k != "fnos"} for l in an["locs"] if l["fnos"] & fn]
        groups, ravg = self._groups(con, ds, " AND ribbon=?", (ribbon,))
        cells = {}
        for key, grp in groups.items():
            lim = self._limit(meta.get(key[1]), ribbon, st, an["nr"])
            for e in grp:
                v, how, gain = splice_value(e["ab"], e["ba"], st, *ravg[key])
                if v is None:
                    continue
                k = f"{e['fno']}|{e['loc']}"
                rank = -1 if how == "one_way" else v
                if k not in cells or rank > cells[k]["_rank"]:
                    cells[k] = {"value": round(v, 3), "ab": e["ab"], "ba": e["ba"], "how": how,
                                "gainer": gain, "limit": lim, "_rank": rank}
        for c in cells.values():
            c.pop("_rank", None)
        return {"ribbon": ribbon, "fibres": fibres, "locations": locs, "cells": cells, "settings": st}


    # ---------------------------------------------------------------- snapshots and compare
    def set_dataset(self, section, ds, label=None, baseline=None):
        """Rename a pull or mark it as the baseline. Results themselves are never changed."""
        con = self.db(section)
        row = con.execute("SELECT notes FROM datasets WHERE id=?", (ds,)).fetchone()
        if not row:
            raise KeyError("dataset")
        if label is not None and str(label).strip():
            con.execute("UPDATE datasets SET label=? WHERE id=?", (str(label).strip()[:80], ds))
        if baseline is not None:
            for r in con.execute("SELECT id, notes FROM datasets").fetchall():
                try:
                    n = json.loads(r["notes"] or "{}")
                except ValueError:
                    n = {}
                want = bool(baseline) and r["id"] == ds
                if bool(n.get("baseline")) != want:
                    n["baseline"] = want
                    con.execute("UPDATE datasets SET notes=? WHERE id=?", (json.dumps(n), r["id"]))
        con.commit()
        return self.datasets(section)

    def judged(self, section, ds, st):
        """Every splice judged in one pull, worst per fibre per location."""
        con = self.db(section)
        an = self.analysis(section, ds, st)
        meta = {l["loc"]: l for l in an["locs"]}
        groups, ravg = self._groups(con, ds)
        out = {}
        for key, grp in groups.items():
            m = meta.get(key[1])
            lim = self._limit(m, key[0], st, an["nr"])
            for e in grp:
                v, how, gain = splice_value(e["ab"], e["ba"], st, *ravg[key])
                if v is None or how == "one_way":
                    continue
                k = (e["fno"], e["loc"])
                if k not in out or v > out[k]["value"]:
                    out[k] = {"fno": e["fno"], "ribbon": e["ribbon"], "loc": e["loc"], "pos": e["pos"],
                              "value": round(v, 3), "how": how, "ab": e["ab"], "ba": e["ba"],
                              "limit": lim, "fail": v >= lim}
        return out, an

    def compare(self, section, base, new, st, group=None):
        """What changed between two pulls: solved, remaining, new and not seen now."""
        con = self.db(section)
        info = {d["id"]: d for d in self.datasets(section)}
        if base not in info or new not in info:
            raise KeyError("dataset")
        jb, anb = self.judged(section, base, st)
        jn, ann = self.judged(section, new, st)
        gsel = next((g for g in ann["groups"] if g["id"] == group), None)
        keep = gsel["fnos"] if gsel else None
        meta = {l["loc"]: l for l in anb["locs"]}
        meta.update({l["loc"]: l for l in ann["locs"]})
        by_fno = {}
        for r in jn.values():
            by_fno.setdefault(r["fno"], []).append(r)
        used, rows = set(), []

        def match(r):
            k = (r["fno"], r["loc"])
            if k in jn:
                return jn[k]
            near = [x for x in by_fno.get(r["fno"], []) if x["pos"] is not None and r["pos"] is not None
                    and abs(x["pos"] - r["pos"]) <= MATCH_TOL_M and (x["fno"], x["loc"]) not in used]
            return min(near, key=lambda x: abs(x["pos"] - r["pos"])) if near else None

        for r in jb.values():
            if keep is not None and r["fno"] not in keep:
                continue
            m = match(r)
            if m:
                used.add((m["fno"], m["loc"]))
            if not r["fail"] and not (m and m["fail"]):
                continue
            if r["fail"] and m is None:
                status = "not_seen"
            elif r["fail"] and m["fail"]:
                status = "remaining"
            elif r["fail"]:
                status = "solved"
            else:
                status = "new"
            rows.append(self._crow(r, m, status, meta))
        for k, m in jn.items():
            if k in used or not m["fail"] or (keep is not None and m["fno"] not in keep):
                continue
            rows.append(self._crow(None, m, "new", meta))
        statuses = ("solved", "remaining", "new", "not_seen")
        totals = {s: sum(1 for r in rows if r["status"] == s) for s in statuses}
        totals["fail_before"] = sum(1 for r in jb.values() if r["fail"] and (keep is None or r["fno"] in keep))
        totals["fail_now"] = sum(1 for r in jn.values() if r["fail"] and (keep is None or r["fno"] in keep))
        locs = {}
        for r in rows:
            L = locs.setdefault(r["loc"], {"loc": r["loc"], "pos": r["loc_pos"], "spur": r["spur"],
                                           "sodf": r["sodf"], **{s: 0 for s in statuses}})
            L[r["status"]] += 1
        ribs = {}
        for r in rows:
            R = ribs.setdefault(r["ribbon"], {"ribbon": r["ribbon"], **{s: 0 for s in statuses}})
            R[r["status"]] += 1
        rows.sort(key=lambda r: (statuses.index(r["status"]), r["loc_pos"] if r["loc_pos"] is not None else 9e9, r["fno"]))
        return {"base": info[base], "new": info[new], "settings": st, "totals": totals,
                "locations": sorted(locs.values(), key=lambda x: x["pos"] if x["pos"] is not None else 9e9),
                "ribbons": sorted(ribs.values(), key=lambda x: x["ribbon"]),
                "rows": rows,
                "groups": [{k: v for k, v in g.items() if k != "fnos"} for g in ann["groups"]],
                "group": group if gsel else None,
                "spurs": [sp["name"] for sp in ann["spurs"]]}

    @staticmethod
    def _crow(b, n, status, meta):
        r = n or b
        loc = r["loc"]
        m = meta.get(loc) or {}
        return {"fno": r["fno"], "ribbon": r["ribbon"], "loc": loc,
                "loc_pos": m.get("pos", r["pos"]), "spur": m.get("spur"), "sodf": bool(m.get("sodf")),
                "status": status,
                "before": b["value"] if b else None, "before_how": b["how"] if b else None,
                "now": n["value"] if n else None, "now_how": n["how"] if n else None,
                "limit": r["limit"],
                "change": round(n["value"] - b["value"], 3) if (b and n) else None}


# --------------------------------------------------------------------------- analysis helpers
def parse_ribbons(spec):
    out = set()
    for part in str(spec or "").replace(" ", "").split(","):
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                out.update(range(int(a), int(b) + 1))
            else:
                out.add(int(part))
        except ValueError:
            pass
    return out


def suggest_nr(groups, meta, min_gap=0.02):
    """Which ribbons carry two splices at the S-ODFs: average both end loss per ribbon at
    every S-ODF, then split the ribbons at the biggest step between those averages."""
    per = {}
    for (rb, loc), grp in groups.items():
        if not (meta.get(loc) or {}).get("sodf"):
            continue
        for e in grp:
            if e["ab"] is not None and e["ba"] is not None:
                per.setdefault(rb, []).append((e["ab"] + e["ba"]) / 2)
    means = {rb: sum(v) / len(v) for rb, v in per.items() if len(v) >= 3}
    if len(means) < 4:
        return None
    order = sorted(means)                       # NR ribbons sit in one run, so split by ribbon order
    vals = [means[r] for r in order]
    tot = sum(vals)
    best = None
    for k in range(1, len(order)):
        lo, hi = vals[:k], vals[k:]
        ml, mh = sum(lo) / len(lo), (tot - sum(lo)) / len(hi)
        score = len(lo) * len(hi) * (ml - mh) ** 2           # between class variance
        if best is None or score > best[0]:
            best = (score, k, ml, mh)
    _, k, ml, mh = best
    first = ml > mh
    high = order[:k] if first else order[k:]
    diff = abs(ml - mh)
    out = {"diff": round(diff, 3), "high_avg": round(max(ml, mh), 3), "low_avg": round(min(ml, mh), 3),
           "means": {r: round(m, 3) for r, m in sorted(means.items())}}
    if diff < min_gap:
        return out | {"spec": "", "ribbons": []}
    if first and high[0] == order[0]:
        high = list(range(1, high[-1] + 1))   # ribbons before the first S-ODF reading belong with the run
    return out | {"spec": ribbon_span(high).replace("R", "").replace(" ", ""), "ribbons": high}


def is_sodf(kind):
    return bool(kind) and re.sub(r"[^A-Z]", "", str(kind).upper()) == "SODF"


def ribbon_span(ribbons):
    rs = sorted(ribbons)
    if not rs:
        return ""
    parts, start, prev = [], rs[0], rs[0]
    for r in rs[1:] + [None]:
        if r is not None and r == prev + 1:
            prev = r
            continue
        parts.append(f"R{start}" if start == prev else f"R{start}-{prev}")
        if r is not None:
            start = prev = r
    return ", ".join(parts)


def fibre_groups(fibres, tol=100.0):
    """Group fibres by measured length: fibres whose lengths chain within tol metres
    take the same route. A fibre with no length joins the group of its ribbon."""
    pts = sorted(((f["len_a"] or f["len_b"] or 0), f["fno"]) for f in fibres if (f["len_a"] or f["len_b"]))
    runs, cur = [], []
    for p in pts:
        if cur and p[0] - cur[-1][0] > tol:
            runs.append(cur); cur = []
        cur.append(p)
    if cur:
        runs.append(cur)
    big = [r for r in runs if len(r) >= 3]
    small = [p for r in runs if len(r) < 3 for p in r]
    groups = [{"fnos": {f for _, f in r}, "km": statistics.median(x for x, _ in r) / 1000.0} for r in big]
    by_ribbon = {}
    for g in groups:
        for f in g["fnos"]:
            by_ribbon.setdefault(ribbon_of(f), []).append(g)
    lost = [f["fno"] for f in fibres if not (f["len_a"] or f["len_b"])] + [f for _, f in small]
    for f in lost:
        cand = by_ribbon.get(ribbon_of(f)) or groups
        if cand:
            max(cand, key=lambda g: len(g["fnos"]))["fnos"].add(f)
    groups.sort(key=lambda g: g["km"])
    for i, g in enumerate(groups, 1):
        rb = {ribbon_of(f) for f in g["fnos"]}
        g.update(id=f"G{i}", n=len(g["fnos"]), ribbons=ribbon_span(rb), ribbon_list=sorted(rb),
                 label=f"Group {i} · {g['km']:.1f} km · {ribbon_span(rb)} ({len(g['fnos'])} fibres)")
    return groups


def refine_groups(groups, loc_f, hi=0.8, lo=0.2, min_joints=2):
    """Second pass on the length groups: split a group when some of its ribbons clearly go
    through joints the others clearly do not (seen by at least 80% of one ribbon's fibres and
    at most 20% of the other's, on at least 2 joints). Single missing splices are normal, as low
    loss splices are not always detected, so they never split a group."""
    out = []
    for g in groups:
        by_rib = {}
        for f in g["fnos"]:
            by_rib.setdefault(ribbon_of(f), set()).add(f)
        ribs = sorted(by_rib)
        frac = {r: {loc: len(fs & by_rib[r]) / len(by_rib[r]) for loc, fs in loc_f.items()} for r in ribs}

        def differ(a, b):
            return [l for l in loc_f if (frac[a][l] >= hi and frac[b][l] <= lo) or (frac[b][l] >= hi and frac[a][l] <= lo)]
        parent = {r: r for r in ribs}

        def find(r):
            while parent[r] != r:
                parent[r] = parent[parent[r]]
                r = parent[r]
            return r
        for i, a in enumerate(ribs):
            for b in ribs[i + 1:]:
                if len(differ(a, b)) < min_joints:
                    parent[find(a)] = find(b)
        comps = {}
        for r in ribs:
            comps.setdefault(find(r), []).append(r)
        parts = sorted(comps.values(), key=lambda x: x[0])
        if len(parts) == 1:
            out.append(g | {"route": "confirmed by splice locations"})
            continue
        for k, rs in enumerate(parts):
            fn = set().union(*(by_rib[r] for r in rs))
            d = differ(rs[0], next(x for p in parts if p is not rs for x in p))
            sub = g | {"id": f"{g['id']}{'abcdefghij'[k]}", "fnos": fn, "n": len(fn), "ribbon_list": rs,
                       "ribbons": ribbon_span(rs), "route": f"split by joints: {', '.join(d[:3])}"}
            sub["label"] = f"Group {sub['id'][1:]} · {g['km']:.1f} km · {sub['ribbons']} ({len(fn)} fibres)"
            out.append(sub)
    return out


def detect_spurs(locs, loc_f, groups, total, gap=1500.0):
    """A spur shows as a run of locations, a short distance apart, seen almost only by a
    small group of fibres (the ribbons that leave the main line to a REB and come back).
    Small clusters inside the run are allowed; a location most fibres see ends it."""
    gid = {f: g["id"] for g in groups for f in g["fnos"]}
    gsize = {g["id"]: len(g["fnos"]) for g in groups}
    marks = []
    for l in locs:
        f = [x for x in loc_f.get(l["loc"], ()) if x in gid]
        if len(f) < 3:
            marks.append(("minor", None)); continue
        cnt = {}
        for x in f:
            cnt[gid[x]] = cnt.get(gid[x], 0) + 1
        g, n = max(cnt.items(), key=lambda kv: kv[1])
        if n / len(f) >= 0.8 and gsize[g] <= total / 3 and len(f) >= 0.25 * gsize[g]:
            marks.append(("spur", g))
        elif len(f) < 0.10 * total:
            marks.append(("minor", None))
        else:
            marks.append(("main", None))
    spurs, run = [], []

    def close():
        while run and run[-1][1] != "spur":
            run.pop()
        if run:
            members = [l for l, k, _ in run if k == "spur"]
            odf = next((l for l in members if (l.get("kind") or "").upper().endswith("ODF")), None)
            name = (odf["loc"] if odf else members[0]["loc"]) + " spur"
            spurs.append({"name": name, "group": run[0][2], "locations": [l["loc"] for l, _, _ in run],
                          "start": run[0][0]["pos"], "end": run[-1][0]["pos"]})
    for l, (kind, g) in zip(locs, marks):
        if kind == "spur":
            if run and (g != run[0][2] or l["pos"] - run[-1][0]["pos"] > gap):
                close(); run = []
            run.append((l, "spur", g))
        elif kind == "minor" and run and l["pos"] - run[-1][0]["pos"] <= gap:
            run.append((l, "minor", run[0][2]))
        else:
            close(); run = []
    close()
    return spurs
