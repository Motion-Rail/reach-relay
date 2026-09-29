"""
continuity_engine.py  -  the search that finds where each fibre lands at the far end.

Pure logic, no FMS calls. The relay hands it an async `test(source, candidate)` that
returns "clash" (the toned source is on that candidate), "clean", or "error".

Ported from the continuity rig (artifact 35hoGyGd2ZmXee78uFXj8H, v5) and checked
against its simulator: every fibre resolved, no wrong answers, for straight, reversed
ribbon, swapped ribbons, swapped bundles, single pair and mixed faults.

Search order for source fibre f (ribbon r, position p), 12 per ribbon, 6 ribbons a bundle:
  expected f, learned pattern, fibre either side, ribbon reversed, ribbon before/after,
  those reversed, bundle before/after (ribbon +/-6), then a sweep of the ribbon
  (or the bundle). A far end fibre already claimed by another source is skipped.
A cross is only accepted after a confirming retest. Fibres not found get a second
pass with one more retry each and a bigger budget.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

RIBBONS, PER, PERBUNDLE = 36, 12, 6
TOTAL = RIBBONS * PER


def gf(r: int, p: int) -> int:
    return (r - 1) * PER + p


def rp(f: int) -> tuple[int, int]:
    return (f - 1) // PER + 1, (f - 1) % PER + 1


def bund(r: int) -> int:
    return (r - 1) // PERBUNDLE + 1


def fname(f: int) -> str:
    return f"F{f:03d}"


def rplabel(f: int) -> str:
    r, p = rp(f)
    return f"R{r} f{p}"


@dataclass
class Options:
    retry_straight: int = 1      # extra attempts on the expected fibre
    retry_other: int = 0
    max_tests: int = 14          # budget per source fibre
    confirm: bool = True         # retest a cross before accepting it
    learn: bool = True           # reuse a proven fault pattern
    deep: str = "ribbon"         # ribbon | bundle | stop


@dataclass
class Hyp:
    type: str
    label: str
    a: int = 0
    b: int = 0
    r: int = 0
    d: int = 0
    hits: int = 1
    miss: int = 0


@dataclass
class Result:
    state: str                   # straight | cross | dis | unres
    found: int = 0
    tests: int = 0
    why: str = ""


@dataclass
class Engine:
    opts: Options = field(default_factory=Options)
    results: dict[int, Result] = field(default_factory=dict)
    used_far: set[int] = field(default_factory=set)
    hyp: list[Hyp] = field(default_factory=list)
    tests: int = 0
    log: list[str] = field(default_factory=list)
    current: int = 0
    candidate: int = 0

    def say(self, msg: str):
        self.log.append(time.strftime("%H:%M:%S ") + msg)
        del self.log[:-400]

    # ---- candidate order ----
    def ladder(self, f: int, opts: Options) -> list[tuple[int, str]]:
        r, p = rp(f)
        out, seen = [], set()

        def add(rr, pp, why):
            if rr < 1 or rr > RIBBONS or pp < 1 or pp > PER:
                return
            g = gf(rr, pp)
            if g in seen or (g in self.used_far and g != f):
                return
            seen.add(g)
            out.append((g, why))

        add(r, p, "expected")
        hyp_ribbons = []
        if opts.learn:
            for h in self.hyp:
                g = self.apply(h, f)
                if g:
                    gr, gp = rp(g)
                    add(gr, gp, h.label)
                    add(gr, PER + 1 - gp, h.label + ", reversed")
                    add(gr - 1, gp, h.label + ", ribbon before")
                    add(gr + 1, gp, h.label + ", ribbon after")
                    hyp_ribbons.append(gr)
        add(r, p - 1, "fibre before")
        add(r, p + 1, "fibre after")
        add(r, PER + 1 - p, "ribbon reversed")
        add(r - 1, p, "ribbon before")
        add(r + 1, p, "ribbon after")
        add(r - 1, PER + 1 - p, "ribbon before, reversed")
        add(r + 1, PER + 1 - p, "ribbon after, reversed")
        add(r - PERBUNDLE, p, "bundle before")
        add(r + PERBUNDLE, p, "bundle after")
        if opts.deep != "stop":
            for pp in range(1, PER + 1):
                add(r, pp, "ribbon sweep")
            if opts.deep == "bundle":
                for gr in hyp_ribbons:              # where a proven pattern says it went
                    for pp in range(1, PER + 1):
                        add(gr, pp, "pattern ribbon sweep")
                first = (bund(r) - 1) * PERBUNDLE + 1
                for rr in range(first, first + PERBUNDLE):
                    for pp in range(1, PER + 1):
                        add(rr, pp, "bundle sweep")
        return out

    # ---- learned patterns ----
    @staticmethod
    def apply(h: Hyp, f: int) -> int:
        r, p = rp(f)
        if h.type == "reverse" and r == h.r:
            return gf(r, PER + 1 - p)
        if h.type == "swapRib":
            if r == h.a:
                return gf(h.b, p)
            if r == h.b:
                return gf(h.a, p)
        if h.type == "swapBundle":
            b, off = bund(r), (r - 1) % PERBUNDLE
            if b == h.a:
                return gf((h.b - 1) * PERBUNDLE + 1 + off, p)
            if b == h.b:
                return gf((h.a - 1) * PERBUNDLE + 1 + off, p)
        if h.type == "shift" and r == h.r and 1 <= p + h.d <= PER:
            return gf(r, p + h.d)
        return 0

    def learn_from(self, src: int, found: int):
        (ar, ap), (br, bp) = rp(src), rp(found)
        h = None
        if ar == br and bp == PER + 1 - ap and ap != bp:
            h = Hyp("reverse", f"ribbon {ar} reversed", r=ar)
        elif ar == br and ap != bp:
            h = Hyp("shift", f"ribbon {ar} shifted {bp - ap}", r=ar, d=bp - ap)
        elif ap == bp and bund(ar) != bund(br) and (ar - 1) % PERBUNDLE == (br - 1) % PERBUNDLE:
            h = Hyp("swapBundle", f"bundles {bund(ar)} and {bund(br)} crossed", a=bund(ar), b=bund(br))
        elif ap == bp and ar != br:
            h = Hyp("swapRib", f"ribbons {ar} and {br} crossed", a=ar, b=br)
        if not h:
            return
        for x in self.hyp:
            if x.label == h.label:
                x.hits += 1
                return
        self.hyp.insert(0, h)
        self.say("pattern learned: " + h.label)

    # ---- one source fibre ----
    async def run_one(self, f: int, test, opts: Options, control, skip_expected: bool = False) -> Result | None:
        used = 0
        self.current = f
        for cand, why in self.ladder(f, opts):
            if skip_expected and why == "expected":
                continue
            if used >= opts.max_tests:
                break
            retries = opts.retry_straight if why == "expected" else opts.retry_other
            for _ in range(retries + 1):
                if not await control():
                    return None
                used += 1
                self.tests += 1
                self.candidate = cand
                res = await test(f, cand)
                if res == "clash":
                    if opts.confirm and why != "expected":
                        used += 1
                        self.tests += 1
                        again = await test(f, cand)
                        if again != "clash":
                            self.say(f"  {fname(cand)} did not confirm, carrying on")
                            continue
                    return Result("straight" if cand == f else "cross", cand, used, why)
                if res == "error":
                    self.say(f"  test error on {fname(cand)}")
            if why != "expected":
                for h in self.hyp:
                    if self.apply(h, f) == cand:
                        h.miss += 1
        return Result("unres", 0, used, "not found")

    def record(self, f: int, res: Result, suffix: str = ""):
        if res.found:
            self.used_far.add(res.found)
        res.why += suffix
        self.results[f] = res
        if res.state == "straight":
            self.say(f"{fname(f)} {rplabel(f)} straight, {res.tests} test{'s' if res.tests != 1 else ''}")
        elif res.state == "cross":
            self.say(f"{fname(f)} {rplabel(f)} CROSS to {fname(res.found)} {rplabel(res.found)} ({res.why})")
            if self.opts.learn:
                self.learn_from(f, res.found)
        elif res.state == "dis":
            self.say(f"{fname(f)} {rplabel(f)} DIS, {res.why} ({res.tests} tests)")
        else:
            self.say(f"{fname(f)} {rplabel(f)} NOT FOUND after {res.tests} tests")
        self.hyp = [h for h in self.hyp if h.miss < 3]

    # ---- v18: ribbon by ribbon, physical fault logic ----
    async def _t(self, src, cand, test, control, long=False):
        if not await control():
            return None
        self.tests += 1
        self.candidate = cand
        return await test(src, cand, long=long)

    async def _hit(self, src, cand, test, control, tries=2, long=True):
        """True if cand is live while src is toned, in up to `tries` attempts. None if stopped."""
        for _ in range(tries):
            r = await self._t(src, cand, test, control, long=long)
            if r is None:
                return None
            if r == "clash":
                return True
        return False

    def _unclaimed(self, r: int, targets: set[int]) -> list[int]:
        return [gf(r, p) for p in range(1, PER + 1)
                if gf(r, p) in targets and gf(r, p) not in self.used_far]

    async def _swap_check(self, a: int, b: int, test, control, targets: set[int], counts: dict) -> bool | None:
        """a was found on b. If b is an untested or unresolved source, test b on a straight away."""
        if b not in targets or b in self.results or a in self.used_far:
            return False
        self.current = b
        self.say(f"  {fname(a)} lands on {fname(b)}: checking {fname(b)} on {fname(a)} (swap)")
        n0 = self.tests
        hit = await self._hit(b, a, test, control)
        if hit is None:
            return None
        if hit:
            self.record(b, Result("cross", a, self.tests - n0 + counts.get(b, 0), f"swapped with {fname(a)}"))
            return True
        return False

    async def run_ribbon(self, r: int, fibres: list[int], test, control, targets: set[int]) -> bool:
        counts: dict[int, int] = {}
        pending: list[int] = []
        # 1. straight pass: every fibre on its own position, one retry with a long tone
        for f in fibres:
            if f in self.results:
                continue
            self.current = f
            n0 = self.tests
            res = await self._t(f, f, test, control)
            if res is None:
                return False
            if res != "clash":
                res = await self._t(f, f, test, control, long=True)
                if res is None:
                    return False
            counts[f] = self.tests - n0
            if res == "clash":
                self.record(f, Result("straight", f, counts[f], "expected"))
            else:
                pending.append(f)
                self.say(f"  {fname(f)} {rplabel(f)} dark on its own position, will search after the ribbon")
        straight = sum(1 for f in fibres if self.results.get(f) and self.results[f].state == "straight")
        # 2. gaps: pending sources against unclaimed far ends in this ribbon
        if pending and straight:
            for f in pending:
                if f in self.results:
                    continue
                self.current = f
                n0 = self.tests
                found = 0
                gaps = sorted(self._unclaimed(r, targets), key=lambda g: (abs(g - f), g))
                gaps = [g for g in gaps if g != f]
                for g in gaps:
                    if g in self.used_far:
                        continue
                    hit = await self._hit(f, g, test, control, tries=1)
                    if hit is None:
                        return False
                    if hit:
                        found = g
                        break
                counts[f] = counts.get(f, 0) + self.tests - n0
                if found:
                    self.record(f, Result("cross", found, counts[f], "gap in ribbon"))
                    sw = await self._swap_check(f, found, test, control, targets, counts)
                    if sw is None:
                        return False
                    continue
                # 3. not on any free far end in its ribbon, rest of the ribbon is in place: DIS after rechecks
                self.say(f"  {fname(f)} not on any free far end in R{r}; rechecking its own position")
                n0 = self.tests
                hit = await self._hit(f, f, test, control, tries=2)
                if hit is None:
                    return False
                counts[f] += self.tests - n0
                if hit:
                    self.record(f, Result("straight", f, counts[f], "expected, on recheck"))
                else:
                    self.record(f, Result("dis", 0, counts[f],
                                          f"no light at the far end; {straight} of R{r} straight"))
        elif pending:
            # 4. nothing in this ribbon is straight: the ribbon itself is crossed, use the full ladder
            self.say(f"R{r}: no fibre straight, searching neighbouring ribbons and bundles")
            for f in pending:
                if f in self.results:
                    continue
                res = await self.run_one(f, test, self.opts, control, skip_expected=True)
                if res is None:
                    return False
                res.tests += counts.get(f, 0)
                self.record(f, res)
                if res.state == "cross":
                    sw = await self._swap_check(f, res.found, test, control, targets, counts)
                    if sw is None:
                        return False
        return True

    async def run(self, targets: list[int], test, control) -> bool:
        """Returns False if stopped."""
        tset = set(targets)
        ribbons: dict[int, list[int]] = {}
        for f in targets:
            ribbons.setdefault(rp(f)[0], []).append(f)
        for r in sorted(ribbons):
            if not await self.run_ribbon(r, ribbons[r], test, control, tset):
                return False
        # 5. second pass for fibres the ladder could not place (crossed ribbons only)
        left = [f for f in targets if self.results.get(f) and self.results[f].state == "unres"]
        if left:
            self.say(f"Second pass over {len(left)} fibre{'s' if len(left) != 1 else ''}")
            o2 = Options(**{**self.opts.__dict__,
                            "retry_other": self.opts.retry_other + 1,
                            "max_tests": self.opts.max_tests + 80,
                            "deep": "bundle" if self.opts.deep != "stop" else "stop"})
            for f in left:
                old = self.results.pop(f)
                res = await self.run_one(f, test, o2, control, skip_expected=True)
                if res is None:
                    self.results[f] = old
                    return False
                res.tests += old.tests
                self.record(f, res, ", second pass")
        # 6. leftovers: DIS or unresolved sources against unclaimed far ends in other tested ribbons
        left = [f for f in targets if self.results.get(f) and self.results[f].state in ("dis", "unres")]
        free = [g for g in targets if g not in self.used_far
                and any(rp(g)[0] != rp(f)[0] for f in left)]
        if left and free:
            self.say(f"Leftovers: {len(left)} fibre{'s' if len(left) != 1 else ''} against "
                     f"{len(free)} free far end{'s' if len(free) != 1 else ''} in other ribbons")
            budget = 40 + 12 * len(left)
            for f in left:
                self.current = f
                pred = [self.apply(h, f) for h in self.hyp]
                order = [g for g in pred if g in free] + sorted(free, key=lambda g: (abs(g - f), g))
                seen = set()
                for g in order:
                    if g in seen or g in self.used_far or rp(g)[0] == rp(f)[0] or budget <= 0:
                        continue
                    seen.add(g)
                    budget -= 1
                    hit = await self._hit(f, g, test, control, tries=1)
                    if hit is None:
                        return False
                    if hit:
                        old = self.results.pop(f)
                        self.record(f, Result("cross", g, old.tests + len(seen), "found in leftovers check"))
                        break
        self.current = self.candidate = 0
        return True

    async def run_classic(self, targets: list[int], test, control) -> bool:
        """The v12 to v17 search, kept for comparison and the simulator tests."""
        for f in targets:
            if f in self.results:
                continue
            res = await self.run_one(f, test, self.opts, control)
            if res is None:
                return False
            self.record(f, res)
        left = [f for f in targets if self.results.get(f) and self.results[f].state == "unres"]
        if left:
            self.say(f"Second pass over {len(left)} fibre{'s' if len(left) != 1 else ''}")
            o2 = Options(**{**self.opts.__dict__,
                            "retry_straight": self.opts.retry_straight + 1,
                            "retry_other": self.opts.retry_other + 1,
                            "max_tests": self.opts.max_tests + 80,
                            "deep": "bundle" if self.opts.deep != "stop" else "stop"})
            for f in left:
                del self.results[f]
                res = await self.run_one(f, test, o2, control)
                if res is None:
                    return False
                self.record(f, res, ", second pass")
        self.current = self.candidate = 0
        return True

    def snapshot(self) -> dict:
        return {
            "results": {str(f): {"state": r.state, "found": r.found, "tests": r.tests, "why": r.why}
                        for f, r in sorted(self.results.items())},
            "patterns": [{"label": h.label, "hits": h.hits} for h in self.hyp],
            "tests": self.tests, "current": self.current, "candidate": self.candidate,
            "log": self.log[-80:],
        }


def ribbons_to_targets(ribbons: list[int]) -> list[int]:
    out = []
    for r in sorted(set(int(x) for x in ribbons if 1 <= int(x) <= RIBBONS)):
        out += [gf(r, p) for p in range(1, PER + 1)]
    return out


# ---- simulator, for the app's demo mode and for tests ----
def planted_truth(kind: str, seed: int | None = None) -> tuple[dict[int, int], list[str]]:
    import random
    rnd = random.Random(seed)
    m = {f: f for f in range(1, TOTAL + 1)}
    notes = []

    def swap(x, y):
        m[x], m[y] = m[y], m[x]

    def reverse(r):
        for p in range(1, PER // 2 + 1):
            swap(gf(r, p), gf(r, PER + 1 - p))
        notes.append(f"ribbon {r} reversed")

    def swap_rib(r):
        r2 = r + 1 if r < RIBBONS else r - 1
        for p in range(1, PER + 1):
            swap(gf(r, p), gf(r2, p))
        notes.append(f"ribbons {min(r, r2)} and {max(r, r2)} crossed")

    def swap_bundle():
        b = rnd.randint(1, RIBBONS // PERBUNDLE - 1)
        for o in range(PERBUNDLE):
            for p in range(1, PER + 1):
                swap(gf((b - 1) * PERBUNDLE + 1 + o, p), gf(b * PERBUNDLE + 1 + o, p))
        notes.append(f"bundles {b} and {b + 1} crossed")

    def pair():
        r, p = rnd.randint(1, RIBBONS), rnd.randint(1, PER - 1)
        swap(gf(r, p), gf(r, p + 1))
        notes.append(f"pair swapped at R{r} f{p}/f{p + 1}")

    if kind == "reverse":
        reverse(rnd.randint(1, RIBBONS))
    elif kind == "swapRibbon":
        swap_rib(rnd.randint(1, RIBBONS))
    elif kind == "swapBundle":
        swap_bundle()
    elif kind == "pair":
        pair()
    elif kind == "mixed":
        reverse(rnd.randint(1, RIBBONS)); swap_rib(rnd.randint(1, RIBBONS)); pair(); swap_bundle()
    return m, notes


def sim_tester(truth: dict[int, int], miss_rate: float = 0.0, delay: float = 0.0, seed=None):
    import random
    rnd = random.Random(seed)

    async def test(src, cand, long=False):
        if delay:
            await asyncio.sleep(delay)
        hit = truth.get(src) == cand
        if hit and rnd.random() < (miss_rate / 4 if long else miss_rate):
            return "clean"
        return "clash" if hit else "clean"
    return test
