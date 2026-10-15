"""Brunel 1.0: learned locations and the asset inventory per cable.

Instead of typing in the distance of every joint, the report learns them from the test events. A real joint
shows up as an event on many fibres at the same distance; each such cluster becomes a location. The first time
one appears it is "Unnamed" and flagged on the Locations screen; someone names it from the design once and every
later report uses the name. Each report refines the learned distance and tightens the match window.

Pure functions, no network: the report job (report_job.py) learns from a run, the relay (relay_assets.py) keeps
the state per cable in the log repo and applies names, splits and settings.

State, one per cable (config/learned/<F-XXXX-YYYY>.json):
  {"cable", "settings": {"mode": "learned"|"schedule", "minShare": 0.2, "tol": 10.0},
   "locations": [{"id", "name", "type", "chainage", "distance_m", "tolerance_m", "fibre_count", "tests_seen",
                  "status": "unnamed"|"named"|"notjoint"|"split", "named_by", "named_at", "first_seen",
                  "last_seen", "history": [[date, distance, fibres]], "stats": {...}, "parent", "children"}],
   "buildEnd": {"distance_m", "at", "fibres", "complete"}, "runs": [...], "audit": [...]}
Distances are optical distance from End A (the easterly end), metres.
"""
from __future__ import annotations

import copy
import math
import statistics
import time
import uuid

TYPES = ["Joint", "ODF", "Spur", "REB", "Backsplice", "Other"]
SPLICE_TYPES = ("Splice", "Group")
END_MARGIN_M = 60.0          # launch dead zone and end of fibre: not field joints
CHAIN_M = 10.0               # events chained within this form one cluster (the client's Hydra length)
MIN_TOL_M = 5.0              # the match window never tightens below this
MIN_FIBRES = 3               # a cluster needs at least this many fibres to be a joint
LEN_SAME = 0.02              # fibres within 2 % of the median length are scaled to it


def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime())


def new_id() -> str:
    return "L" + uuid.uuid4().hex[:8]


def new_state(cable: str, mode: str = "learned") -> dict:
    return {"cable": cable, "version": 1, "settings": {"mode": mode, "minShare": 0.2, "tol": 10.0},
            "locations": [], "buildEnd": None, "runs": [], "audit": []}


def fix_state(st: dict | None, cable: str) -> dict:
    """Fill in anything missing, so an older or hand edited file still loads."""
    base = new_state(cable)
    if not isinstance(st, dict):
        return base
    for k, v in base.items():
        st.setdefault(k, copy.deepcopy(v))
    for k, v in base["settings"].items():
        st["settings"].setdefault(k, v)
    for loc in st["locations"]:
        loc.setdefault("name", "")
        loc.setdefault("type", "")
        loc.setdefault("chainage", None)
        loc.setdefault("status", "named" if loc.get("name") else "unnamed")
        loc.setdefault("history", [])
        loc.setdefault("stats", {})
        loc.setdefault("tests_seen", len(loc["history"]))
        loc.setdefault("tolerance_m", st["settings"]["tol"])
    return st


def active(st: dict) -> list[dict]:
    """Locations events can match: everything except a location that has been split in two."""
    return [x for x in st["locations"] if x.get("status") != "split"]


def label(loc: dict) -> str:
    if loc.get("name"):
        return loc["name"]
    return f"Unnamed {loc['distance_m']:,.0f} m"


def _audit(st: dict, by: str, what: str):
    st["audit"].append({"at": now_iso(), "by": by or "", "what": what})
    del st["audit"][:-300]


# ───────────────────────────── learning from one report ─────────────────────────────
class Frame:
    """Puts each fibre's event positions on one scale: fibres whose length is within 2 % of the median are
    scaled to the median length, so small length differences inside the cable do not smear the clusters.
    A fibre much shorter or longer (still being built, or broken) is left as measured."""

    def __init__(self, lengths: dict):
        ls = [v for v in lengths.values() if v]
        self.med = statistics.median(ls) if ls else None
        self.k = {}
        for fid, ln in lengths.items():
            if ln and self.med and abs(ln - self.med) <= LEN_SAME * self.med:
                self.k[fid] = self.med / ln
            else:
                self.k[fid] = 1.0
        self.len = dict(lengths)

    def to_common(self, fid, pos):
        return pos * self.k.get(fid, 1.0)

    def to_fibre(self, fid, d):
        return d / self.k.get(fid, 1.0)


