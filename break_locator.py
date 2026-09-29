"""
break_locator.py  -  how far a DIS fibre runs before it stops (v21, distance only since v22).

A DIS fibre gives no live refusal, so the far RTU runs a full OTDR on it and FMS returns a
link length: the distance from that RTU to the fibre's open end. The report gives that
distance straight from FMS, measured from the RTU's site. No joint schedule is used, so
nothing needs updating when routes change.
"""
from __future__ import annotations

import re

APPROX_LIMIT_M = 55000      # 3 s OTDRs have read the far end of a 70 km fibre short (61-65 km seen on R1)


def site_of(rtu_name: str) -> str:
    """RTU2-SNBC-1991132 -> SNBC, RTU2-RGAC2-1991133 -> RGAC2"""
    m = re.match(r"^RTU\d*-([A-Z0-9]+)-", str(rtu_name or ""), re.I)
    return m.group(1).upper() if m else str(rtu_name or "")


def locate(stem: str, test_rtu: str, fibre: int, dist_m: float, approx: bool = False) -> dict:
    site = site_of(test_rtu)
    d = round(dist_m)
    text = f"stops {d} m from {site}" if d < 1000 else f"stops {dist_m / 1000:.2f} km from {site}"
    out = {"distM": d, "from": site, "text": text}
    if approx and dist_m > APPROX_LIMIT_M:
        out["text"] += " (quick 3 s reading; beyond about 55 km it can read short)"
        out["approx"] = True
    return out
