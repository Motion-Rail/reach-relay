"""Backbone alignment: put every fibre's events on one common distance scale.

Fibres that run through a spur are longer than the backbone: each spur adds a loop out to
the REB and back. Read raw, the same backbone joint sits at a different distance on those
fibres, so the joint does not line up with the rest of the cable.

Each fibre is mapped onto the backbone with its own offset at the joints most of the cable
shares (anchors). Between two anchors the offset either stays nearly the same (straight
cable, small differences are spread evenly) or jumps by the length of a spur loop. Where it
jumps, the loop is placed using the spur's own joints: the far end of the loop (the REB ODF)
sits half way round, so the loop starts half a loop before it. Every event inside the loop
is a spur event: it keeps the backbone distance of the junction and gets its distance along
the spur (out and back), so it shows only on the fibres that go through that spur.
"""
import statistics

ALIGN_V = 1
STEP_M = 40.0          # an offset change bigger than this between two anchors is a spur loop
SAME_M = 30.0          # a fibre's own reading within this of its ribbon's is trusted
EDGE_M = 15.0          # slack at both ends of a loop
CLUSTER_M = 25.0
SPUR_CLUSTER_M = 15.0
MIN_FIBRES = 3


def _ribbon(fno):
    return (int(fno) - 1) // 12 + 1


def _lowest_cluster(vals, gap=SAME_M):
    """The backbone reading of a joint: the lowest run of ribbon medians that holds enough
    ribbons (longer readings are ribbons that went round a spur first)."""
    vals = sorted(vals)
    need = max(3, round(0.2 * len(vals)))
    runs, cur = [], [vals[0]]
    for v in vals[1:]:
        if v - cur[-1] <= gap:
            cur.append(v)
        else:
            runs.append(cur); cur = [v]
    runs.append(cur)
    for r in runs:
        if len(r) >= need:
            return statistics.median(r)
    return statistics.median(vals)


def _turn_name(name):
    u = (name or "").upper()
    return "ODF" in u or "REB" in u


def align(events, scheduled):
    """events: dicts with fno, position (raw metres on that fibre's trace) and location
    (scheduled names only; others None). scheduled: set of scheduled location names.
    Sets on every event with a position: bpos (backbone metres), spur_d (metres along the
    spur loop from the junction, None on the backbone), spur_len (one way spur length).
    Returns a summary dict."""
    evs = [e for e in events if e.get("position") is not None]
    for e in evs:
        e["bpos"], e["spur_d"], e["spur_len"] = e["position"], None, None
    if not evs:
        return {"anchors": 0, "loops": 0}
    ribbons = sorted({_ribbon(e["fno"]) for e in evs})
    # readings per named location per ribbon
    at = {}
    for e in evs:
        loc = e.get("location")
        if loc in scheduled:
            at.setdefault(loc, {}).setdefault(_ribbon(e["fno"]), []).append(e)
    rmed = {loc: {rb: statistics.median(x["position"] for x in xs) for rb, xs in per.items() if len(xs) >= MIN_FIBRES}
            for loc, per in at.items()}
    # anchors: joints a good share of the ribbons go through
    anchors = {}
    for loc, per in rmed.items():
        if len(per) >= max(2, 0.25 * len(ribbons)):
            anchors[loc] = _lowest_cluster(list(per.values()))
    # per ribbon anchor points (raw, offset), in route order
    rpts = {}
    for loc, ref in anchors.items():
        for rb, m in rmed[loc].items():
            rpts.setdefault(rb, []).append((m, m - ref, loc))
    for rb in rpts:
        pts = sorted(rpts[rb])
        clean = []
        for p in pts:                       # keep the route order honest: backbone distance must rise
            if clean and anchors[p[2]] <= anchors[clean[-1][2]]:
                continue
            clean.append(p)
        rpts[rb] = clean
    # spur turn points per ribbon: named joints off the anchors, seen by this ribbon
    turns = {}
    for loc, per in rmed.items():
        if loc in anchors:
            continue
        for rb, m in per.items():
            turns.setdefault(rb, []).append((m, loc))
    by_fibre = {}
    for e in evs:
        by_fibre.setdefault(e["fno"], []).append(e)
    loops = set()
    for fno, fe in by_fibre.items():
        rb = _ribbon(fno)
        pts = rpts.get(rb)
        if not pts:
            continue
        own = {e.get("location"): e["position"] for e in fe if e.get("location") in anchors}
        fp = []
        for m, off, loc in pts:
            raw = own.get(loc)
            if raw is not None and abs(raw - m) <= SAME_M:
                fp.append((raw, raw - anchors[loc], loc))
            else:
                fp.append((m, off, loc))
        for e in fe:
            r = e["position"]
            if r <= fp[0][0]:
                e["bpos"] = r - fp[0][1]
                continue
            if r >= fp[-1][0]:
                e["bpos"] = r - fp[-1][1]
                continue
            k = max(i for i in range(len(fp)) if fp[i][0] <= r)
            (r0, o0, l0), (r1, o1, _) = fp[k], fp[k + 1]
            step = o1 - o0
            if step <= STEP_M:
                t = (r - r0) / (r1 - r0) if r1 > r0 else 0
                e["bpos"] = r - (o0 + t * step)
                continue
            # a loop: find where it starts from this ribbon's spur joints in this stretch
            tp = [(m, loc) for m, loc in turns.get(rb, ()) if r0 < m < r1]
            if not tp:
                t = (r - r0) / (r1 - r0) if r1 > r0 else 0
                e["bpos"] = r - (o0 + t * step)
                continue
            far = [m for m, loc in tp if _turn_name(loc)]
            mid = statistics.median(far) if far else (min(m for m, _ in tp) + max(m for m, _ in tp)) / 2
            if "SPUR" in (l0 or "").upper():
                j = r0                      # the schedule's own spur joint is where the loop leaves
            else:
                j = min(max(mid - step / 2, r0), r1 - step)
            loops.add((rb, round(j)))
            d = r - j
            if -EDGE_M <= d <= step + EDGE_M:
                e["bpos"] = j - o0
                e["spur_d"] = round(min(max(d, 0.0), step), 1)
                e["spur_len"] = round(step / 2, 1)
            elif d < 0:
                e["bpos"] = r - o0
            else:
                e["bpos"] = r - o1
    return {"anchors": len(anchors), "loops": len(loops)}