def _is_end(pos, length, typ) -> bool:
    t = str(typ or "").lower()
    if any(x in t for x in ("launch", "end of fiber", "end of analysis")):
        return True
    if pos is None or pos < END_MARGIN_M:
        return True
    return bool(length and pos > length - END_MARGIN_M)


def _clusters(points):
    """points: [(common_pos, fid, event)] -> [{centre, fids, events, spread}] chained within CHAIN_M."""
    pts = sorted(points, key=lambda p: p[0])
    out, cur = [], []
    for p in pts:
        if cur and p[0] - cur[-1][0] > CHAIN_M:
            out.append(cur)
            cur = []
        cur.append(p)
    if cur:
        out.append(cur)
    res = []
    for cl in out:
        ds = [p[0] for p in cl]
        res.append({"centre": statistics.median(ds), "fids": {p[1] for p in cl}, "events": [p[2] for p in cl],
                    "spread": (max(ds) - min(ds)) if len(ds) > 1 else 0.0, "_d": ds, "_f": [p[1] for p in cl]})
    return res


def _divide(cls, locs):
    """A cluster that spans two known locations (for example two joints split apart on the Locations screen,
    closer together than the chain distance) is shared out, each event to its nearest location."""
    out = []
    for c in cls:
        ds = [fr_d for fr_d in c["_d"]]
        lo, hi = min(ds), max(ds)
        inside = [L for L in locs if lo - L["tolerance_m"] <= L["distance_m"] <= hi + L["tolerance_m"]]
        if len(inside) < 2:
            out.append(c)
            continue
        parts: dict[str, list] = {}
        for d, fid, e in zip(c["_d"], c["_f"], c["events"]):
            L = min(inside, key=lambda x: abs(x["distance_m"] - d))
            parts.setdefault(L["id"], []).append((d, fid, e))
        for pl in parts.values():
            out.extend(_clusters(pl))
    return out


def _loss(e, wl=1550, nominal=0.02):
    a, b = e.get(f"a{wl}"), e.get(f"b{wl}")
    if e.get("single"):
        return a if a is not None else b
    if a is None and b is None:
        return None
    return ((a if a is not None else nominal) + (b if b is not None else nominal)) / 2


def _ribbon(fid):
    import re
    m = re.search(r"(\d+)\s*$", str(fid or ""))
    return (int(m.group(1)) - 1) // 12 + 1 if m else None


def _tighten(loc: dict, start_tol: float):
    """The match window starts at the cable's starting tolerance and closes in on 3 standard deviations of
    the learned distance (plus 2 m) once a location has been seen 3 times. A location made by a split keeps
    its window under half the gap to its neighbour."""
    cap = loc.get("tol_cap") or start_tol
    floor = min(MIN_TOL_M, cap)
    ds = [h[1] for h in loc["history"][-12:]]
    if len(ds) >= 3:
        sd = statistics.pstdev(ds)
        loc["tolerance_m"] = round(max(floor, min(cap, 3 * sd + 2)), 1)
    else:
        loc["tolerance_m"] = round(cap, 1)


def _refine(loc: dict):
    """Learned distance = fibre weighted mean of the recent runs."""
    hs = loc["history"][-12:]
    w = sum(max(1, h[2]) for h in hs)
    loc["distance_m"] = round(sum(h[1] * max(1, h[2]) for h in hs) / w, 1) if w else loc["distance_m"]


