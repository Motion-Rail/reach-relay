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
import bisect
import statistics

ALIGN_V = 3
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


def align(events, scheduled, loose_loops=False):
    """events: dicts with fno, position (raw metres on that fibre's trace) and location
    (scheduled names only; others None). scheduled: set of scheduled location names.
    Sets on every event with a position: bpos (backbone metres), spur_d (metres along the
    spur loop from the junction, None on the backbone), spur_len (one way spur length).
    Returns a summary dict."""
    evs = sorted((e for e in events if e.get("position") is not None), key=lambda e: (e["fno"], e["position"]))
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
    loose = {}                              # unnamed readings per ribbon, to place loops with no named spur joints
    for e in evs:
        if e.get("location") not in scheduled:
            loose.setdefault(_ribbon(e["fno"]), []).append(e)
    nrib = {}
    for f in by_fibre:
        nrib[_ribbon(f)] = nrib.get(_ribbon(f), 0) + 1
    jcache = {}
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
            if "SPUR" in (l0 or "").upper():
                j = r0                      # the schedule's own spur joint is where the loop leaves
            elif tp:
                far = [m for m, loc in tp if _turn_name(loc)]
                mid = statistics.median(far) if far else (min(m for m, _ in tp) + max(m for m, _ in tp)) / 2
                j = min(max(mid - step / 2, r0), r1 - step)
            else:
                key = (rb, l0)
                if key not in jcache:
                    jcache[key] = _loop_start(r0, r1, step, loose.get(rb, ()), nrib.get(rb, 1)) if loose_loops else None
                j = jcache[key]
                if j is None:
                    t = (r - r0) / (r1 - r0) if r1 > r0 else 0
                    e["bpos"] = r - (o0 + t * step)
                    continue
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


def _loop_start(r0, r1, step, loose, nr):
    """Where a spur loop leaves the backbone when no spur joint is named: the ribbon's own
    unnamed joints in the stretch must sit inside the loop, typically one at the far end (half
    way round) and one where it comes back (a whole loop on). Tries the anchor itself and
    every placement those joints suggest, and keeps the one that explains most of them."""
    pos = [e["position"] for e in loose if r0 + 5 < e["position"] < r1 - 5]
    if not pos:
        return None
    need = max(2, round(0.4 * nr))
    cl = []
    for c, members in _clusters([{"fno": i, "position": p} for i, p in enumerate(pos)],
                                lambda e: e["position"], SPUR_CLUSTER_M):
        cl.append((c, len(members)))
    # one reading per fibre is not tracked here; a joint must be seen by a good share of the ribbon
    cl = [c for c, n in cl if n >= need]
    if not cl:
        return None
    cands = {r0}
    for c in cl:
        cands.update((c - step, c - step / 2, c))
    best = None
    for j in cands:
        if j < r0 - 5 or j > r1 - step + 5:
            continue
        if any(c < j - EDGE_M or c > j + step + EDGE_M for c in cl):
            continue
        fit = sum(1 for c in cl if min(abs(c - j), abs(c - j - step / 2), abs(c - j - step)) <= EDGE_M)
        score = (fit, -abs(j - r0))
        if best is None or score > best[0]:
            best = (score, j)
    if best is None:
        return None
    return min(max(best[1], r0), r1 - step)


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
    loose = sorted((e for e in events if e.get("position") is not None and e.get("location") not in scheduled),
                   key=lambda e: (e["fno"], e["position"]))
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
    left = sorted(items, key=lambda e: (key(e), e.get("fno", 0)))
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
    out.sort(key=lambda x: (-len(x[1]), x[0]))
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


# --------------------------------------------------------------------------- no schedule
ANCHOR_SHARE = 0.2      # a joint most of the cable shares is seen by at least this share of the fibres
ANCHOR_M = 10.0         # readings of one joint on the backbone fibres sit within this of each other
MATCH_M = 15.0          # a ribbon's reading of a shared joint, once its offset is known
LEN_SAME = 0.02         # learned locations scale fibres within 2 % of the median length (Brunel 1.0 Frame)


def _frame(lengths):
    ls = [v for v in (lengths or {}).values() if v]
    med = statistics.median(ls) if ls else None
    return {f: (med / ln if ln and med and abs(ln - med) <= LEN_SAME * med else 1.0)
            for f, ln in (lengths or {}).items()}