def spur_place(spur_d, spur_len):
    """('out', metres from the junction), ('end', metres) at the far end, or ('back', metres)."""
    if spur_d is None or not spur_len:
        return None, None
    if abs(spur_d - spur_len) <= 10:
        return "end", round(spur_len)
    if spur_d < spur_len:
        return "out", round(spur_d)
    return "back", round(2 * spur_len - spur_d)


def label_loose(events, scheduled, min_fibres=MIN_FIBRES):
    """Unscheduled joints, clustered on the backbone scale (spur events within their spur),
    so the same physical joint is one location on every fibre."""
    loose = [e for e in events if e.get("position") is not None and e.get("location") not in scheduled]
    for e in loose:
        e["location"] = None
        e.pop("unscheduled", None)
    bb = [e for e in loose if e.get("spur_d") is None]
    sp = [e for e in loose if e.get("spur_d") is not None]
    kept = 0
    for c, members in _clusters(bb, lambda e: e["bpos"], CLUSTER_M):
        if len({e["fno"] for e in members}) >= min_fibres:
            kept += 1
            for e in members:
                e["location"], e["unscheduled"] = f"{round(c):,} m (unscheduled)", True
    # spur events: first the junction, then the distance along the loop
    for jc, jm in _clusters(sp, lambda e: e["bpos"], 60.0):
        for c, members in _clusters(jm, lambda e: e["spur_d"], SPUR_CLUSTER_M):
            if len({e["fno"] for e in members}) < min_fibres:
                continue
            kept += 1
            ln = statistics.median(e["spur_len"] for e in members)
            leg, m = spur_place(c, ln)
            for e in members:
                e["location"] = f"{round(jc):,} m spur, {m} m {'to the end' if leg == 'end' else leg} (unscheduled)"
                e["unscheduled"] = True
    return kept


def _clusters(items, key, tol):
    """Joints by density: take the busiest stretch 2 x tol wide (most fibres), make it one
    joint, and repeat. Chaining events one after another would run neighbouring joints
    together where events are close all the way along."""
    left = sorted(items, key=key)
    out = []
    while left:
        best, i0 = None, 0
        for i in range(len(left)):
            while key(left[i]) - key(left[i0]) > 2 * tol:
                i0 += 1
            n = len({e["fno"] for e in left[i0:i + 1]})
            if best is None or n > best[0]:
                best = (n, i0, i)
        _, a, b = best
        grp = left[a:b + 1]
        out.append((statistics.median(key(e) for e in grp), grp))
        left = left[:a] + left[b + 1:]
    # a joint read at long range scatters more than the window: pick up its stragglers from
    # the small clusters either side, one reading per fibre
    out.sort(key=lambda x: -len(x[1]))
    for i, (c, big) in enumerate(out):
        have = {e["fno"] for e in big}
        for j in range(i + 1, len(out)):
            c2, small = out[j]
            if abs(c2 - c) > 2 * tol or len(small) * 4 > len(big):
                continue
            move = [e for e in small if e["fno"] not in have and abs(key(e) - c) <= 2 * tol]
            for e in move:
                have.add(e["fno"])
            big.extend(move)
            out[j] = (c2, [e for e in small if e not in move])
    out = [(statistics.median(key(e) for e in g), g) for _, g in out if g]
    out.sort(key=lambda x: x[0])
    return out
