"""
break_locator.py  -  where along the route a DIS fibre stops (v21).

A DIS fibre gives no live refusal, so the far RTU runs a full OTDR on it and FMS returns a
link length: the distance from that RTU to the fibre's open end. Compared with the route's
joint schedule (Distances.xlsx, route_schedules.json) it names the joint or ODF where the
fibre stops.

  near end   length within END_TOL_M of 0                -> the test RTU's own ODF or patch
  far end    length within END_TOL_M of the route total  -> the far ODF or patch (tone end)
  otherwise  the nearest scheduled location, or "between X and Y"
"""
from __future__ import annotations

import json
import os
import re

END_TOL_M = 150
NEAR_TOL_M = 150

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "route_schedules.json")
try:
    SCHED = json.load(open(_PATH)).get("cables", {})
except Exception:                                    # noqa: BLE001
    SCHED = {}


def site_of(rtu_name: str) -> str:
    """RTU2-SNBC-1991132 -> SNBC, RTU2-RGAC2-1991133 -> RGAC2"""
    m = re.match(r"^RTU\d*-([A-Z0-9]+)-", str(rtu_name or ""), re.I)
    return m.group(1).upper() if m else str(rtu_name or "")


def cable_key(stem: str) -> str:
    """F-RGAC-SNBC-A-R432 -> F-RGAC-SNBC"""
    m = re.match(r"^(F-[A-Z0-9]+-[A-Z0-9]+)", str(stem or ""), re.I)
    return m.group(1).upper() if m else str(stem or "")


def _group(sheet: dict, fibre: int) -> str | None:
    for g in sheet.get("groups", []):
        if g["from"] <= fibre <= g["to"]:
            return g["label"]
    return None


def locate(stem: str, test_rtu: str, fibre: int, dist_m: float) -> dict:
    site = site_of(test_rtu)
    out = {"distM": round(dist_m), "from": site}
    sheet = (SCHED.get(cable_key(stem)) or {}).get(site)
    if dist_m <= END_TOL_M:
        out.update(where="near end", text=f"stops within {round(dist_m)} m of {site}: {site} ODF or patch")
        return out
    if not sheet:
        out.update(where="unscheduled", text=f"stops about {dist_m/1000:.2f} km from {site} (no joint schedule for this cable)")
        return out
    grp = _group(sheet, fibre)
    pts = sorted([(l["dist"][grp], l["name"]) for l in sheet["locations"] if grp and grp in l["dist"]])
    if not pts:
        out.update(where="unscheduled", text=f"stops about {dist_m/1000:.2f} km from {site}")
        return out
    total = pts[-1][0]
    out["totalM"] = round(total)
    if dist_m >= total - END_TOL_M:
        out.update(where="far end", near=pts[-1][1],
                   text=f"runs the full {total/1000:.1f} km: open at the far end ({sheet.get('to')} ODF or patch)")
        return out
    near = min(pts, key=lambda p: abs(p[0] - dist_m))
    off = round(dist_m - near[0])
    if abs(off) <= NEAR_TOL_M:
        out.update(where="at joint", near=near[1], offsetM=off,
                   text=f"stops about {dist_m/1000:.2f} km from {site}, at {near[1]} ({'+' if off >= 0 else ''}{off} m)")
        return out
    before = max((p for p in pts if p[0] <= dist_m), default=pts[0])
    after = min((p for p in pts if p[0] > dist_m), default=pts[-1])
    out.update(where="between", near=before[1], after=after[1], offsetM=round(dist_m - before[0]),
               text=f"stops about {dist_m/1000:.2f} km from {site}, between {before[1]} and {after[1]} "
                    f"({round(dist_m - before[0])} m past {before[1]})")
    return out