def find_backbone(events, lengths=None, learned=None, share=ANCHOR_SHARE):
    """For a cable with no Distances schedule: find the joints most of the cable shares from the
    results, name them from the app's learned locations where they match, and name each
    ribbon's reading of them. Ribbons are matched joint by joint along the cable, so a ribbon
    that went round a spur first (and reads every later joint further on) still gets its names.
    events: mid route event dicts (fno, position); sets e["location"] on the readings it names.
    learned: [{"name", "distance_m", "tolerance_m", "type"}] in Brunel 1.0's length scaled frame.
    Returns {"names": [...], "kinds": {name: type}, "learned": n named from learned locations}."""
    evs = sorted((e for e in events if e.get("position") is not None), key=lambda e: (e["fno"], e["position"]))
    fibres = {e["fno"] for e in evs}
    if not fibres:
        return {"names": [], "kinds": {}, "learned": 0}
    need = max(MIN_FIBRES, share * len(fibres))
    found = []
    for c, members in _clusters(evs, lambda e: e["position"], ANCHOR_M):
        fs = {e["fno"] for e in members}
        if len({_ribbon(f) for f in fs}) >= 3:
            found.append((c, members))
    # one joint read a little apart by two groups of fibres shows as two clusters with different
    # fibres in each; two joints close together are both seen by the same fibres
    merged = []
    for c, members in found:
        if merged and c - merged[-1][0] <= 2.5 * ANCHOR_M:
            pc, pm = merged[-1]
            a, b = {e["fno"] for e in pm}, {e["fno"] for e in members}
            if len(a & b) <= 0.1 * min(len(a), len(b)):
                allm = pm + members
                merged[-1] = (statistics.median(e["position"] for e in allm), allm)
                continue
        merged.append((c, members))
    found = [(statistics.median(e["position"] for e in m), m) for c, m in merged
             if len({e["fno"] for e in m}) >= need]
    # names: learned locations where one matches (compared in their own length scaled frame)
    k = _frame(lengths)
    pool = [dict(L) for L in (learned or []) if L.get("distance_m") is not None]
    used, names, kinds, from_learned = set(), [], {}, 0
    for c, members in found:
        common = statistics.median(e["position"] * k.get(e["fno"], 1.0) for e in members)
        best = None
        for i, L in enumerate(pool):
            gap = abs(L["distance_m"] - common)
            if i not in used and gap <= float(L.get("tolerance_m") or 10.0) + MATCH_M and (best is None or gap < best[0]):
                best = (gap, i)
        if best:
            used.add(best[1])
            L = pool[best[1]]
            nm = L.get("name") or f"Unnamed {L['distance_m']:,.0f} m"
            kinds[nm] = L.get("type") or ""
            from_learned += 1
        else:
            nm = f"Unnamed {c:,.0f} m"
        while nm in names:
            nm += " b"
        names.append(nm)
    points = [(nm, c) for nm, (c, _) in zip(names, found)]
    # each ribbon, joint by joint: the offset can only grow (a spur loop adds length). A jump is
    # only believed when the next joint agrees; a ribbon that simply does not pass a joint would
    # otherwise take the following joint for it and look as if it had gone round a spur.
    by_rib = {}
    for e in evs:
        by_rib.setdefault(_ribbon(e["fno"]), []).append(e)
    for rb, rev in by_rib.items():
        rev.sort(key=lambda e: e["position"])
        rpos = [e["position"] for e in rev]
        nr = len({e["fno"] for e in rev})
        enough = max(2, 0.3 * nr)

        def near(at, tol=MATCH_M):
            i = bisect.bisect_left(rpos, at - tol)
            out = []
            while i < len(rev) and rpos[i] <= at + tol:
                if rev[i].get("location") is None:
                    out.append(rev[i])
                i += 1
            return out

        def score(at):
            return len({e["fno"] for e in near(at)})

        s = 0.0
        for i, (nm, d) in enumerate(points):
            lo, hi = s - 30.0, s + 1500.0
            a, b = bisect.bisect_left(rpos, d + lo), bisect.bisect_right(rpos, d + hi)
            later = [d2 for _, d2 in points[i + 1:]]
            cands = sorted({round(rpos[x] - d, 1) for x in range(a, b) if rev[x].get("location") is None
                            and not any(abs(rpos[x] - d2 - s) <= 2 * MATCH_M for d2 in later[:6])})
            scored = [(score(d + c), c) for c in cands]
            if not scored:
                continue
            close = [(n, c) for n, c in scored if c - s <= STEP_M]
            if close and max(n for n, _ in close) >= enough:
                top = max(n for n, _ in close)      # the ribbon reads it at the offset it already has
                cand = min(c for n, c in close if n >= 0.8 * top)
            else:
                top = max(n for n, _ in scored)
                if top < enough:
                    continue
                cand = min(c for n, c in scored if n >= 0.8 * top)
            if cand - s > STEP_M:
                ahead = points[i + 1:i + 3]
                if not ahead:
                    continue            # nothing after it to confirm a loop
                was = max(score(d2 + s) for _, d2 in ahead)
                now = max(score(d2 + cand) for _, d2 in ahead)
                if was >= enough and was >= now:
                    continue            # the next joints still read at the old offset: no loop here
            pick = {}
            for e in near(d + cand):
                g = abs(e["position"] - d - cand)
                if e["fno"] not in pick or g < pick[e["fno"]][0]:
                    pick[e["fno"]] = (g, e)
            for _, e in pick.values():
                e["location"] = nm
            s = max(s - 30.0, statistics.median(e["position"] - d for _, e in pick.values()))
    return {"names": names, "kinds": kinds, "learned": from_learned}
