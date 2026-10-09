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
    straight_tries: int = 2      # Brunel: goes on the expected fibre in the straight pass (relay sets 3, all short)
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
    after_dis: object = None      # v22: async hook(f) run once a fibre is marked DIS (relay measures its length)
    dark_ribbons: list = field(default_factory=list)   # v26: ribbons with no light at all
    flipped_ribbons: list = field(default_factory=list)  # Brunel.4: ribbons declared flipped (1 to 12)
    no_flip: set = field(default_factory=set)            # Brunel.4: fibres already shown not to be on their mirror

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
        elif res.state == "flip":
            self.say(f"{fname(f)} {rplabel(f)} FLIPPED, lands on {fname(res.found)} {rplabel(res.found)}")
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

    # ---- Brunel.4: flipped ribbon (1 to 12, 12 to 1) found early and declared, not tested fibre by fibre ----
    FLIP_PAIRS = 2          # mirror pairs proven both ways before the whole ribbon is declared flipped
    FLIP_RANDOM_CHECK = True  # Brunel.4 (Alkis): then one more pair, picked at random from the rest, must agree

    @staticmethod
    def mirror(f: int) -> int:
        r, p = rp(f)
        return gf(r, PER + 1 - p)

    def _declare_flip(self, r: int, fibres: list[int], proven: list[tuple[int, int]], counts: dict):
        pairs = ", ".join(f"{fname(a)}/{fname(b)}" for a, b in proven)
        self.say(f"R{r}: FLIPPED. {pairs} proven both ways, the last picked at random (f1 lands on f12, f12 on f1). "
                 f"Stopping here: the rest of R{r} is declared flipped, not tested one by one.")
        done = {x for a, b in proven for x in (a, b)}
        for f in fibres:
            if f in self.results and self.results[f].state != "cross":
                continue
            if f in self.no_flip:                    # already seen dark on its mirror: checked again by the caller
                continue
            m = self.mirror(f)
            if f in done:
                why = "flipped ribbon, proven"
            else:
                why = f"flipped ribbon, declared from {pairs}"
            res = Result("flip", m, counts.get(f, 0), why)
            self.used_far.add(m)
            self.results[f] = res
        self.flipped_ribbons.append(r)
        self.hyp = [h for h in self.hyp if not (h.type == "reverse" and h.r == r)]

    async def _flip_leftovers(self, r: int, fibres: list[int], test, control, counts: dict) -> bool:
        """After a flip is declared: a fibre already seen dark on its mirror gets one more long check, then DIS."""
        for f in fibres:
            if f in self.results:
                continue
            m = self.mirror(f)
            self.current = f
            n0 = self.tests
            hit = await self._hit(f, m, test, control, tries=2) if m not in self.used_far else False
            if hit is None:
                return False
            counts[f] = counts.get(f, 0) + self.tests - n0
            if hit:
                self.used_far.add(m)
                self.results[f] = Result("flip", m, counts[f], "flipped ribbon, on recheck")
                self.say(f"{fname(f)} {rplabel(f)} FLIPPED, lands on {fname(m)} on recheck")
            else:
                self.record(f, Result("dis", 0, counts[f], f"no light on its own position or its mirror; R{r} flipped"))
                if self.after_dis:
                    await self.after_dis(f)
        return True

    async def _flip_random_check(self, r: int, fibres: list[int], proven: list, test, control, counts: dict):
        """One more mirror pair, chosen at random from the pairs not yet tested, proven both ways.
        True if it agrees (added to proven), False if not, None if stopped. True if no pair is left to try."""
        import random
        done = {x for a, b in proven for x in (a, b)}
        cands = [a for a in fibres if a not in done and a not in self.results and a not in self.no_flip
                 and rp(a)[1] <= PER // 2 and self.mirror(a) not in self.used_far and self.mirror(a) not in done]
        if not cands:
            return True
        a = random.choice(cands)
        b = self.mirror(a)
        self.say(f"  random check: {fname(a)} {rplabel(a)} on {fname(b)} and back.")
        for src, dst in ((a, b), (b, a)):
            if src not in fibres:
                continue
            self.current = src
            n0 = self.tests
            hit = await self._hit(src, dst, test, control, tries=2)
            counts[src] = counts.get(src, 0) + self.tests - n0
            if hit is None:
                return None
            if not hit:
                self.say(f"  random check failed: {fname(src)} is not on {fname(dst)}.")
                self.no_flip.add(src)
                return False
        proven.append((a, b))
        self.say(f"  random check agrees: {fname(a)} on {fname(b)} and {fname(b)} on {fname(a)}.")
        return True

    async def _flip_check(self, r: int, f: int, fibres: list[int], test, control, counts: dict, known: bool = False):
        """f is dark on its own position and nothing in R{r} is straight yet. Try its mirror (f1 on f12).
        Returns None if stopped, False if not flipped, ("flip",) once declared, or ("pair", g) when only
        f and its mirror are swapped (recorded as crosses; the rest of the ribbon carries on as normal)."""
        m = self.mirror(f)
        if m == f or (m in self.used_far and not known):
            return False
        if not known:
            self.say(f"R{r}: {fname(f)} is dark on its own position; checking for a flipped ribbon ({fname(f)} on {fname(m)}).")
            self.current = f
            n0 = self.tests
            hit = await self._hit(f, m, test, control, tries=1)
            counts[f] = counts.get(f, 0) + self.tests - n0
            if hit is None:
                return None
            if not hit:
                self.say(f"  {fname(f)} is not on {fname(m)}: not a flipped ribbon so far.")
                return False
        self.say(f"  {fname(f)} lands on {fname(m)}. Checking the other way and one more pair before calling it a flip.")
        proven: list[tuple[int, int]] = []
        order = [f] + [x for x in fibres if x != f and x != m and x not in self.results and x not in self.no_flip]
        seen: set[int] = set()
        misses = 0
        for a in order:
            if len(proven) >= self.FLIP_PAIRS:
                break
            b = self.mirror(a)
            if a in seen or b in seen or a == b or (b in self.used_far and a != f):
                continue
            seen.update((a, b))
            self.current = a
            if a != f:                                          # a to its mirror (already done for f)
                n0 = self.tests
                hit = await self._hit(a, b, test, control, tries=2)
                counts[a] = counts.get(a, 0) + self.tests - n0
                if hit is None:
                    return None
                if not hit:                                     # could be one dead fibre in a flipped ribbon
                    self.no_flip.add(a)
                    misses += 1
                    if misses >= 2:
                        break
                    continue
            if b in fibres and b not in self.results:          # and back: its mirror on it
                self.current = b
                n0 = self.tests
                back = await self._hit(b, a, test, control, tries=2)
                counts[b] = counts.get(b, 0) + self.tests - n0
                if back is None:
                    return None
                if not back:
                    self.say(f"  {fname(b)} is not on {fname(a)}, so {fname(a)}/{fname(b)} is not a clean swap.")
                    self.record(a, Result("cross", b, counts[a], f"R{r} reversed"))
                    break
            proven.append((a, b))
            self.say(f"  pair {len(proven)}: {fname(a)} on {fname(b)} and {fname(b)} on {fname(a)}.")
        if len(proven) >= self.FLIP_PAIRS and self.FLIP_RANDOM_CHECK:
            ok = await self._flip_random_check(r, fibres, proven, test, control, counts)
            if ok is None:
                return None
            if not ok:
                self.say(f"R{r}: the random check did not agree, so the ribbon is not declared flipped; "
                         f"searching the rest fibre by fibre.")
                for a, b in proven:
                    if a not in self.results:
                        self.record(a, Result("cross", b, counts.get(a, 0), f"R{r} reversed"))
                    if b in fibres and b not in self.results:
                        self.record(b, Result("cross", a, counts.get(b, 0), f"swapped with {fname(a)}"))
                return ("pair", m)
        if len(proven) >= self.FLIP_PAIRS:
            self._declare_flip(r, fibres, proven, counts)
            return ("flip",)
        # not enough to call a flip: keep what was proven as crosses, the rest goes through the pattern
        for a, b in proven:
            if a not in self.results:
                self.record(a, Result("cross", b, counts.get(a, 0), f"R{r} reversed"))
            if b in fibres and b not in self.results:
                self.record(b, Result("cross", a, counts.get(b, 0), f"swapped with {fname(a)}"))
        if f in self.results and self.results[f].state == "cross":
            return ("pair", m)
        return False

    # ---- v26: whole ribbon dark ----
    EARLY_DARK = 4

    def _probe_candidates(self, r: int, f: int) -> list[tuple[int, str]]:
        _, p = rp(f)
        q = PER + 1 - p
        out = []
        for rr, pp, why in [(r, q, f"R{r} reversed"),
                            (r - 1, p, f"R{r} crossed with R{r - 1}"), (r + 1, p, f"R{r} crossed with R{r + 1}"),
                            (r - 1, q, f"R{r} crossed with R{r - 1}, reversed"), (r + 1, q, f"R{r} crossed with R{r + 1}, reversed"),
                            (r + PERBUNDLE, p, f"bundle crossed: R{r} landing on R{r + PERBUNDLE} in the next bundle"),
                            (r - PERBUNDLE, p, f"bundle crossed: R{r} landing on R{r - PERBUNDLE} in the previous bundle"),
                            (r + PERBUNDLE, q, f"bundle crossed with R{r + PERBUNDLE}, reversed"),
                            (r - PERBUNDLE, q, f"bundle crossed with R{r - PERBUNDLE}, reversed")]:
            if 1 <= rr <= RIBBONS:
                g = gf(rr, pp)
                if rr == r and pp == q and f in self.no_flip:
                    continue
                if g != f and g not in self.used_far:
                    out.append((g, why))
        return out

    async def _ribbon_probe(self, r: int, f: int, test, control, counts: dict):
        """Is the whole ribbon crossed? Try one fibre where a crossed ribbon or bundle would put it."""
        side = ", ".join(f"R{x}" for x in (r - 1, r + 1) if 1 <= x <= RIBBONS)
        bun = ", ".join(f"R{x}" for x in (r + PERBUNDLE, r - PERBUNDLE) if 1 <= x <= RIBBONS)
        self.say(f"R{r}: checking whether the whole ribbon is crossed, using {fname(f)}. Order: R{r} reversed, "
                 f"then the ribbon{'s' if ',' in side else ''} either side ({side}), then the same ribbon in the "
                 f"neighbouring bundle ({bun}).")
        self.current = f
        n0 = self.tests
        for g, why in self._probe_candidates(r, f):
            self.say(f"  {fname(f)} on {fname(g)} {rplabel(g)}: {why}?")
            hit = await self._hit(f, g, test, control, tries=1)
            if hit is None:
                return None
            if hit:
                counts[f] = counts.get(f, 0) + self.tests - n0
                self.say(f"  found: {fname(f)} lands on {fname(g)}. {why[0].upper() + why[1:]}.")
                return g, why
        counts[f] = counts.get(f, 0) + self.tests - n0
        self.say(f"  {fname(f)} is not in the reversed ribbon, the ribbons either side or the neighbouring bundles.")
        return False

    async def _apply_pattern(self, r: int, fibres: list[int], test, control, targets: set[int], counts: dict, why: str):
        """A crossed ribbon or bundle is proven: test each remaining fibre where the pattern says first."""
        self.say(f"R{r}: applying the pattern to the rest of the ribbon ({why}).")
        for f in fibres:
            if f in self.results:
                continue
            self.current = f
            n0 = self.tests
            placed = False
            for h in list(self.hyp):
                g = self.apply(h, f)
                if not g or g in self.used_far:
                    continue
                hit = await self._hit(f, g, test, control, tries=2)
                if hit is None:
                    return False
                if hit:
                    counts[f] = counts.get(f, 0) + self.tests - n0
                    self.record(f, Result("cross", g, counts[f], f"same pattern: {h.label}"))
                    if await self._swap_check(f, g, test, control, targets, counts) is None:
                        return False
                    placed = True
                    break
            if placed:
                continue
            self.say(f"  {fname(f)} does not follow the pattern; searching for it on its own")
            res = await self.run_one(f, test, self.opts, control)
            if res is None:
                return False
            res.tests += counts.get(f, 0) + self.tests - n0
            self.record(f, res)
            if res.state == "cross":
                if await self._swap_check(f, res.found, test, control, targets, counts) is None:
                    return False
        return True

    async def run_ribbon(self, r: int, fibres: list[int], test, control, targets: set[int]) -> bool:
        counts: dict[int, int] = {}
        pending: list[int] = []
        probed: set[int] = set()
        flip_checks = 0
        # 1. straight pass: every fibre on its own position, one retry with a long tone
        for f in fibres:
            if f in self.results:
                continue
            self.current = f
            n0 = self.tests
            res = await self._t(f, f, test, control)
            if res is None:
                return False
            for _ in range(max(1, self.opts.straight_tries) - 1):
                if res == "clash":
                    break
                res = await self._t(f, f, test, control, long=True)
                if res is None:
                    return False
            counts[f] = self.tests - n0
            if res == "clash":
                self.record(f, Result("straight", f, counts[f], "expected"))
            else:
                pending.append(f)
                self.say(f"  {fname(f)} {rplabel(f)} dark on its own position, will search after the ribbon")
                # Brunel.4: the first dark fibre with nothing straight yet: is the ribbon flipped (f1 on f12)?
                if flip_checks < 2 and not any(self.results.get(x) and self.results[x].state == "straight" for x in fibres):
                    flip_checks += 1
                    fl = await self._flip_check(r, f, fibres, test, control, counts)
                    if fl is None:
                        return False
                    if fl == ("flip",):
                        return await self._flip_leftovers(r, fibres, test, control, counts)
                    if not fl:
                        self.no_flip.add(f)
                    continue                                  # a lone swapped pair: carry on with the straight pass
            # v26: the first fibres of the ribbon all dark: check for a crossed ribbon or bundle straight away
            straight_now = sum(1 for x in fibres if self.results.get(x) and self.results[x].state == "straight")
            if not straight_now and len(pending) == self.EARLY_DARK and not probed:
                self.say(f"R{r}: the first {self.EARLY_DARK} fibres are all dark on their own positions. "
                         f"Either the ribbon is crossed with another ribbon or bundle, or it is disconnected.")
                probed.add(pending[0])
                found = await self._ribbon_probe(r, pending[0], test, control, counts)
                if found is None:
                    return False
                if found:
                    g, why = found
                    f0 = pending[0]
                    if g == self.mirror(f0):                   # Brunel.4: reversed, prove it as a flip
                        fl = await self._flip_check(r, f0, fibres, test, control, counts, known=True)
                        if fl is None:
                            return False
                        if fl == ("flip",):
                            return await self._flip_leftovers(r, fibres, test, control, counts)
                        rest = [x for x in fibres if x not in self.results]
                        return await self._apply_pattern(r, rest, test, control, targets, counts, why)
                    self.record(f0, Result("cross", g, counts[f0], why))
                    if await self._swap_check(f0, g, test, control, targets, counts) is None:
                        return False
                    rest = [x for x in fibres if x not in self.results]
                    return await self._apply_pattern(r, rest, test, control, targets, counts, why)
                self.say(f"R{r}: testing the rest of the ribbon on its own positions to see if any fibre gets through.")
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
                    if self.after_dis:
                        await self.after_dis(f)
        elif pending:
            # 4. nothing in this ribbon is straight: crossed ribbon or bundle, or the whole ribbon is out
            self.say(f"R{r}: no fibre gets through on its own position.")
            found = False
            for f in [x for x in (pending[len(pending) // 2], pending[0], pending[-1]) if x not in probed][:2]:
                probed.add(f)
                found = await self._ribbon_probe(r, f, test, control, counts)
                if found is None:
                    return False
                if found:
                    g, why = found
                    if g == self.mirror(f):                    # Brunel.4: reversed, prove it as a flip
                        fl = await self._flip_check(r, f, fibres, test, control, counts, known=True)
                        if fl is None:
                            return False
                        if fl == ("flip",):
                            return await self._flip_leftovers(r, fibres, test, control, counts)
                        return await self._apply_pattern(r, [x for x in pending if x not in self.results],
                                                         test, control, targets, counts, why)
                    self.record(f, Result("cross", g, counts[f], why))
                    if await self._swap_check(f, g, test, control, targets, counts) is None:
                        return False
                    return await self._apply_pattern(r, [x for x in pending if x not in self.results],
                                                     test, control, targets, counts, why)
            where = ", ".join(f"R{x}" for x in (r - 1, r + 1, r + PERBUNDLE, r - PERBUNDLE) if 1 <= x <= RIBBONS)
            self.say(f"R{r}: no light on any fibre, and none of them turn up in R{r} reversed or in {where}. "
                     f"The ribbon looks disconnected or unpatched, not crossed. "
                     f"Next step on site: check the patching at both ODFs and the ribbon splices. "
                     f"If other ribbons are also all dark, check the tone RTU and FMS first.")
            self.dark_ribbons.append(r)
            for i, f in enumerate(pending):
                if f in self.results:
                    continue
                self.record(f, Result("dis", 0, counts.get(f, 0), f"whole R{r} dark; not crossed with a neighbouring ribbon or bundle"))
                if self.after_dis and i < 2:
                    await self.after_dis(f)
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
            if len(self.dark_ribbons) >= 2 and self.dark_ribbons[-1] == r and self.dark_ribbons[-2] == r - 1:
                self.say(f"Warning: R{r - 1} and R{r} are both completely dark. That usually means the tone is not "
                         f"reaching the cable at all: check the tone RTU, its patch to the ODF, and FMS.")
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
            "flippedRibbons": list(self.flipped_ribbons),
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