def learn(st: dict, events: list, lengths: dict, *, report: str = "", two_ended: bool = False,
          when: str | None = None, threshold: float = 0.15, nominal: float = 0.02, wl: int = 1550) -> dict:
    """Learn from one report's events (positions from End A).

    events:  rows like fms_pull.all_events_table gives: fid, position, type, length, a1550, b1550, single
    lengths: {fid: fibre length in metres} for every fibre in the report (with or without events)
    Changes st in place and returns a summary. Run it once per report."""
    when = when or now_iso()
    s = st["settings"]
    start_tol = float(s.get("tol") or 10.0)
    fr = Frame(lengths)
    tested = len([f for f, v in lengths.items() if v]) or len({e.get("fid") for e in events})
    need = max(MIN_FIBRES, math.ceil(float(s.get("minShare") or 0.2) * tested))
    pts = []
    for e in events:
        pos, fid = e.get("position"), e.get("fid")
        if _is_end(pos, lengths.get(fid) or e.get("length"), e.get("type")):
            continue
        pts.append((fr.to_common(fid, pos), fid, e))
    cls = _divide(_clusters(pts), active(st))

    # closest pair first, one cluster per location; a smaller fragment within the window joins it
    locs = active(st)
    pairs = sorted((abs(c["centre"] - L["distance_m"]), ci, li) for ci, c in enumerate(cls)
                   for li, L in enumerate(locs) if abs(c["centre"] - L["distance_m"]) <= L["tolerance_m"])
    got: dict[int, list] = {}
    used_c, used_l = set(), set()
    for _, ci, li in pairs:
        if ci in used_c:
            continue
        if li in used_l:
            got[li].append(cls[ci])         # a fragment of the same joint
            used_c.add(ci)
            continue
        used_c.add(ci)
        used_l.add(li)
        got[li] = [cls[ci]]

    matched = 0
    for li, parts in got.items():
        L = locs[li]
        fids = set().union(*[p["fids"] for p in parts])
        ds = [d for p in parts for d in [p["centre"]] * len(p["fids"])]
        centre = statistics.median(ds)
        L["history"].append([when, round(centre, 1), len(fids)])
        del L["history"][:-30]
        _refine(L)
        _tighten(L, start_tol)
        L["fibre_count"] = len(fids)
        L["tests_seen"] = L.get("tests_seen", 0) + 1
        L["last_seen"] = when
        matched += 1

    new = []
    for ci, c in enumerate(cls):
        if ci in used_c or len(c["fids"]) < need:
            continue
        L = {"id": new_id(), "name": "", "type": "", "chainage": None, "distance_m": round(c["centre"], 1),
             "tolerance_m": start_tol, "fibre_count": len(c["fids"]), "tests_seen": 1, "status": "unnamed",
             "named_by": "", "named_at": "", "first_seen": when, "last_seen": when, "first_report": report,
             "history": [[when, round(c["centre"], 1), len(c["fids"])]], "stats": {}}
        st["locations"].append(L)
        new.append(L)
    st["locations"].sort(key=lambda x: x["distance_m"])

    # stats for the inventory, from this run's events at each location
    tag = tagger(st, lengths)
    per: dict[str, list] = {}
    for e in events:
        lid = tag(e.get("fid"), e.get("position"))
        if lid:
            per.setdefault(lid, []).append(e)
    for L in st["locations"]:
        evs = per.get(L["id"])
        if not evs:
            continue
        sp = [e for e in evs if e.get("type") in SPLICE_TYPES]
        vals = [v for v in (_loss(e, wl, nominal) for e in sp) if v is not None]
        L["stats"] = {"at": when, "report": report, "fibres": len({e.get("fid") for e in evs}),
                      "splices": len(sp), "ribbons": len({_ribbon(e.get("fid")) for e in evs} - {None}),
                      "avg": round(sum(vals) / len(vals), 3) if vals else None,
                      "worst": round(max(vals), 3) if vals else None,
                      "over": sum(1 for v in vals if v >= threshold), "threshold": threshold, "tested": tested}

    if fr.med:
        st["buildEnd"] = {"distance_m": round(fr.med, 1), "at": when, "fibres": tested, "complete": bool(two_ended)}
    summ = {"at": when, "report": report, "fibres": tested, "clusters": len(cls), "matched": matched,
            "new": len(new), "unnamed": sum(1 for x in st["locations"] if x["status"] == "unnamed"),
            "need": need, "newIds": [x["id"] for x in new]}
    st["runs"].append({k: v for k, v in summ.items() if k != "newIds"})
    del st["runs"][:-60]
    return summ


def tagger(st: dict, lengths: dict):
    """-> fn(fid, position) giving the location id an event belongs to, or None.
    A location marked Not a joint still claims its events (so they are not reported as a new joint again)
    but they carry no location name."""
    fr = Frame(lengths)
    locs = sorted(active(st), key=lambda x: x["distance_m"])

    def fn(fid, pos):
        if pos is None or _is_end(pos, lengths.get(fid), None):
            return None
        d = fr.to_common(fid, pos)
        best = None
        for L in locs:
            gap = abs(L["distance_m"] - d)
            if gap <= L["tolerance_m"] + 2 and (best is None or gap < best[0]):
                best = (gap, L["id"])
        return best[1] if best else None
    return fn


def apply_tags(st: dict, events: list, lengths: dict) -> int:
    """Name each event after its learned location (fields the report engine already reads)."""
    tag, fr, by = tagger(st, lengths), Frame(lengths), {x["id"]: x for x in st["locations"]}
    hits = 0
    for e in events:
        lid = tag(e.get("fid"), e.get("position"))
        if not lid:
            continue
        L = by[lid]
        e["locId"] = lid
        if L["status"] == "notjoint":
            continue
        e["location"] = label(L)
        e["sched_m"] = round(fr.to_fibre(e.get("fid"), L["distance_m"]), 1)
        e["delta_m"] = round(e["position"] - e["sched_m"], 1)
        e["leg"] = e.get("leg") or ""
        hits += 1
    return hits


def schedule(st: dict) -> list:
    """The learned locations in the report engine's schedule layout, so the cable views get the names too."""
    return [{"location": label(L), "leg": "", "dists": {(1, 9999): {"design": None, "actual": L["distance_m"]}}}
            for L in active(st) if L["status"] != "notjoint"]


# ───────────────────────────── changes made on the Locations screen ─────────────────────────────
def find(st: dict, lid: str) -> dict:
    for L in st["locations"]:
        if L["id"] == lid:
            return L
    raise KeyError("No such location")


def name(st: dict, lid: str, nm: str, typ: str = "", chainage=None, by: str = "") -> dict:
    L = find(st, lid)
    nm = " ".join(str(nm or "").split())[:80]
    if not nm:
        raise ValueError("Give it a name")
    if any(x is not L and x.get("name", "").lower() == nm.lower() and x["status"] != "split" for x in st["locations"]):
        raise ValueError(f"{nm} is already used on this cable")
    if typ and typ not in TYPES:
        raise ValueError("Unknown type")
    old = label(L)
    L.update(name=nm, type=typ or L.get("type") or "Joint", status="named", named_by=by, named_at=now_iso())
    if chainage not in (None, ""):
        L["chainage"] = round(float(chainage), 1)
    _audit(st, by, f"named {old} as {nm}")
    return L


def not_joint(st: dict, lid: str, by: str = "", undo: bool = False) -> dict:
    L = find(st, lid)
    if undo:
        L["status"] = "named" if L.get("name") else "unnamed"
        _audit(st, by, f"{label(L)} restored as a location")
    else:
        L["status"] = "notjoint"
        _audit(st, by, f"{label(L)} marked not a joint")
    return L


def split(st: dict, lid: str, at: list, by: str = "") -> list:
    """Two joints too close to tell apart become two locations at the distances given."""
    L = find(st, lid)
    if L["status"] == "split":
        raise ValueError("Already split")
    ds = sorted(round(float(x), 1) for x in at)
    if len(ds) != 2 or ds[1] - ds[0] < 2:
        raise ValueError("Give two distances at least 2 m apart")
    if any(abs(d - L["distance_m"]) > max(50.0, 3 * L["tolerance_m"]) for d in ds):
        raise ValueError("Both distances must be near the location being split")
    tol = round(max(1.0, min(L["tolerance_m"], (ds[1] - ds[0]) / 2 - 0.5)), 1)
    kids = []
    for d in ds:
        k = {"id": new_id(), "name": "", "type": "", "chainage": None, "distance_m": d, "tolerance_m": tol,
             "fibre_count": 0, "tests_seen": 0, "status": "unnamed", "named_by": "", "named_at": "",
             "first_seen": L.get("first_seen"), "last_seen": "", "history": [], "stats": {}, "parent": L["id"],
             "tol_cap": tol}
        kids.append(k)
        st["locations"].append(k)
    L["status"], L["children"] = "split", [k["id"] for k in kids]
    st["locations"].sort(key=lambda x: x["distance_m"])
    _audit(st, by, f"split {label(L)} into {ds[0]:,.0f} m and {ds[1]:,.0f} m")
    return kids


def set_settings(st: dict, by: str = "", **kw) -> dict:
    s = st["settings"]
    if "mode" in kw and kw["mode"] is not None:
        if kw["mode"] not in ("learned", "schedule"):
            raise ValueError("Mode is learned or schedule")
        s["mode"] = kw["mode"]
    if kw.get("minShare") is not None:
        v = float(kw["minShare"])
        if not 0.02 <= v <= 1:
            raise ValueError("Share of fibres is 2 % to 100 %")
        s["minShare"] = round(v, 2)
    if kw.get("tol") is not None:
        v = float(kw["tol"])
        if not 2 <= v <= 100:
            raise ValueError("Starting tolerance is 2 to 100 m")
        s["tol"] = round(v, 1)
    _audit(st, by, f"settings {s}")
    return s


def seed(st: dict, sched: list, by: str = "") -> int:
    """Start a cable's learned list from its Distances schedule: each scheduled location becomes a named one."""
    n = 0
    have = {x.get("name", "").lower() for x in st["locations"]}
    for row in sched or []:
        d = None
        for (lo, hi), kinds in (row.get("dists") or {}).items():
            if lo <= 1 <= hi:
                d = kinds.get("actual") if kinds.get("actual") is not None else kinds.get("design")
                break
        if d is None or not row.get("location") or row["location"].lower() in have:
            continue
        st["locations"].append({"id": new_id(), "name": row["location"], "type": "Joint", "chainage": None,
                                "distance_m": round(float(d), 1), "tolerance_m": st["settings"]["tol"],
                                "fibre_count": 0, "tests_seen": 0, "status": "named", "named_by": by,
                                "named_at": now_iso(), "first_seen": "", "last_seen": "", "history": [],
                                "stats": {}, "seeded": True})
        have.add(row["location"].lower())
        n += 1
    st["locations"].sort(key=lambda x: x["distance_m"])
    if n:
        _audit(st, by, f"started from the Distances schedule ({n} locations)")
    return n


MEASURED = ("distance_m", "tolerance_m", "fibre_count", "tests_seen", "first_seen", "last_seen", "history", "stats",
            "first_report")


def merge(repo: dict, run: dict) -> dict:
    """Put a finished report's learning into the stored state, which may have changed while the report ran
    (someone named or split a location). Names, types, status and settings always come from the stored state;
    measured values come from the run."""
    out = copy.deepcopy(repo)
    by = {x["id"]: x for x in out["locations"]}
    for L in run.get("locations", []):
        cur = by.get(L["id"])
        if cur is None:
            out["locations"].append(copy.deepcopy(L))
            continue
        for k in MEASURED:
            if k in L:
                cur[k] = copy.deepcopy(L[k])
    out["locations"].sort(key=lambda x: x["distance_m"])
    if run.get("buildEnd"):
        out["buildEnd"] = run["buildEnd"]
    seen = {(r.get("at"), r.get("report")) for r in out.get("runs", [])}
    for r in run.get("runs", []):
        if (r.get("at"), r.get("report")) not in seen:
            out["runs"].append(r)
    del out["runs"][:-60]
    return out


# ───────────────────────────── the asset inventory ─────────────────────────────
def inventory(st: dict) -> dict:
    """One row per asset in route order from End A, plus the build end."""
    rows = []
    for L in sorted(active(st), key=lambda x: x["distance_m"]):
        if L["status"] == "notjoint":
            continue
        s = L.get("stats") or {}
        rows.append({"id": L["id"], "asset": label(L), "named": L["status"] == "named", "type": L.get("type") or "",
                     "distance_m": L["distance_m"], "tolerance_m": L["tolerance_m"], "chainage": L.get("chainage"),
                     "first_seen": L.get("first_seen") or "", "last_seen": L.get("last_seen") or "",
                     "tests_seen": L.get("tests_seen", 0), "fibres": s.get("fibres"), "tested": s.get("tested"),
                     "splices": s.get("splices"), "ribbons": s.get("ribbons"), "avg": s.get("avg"),
                     "worst": s.get("worst"), "over": s.get("over"), "threshold": s.get("threshold", 0.15)})
    return {"cable": st["cable"], "rows": rows, "buildEnd": st.get("buildEnd"),
            "unnamed": sum(1 for x in st["locations"] if x["status"] == "unnamed"),
            "notjoint": sum(1 for x in st["locations"] if x["status"] == "notjoint")}
