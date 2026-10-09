#!/usr/bin/env python3
"""
fms_pull.py — pull EXFO FMS results and build the bidirectional results workbook.

RUN ON A MACHINE THAT CAN REACH THE FMS TENANT (your laptop / a networked server).
It never stores your password: set FMS_USER / FMS_PASS env vars, or let it prompt.

How it works
------------
A cable has two ends, each an RTU (e.g. NRS-303 = RTU2-0057 Slough + RTU2-RGAC Reading;
NRS-304 = RTU2-RGAC2 Reading + RTU2-SNBC Swindon). You pick the two RTUs, the tool pulls
each fibre's latest iOLM result (per-wavelength loss @1310/1550/1625, length, ORL, star,
verdict), pairs the two ends by fibre name, and writes the bidirectional report.

Usage
-----
    # interactive: lists your cables, you pick one; the tool finds the RTU at each end
    python fms_pull.py

    # scripted: pick the cable and let the tool resolve the two ends
    python fms_pull.py --cable RGAC-SNBC

    # scripted: name the two RTUs directly (any distinctive token; overrides --cable)
    python fms_pull.py --rtu-a 0057 --rtu-b RGAC-1991133 --out NRS303.xlsx

    # limit to a date/time window (defaults to the latest result per fibre)
    python fms_pull.py --rtu-a 0057 --rtu-b RGAC-1991133 --from 2026-06-18 --to 2026-06-19 --out NRS303.xlsx

    # quick test: only the first N fibres per end
    python fms_pull.py --rtu-a 0057 --rtu-b RGAC-1991133 --limit 5 --out test.xlsx

    # just list the RTUs available on the tenant
    python fms_pull.py --list-rtus

Deps: requests, openpyxl  (pip install -r requirements.txt)
A full cable is ~864 result calls (2 ends x 432 fibres) and takes a few minutes.
Open the finished .xlsx once in Excel so the formulas evaluate.
"""
import argparse
import math
import os
import re
import sys
import time
import getpass

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

from bidir_report import build_workbook

BASE = "https://raman.ems.exfo-fms.com"
TOKEN_URL = f"{BASE}/auth/realms/Fiber/protocol/openid-connect/token"
GRAPHQL_URL = f"{BASE}/topology/graphql/graphql"
RESULTS_URL = f"{BASE}/api/measure/v1/results/"
CLIENT_ID = "fg-topologyui"
FIBRES_PER_RIBBON = 12

# ===================== DEBUG LOG (build 2026-10-06a) =====================
# Plain-language run log written next to the workbook. Records, per fibre,
# whether its result exported, was skipped (and why), or failed (and why),
# then flags data-quality issues and ends with a clear WORKED / ISSUES summary.
class DebugLog:
    def __init__(self):
        self.lines = []
        self.path = None
        self.fibre_ok = []      # (end, fibre) exported fine
        self.fibre_skip = []    # (end, fibre, reason) nothing to export, explained
        self.fibre_fail = []    # (end, fibre, reason) should have worked, did not
        self.flags = []         # data-quality notes (text)

    def w(self, text=""):
        self.lines.append(text)

    def section(self, title):
        self.w("")
        self.w("=" * 64)
        self.w(f"  {title}")
        self.w("=" * 64)

    def ok(self, end, fibre, detail=""):
        self.fibre_ok.append((end, fibre))
        self.w(f"  {fibre:<6} {end:<6} EXPORTED   {detail}")

    def skip(self, end, fibre, reason):
        self.fibre_skip.append((end, fibre, reason))
        self.w(f"  {fibre:<6} {end:<6} SKIPPED    {reason}")

    def fail(self, end, fibre, reason):
        self.fibre_fail.append((end, fibre, reason))
        self.w(f"  {fibre:<6} {end:<6} NOT EXPORTED  {reason}")

    def flag(self, text):
        self.flags.append(text)

    def save(self, path):
        self.path = path
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(self.lines) + "\n")
            return True
        except Exception:
            return False

# One shared logger for the run
DBG = DebugLog()
# Human-readable reason text for each empty-result code from fetch_iolm
EMPTY_REASONS = {
    "no_results":   "no test of any kind found for this fibre on this cable",
    "wrong_type":   "a test exists but not the type asked for (e.g. only OTDR, iOLM requested)",
    "no_linkresults": "a result exists but carries no loss data (incomplete result)",
    "out_of_window": "a test exists but falls outside the --from / --to date range",
    "no_asset":     "this route has no asset id, so no result can be fetched",
}

TOOL_VERSION = "v1.6"
TOOL_BUILD = "2026-10-09"   # date of v1.6; version is TOOL_VERSION

SEARCH_QUERY = """
query searchOpticalRouteByRtu($search: String, $condition: OpticalRouteSearchOutputCondition, $orderBy: OpticalRouteSearchOutputsOrderBy!, $first: Int!, $offset: Int!) {
  routeSearchResult: searchOpticalRouteByRtu(matchType: CONTAINS, search: $search, condition: $condition, orderBy: $orderBy, first: $first, offset: $offset) {
    totalCount
    nodes {
      id type status name rtuId rtuName description portLabel
      rtu { name serialNumber attachStatus model site { id name __typename } __typename }
      __typename
    }
    __typename
  }
}
""".strip()


# The exact filter the FMS UI uses to list a route's real measurements (iOLM + adhoc),
# keyed by AssetId (= topology route id). Confirmed from live capture 2026-08-06.
RELAXED_FILTERS = [
    "metadata/AssetId eq {id} and brief ne null",
    "metadata/AssetId eq {id}",
]
ANY_CATEGORY = False


def relaxed_filters(asset_id):
    return [f.format(id=asset_id) for f in RELAXED_FILTERS]


def adhoc_filter(asset_id):
    if ANY_CATEGORY:
        return f"metadata/AssetId eq {asset_id} and brief ne null"
    return (
        f"metadata/AssetId eq {asset_id} and brief ne null and (metadata/HasError eq true "
        "and (metadata/TestType ne 'OTDR' or (metadata/TestType eq 'OTDR' and "
        "metadata/TestCategory eq 'Adhoc' and measurement ne null)) or "
        "(metadata/TestCategory ne 'RLNulling' and metadata/TestCategory ne 'ScanMonitoring' "
        "and (metadata/TestCategory ne 'FastMonitoring' or metadata/FaultStatus eq 'Detected' "
        "or metadata/FaultStatus eq 'Cleared' or metadata/FaultStatus eq null) "
        "and (metadata/AnalysisStatus ne 'InvalidIolmAnalysis' or metadata/TestCategory ne 'Monitoring') "
        "and (metadata/TestType eq 'iOLM' or (metadata/TestType eq 'OTDR' and "
        "metadata/TestCategory eq 'Adhoc' and measurement ne null))))")



# --------------------------------------------------------------------------- #
# Console identity
# --------------------------------------------------------------------------- #
TEAL = "\033[38;2;13;110;128m"
TEALD = "\033[38;2;9;78;91m"
GREY = "\033[38;2;130;130;130m"
BOLD = "\033[1m"
OFF = "\033[0m"

WORDMARK = [
    "  ███╗   ███╗  ██████╗  ████████╗ ██╗  ██████╗  ███╗   ██╗",
    "  ████╗ ████║ ██╔═══██╗ ╚══██╔══╝ ██║ ██╔═══██╗ ████╗  ██║",
    "  ██╔████╔██║ ██║   ██║    ██║    ██║ ██║   ██║ ██╔██╗ ██║",
    "  ██║╚██╔╝██║ ██║   ██║    ██║    ██║ ██║   ██║ ██║╚██╗██║",
    "  ██║ ╚═╝ ██║ ╚██████╔╝    ██║    ██║ ╚██████╔╝ ██║ ╚████║",
    "  ╚═╝     ╚═╝  ╚═════╝     ╚═╝    ╚═╝  ╚═════╝  ╚═╝  ╚═══╝",
]

OWNER = "Alkis Kardasopoulos"
COMPANY = "Motion Rail"


def _enable_colour():
    """Windows consoles need VT processing switching on before ANSI colour works."""
    if os.name != "nt":
        return sys.stdout.isatty()
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleMode(k.GetStdHandle(-11), 7)
        return True
    except Exception:
        return False


ASCII_MARK = [
    "  __  __    ___    _____   ___    ___    _   _ ",
    " |  \\/  |  / _ \\  |_   _| |_ _|  / _ \\  | \\ | |",
    " | |\\/| | | | | |   | |    | |  | | | | |  \\| |",
    " | |  | | | |_| |   | |    | |  | |_| | | |\\  |",
    " |_|  |_|  \\___/    |_|   |___|  \\___/  |_| \\_|",
]


def _mark():
    """Block-drawing wordmark where the console can render it, plain ASCII where it
    cannot. An old Windows codepage would otherwise turn the logo into mojibake, so we
    ask for UTF-8 first and only fall back if the console refuses."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    probe = "".join(WORDMARK) + "╭╮╰╯│─·"
    try:
        probe.encode(enc)
        return WORDMARK
    except (UnicodeEncodeError, LookupError):
        return ASCII_MARK


def banner():
    col = _enable_colour()
    t, td, g, b, o = (TEAL, TEALD, GREY, BOLD, OFF) if col else ("", "", "", "", "")
    mark = _mark()
    box = ("╭╮╰╯│─" if mark is WORDMARK else "++++|-")
    print()
    for i, line in enumerate(mark):
        print((t if i < 3 else td) + line + o)
    print(f"{t}  {box[0]}{box[5] * 56}{box[1]}{o}")
    print(f"{t}  {box[4]}{o}{b}   F M S   T E S T   R E S U L T S   R E P O R T        {o}{t}{box[4]}{o}")
    print(f"{t}  {box[2]}{box[5] * 56}{box[3]}{o}")
    dot = "·" if mark is WORDMARK else "-"
    print(f"{g}   {TOOL_VERSION}   ({TOOL_BUILD})   {dot}   {COMPANY}   {dot}   {OWNER}{o}")
    print(f"{g}   --changelog for what has changed{o}")
    print()


def show_changelog():
    p = os.path.join(tool_dir(), "CHANGELOG.txt")
    if not os.path.isfile(p):
        print("No CHANGELOG.txt next to the tool.")
        return
    with open(p, "r", encoding="utf-8") as fh:
        print(fh.read())


# --------------------------------------------------------------------------- #
# Auth session with automatic refresh
# --------------------------------------------------------------------------- #
TRANSIENT = (500, 502, 503, 504, 408, 429)


def _retry(fn, what="request", tries=4, verbose=True):
    """Retry a call that failed for reasons that have nothing to do with the request.

    A dropped connection, a gateway hiccup or a 503 from the tenant is not a bug in the
    query and not something the operator can act on. Back off and try again rather than
    throwing a sixty line traceback at someone who just wants their results."""
    delay = 2.0
    last = None
    for attempt in range(1, tries + 1):
        try:
            r = fn()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError) as ex:
            last = ex
        else:
            if getattr(r, "status_code", 200) not in TRANSIENT:
                return r
            last = f"HTTP {r.status_code}"
        if attempt < tries:
            if verbose:
                print(f"  {what} failed ({type(last).__name__ if not isinstance(last, str) else last}), "
                      f"retrying in {delay:.0f}s  [{attempt}/{tries - 1}]")
            time.sleep(delay)
            delay *= 2
    raise FmsUnavailable(what, last)


class FmsUnavailable(Exception):
    def __init__(self, what, cause):
        self.what, self.cause = what, cause
        super().__init__(f"{what}: {cause}")


class FmsSession:
    def __init__(self, user, password, verbose=True, retry_password=False):
        self._user, self._password, self.verbose = user, password, verbose
        self._retry_password = retry_password and sys.stdin.isatty()
        self._attempts = 0
        self.access_token = self.refresh_token = None
        self._expiry = 0
        self.s = requests.Session()
        self._login()

    def _login(self):
        r = _retry(lambda: self.s.post(
            TOKEN_URL, data={"client_id": CLIENT_ID, "grant_type": "password",
                             "username": self._user, "password": self._password},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30),
            what="login", verbose=self.verbose)
        if r.status_code != 200:
            body = r.text[:300]
            if r.status_code == 401 and "invalid_grant" in body and self._retry_password:
                # A mistyped password should not throw the run away, but Keycloak locks
                # accounts after repeated failures, so this is deliberately limited.
                self._attempts += 1
                print(f"  wrong username or password for {self._user}")
                if self._attempts >= 3:
                    raise SystemExit("Three failed attempts. Stopping before the account "
                                     "gets locked out. Check the password in a browser first.")
                print(f"  attempt {self._attempts + 1} of 3 - too many will lock the account")
                self._password = getpass.getpass("FMS password: ")
                return self._login()
            raise SystemExit(
                f"Login failed ({r.status_code}): {body}\n"
                + ("The server rejected the username or password. If FMS_PASS is set in the "
                   "environment it may be stale; clear it and let the tool prompt instead.\n"
                   if "invalid_grant" in body else
                   f"If client '{CLIENT_ID}' is rejected, switch CLIENT_ID to "
                   f"'fg-topologyapi' (needs a client secret).\n"))
        j = r.json()
        self.access_token = j["access_token"]
        self.refresh_token = j.get("refresh_token")
        self._expiry = time.time() + int(j.get("expires_in", 900)) - 60
        if self.verbose:
            print("  authenticated OK")

    def _refresh(self):
        if not self.refresh_token:
            return self._login()
        try:
            r = _retry(lambda: self.s.post(
                TOKEN_URL, data={"client_id": CLIENT_ID, "grant_type": "refresh_token",
                                 "refresh_token": self.refresh_token},
                headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30),
                what="token refresh", tries=3, verbose=self.verbose)
        except FmsUnavailable:
            return self._login()
        if r.status_code != 200:
            return self._login()
        j = r.json()
        self.access_token = j["access_token"]
        self.refresh_token = j.get("refresh_token", self.refresh_token)
        self._expiry = time.time() + int(j.get("expires_in", 900)) - 60

    def _auth(self):
        if time.time() >= self._expiry:
            self._refresh()
        return {"Authorization": f"Bearer {self.access_token}"}

    def graphql(self, variables, operation="searchOpticalRouteByRtu"):
        r = _retry(lambda: self.s.post(
            GRAPHQL_URL, json={"operationName": operation, "variables": variables,
                               "query": SEARCH_QUERY},
            headers={**self._auth(), "Content-Type": "application/json"}, timeout=60),
            what="topology query", verbose=self.verbose)
        r.raise_for_status()
        j = r.json()
        if "errors" in j:
            raise RuntimeError(f"GraphQL errors: {j['errors']}")
        return j["data"]

    def rest_get(self, url, params=None):
        r = _retry(lambda: self.s.get(url, params=params, headers=self._auth(), timeout=60),
                   what="results query", verbose=self.verbose)
        r.raise_for_status()
        return r.json()

    def rest_post(self, url, payload=None):
        r = _retry(lambda: self.s.post(url, json=(payload or {}),
                   headers={**self._auth(), "Content-Type": "application/json"}, timeout=60),
                   what="results request", verbose=self.verbose)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return {"_raw": r.text[:2000]}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


WL_TARGETS = (1310, 1550, 1625)


def _to_nm(v):
    if isinstance(v, str):
        m = re.search(r'[\d.]+', v)
        v = m.group(0) if m else v
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v < 0.01:
        v *= 1e9
    nm = int(round(v))
    return min(WL_TARGETS, key=lambda t: abs(t - nm)) if nm else None


def fiber_key(name):
    if not name:
        return None
    m = re.search(r'F(\d{2,4})\b', name)
    return f"F{int(m.group(1)):04d}" if m else name.strip()


def search_by_rtu(session, token, page=50, verbose=True):
    """searchOpticalRouteByRtu matches RTU NAME. Returns all matching route nodes."""
    nodes, offset = [], 0
    while True:
        block = session.graphql({"search": token, "condition": {}, "orderBy": "NAME_ASC",
                                 "first": page, "offset": offset})["routeSearchResult"]
        total, got = block["totalCount"], block["nodes"]
        nodes.extend(got)
        if verbose:
            print(f"  fetched {len(nodes)}/{total}")
        offset += page
        if offset >= total or not got:
            break
    return nodes



def parse_desc(desc):
    """Route description carries the leg, e.g. 'RGAC:F197' = main fibre,
    'RGAC-L:F209' = loop-back fibre. Returns (tag, fibre_no, is_loop)."""
    if not desc:
        return (None, None, False)
    d = str(desc).strip()
    tag, _, ref = d.partition(":")
    tag = tag.strip()
    m = re.search(r'F?(\d{1,4})', ref or "")
    num = int(m.group(1)) if m else None
    is_loop = bool(re.search(r'-\s*L\b|-L$|\bLOOP', tag, re.I))
    return (tag, num, is_loop)


def desc_of(node):
    return (node.get("description") or "").strip()


def split_main_loop(nodes):
    """Split routes into (main, loopback) using the description's -L marker."""
    main, loop = [], []
    for n in nodes:
        _, _, is_loop = parse_desc(desc_of(n))
        (loop if is_loop else main).append(n)
    return main, loop


def collect_rtus(nodes):
    """RTUs with the cable each one serves.

    A site has an RTU per cable, so the RTU name alone does not say which cable you are
    about to pull. The route names do, and they come back with the search anyway, so the
    menu can just show it rather than leaving it to memory."""
    d = {}
    for n in nodes:
        rtu = n.get("rtu") or {}
        name = rtu.get("name") or n.get("rtuName")
        if not name:
            continue
        e = d.setdefault(name, {"name": name, "rtuId": n.get("rtuId"),
                                "site": (rtu.get("site") or {}).get("name"), "count": 0,
                                "cables": {}})
        e["count"] += 1
        m = re.match(r'^(F-[A-Z0-9]+-[A-Z0-9]+)', str(n.get("name") or ""))
        if m:
            e["cables"][m.group(1)] = e["cables"].get(m.group(1), 0) + 1
    for e in d.values():
        e["cable"] = ", ".join(k for k, _ in sorted(e["cables"].items(),
                                                    key=lambda x: -x[1])[:2]) or "?"
    return sorted(d.values(), key=lambda x: x["name"])


def _parse_events(elements, length_m):
    """Flatten iOLM Elements into per-event dicts (position m, per-wavelength loss +
    reflectance). Drops launch/receive (LinkStart/LinkEnd) from the list but returns the
    LinkStart 1550 loss as sw_loss (the 'SW Loss @1550' switch/launch loss for the ODF tab).
    Returns (events, sw_loss)."""
    raw = [_f(e.get("Position")) for e in elements]
    maxpos = max([abs(p) for p in raw if p is not None], default=None)
    scale = 1000 if (length_m and maxpos and (length_m / maxpos) > 100) else 1
    events, sw_loss = [], None
    for e in elements:
        st = e.get("Status")
        if st == "LinkStart":
            for w in (e.get("Results") or []):
                if _to_nm(w.get("Wavelength")) == 1550:
                    sw_loss = _f(w.get("Loss"))
        if st in ("LinkStart", "LinkEnd"):
            continue
        pos = _f(e.get("Position"))
        ev = {"position": (pos * scale) if pos is not None else None,
              "type": e.get("Type"), "status": st, "verdict": e.get("ElementVerdict")}
        for w in (e.get("Results") or []):
            nm = _to_nm(w.get("Wavelength"))
            if nm:
                ev[f"w{nm}"] = _f(w.get("Loss"))
                ev[f"r{nm}"] = _f(w.get("Reflectance"))
        events.append(ev)
    return events, sw_loss


def _parse_otdr_events(meas, length_m=None):
    """OTDR results DO carry an event table. It lives at
    OtdrMeasurements[0].Events and uses different field names to the iOLM Elements list,
    which is why it looked as though raw OTDR results had no events at all.
    Returns (events, sw_loss) in the same shape as _parse_events."""
    evs = meas.get("Events") or []
    wl = _to_nm(meas.get("Wavelength")) or 1550
    raw = [_f(e.get("Position")) for e in evs]
    maxpos = max([abs(p) for p in raw if p is not None], default=None)
    scale = 1000 if (length_m and maxpos and (length_m / maxpos) > 100) else 1
    out, sw_loss = [], None
    for e in evs:
        st = str(e.get("Status") or "")
        typ = str(e.get("Type") or "")
        if "SpanStart" in st or "LaunchLevel" in st or typ == "Launch Level":
            l = _f(e.get("Loss"))
            if l is not None and wl == 1550:
                sw_loss = l
            continue
        if "SpanEnd" in st or typ in ("End of Fiber", "End of Analysis"):
            continue
        pos = _f(e.get("Position"))
        ev = {"position": (pos * scale) if pos is not None else None,
              "type": typ or e.get("TypeCode"), "status": st or None,
              "verdict": e.get("EventVerdict")}
        ev[f"w{wl}"] = _f(e.get("Loss"))
        ev[f"r{wl}"] = _f(e.get("Reflectance"))
        out.append(ev)
    return out, sw_loss


def _otdr_meas(brief):
    lst = ((brief or {}).get("Measurement") or {}).get("OtdrMeasurements") or []
    if isinstance(lst, dict):
        lst = [lst]
    return lst[0] if lst else {}


def _latest_any(results, dfrom=None, dto=None):
    """The newest result of ANY test type in the window, whether or not it carries an
    event table. A raw OTDR has no Elements[] so it can never give splice-by-location,
    but it DOES carry LinkResults: overall link loss, length and the test date. That is
    the post-remedial 'is the fibre better now' figure, so we pull it alongside the
    analysed iOLM instead of ignoring it."""
    best = None
    for r in results:
        md = r.get("metadata") or {}
        t = md.get("TestTime", "") or ""
        if not t:
            continue
        if dfrom and t < dfrom:
            continue
        if dto and t > dto:
            continue
        if best is None or t > (best.get("time") or ""):
            lr = ((r.get("brief") or {}).get("LinkResults")) or {}
            els = (((r.get("brief") or {}).get("Measurement") or {}).get("Elements")) or []
            best = {"time": t, "type": md.get("TestType"), "length": _f(lr.get("Length")),
                    "events": len(els), "resultid": r.get("resultid"), "linkresults": bool(lr)}
            for w in (lr.get("Results") or []):
                nm = _to_nm(w.get("Wavelength"))
                if nm:
                    best[f"w{nm}"] = _f(w.get("Loss"))
    return best


def fetch_otdr_events(session, asset_id, resultid=None, dfrom=None, dto=None):
    """Pull the event table out of an OTDR result.

    The light projection used for the results list does not include OtdrMeasurements, so
    the events are invisible there. Ask for them explicitly, narrowest projection first,
    because the widest one drags the whole sample blob (about 300 kB per result) with it."""
    selects = ("resultid,metadata,brief/LinkResults,brief/Measurement/OtdrMeasurements/Events,"
               "brief/Measurement/OtdrMeasurements/Wavelength",
               "resultid,metadata,brief/LinkResults,brief/Measurement/OtdrMeasurements",
               "resultid,metadata,brief")
    for sel in selects:
        try:
            data = session.rest_get(RESULTS_URL, params={
                "$filter": adhoc_filter(asset_id), "$orderby": "metadata/TestTime desc",
                "$top": 5, "$skip": 0, "$select": sel})
        except Exception:
            continue
        for r in (data.get("results") or data.get("value") or []):
            if resultid and r.get("resultid") != resultid:
                continue
            md = r.get("metadata") or {}
            tt = md.get("TestTime", "") or ""
            if (dfrom and tt < dfrom) or (dto and tt > dto):
                continue
            meas = _otdr_meas(r.get("brief"))
            if meas.get("Events"):
                return r, meas
    return None, None


def fetch_iolm(session, asset_id, dfrom=None, dto=None, test_type="iOLM"):
    """Latest iOLM result for a route (AssetId), optionally within [dfrom, dto].
    Returns dict(testtime, length, star, verdict, w1310/w1550/w1625, orl*, events[])."""
    if not asset_id:
        return {"_empty_reason": "no_asset"}
    data = session.rest_get(RESULTS_URL, params={
        "$filter": adhoc_filter(asset_id), "$orderby": "metadata/TestTime desc",
        "$top": 30, "$skip": 0,
        "$select": "resultid,brief/GlobalVerdict,brief/LinkResults,brief/Measurement/Elements,metadata,birthCertificate"})
    results = data.get("results") or data.get("value") or []
    # Keep server order (newest first). OTDR results DO carry an event table, it just
    # lives under a different field that the list projection omits, so we no longer
    # demote them behind older iOLMs.
    fallback = None
    otdr_tried = False
    for r in results:                       # already newest-first
        md = r.get("metadata") or {}
        if test_type != "auto" and md.get("TestType") != test_type:
            continue
        brief = r.get("brief") or {}
        lr = brief.get("LinkResults")
        if not lr:
            continue
        tt = md.get("TestTime", "") or ""
        if dfrom and tt < dfrom:
            continue
        if dto and tt > dto:
            continue
        out = {"testtime": tt, "length": _f(lr.get("Length")),
               "star": _f(lr.get("LossStarRating")),
               "verdict": brief.get("GlobalVerdict"), "resultid": r.get("resultid")}
        for w in (lr.get("Results") or []):
            nm = _to_nm(w.get("Wavelength"))
            if nm:
                out[f"w{nm}"] = _f(w.get("Loss"))
                out[f"orl{nm}"] = _f(w.get("Orl"))
        els = ((brief.get("Measurement") or {}).get("Elements")) or []
        out["events"], out["sw_loss"] = _parse_events(els, out["length"])
        out["src_type"] = md.get("TestType")
        if not out["events"] and md.get("TestType") == "OTDR" and not otdr_tried \
                and test_type in ("auto", "OTDR"):
            otdr_tried = True
            rec2, meas2 = fetch_otdr_events(session, asset_id, r.get("resultid"), dfrom, dto)
            if meas2:
                out["events"], out["sw_loss"] = _parse_otdr_events(meas2, out["length"])
                out["src_type"] = "OTDR"
        # note any NEWER test that carries no event table: the classic pre/post-remedial
        # trap, where newer work would otherwise be invisible
        newer = []
        if out["events"]:
            for r2 in results:
                md2 = r2.get("metadata") or {}
                t2 = md2.get("TestTime", "") or ""
                if t2 > tt:
                    b2 = r2.get("brief") or {}
                    if not (((b2.get("Measurement") or {}).get("Elements")) or []):
                        newer.append((t2, md2.get("TestType")))
        if newer:
            newer.sort(reverse=True)
            out["newer_no_events"] = newer[0]
            out["newer_count"] = len(newer)
        # hybrid: keep the newest test of any type for the "loss now" comparison
        la = _latest_any(results, dfrom, dto)
        if la and (la.get("time") or "") > tt:
            out["latest"] = la
        if out["events"]:
            return out
        if fallback is None:
            fallback = out
    if fallback:
        return fallback
    # Work out, in plain terms, why nothing came back
    if not results:
        return {"_empty_reason": "no_results"}
    # results existed but none produced an "out" — figure out the dominant reason
    saw_type = False
    saw_linkresults = False
    saw_in_window = False
    for r in results:
        md = r.get("metadata") or {}
        if test_type == "auto" or md.get("TestType") == test_type:
            saw_type = True
            brief = r.get("brief") or {}
            if brief.get("LinkResults"):
                saw_linkresults = True
                tt = md.get("TestTime", "") or ""
                if (not dfrom or tt >= dfrom) and (not dto or tt <= dto):
                    saw_in_window = True
    if not saw_type:
        return {"_empty_reason": "wrong_type"}
    if not saw_linkresults:
        return {"_empty_reason": "no_linkresults"}
    if not saw_in_window:
        return {"_empty_reason": "out_of_window"}
    return {"_empty_reason": "no_results"}


def fetch_iolm_multi(session, asset_id, dfrom=None, dto=None, test_type="iOLM", count=2):
    """Up to `count` newest full results for ONE route (used for backsplice loops where
    both direction traces are saved under the same route name). Prefers results carrying
    all three wavelengths; falls back to partials to fill the count."""
    if not asset_id:
        return []
    data = session.rest_get(RESULTS_URL, params={
        "$filter": adhoc_filter(asset_id), "$orderby": "metadata/TestTime desc",
        "$top": 30, "$skip": 0,
        "$select": "resultid,brief/GlobalVerdict,brief/LinkResults,brief/Measurement/Elements,metadata,birthCertificate"})
    results = data.get("results") or data.get("value") or []
    if test_type == "auto":
        results = sorted(results, key=lambda r: 0 if ((r.get("metadata") or {}).get("TestType") == "iOLM") else 1)
    full, partial = [], []
    for r in results:                       # newest-first
        md = r.get("metadata") or {}
        if test_type != "auto" and md.get("TestType") != test_type:
            continue
        brief = r.get("brief") or {}
        lr = brief.get("LinkResults")
        if not lr:
            continue
        tt = md.get("TestTime", "") or ""
        if dfrom and tt < dfrom:
            continue
        if dto and tt > dto:
            continue
        out = {"testtime": tt, "length": _f(lr.get("Length")),
               "star": _f(lr.get("LossStarRating")),
               "verdict": brief.get("GlobalVerdict"), "resultid": r.get("resultid")}
        for w in (lr.get("Results") or []):
            nm = _to_nm(w.get("Wavelength"))
            if nm:
                out[f"w{nm}"] = _f(w.get("Loss"))
                out[f"orl{nm}"] = _f(w.get("Orl"))
        els = ((brief.get("Measurement") or {}).get("Elements")) or []
        out["events"], out["sw_loss"] = _parse_events(els, out["length"])
        if not out["events"] and md.get("TestType") == "OTDR" and test_type in ("auto", "OTDR"):
            _rec2, _meas2 = fetch_otdr_events(session, asset_id, r.get("resultid"), dfrom, dto)
            if _meas2:
                out["events"], out["sw_loss"] = _parse_otdr_events(_meas2, out["length"])
        if out["events"]:
            full.append(out)
        else:
            partial.append(out)
        if len(full) >= count:
            break
    return (full + partial)[:count]


def build_loop_rows(session, nodes, dfrom, dto, headline_wl=1550, label="", pairs=None,
                    reversed_map=False, per=12, verbose=True, limit=None, test_type="iOLM"):
    """Backsplice loops stored under ONE route name: for each route pull the TWO newest
    results (the two direction traces) and return (rowsA, rowsB) keyed identically so the
    standard bidirectional pairing/averaging applies. The fibre label shows the loop
    partner (e.g. F001 <-> F013)."""
    items = nodes[:limit] if limit else nodes
    rowsA, rowsB = [], []
    for i, n in enumerate(items):
        dets = []
        try:
            dets = fetch_iolm_multi(session, n.get("id"), dfrom, dto, test_type=test_type)
        except Exception as ex:
            if verbose and i < 3:
                print(f"   detail error for {n.get('name')}: {ex}")
        na = fibre_num(n.get("name"))
        nb = backsplice_partner(na, pairs or [(1, 2)], reversed_map, per) if na else None
        fid = f"{n.get('name')}  <-> F{nb:03d}" if nb else n.get("name")
        key = fiber_key(n.get("name"))

        def rec(det):
            det = det or {}
            return {"fid": fid, "date": det.get("testtime"),
                    "loss": det.get(f"w{headline_wl}"), "length": det.get("length"),
                    "star": det.get("star"), "w1310": det.get("w1310"),
                    "w1550": det.get("w1550"), "w1625": det.get("w1625"),
                    "verdict": det.get("verdict"), "_key": key,
                    "events": det.get("events") or [], "sw_loss": det.get("sw_loss"),
                    "_end": label, "desc": desc_of(n)}
        rowsA.append(rec(dets[0] if len(dets) > 0 else None))
        rowsB.append(rec(dets[1] if len(dets) > 1 else None))
        if verbose and (i + 1) % 10 == 0:
            print(f"   {i + 1}/{len(items)} loops pulled")
    return rowsA, rowsB


def dump_elements(session, asset_id):
    """Print the iOLM event/element table shape (splice/connector losses) for one route."""
    import json as _j
    data = session.rest_get(RESULTS_URL, params={
        "$filter": adhoc_filter(asset_id), "$orderby": "metadata/TestTime desc", "$top": 30,
        "$skip": 0, "$select": "resultid,brief/Measurement/Elements,brief/LinkResults,metadata"})
    results = data.get("results") or data.get("value") or []
    for r in results:
        md = r.get("metadata") or {}
        if md.get("TestType") != "iOLM":
            continue
        meas = (r.get("brief") or {}).get("Measurement") or {}
        els = meas.get("Elements")
        print(f"AssetId {asset_id}  TestTime {md.get('TestTime')}  "
              f"element_count={len(els) if els else 0}")
        print("Measurement keys:", list(meas.keys()))
        print("\nFIRST 4 ELEMENTS:")
        print(_j.dumps(els[:4], indent=2)[:6000] if els else "(no Elements)")
        return
    print("(no iOLM result with Measurement.Elements found)")


def build_odf(rowsA_raw, rowsB_raw):
    """The ODF connector at each end = the first event (smallest position) after launch.
    Returns one record per fibre with each end's switch loss, position, event type,
    reflectance@1550 and per-wavelength loss (for the client-style ODF Connection tab)."""
    bmap = {r["_key"]: r for r in rowsB_raw}

    def first_event(row):
        # the ODF connector is the first real event after the launch (skip <=0 launch artefacts)
        evs = [e for e in (row.get("events") or []) if e.get("position") is not None and e["position"] > 0]
        return min(evs, key=lambda e: e["position"]) if evs else None

    out = []
    for ra in rowsA_raw:
        rb = bmap.get(ra["_key"])
        ea = first_event(ra)
        eb = first_event(rb) if rb else None
        rec = {"fid": ra["fid"], "a_sw": ra.get("sw_loss"), "b_sw": rb.get("sw_loss") if rb else None}
        for tag, e in (("a", ea), ("b", eb)):
            rec[f"{tag}_pos"] = e.get("position") if e else None
            rec[f"{tag}_event"] = e.get("type") if e else None
            rec[f"{tag}_refl"] = e.get("r1550") if e else None
            for w in (1310, 1550, 1625):
                rec[f"{tag}_w{w}"] = e.get(f"w{w}") if e else None
        out.append(rec)
    return out


def build_end_rows(session, nodes, dfrom, dto, headline_wl=1550, end_label="", verbose=True, limit=None, test_type="iOLM"):
    items = nodes[:limit] if limit else nodes
    rows, errs = [], 0
    DBG.w("")
    DBG.w(f"PULLING {end_label or 'END'}  ({len(items)} fibres selected)")
    for i, n in enumerate(items):
        det = {}
        fibre_name = n.get("name") or "?"
        fibre_short = fiber_key(fibre_name) or fibre_name
        try:
            det = fetch_iolm(session, n.get("id"), dfrom, dto, test_type)
        except Exception as ex:
            errs += 1
            DBG.fail(end_label or "END", fibre_short, f"connection/parse error: {ex}")
            if verbose and errs <= 3:
                print(f"   detail error for {n.get('name')}: {ex}")
        else:
            if det.get("testtime"):
                st = det.get("src_type") or "iOLM"
                DBG.ok(end_label or "END", fibre_short,
                       f"{st} {det.get('testtime','')[:16].replace('T',' ')}")
            else:
                reason_code = det.get("_empty_reason", "no_results")
                reason = EMPTY_REASONS.get(reason_code, reason_code)
                if reason_code in ("wrong_type", "out_of_window", "no_linkresults"):
                    DBG.skip(end_label or "END", fibre_short, reason)
                else:
                    DBG.fail(end_label or "END", fibre_short, reason)
        rows.append({
            "fid": n.get("name"), "date": det.get("testtime"),
            "loss": det.get(f"w{headline_wl}"), "length": det.get("length"),
            "star": det.get("star"), "w1310": det.get("w1310"),
            "w1550": det.get("w1550"), "w1625": det.get("w1625"),
            "verdict": det.get("verdict"), "_key": fiber_key(n.get("name")),
            "events": det.get("events") or [], "sw_loss": det.get("sw_loss"), "_end": end_label,
            "desc": desc_of(n), "newer_no_events": det.get("newer_no_events"),
            "newer_count": det.get("newer_count"), "latest": det.get("latest"),
            "src_type": det.get("src_type")})
        if verbose and (i + 1) % 50 == 0:
            print(f"   {i + 1}/{len(items)} fibres pulled")
    return rows


# ===================== RAW OTDR TRACE -> READABLE DATA =====================
# A raw OTDR result carries no event table, only the trace itself: tens of thousands of
# backscatter power samples. Everything below turns that into numbers you can read:
# decode the samples, build a distance axis, and measure the loss step at a known
# location the same way an OTDR does it, with a least-squares fit either side.

def _b64ish(x):
    if not isinstance(x, str) or len(x) < 2000:
        return False
    sample = x[:200]
    ok = sum(1 for ch in sample if ch.isalnum() or ch in "+/=-_")
    return ok >= len(sample) - 2


def find_trace_payload(obj, path="", depth=0, best=None):
    """Walk the whole result record looking for the trace samples, wherever they live.
    A trace is either a very long base64 string or a very long list of numbers, so we
    hunt for both rather than relying on one documented field name."""
    if best is None:
        best = {"score": 0, "path": None, "value": None, "kind": None}
    if depth > 8:
        return best
    if isinstance(obj, dict):
        for k, v in obj.items():
            find_trace_payload(v, f"{path}.{k}" if path else k, depth + 1, best)
    elif isinstance(obj, list):
        nums = [x for x in obj[:50] if isinstance(x, (int, float))]
        if len(obj) >= 500 and len(nums) == len(obj[:50]):
            if len(obj) > best["score"]:
                best.update(score=len(obj), path=path, value=obj, kind="numeric list")
        else:
            for i, v in enumerate(obj[:40]):
                find_trace_payload(v, f"{path}[{i}]", depth + 1, best)
    elif _b64ish(obj):
        if len(obj) > best["score"]:
            best.update(score=len(obj), path=path, value=obj, kind="base64 string")
    return best


def describe_record(obj, path="", depth=0, out=None, maxdepth=6):
    """Compact map of a result record: every key, its type and size. Used when the trace
    cannot be found, so the structure can be inspected instead of guessed at."""
    if out is None:
        out = []
    if depth > maxdepth:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            pk = f"{path}.{k}" if path else k
            if isinstance(v, (dict, list)):
                out.append(f"{pk}: {type(v).__name__}[{len(v)}]")
                describe_record(v, pk, depth + 1, out, maxdepth)
            elif isinstance(v, str):
                out.append(f"{pk}: str[{len(v)}]" + ("" if len(v) > 80 else f" = {v!r}"))
            else:
                out.append(f"{pk}: {v!r}")
    elif isinstance(obj, list):
        if obj and isinstance(obj[0], (dict, list)):
            describe_record(obj[0], f"{path}[0]", depth + 1, out, maxdepth)
        elif obj:
            out.append(f"{path}[]: {len(obj)} x {type(obj[0]).__name__}")
    return out


def _decode_candidates(blob):
    """Every plausible way of reading the sample bytes, best first.

    The width, endianness and unit are all undocumented, and no single shape test settles
    them: smoothness is scale invariant, and unsigned data misread as signed still looks
    smooth because only the values above 32767 wrap. So this returns candidates and lets
    the caller validate each one against physics."""
    import base64, struct
    if isinstance(blob, (list, tuple)):
        return [(1e9, "already numeric", [float(x) for x in blob])]
    if not isinstance(blob, str) or len(blob) < 64:
        return []
    try:
        raw = base64.b64decode(blob, validate=False)
    except Exception:
        return []
    cands = []
    layouts = [("<u2", "<H", 2), (">u2", ">H", 2), ("<u4", "<I", 4), (">u4", ">I", 4),
               ("<i2", "<h", 2), (">i2", ">h", 2), ("<i4", "<i", 4),
               ("<f4", "<f", 4), (">f4", ">f", 4), ("<f8", "<d", 8)]
    for name, fmt, width in layouts:
        n = len(raw) // width
        if n < 500:
            continue
        try:
            vals = [v[0] for v in struct.iter_unpack(fmt, raw[:n * width])]
        except Exception:
            continue
        try:
            lo, hi = min(vals), max(vals)
        except (TypeError, ValueError):
            continue
        if lo != lo or hi != hi or hi in (float('inf'), float('-inf')):
            continue
        rng = hi - lo
        # unsigned samples read as signed wrap: everything above half scale flips negative,
        # which leaves the values straddling zero with a span near full scale. Genuine
        # signed data never looks like that, so reject it rather than decode it upside down.
        if "i" in name and lo < 0 < hi and rng > 0.8 * (2 ** (8 * width - 1)):
            continue
        if not any(0.5 <= rng * sc <= 80.0 for sc in (1.0, 0.1, 0.01, 0.001, 0.0001)):
            continue
        m = min(n, 6000)
        mad = sum(abs(vals[i] - vals[i - 1]) for i in range(1, m)) / float(m - 1)
        if mad <= 0:
            continue
        smooth = rng / mad
        if smooth < 5:
            continue
        dec = sum(1 for i in range(1, m) if vals[i] <= vals[i - 1])
        frac = max(dec / float(m - 1), 1.0 - dec / float(m - 1))
        score = math.log(smooth) + frac
        cands.append((score, f"{name} (smoothness {smooth:.0f}:1)", vals))
    cands.sort(key=lambda c: -c[0])
    return cands[:6]


def _orient(vals):
    """Make the trace fall with distance. Judged on the fibre only: the samples after the
    fibre end sit in the noise floor, so comparing first sample with last flips the trace
    upside down and reverses the sign of every splice."""
    n = len(vals)
    rng = max(vals) - min(vals)
    cliff = n
    for i in range(1, min(n, 200000)):
        if abs(vals[i] - vals[i - 1]) > 0.25 * rng:
            cliff = i
            break
    m = max(min(cliff, n), 200)
    f = _lsq(list(range(m)), vals[:m])
    if f and f[0] > 0:
        top = max(vals)
        return [top - x for x in vals], True
    return vals, False


def _decode_points(blob):
    """Kept for the simple case: best candidate, oriented."""
    c = _decode_candidates(blob)
    if not c:
        return None, "no sensible layout found"
    vals, inv = _orient(list(c[0][2]))
    return vals, c[0][1] + (" (inverted)" if inv else "")


def _find_spacing_key(obj, path="", depth=0, hits=None):
    """Hunt the record for the sample spacing. The payload turned out to be nested
    (DataPoints.Points), so the spacing is very likely nested alongside it under a name
    we have not seen. Match on the name and on a physically plausible value."""
    if hits is None:
        hits = []
    if depth > 8:
        return hits
    if isinstance(obj, dict):
        for k, v in obj.items():
            pk = f"{path}.{k}" if path else k
            lk = k.lower()
            num = v if isinstance(v, (int, float)) and not isinstance(v, bool) else _f(v)
            if num is not None:
                named = any(t in lk for t in ("spacing", "resolution", "deltadistance",
                                              "sampledistance", "pointdistance", "dx",
                                              "step", "interval", "pulsedistance"))
                if named:
                    if 0.01 <= num <= 50:
                        hits.append((float(num), pk, "m"))
                    elif 0.00001 <= num <= 0.05:
                        hits.append((float(num) * 1000.0, pk, "km"))
            elif isinstance(v, (dict, list)):
                _find_spacing_key(v, pk, depth + 1, hits)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:20]):
            if isinstance(v, (dict, list)):
                _find_spacing_key(v, f"{path}[{i}]", depth + 1, hits)
    return hits


def _end_index(vals):
    """Index of the fibre end: the last sample still above the noise floor. An OTDR
    acquires past the end of the fibre, so the trace is longer than the link. Pinning the
    END to the reported link length is the only sound way to calibrate the distance axis;
    dividing the whole trace by the length silently compresses everything."""
    n = len(vals)
    if n < 200:
        return n - 1
    tail = sorted(vals[int(n * 0.98):])
    if not tail:
        return n - 1
    floor = tail[len(tail) // 2]
    head = sorted(vals[:int(n * 0.02)])
    top = head[len(head) // 2]
    span = top - floor
    if span <= 0:
        return n - 1
    thresh = floor + span * 0.04          # comfortably clear of the noise
    for i in range(n - 1, int(n * 0.05), -1):
        if vals[i] > thresh:
            return i
    return n - 1


def _expected_slope(record):
    """The record states the attenuation the instrument measured (Results.AveragedLoss,
    dB/km). Anchoring the sample unit to that beats assuming a textbook 0.2."""
    meas = _otdr_meas((record or {}).get("brief") if isinstance(record, dict) else None)
    v = _f(((meas.get("Results") or {}) if isinstance(meas, dict) else {}).get("AveragedLoss"))
    if v is not None and 0.05 <= v <= 1.0:
        return v, "record AveragedLoss"
    return 0.21, "assumed 0.21 dB/km"


def _pick_scale(vals, spacing_m, upto=None, target=0.21):
    """Samples are stored as integers in an undocumented unit: dB, tenths, hundredths or
    thousandths. Shape alone cannot tell them apart because smoothness is scale
    invariant, so we use physics: a real fibre attenuates about 0.2 dB/km at 1550 nm.
    Pick the multiplier that puts the measured slope in that range."""
    n = len(vals)
    hi = int(upto) if upto else int(n * 0.6)
    hi = max(min(hi, n), 200)
    lo = int(hi * 0.1)
    xs = [i * spacing_m for i in range(lo, hi)]
    fit = _lsq(xs, vals[lo:hi])
    if not fit or fit[0] == 0:
        return 0.001, "default (slope unmeasurable)"
    slope_km_raw = -fit[0] * 1000.0          # in stored units per km
    best, bestd = None, None
    for sc in (1.0, 0.1, 0.01, 0.001, 0.0001):
        s_km = slope_km_raw * sc
        if not (0.05 <= s_km <= 1.0):
            continue
        d = abs(s_km - target)
        if bestd is None or d < bestd:
            best, bestd = sc, d
    if best is None:
        return 0.001, f"default (slope {slope_km_raw:.4g}/km unrecognised)"
    return best, f"{best} dB per unit -> {slope_km_raw * best:.3f} dB/km"


def _spacing_m(meas, npts, length_m=None, vals=None, record=None):
    """Metres per sample.

    Preference order: a spacing field stated anywhere in the record, then calibration
    against the detected fibre end. The end matters because an OTDR keeps acquiring past
    the fibre, so spreading the link length across the WHOLE trace compresses the distance
    axis and every location then gets measured in the wrong place."""
    for src in (meas, record):
        if not src:
            continue
        hits = _find_spacing_key(src)
        if hits:
            v, pth, unit = hits[0]
            return v, f"{pth}" + (" (km)" if unit == "km" else "")
    if length_m and vals:
        ei = _end_index(vals)
        if ei > 50:
            return float(length_m) / ei, f"calibrated on fibre end at sample {ei} of {len(vals)}"
    if length_m and npts:
        return float(length_m) / max(npts - 1, 1), "spread over whole trace (last resort)"
    return 1.0, "unknown, assumed 1 m"


def otdr_trace(record):
    """Decode one OTDR result record into a readable trace."""
    brief = record.get("brief") or record.get("measurement") or {}
    meas = (brief.get("Measurement") or brief) if isinstance(brief, dict) else {}
    lst = meas.get("OtdrMeasurements") or meas.get("Otdrmeasurements") or []
    if isinstance(lst, dict):
        lst = [lst]
    if not lst:
        cand = find_trace_payload(record)
        if cand["value"] is None:
            return {"error": "no OtdrMeasurements and no trace-sized payload anywhere",
                    "record": record}
        lst = [{"DataPoints": cand["value"], "_path": cand["path"]}]
    m0 = lst[0]
    blob = m0.get("DataPoints") or m0.get("dataPoints")
    src = "OtdrMeasurements[0].DataPoints"
    cands = _decode_candidates(blob)
    if not cands:
        found = find_trace_payload(record)
        if found["value"] is not None:
            cands = _decode_candidates(found["value"])
            src = f"{found['path']} ({found['kind']}, {found['score']})"
    if not cands:
        return {"error": "no sample data found in the record", "keys": sorted(m0.keys())[:25],
                "record": record}
    lr = (brief.get("LinkResults") or {}) if isinstance(brief, dict) else {}
    length = None
    try:
        length = float(lr.get("Length"))
    except (TypeError, ValueError):
        pass
    wl = None
    for k in ("Wavelength", "wavelength"):
        if m0.get(k) is not None:
            wl = _to_nm(m0.get(k))
            break

    # Validate each candidate all the way through instead of trusting the top score.
    # The test is physics: real single mode fibre attenuates about 0.2 dB/km at 1550 nm,
    # so a reading that implies something wildly different has been decoded wrongly.
    chosen = None
    for score, how, vals in cands:
        v, inv = _orient(list(vals))
        sp, spsrc = _spacing_m(m0, len(v), length, vals=v, record=record)
        ei = _end_index(v)
        if length and ei > 50 and not spsrc.startswith("calibrated"):
            pass
        tgt, tgtsrc = _expected_slope(record)
        sc, scsrc = _pick_scale(v, sp, upto=ei, target=tgt)
        w = [x * sc for x in v] if sc != 1.0 else v
        f = _lsq([i * sp for i in range(int(ei * 0.1), int(ei * 0.9))],
                 w[int(ei * 0.1):int(ei * 0.9)])
        slope = round(-f[0] * 1000.0, 3) if f else None
        cand = {"db": w, "n": len(w), "spacing": sp, "spacing_src": spsrc, "end_idx": ei,
                "slope_km": slope, "wl": wl, "length": length,
                "how": f"{how}{' (inverted)' if inv else ''} from {src}"}
        cand["target_km"] = tgt
        cand["how"] += f", scale {sc} dB per unit -> {slope} dB/km " \
                       f"(target {tgt}, {tgtsrc})"
        if slope is not None and abs(slope - tgt) <= max(0.06, tgt * 0.3):
            chosen = cand
            break
        if chosen is None:
            chosen = cand                      # keep the best effort if nothing validates
    pts = chosen["db"]
    sp = chosen["spacing"]
    chosen["range_m"] = round(len(pts) * sp)
    chosen["dist"] = [i * sp for i in range(len(pts))]
    return chosen


def _lsq(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    b = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / sxx
    return b, my - b * mx


def splice_loss_at(tr, pos_m, win=250.0, gap=30.0):
    """Loss of the event at pos_m, measured the way an OTDR does: fit the backscatter
    slope either side, extrapolate both fits to the event, take the step between them.
    The gap skips the dead zone right at the event. Returns (loss_dB, quality)."""
    dist, db, sp = tr["dist"], tr["db"], tr["spacing"]
    if sp <= 0:
        return None, "no distance axis"
    i = int(round(pos_m / sp))
    g = max(int(gap / sp), 2)
    w = max(int(win / sp), 8)
    # shrink the fit windows rather than give up near the ends of the trace
    w = min(w, i - g, len(db) - 1 - i - g)
    if w < 8:
        return None, "event too close to the trace ends"
    l0, l1 = i - g - w, i - g
    r0, r1 = i + g, i + g + w
    fl = _lsq(dist[l0:l1], db[l0:l1])
    fr = _lsq(dist[r0:r1], db[r0:r1])
    if not fl or not fr:
        return None, "fit failed"
    left_at = fl[0] * dist[i] + fl[1]
    right_at = fr[0] * dist[i] + fr[1]
    loss = left_at - right_at
    # sanity: backscatter slope should be a gentle fall, roughly 0.15-0.4 dB/km
    slope_kmL = -fl[0] * 1000.0
    slope_kmR = -fr[0] * 1000.0
    q = "ok"
    if not (0.0 <= slope_kmL <= 1.2) or not (0.0 <= slope_kmR <= 1.2):
        q = f"noisy fit ({slope_kmL:.2f}/{slope_kmR:.2f} dB/km)"
    return round(loss, 3), q


def fetch_otdr_record(session, asset_id, dfrom=None, dto=None):
    """Newest OTDR result for a route, WITH the trace payload attached.

    The list endpoint returns a projection that usually omits the samples, so once the
    result is identified we go back for the full record by id, and fall back to the
    otdr/extract endpoint. Whichever response actually carries trace-sized data wins."""
    pick = None
    for sel in ("resultid,metadata,brief,measurement", "resultid,metadata,brief"):
        try:
            data = session.rest_get(RESULTS_URL, params={
                "$filter": adhoc_filter(asset_id), "$orderby": "metadata/TestTime desc",
                "$top": 10, "$skip": 0, "$select": sel})
        except Exception:
            continue
        for r in (data.get("results") or data.get("value") or []):
            md = r.get("metadata") or {}
            tt = md.get("TestTime", "") or ""
            if md.get("TestType") != "OTDR":
                continue
            if (dfrom and tt < dfrom) or (dto and tt > dto):
                continue
            pick = r
            break
        if pick:
            break
    if not pick:
        return None
    if find_trace_payload(pick)["value"] is not None:
        return pick
    rid = pick.get("resultid")
    if not rid:
        return pick
    for getter in (lambda: session.rest_get(RESULTS_URL + str(rid)),
                   lambda: session.rest_get(RESULTS_URL + str(rid), params={"$expand": "measurement"}),
                   lambda: session.rest_post(RESULTS_URL + f"{rid}/otdr/extract")):
        try:
            full = getter()
        except Exception:
            continue
        if not isinstance(full, dict):
            continue
        if isinstance(full.get("results"), list) and full["results"]:
            full = full["results"][0]
        if find_trace_payload(full)["value"] is not None:
            full.setdefault("metadata", pick.get("metadata"))
            full.setdefault("brief", pick.get("brief"))
            return full
    return pick


def otdr_to_csv(tr, path, every=1):
    """Write the decoded trace out so it can be opened and plotted in Excel."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("distance_m,power_dB\n")
        for i in range(0, tr["n"], max(every, 1)):
            fh.write(f"{tr['dist'][i]:.2f},{tr['db'][i]:.4f}\n")
    return path

# =================== END RAW OTDR TRACE -> READABLE DATA ===================

def otdr_measure_all(session, nodes, schedule, dfrom=None, dto=None,
                     win=250.0, gap=30.0, csv_dir=None, verbose=True, extra_points=None):
    """Measure splice loss straight off the raw OTDR traces at every scheduled location.
    This is the fallback when no analysed iOLM exists: the trace still holds the events,
    they have simply never been extracted. Because the schedule tells us where to look,
    we measure at known distances instead of hunting blind for events, which is far more
    reliable. Returns (rows, diag)."""
    rows, diag = [], {"traces": 0, "failed": 0, "how": None, "errors": []}
    far = [p[0] for n in nodes for p in _sched_points(schedule, fibre_num(n.get("name") or ""))]
    diag["furthest"] = max(far) if far else None
    for n in nodes:
        rec = fetch_otdr_record(session, n["id"], dfrom, dto)
        if not rec:
            continue
        tr = otdr_trace(rec)
        if not tr or tr.get("error"):
            diag["failed"] += 1
            if len(diag["errors"]) < 5:
                diag["errors"].append(f"{n.get('name')}: {(tr or {}).get('error', 'no trace in record')}")
            if not diag.get("dumped"):
                diag["dumped"] = "otdr_structure.txt"
                try:
                    with open("otdr_structure.txt", "w", encoding="utf-8") as fh:
                        fh.write(f"route {n.get('name')}\n")
                        fh.write("map of the OTDR result record (key: type[size]):\n\n")
                        for line in describe_record(rec):
                            fh.write(line[:300] + "\n")
                except Exception:
                    diag["dumped"] = None
            continue
        diag["traces"] += 1
        if not diag.get("fields"):
            diag["fields"] = "otdr_fields.txt"
            try:
                with open("otdr_fields.txt", "w", encoding="utf-8") as fh:
                    fh.write(f"route {n.get('name')}  (sample data stripped, field names only)\n\n")
                    for line in describe_record(rec):
                        fh.write(line[:300] + "\n")
            except Exception:
                diag["fields"] = None
        if not diag["how"]:
            diag["how"] = (f"{tr['how']}; {tr['n']} samples at {tr['spacing']:.4g} m "
                           f"({tr['spacing_src']}); acquisition range {tr['range_m']} m; "
                           f"fibre slope {tr['slope_km']} dB/km")
            diag["slope_km"] = tr.get("slope_km")
            diag["target_km"] = tr.get("target_km")
            diag["range_m"] = tr.get("range_m")
            diag["spacing"] = tr.get("spacing")
            diag["spacing_src"] = tr.get("spacing_src")
        tt = ((rec.get("metadata") or {}).get("TestTime") or "")[:19]
        fno = fibre_num(n.get("name") or "")
        # unscheduled joints get measured too, otherwise their columns would be one real
        # reading and a wall of nominals
        pts = _sched_points(schedule, fno) + list(extra_points or [])
        if csv_dir and diag["traces"] <= 3:
            try:
                otdr_to_csv(tr, os.path.join(csv_dir, f"trace_{n.get('name')}.csv"))
            except Exception:
                pass
        for d, loc, leg in pts:
            if d is None or d <= 0:
                continue
            loss, q = splice_loss_at(tr, d, win=win, gap=gap)
            rows.append({"fid": n.get("name"), "location": loc, "leg": leg, "sched_m": d,
                         "desc": desc_of(n), "is_loop": parse_desc(desc_of(n))[2],
                         "col": f"{loc} ({leg})" if leg else loc,
                         "loss": loss, "quality": q, "wl": tr.get("wl"), "date": tt,
                         "source": "raw OTDR trace"})
        if verbose and diag["traces"] % 10 == 0:
            print(f"   {diag['traces']} traces analysed")
    return rows, diag


def find_route(session, text, scan="RTU"):
    """Which RTU holds the routes whose names contain TEXT.

    An RTU is one end of several cables, and the route name says which cable. Picking the
    right RTU but the wrong cable gives a full, healthy looking report built on the wrong
    fibres, so this answers 'where do the F-SGIC-SNBC routes live' directly."""
    want = str(text).strip().lower()
    print(f"Searching every route for '{text}' ...")
    nodes = search_by_rtu(session, scan)
    hits = {}
    for n in nodes:
        nm = str(n.get("name") or "")
        if want not in nm.lower():
            continue
        rtu = (n.get("rtu") or {})
        key = rtu.get("name") or n.get("rtuName") or "?"
        e = hits.setdefault(key, {"n": 0, "site": (rtu.get("site") or {}).get("name"),
                                  "sample": nm})
        e["n"] += 1
    if not hits:
        print(f"  no routes matched '{text}'")
        return
    print(f"\n  routes matching '{text}':")
    for k in sorted(hits):
        h = hits[k]
        print(f"    {k:28s} site={str(h['site']):18s} {h['n']:4d} routes   e.g. {h['sample']}")
    ks = sorted(hits)
    if len(ks) == 2:
        print(f"\n  Two RTUs hold this cable, so they are its two ends. That is a "
              f"bi-directional job:")
        print(f"    python fms_pull.py --rtu-a {ks[0]} --rtu-b {ks[1]} --mode bidir")
    elif len(ks) == 1:
        print(f"\n  One RTU holds this cable, so it is tested from a single end:")
        print(f"    python fms_pull.py --rtu-a {ks[0]}")
    else:
        print("\n  Several RTUs hold routes with this name. Pick the two that are the "
              "ends of the cable you mean.")


def route_families(rows, limit=4):
    """Group route names by their cable prefix, e.g. F-RGAC-SNBC-A-R432-F001 -> F-RGAC-SNBC."""
    fam = {}
    for r in rows or []:
        nm = str(r.get("fid") or "")
        m = re.match(r'^(F-[A-Z0-9]+-[A-Z0-9]+)', nm)
        key = m.group(1) if m else (nm[:14] or "?")
        fam[key] = fam.get(key, 0) + 1
    return sorted(fam.items(), key=lambda x: -x[1])[:limit]


def _client_hdr_map(ws, hrow, ncol):
    """Column indices for the client report's two mirrored blocks.

    The E2E and ODF sheets put End A's columns on the left and End B's mirrored on the
    right, sharing one header row, so the same header text appears twice. Scanning left to
    right gives the A block and right to left gives the B block."""
    def canon(h):
        h = str(h or "").strip().lower()
        if h.startswith("fiber id") or h.startswith("fibre id"):
            return "fid"
        if h.startswith("test date"):
            return "date"
        if h == "direction":
            return "dir"
        if "system loss" in h:
            return "sysloss"
        if h.startswith("length"):
            return "length"
        if "star" in h:
            return "star"
        if h.startswith("sw loss"):
            return "sw"
        if h.startswith("position"):
            return "position"
        if h == "event":
            return "event"
        if h.startswith("reflect"):
            return "refl"
        m = re.search(r'loss@(\d{3,4})', h.replace(" ", ""))
        if m:
            return "w" + m.group(1)
        return None
    left, right = {}, {}
    for c in range(1, ncol + 1):
        k = canon(ws.cell(hrow, c).value)
        if k and k not in left:
            left[k] = c
    for c in range(ncol, 0, -1):
        k = canon(ws.cell(hrow, c).value)
        if k and k not in right:
            right[k] = c
    return left, right


def _find_hdr(ws, first_col_text=("fiber id", "fibre id"), limit=12):
    for r in range(1, min(ws.max_row, limit) + 1):
        v = str(ws.cell(r, 1).value or "").strip().lower()
        if any(v.startswith(t) for t in first_col_text):
            return r
    return None


def load_client_report(path):
    """Read a client 'Customer Cable Report' workbook.

    Only the sheets that carry comparable numbers are read: the end-to-end losses per
    fibre in both directions, and the splice list. Everything is keyed on the fibre ID and
    the event position so it can be lined up with a fresh pull."""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    out = {"e2e": [], "splices": [], "siteA": None, "siteB": None,
           "cable": None, "dates": set()}
    for nm in wb.sheetnames:
        low = nm.lower()
        ws = wb[nm]
        if "e2e" in low or "loss report" in low:
            hrow = _find_hdr(ws)
            if not hrow:
                continue
            out["siteA"] = ws.cell(1, 1).value
            L, R = _client_hdr_map(ws, hrow, ws.max_column)
            out["siteB"] = ws.cell(1, R.get("fid", ws.max_column)).value or \
                next((ws.cell(1, c).value for c in range(ws.max_column, 1, -1)
                      if ws.cell(1, c).value), None)
            for r in range(hrow + 1, ws.max_row + 1):
                fid = ws.cell(r, L.get("fid", 1)).value
                if not fid:
                    continue
                rec = {"fid": str(fid).strip()}
                for side, M in (("a", L), ("b", R)):
                    for k in ("date", "sysloss", "length", "star", "w1310", "w1550", "w1625"):
                        c = M.get(k)
                        rec[f"{side}_{k}"] = (_f(ws.cell(r, c).value)
                                              if c and k != "date" else
                                              (ws.cell(r, c).value if c else None))
                for d in (rec.get("a_date"), rec.get("b_date")):
                    if d:
                        out["dates"].add(str(d)[:10])
                m = re.match(r'^(F-[A-Z0-9]+-[A-Z0-9]+)', rec["fid"])
                if m and not out["cable"]:
                    out["cable"] = m.group(1)
                out["e2e"].append(rec)
        elif "splice" in low:
            hrow = _find_hdr(ws)
            if not hrow:
                continue
            L, _ = _client_hdr_map(ws, hrow, ws.max_column)
            for r in range(hrow + 1, ws.max_row + 1):
                fid = ws.cell(r, L.get("fid", 1)).value
                if not fid:
                    continue
                out["splices"].append({
                    "fid": str(fid).strip(),
                    "event": ws.cell(r, L["event"]).value if L.get("event") else None,
                    "position": _f(ws.cell(r, L["position"]).value) if L.get("position") else None,
                    "w1310": _f(ws.cell(r, L["w1310"]).value) if L.get("w1310") else None,
                    "w1550": _f(ws.cell(r, L["w1550"]).value) if L.get("w1550") else None,
                    "w1625": _f(ws.cell(r, L["w1625"]).value) if L.get("w1625") else None})
    out["dates"] = sorted(out["dates"])
    return out


def verify_client(client, rowsA_raw, rowsB_raw, wl=1550, tol=60.0, single=False,
                  splice_tol=150.0, margin=100.0):
    """Compare a client end-to-end report against a fresh pull, line by line.

    Two comparisons, because they fail for different reasons. End-to-end loss is one
    number per fibre per direction and any disagreement is either a different test or a
    different fibre. Splice loss is per event and has to be matched on position first,
    so the sheet also shows how far apart the two positions were: a big loss difference
    at a matching position is a real disagreement, whereas a small one at a position
    30 m out usually means you are looking at different joints."""
    rows = []
    A = {fiber_key(r.get("fid")): r for r in (rowsA_raw or [])}
    B = {fiber_key(r.get("fid")): r for r in (rowsB_raw or [])}
    for c in client.get("e2e", []):
        k = fiber_key(c["fid"])
        a, b = A.get(k), B.get(k)
        rec = {"kind": "E2E", "fid": c["fid"],
               "their_ab": c.get(f"a_w{wl}"), "their_ba": c.get(f"b_w{wl}"),
               "their_len": c.get("a_length"),
               "their_date": str(c.get("a_date") or "")[:19].replace("T", " "),
               "mine_ab": (a or {}).get(f"w{wl}"), "mine_ba": (b or {}).get(f"w{wl}"),
               "mine_len": (a or {}).get("length"),
               "mine_date": str((a or {}).get("date") or "")[:19].replace("T", " ")}
        for side in ("ab", "ba"):
            t, m = rec.get(f"their_{side}"), rec.get(f"mine_{side}")
            rec[f"d_{side}"] = (None if (t is None or m is None) else round(m - t, 3))
        ds = [abs(x) for x in (rec.get("d_ab"), rec.get("d_ba")) if x is not None]
        if a is None and b is None:
            rec["status"] = "NOT IN PULL"
        elif not ds:
            rec["status"] = "NO VALUE"
        elif max(ds) <= 0.05:
            rec["status"] = "MATCH"
        elif max(ds) <= 0.25:
            rec["status"] = "CLOSE"
        else:
            rec["status"] = "DIFFERS"
        rows.append(rec)

    if client.get("splices"):
        evs = paired_events(rowsA_raw, rowsB_raw, tol=splice_tol, wl=wl, merge=0.0,
                            margin=margin, single=single)
        by_f = {}
        for e in evs:
            by_f.setdefault(fiber_key(e.get("fid")), []).append(e)
        for c in client["splices"]:
            k = fiber_key(c["fid"])
            cand = [e for e in by_f.get(k, []) if e.get("position") is not None]
            hit, gap = None, None
            if cand and c.get("position") is not None:
                hit = min(cand, key=lambda e: abs(e["position"] - c["position"]))
                gap = round(hit["position"] - c["position"], 1)
                if abs(gap) > tol:
                    hit, gap = None, gap
            mine = None
            if hit:
                av, bv = hit.get(f"a{wl}"), hit.get(f"b{wl}")
                vals = [v for v in (av, bv) if v is not None]
                mine = round(sum(vals) / len(vals), 3) if vals else None
            t = c.get(f"w{wl}")
            d = (None if (t is None or mine is None) else round(mine - t, 3))
            rows.append({"kind": "Splice", "fid": c["fid"], "position": c.get("position"),
                         "their_ab": t, "mine_ab": mine, "d_ab": d, "gap": gap,
                         "found_at": (hit or {}).get("position"),
                         "status": ("NOT FOUND" if hit is None else
                                    "MATCH" if (d is not None and abs(d) <= 0.02) else
                                    "CLOSE" if (d is not None and abs(d) <= 0.08) else
                                    "NO VALUE" if d is None else "DIFFERS")})
    return rows


def data_check(rowsA_raw, rowsB_raw, labelA, labelB, assume_yes=False):
    """Show what the report is about to be built from, and stop for a yes.

    Everything downstream is only as good as the tests that got picked up, and a stale or
    partial pull looks identical to a good one once it is in a spreadsheet. Better to see
    the dates before the workbook exists than to find out after issuing it."""
    print("\n" + "=" * 68)
    print("  DATA CHECK - what this report will be built from")
    print("=" * 68)
    total_new, total_old, days, types = None, None, {}, {}
    nores, stale = 0, 0
    for rows, lab in ((rowsA_raw or [], labelA), (rowsB_raw or [], labelB)):
        if not rows or (rowsB_raw is rowsA_raw and lab == labelB):
            continue
        withdate = [r for r in rows if r.get("date")]
        fams = route_families(rows)
        famtxt = ", ".join(f"{k}-* ({v})" for k, v in fams)
        print(f"\n  {lab}: {len(rows)} traces, {len(withdate)} with a test")
        print(f"    cable: {famtxt}")
        for r in rows:
            d = str(r.get("date") or "")[:10]
            if not d:
                nores += 1
                continue
            days[d] = days.get(d, 0) + 1
            t = r.get("src_type") or "?"
            types[t] = types.get(t, 0) + 1
            if r.get("newer_no_events"):
                stale += 1
            total_new = d if (total_new is None or d > total_new) else total_new
            total_old = d if (total_old is None or d < total_old) else total_old
    if not days:
        print("\n  !! No results at all. Nothing to report on.")
        return False
    print("\n  Test dates being used:")
    for d in sorted(days, reverse=True):
        bar = "#" * min(40, max(1, days[d] * 40 // max(days.values())))
        print(f"    {d}   {days[d]:5d} fibres  {bar}")
    print(f"\n  Newest: {total_new}     Oldest: {total_old}")
    print(f"  Test types: " + ", ".join(f"{k} {v}" for k, v in sorted(types.items())))
    if nores:
        print(f"  !! {nores} trace(s) returned no result at all")
    if stale:
        print(f"  !! {stale} fibre(s) have a NEWER test that carries no event table")
    spread = len(days)
    if spread > 3:
        print(f"  !! results span {spread} different days - a mixed-vintage report")
    print("\n  Check the cable line above first: an RTU is one end of SEVERAL cables, and")
    print("  the wrong cable still gives a full, healthy looking report. If the test you")
    print("  want is on another cable, run --find-route SGIC-SNBC to see which RTU holds it.")
    print("=" * 68)
    if assume_yes or not sys.stdin.isatty():
        print("  (continuing without asking)")
        return True
    return _yesno("Build the report from these results?", True)


def latest_check(rowsA_raw, rowsB_raw=None, wl=1550, labelA="End A", labelB="End B"):
    """Hybrid comparison table: the analysed test we took the splice detail from, versus
    the newest test on the same fibre whatever its type. Gives 'loss then' vs 'loss now'
    so remedial work done after the last iOLM is still visible, even though a raw OTDR
    can never tell you WHICH location improved."""
    out = []
    for rows, lab in ((rowsA_raw or [], labelA), (rowsB_raw or [], labelB)):
        for r in rows:
            la = r.get("latest")
            then = r.get(f"w{wl}")
            now = (la or {}).get(f"w{wl}")
            if now is None and la:                       # OTDR often carries one wavelength only
                for w in (1550, 1310, 1625):
                    if la.get(f"w{w}") is not None:
                        now = la.get(f"w{w}"); break
            note = ""
            if not la:
                note = "no newer test"
            elif la.get("events"):
                note = "newer test is analysed - already used"
            elif now is None:
                note = "newer test carries no loss figure"
            else:
                note = "newer trace, no event table (splice detail stays at the date on the left)"
            out.append({"end": lab, "fid": r.get("fid"), "desc": r.get("desc"),
                        "ev_date": (r.get("date") or "")[:19], "ev_type": "iOLM",
                        "loss_then": then, "len_then": r.get("length"),
                        "latest_date": ((la or {}).get("time") or "")[:19],
                        "latest_type": (la or {}).get("type") or "",
                        "loss_now": now, "len_now": (la or {}).get("length"),
                        "change": (None if (then is None or now is None) else round(now - then, 3)),
                        "note": note})
    return out


def auto_filename(rtuA, rtuB=None, ribbons="", single=True, tag="", cable=""):
    """<dd-mm-yy>_<cable>_<ribbons>_<time>.xlsx, e.g. 08-10-26_RGAC2-SNBC_Rall_1132.xlsx.
    Leads with the test date then the cable ID (both ends joined, or the single end), so
    reports sort by date and the cable is obvious at a glance."""
    def _end(r):
        if not r:
            return ""
        raw = str(r.get("site") or r.get("name") or "")
        # 'Reading - RGAC' -> RGAC ; 'RTU2-SNBC-1991132' -> SNBC
        if " - " in raw:
            raw = raw.split(" - ")[-1]
        m = re.match(r'^RTU\d*-(.+?)-\d+$', raw)
        if m:
            raw = m.group(1)
        return re.sub(r'[^A-Za-z0-9]+', '', raw) or "RTU"
    ends = _end(rtuA)
    b = _end(rtuB)
    if b and not single and b != ends:
        ends = f"{ends}-{b}"
    # cable ID: a clean name passed in (e.g. from the route family) wins, else the ends
    cab = re.sub(r'[^A-Za-z0-9\-]+', '', str(cable or "").replace(" ", "-"))
    cab = re.sub(r'^F-', '', cab)
    cable_id = cab or ends or "cable"
    rb = re.sub(r'[^0-9,\-]+', '', str(ribbons or ""))
    rb = ("R" + rb.replace(",", "_").strip("-_")) if rb.strip("-_,") else "Rall"
    if tag:
        rb = f"{rb}-{re.sub(r'[^A-Za-z0-9]+', '', tag)}"
    return f"{time.strftime('%d-%m-%y')}_{cable_id}_{rb}_{time.strftime('%H%M')}.xlsx"


def _maxwl(ev):
    v = [ev.get(f"w{w}") for w in (1310, 1550, 1625) if ev.get(f"w{w}") is not None]
    return max(v) if v else None


def _premerge(events, merge_tol, wl):
    """Combine an END's own events that sit within merge_tol metres (a single splice the
    analysis split into pieces). Losses are summed; position is the loss-weighted centroid.
    merge_tol <= 0 disables this. Returns list of {pos,type,w1310,w1550,w1625,n}."""
    evs = [e for e in events if e.get("position") is not None]
    evs.sort(key=lambda e: e["position"])
    if merge_tol <= 0:
        return [{"pos": e["position"], "type": e.get("type"), "n": 1,
                 **{f"w{w}": e.get(f"w{w}") for w in (1310, 1550, 1625)}} for e in evs]
    out, cur = [], []
    for e in evs:
        if cur and e["position"] - cur[-1]["position"] > merge_tol:
            out.append(cur); cur = []
        cur.append(e)
    if cur:
        out.append(cur)
    merged = []
    for cl in out:
        rec = {"type": max(cl, key=lambda e: abs(e.get(f"w{wl}") or 0.0)).get("type"), "n": len(cl)}
        for w in (1310, 1550, 1625):
            vals = [e.get(f"w{w}") for e in cl if e.get(f"w{w}") is not None]
            rec[f"w{w}"] = sum(vals) if vals else None
        wsum = sum(abs(e.get(f"w{wl}") or 0.0) for e in cl)
        rec["pos"] = (sum(e["position"] * abs(e.get(f"w{wl}") or 0.0) for e in cl) / wsum) \
            if wsum else (sum(e["position"] for e in cl) / len(cl))
        merged.append(rec)
    return merged


def _eff(rec, w, nominal):
    if rec.get("single"):
        return rec.get(f"a{w}")
    """Averaged loss at wavelength w, substituting `nominal` for a direction that saw
    no event (one measured, one blank -> (measured+nominal)/2). None if neither side saw it."""
    a, b = rec.get(f"a{w}"), rec.get(f"b{w}")
    if a is None and b is None:
        return None
    return ((a if a is not None else nominal) + (b if b is not None else nominal)) / 2


SPLICE_TYPES = ("Splice", "Group")   # iOLM "Group" = a merged splice event; both are splices


def paired_events(rowsA_raw, rowsB_raw, tol=150.0, wl=1550, merge=0.0, margin=100.0,
                  single=False):
    """Core pairing engine (validated): per fibre, map End-B events into the End-A frame
    proportionally and pair GLOBALLY closest-first within `tol` m. Returns ALL paired
    records: {fid,_key,position,type,dirs,merged,a1310..b1625,_keep,_length}."""
    bmap = {} if single else {r["_key"]: r for r in rowsB_raw}
    out = []
    for ra in rowsA_raw:
        rb = bmap.get(ra["_key"])
        lenA = ra.get("length")
        lenB = rb.get("length") if rb else None
        length = lenA or lenB
        A = _premerge(ra.get("events", []), merge, wl)
        B = _premerge(rb.get("events", []) if rb else [], merge, wl)
        bframe = []
        for e in B:
            pb = e["pos"]
            if lenA and lenB and lenB > 0:
                bframe.append(lenA * (1 - pb / lenB))
            elif length is not None:
                bframe.append(length - pb)
            else:
                bframe.append(pb)
        cand = []
        for i, ea in enumerate(A):
            for j, af in enumerate(bframe):
                d = abs(ea["pos"] - af)
                if d <= tol:
                    cand.append((d, i, j))
        cand.sort()
        pair, usedA, usedB = {}, set(), set()
        for d, i, j in cand:
            if i in usedA or j in usedB:
                continue
            usedA.add(i); usedB.add(j); pair[i] = j

        def keep(pos):
            return (length is None) or (margin < pos < length - margin)

        for i, ea in enumerate(A):
            m = B[pair[i]] if i in pair else None
            rec = {"fid": ra["fid"], "_key": ra["_key"], "position": ea["pos"],
                   "type": ea.get("type"), "dirs": 2 if m else 1,
                   "merged": ea.get("n", 1) + (m.get("n", 0) if m else 0),
                   "_keep": keep(ea["pos"]), "_length": length, "single": single}
            for w in (1310, 1550, 1625):
                rec[f"a{w}"] = ea.get(f"w{w}")
                rec[f"b{w}"] = m.get(f"w{w}") if m else None
            out.append(rec)
        for j, e in enumerate(B):
            if j in usedB:
                continue
            rec = {"fid": ra["fid"], "_key": ra["_key"], "position": bframe[j],
                   "type": e.get("type"), "dirs": 1, "merged": e.get("n", 1),
                   "_keep": keep(bframe[j]), "_length": length}
            for w in (1310, 1550, 1625):
                rec[f"a{w}"] = None
                rec[f"b{w}"] = e.get(f"w{w}")
            out.append(rec)
    return out


def bidir_splices(rowsA_raw, rowsB_raw, threshold, tol=150.0, wl=1550, merge=0.0,
                  nominal=0.02, margin=100.0, single=False):
    """Flagged-splice list (client "Splices > 0.15dB"): paired events filtered to
    Splice/Group, inside the end margins, with averaged loss at `wl` >= threshold."""
    out = paired_events(rowsA_raw, rowsB_raw, tol=tol, wl=wl, merge=merge, margin=margin,
                        single=single)
    flagged = [r for r in out if r.get("type") in SPLICE_TYPES and r.get("_keep")
               and _eff(r, wl, nominal) is not None and _eff(r, wl, nominal) >= threshold]
    for r in flagged:
        r.pop("_keep", None)
    flagged.sort(key=lambda s: (s["fid"], s.get("position") or 0))
    return flagged


def discover_joints(events, cluster_tol=10.0, min_fibres=3):
    """Cluster ALL fibres' paired-event positions (A-frame) into the cable's joint list.
    cluster_tol ~ the client's "Hydra Length" (10 m): points chained within it form one
    joint, which keeps close-but-distinct joints (e.g. 18610/18660/18700) separate.
    A cluster only counts as a joint when >= min_fibres distinct fibres have an event
    there (drops one-off spur/ghost events). Label = rounded median position."""
    pts = sorted((e["position"], e["fid"]) for e in events if e.get("position") is not None)
    if not pts:
        return []
    clusters, cur = [], [pts[0]]
    for p in pts[1:]:
        if p[0] - cur[-1][0] > cluster_tol:
            clusters.append(cur); cur = []
        cur.append(p)
    clusters.append(cur)
    joints = []
    for cl in clusters:
        med = cl[len(cl) // 2][0]
        if med >= 0 and len({fid for _, fid in cl}) >= min_fibres:
            joints.append(round(med))
    return joints


def joint_matrix(events, joints, cluster_tol=40.0):
    """Assign each fibre's paired events to the nearest joint. Returns
    {fid: {joint_pos: rec}} keeping the strongest rec per fibre/joint."""
    mat = {}
    for e in events:
        pos = e.get("position")
        if pos is None or not joints:
            continue
        j = min(joints, key=lambda x: abs(x - pos))
        if abs(j - pos) > cluster_tol * 2:
            continue
        cell = mat.setdefault(e["fid"], {})
        prev = cell.get(j)
        if prev is None or (abs(e.get("a1550") or e.get("b1550") or 0)
                            > abs(prev.get("a1550") or prev.get("b1550") or 0)):
            cell[j] = e
    return mat


def pair_rows(rowsA, rowsB):
    a = {r["_key"]: r for r in rowsA}
    b = {r["_key"]: r for r in rowsB}
    keys = sorted(set(a) | set(b))
    A, B, unmatched = [], [], []
    for k in keys:
        ra, rb = a.get(k), b.get(k)
        if ra and not rb:
            unmatched.append((k, "B missing"))
        if rb and not ra:
            unmatched.append((k, "A missing"))
        A.append(ra or {"fid": (rb["fid"] if rb else k), "loss": 0, "length": 0})
        B.append(rb or {"fid": (ra["fid"] if ra else k), "loss": 0, "length": 0})
    return A, B, unmatched


def resolve_end(session, token):
    """Search by an RTU token, resolve to exactly one RTU, return (its nodes, rtu dict)."""
    nodes = search_by_rtu(session, token)
    rtus = collect_rtus(nodes)
    if not rtus:
        raise SystemExit(f"No RTU matched '{token}'.")
    exact = [r for r in rtus if r["name"] == token]
    chosen = exact[0] if exact else (rtus[0] if len(rtus) == 1 else None)
    if chosen is None:
        names = ", ".join(r["name"] for r in rtus)
        raise SystemExit(f"'{token}' matches several RTUs: {names}\n"
                         f"Re-run with a more specific --rtu-a/--rtu-b token.")
    end_nodes = [n for n in nodes if ((n.get("rtu") or {}).get("name") or n.get("rtuName")) == chosen["name"]]
    return end_nodes, chosen


ALL_WORDS = {"all", "*", "any", "everything", "full", "a"}


def ribbon_spec_error(spec):
    """Why a ribbon spec cannot be used, or None if it is fine. Checked before the long
    RTU fetch so a typo does not cost you a three thousand route enumeration."""
    if not spec or str(spec).strip().lower() in ALL_WORDS:
        return None
    bad = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        pieces = part.split("-") if "-" in part else [part]
        for x in pieces:
            if not x.strip().isdigit():
                bad.append(part)
                break
    if bad:
        return (f"could not read {', '.join(repr(b) for b in bad)}. Use numbers like "
                f"3, 3-6, or 3,6,8-11. Leave it blank for every ribbon.")
    return None


def parse_ribbons(spec, per=12):
    """'1-2,19-20' -> set of fibre numbers for those ribbons (ribbon 1 = F001..F012).
    An empty spec, or a word like 'all', means no restriction."""
    fibres = set()
    if not spec or str(spec).strip().lower() in ALL_WORDS:
        return fibres
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        a, b = (part.split("-") + [part])[:2] if "-" in part else (part, part)
        try:
            lo, hi = int(a), int(b)
        except ValueError:
            print(f"  ignoring ribbon '{part}': not a number")
            continue
        for rib in range(min(lo, hi), max(lo, hi) + 1):
            fibres.update(range((rib - 1) * per + 1, rib * per + 1))
    return fibres


def fibre_num(name):
    m = re.search(r'F(\d{2,4})\b', name or "")
    return int(m.group(1)) if m else None


def filter_ribbons(nodes, ribbon_spec, verbose=True, per=FIBRES_PER_RIBBON):
    """Select the routes carrying the requested PHYSICAL ribbons.

    On a backsplice loop both legs live under one route name: the route called
    F-...-F025 holds 'RGAC:F025' (physical ribbon 3) and 'RGAC-L:F037' (physical
    ribbon 4). Matching the route name alone therefore misses ribbon 4 completely and
    asking for it returns nothing, even though the data is sitting right there under
    ribbon 3's name. So we match the description too, and say which is which."""
    if not ribbon_spec or str(ribbon_spec).strip().lower() in ALL_WORDS:
        return nodes
    want = parse_ribbons(ribbon_spec, per)
    if not want:
        return nodes
    keep, by_name, by_desc, reached = [], 0, 0, set()
    for n in nodes:
        nm = fibre_num(n.get("name"))
        _, dn, _is_loop = parse_desc(desc_of(n))
        hit_name = nm in want
        hit_desc = dn is not None and dn in want
        if not (hit_name or hit_desc):
            continue
        keep.append(n)
        if hit_name:
            by_name += 1
        else:
            by_desc += 1
        for f in (nm, dn):
            if f:
                reached.add((f - 1) // per + 1)
    # A loop is only measurable as a pair. If the filter caught one leg of a route we must
    # take the other leg too, otherwise the two directions can never be matched and the
    # tool silently falls back to folding a single trace onto itself, which is wrong.
    names = {n.get("name") for n in keep}
    added = 0
    have = {id(n) for n in keep}
    for n in nodes:
        if n.get("name") in names and id(n) not in have:
            keep.append(n)
            added += 1
            nm2 = fibre_num(n.get("name"))
            _, dn2, _ = parse_desc(desc_of(n))
            for f in (nm2, dn2):
                if f:
                    reached.add((f - 1) // per + 1)
    if verbose and added:
        print(f"  pulled in {added} partner leg(s): a loop needs both traces to be paired")
    if verbose:
        asked = sorted({(f - 1) // per + 1 for f in want})
        got = sorted(reached)
        print(f"Ribbon filter '{ribbon_spec}': {len(keep)} traces")
        if by_desc:
            print(f"  {by_desc} of them were found by description: those physical fibres are "
                  f"carried under another ribbon's route name (normal on a loop)")
        extra = [r for r in got if r not in asked]
        if extra:
            print(f"  physical ribbons covered: {', '.join(map(str, got))}  "
                  f"(you asked for {', '.join(map(str, asked))}; "
                  f"{', '.join(map(str, extra))} came with them as the loop-back legs)")
        missing = [r for r in asked if r not in got]
        if missing:
            print(f"  !! nothing found for ribbon(s) {', '.join(map(str, missing))} - "
                  f"check the ribbon numbers, or they may not be tested on this RTU")
    return keep


# --------------------------------------------------------------------------- #
# Interactive wizard
# --------------------------------------------------------------------------- #
def _ask(prompt, default=""):
    v = input(f"{prompt}{' [' + default + ']' if default else ''}: ").strip()
    # Windows "Copy as path" wraps in quotes - strip them so paths just work
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        v = v[1:-1].strip()
    return v or default


def _yesno(prompt, default=False):
    d = "Y/N"
    v = input(f"{prompt} [{d}]: ").strip().lower()
    if not v:
        return default
    return v[0] == "y"


def _ask_choice(title, options):
    print(f"\n{title}")
    for i, o in enumerate(options, 1):
        print(f"  [{i}] {o}")
    while True:
        v = input("Choice: ").strip()
        if v.isdigit() and 1 <= int(v) <= len(options):
            return int(v)
        print("  Enter a number from the list.")


def _pick_rtu(rtus, label):
    print(f"\nSelect {label}:")
    for i, r in enumerate(rtus, 1):
        print(f"  [{i}] {r['name']:28s} site={str(r['site']):10s} "
              f"{r['count']:4d} routes   cable {r.get('cable') or '?'}")
    while True:
        v = input("Number: ").strip()
        if v.isdigit() and 1 <= int(v) <= len(rtus):
            return rtus[int(v) - 1]
        print("  Enter a number from the list.")


def cables_from_rtus(rtus):
    """Group the RTU list by cable: {cable: [rtu, ...]}. An RTU's primary cable is the
    dominant route family (the first token in its 'cable' field). A cable with two RTUs
    is a two-ended (bi-directional) job; one RTU means it is tested from a single end."""
    cab = {}
    for r in rtus:
        c = (r.get("cable") or "").split(",")[0].strip()
        if not c or c == "?":
            continue
        cab.setdefault(c, []).append(r)
    return cab


# The cable runs east (London) to west (Wales) down the GWML. End A is ALWAYS the more
# easterly site, End B the more westerly, whatever order the route name lists them in, so
# the report orientation and the distance schedule always read the same way. Each entry is
# a site, east to west; the tokens are what an RTU's site/name contains.
EAST_TO_WEST = [
    ("0057", "SLOUGH"),      # Slough      (most east)
    ("RGAC", "READING"),     # Reading
    ("SNBC", "SWINDON"),     # Swindon
    ("SGIC", "STOKE"),       # Stoke Gifford
    ("0001", "CARDIFF"),     # Cardiff     (most west)
]


def _geo_rank(rtu):
    """East-to-west position of an RTU's site (0 = most east). 99 if unknown."""
    s = f"{rtu.get('site') or ''} {rtu.get('name') or ''}".upper()
    for i, toks in enumerate(EAST_TO_WEST):
        if any(t in s for t in toks):
            return i
    return 99


def order_ends(cable, ends):
    """Assign End A / End B east to west: the more easterly site is always End A, the more
    westerly is End B, regardless of the order the cable name lists them. Falls back to name
    order only if neither end's site is recognised. Returns (rtuA, rtuB_or_None)."""
    if len(ends) >= 2:
        ranked = sorted(ends, key=_geo_rank)
        if _geo_rank(ranked[0]) != _geo_rank(ranked[-1]) and _geo_rank(ranked[0]) != 99:
            return ranked[0], ranked[-1]
    es = sorted(ends, key=lambda r: str(r.get("name") or ""))
    return es[0], (es[1] if len(es) > 1 else None)


def _match_cable_key(cab, text):
    """Match a user cable token (e.g. 'RGAC-SNBC' or 'rgac snbc') to a cable key."""
    want = re.sub(r'[^A-Z0-9]', '', str(text).upper())
    if not want:
        return None
    for k in cab:
        if want in re.sub(r'[^A-Z0-9]', '', k.upper()):
            return k
    return None


def _pick_cable(rtus, two_rtus=True):
    """Pick ONE cable; the tool then selects the RTU at each end automatically. Returns
    (rtuA, rtuB). (None, None) if no cables could be grouped, so the caller can fall back
    to picking RTUs by hand."""
    cab = cables_from_rtus(rtus)
    keys = sorted(cab)
    if not keys:
        return None, None
    print("\nSelect the cable (the tool picks the RTU at each end):")
    for i, c in enumerate(keys, 1):
        ends = cab[c]
        sites = " <-> ".join(dict.fromkeys(str(e.get('site') or e.get('name')) for e in ends))
        kind = "both ends" if len(ends) >= 2 else "single end"
        print(f"  [{i}] {re.sub(r'^F-', '', c):16s} {sites:24s} ({kind})")
    while True:
        v = input("Number: ").strip()
        if v.isdigit() and 1 <= int(v) <= len(keys):
            key = keys[int(v) - 1]
            a, b = order_ends(key, cab[key])
            if two_rtus and not b:
                print(f"  {re.sub(r'^F-', '', key)} has only one RTU in the list, so it "
                      f"cannot be a two-ended job. Pick another, or run a uni/backsplice report.")
                continue
            return a, (b if two_rtus else a)
        print("  Enter a number from the list.")


def parse_ribbon_pairs(spec):
    """'1-2,3-4' -> [(1,2),(3,4)] — each pair = ribbons backspliced together at the far end."""
    pairs = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        a, b = part.split("-")
        pairs.append((int(a), int(b)))
    return pairs


def backsplice_partner(fnum, pairs, reversed_map=False, per=12):
    """Map fibre number -> its backsplice partner per the ribbon pairs.
    Straight: fibre i of ribbon a <-> fibre i of ribbon b.
    Reversed: fibre i of ribbon a <-> fibre (per+1-i) of ribbon b."""
    rib = (fnum - 1) // per + 1
    idx = (fnum - 1) % per + 1
    for a, b in pairs:
        if rib == a:
            other = b
        elif rib == b:
            other = a
        else:
            continue
        oidx = (per + 1 - idx) if reversed_map else idx
        return (other - 1) * per + oidx
    return None


REPORT_KEYS = {"1": "e2e", "2": "bsplice", "3": "usplice", "4": "odf",
               "5": "remed", "6": "bsremed"}


DEFAULT_SCHEDULE = "Distances.xlsx"
SCHEDULE_TEMPLATE = "Distances TEMPLATE.xlsx"


def tool_dir():
    return os.path.dirname(os.path.abspath(__file__))


def ensure_schedule():
    """The standard distance schedule that ships with the tool.

    Kept as Distances.xlsx next to the script and read automatically, so nobody has to
    paste a path. A new build ships only the TEMPLATE and copies it across when there is
    no Distances.xlsx yet, which means your maintained copy is never overwritten."""
    d = tool_dir()
    live = os.path.join(d, DEFAULT_SCHEDULE)
    if os.path.isfile(live):
        return live
    tmpl = os.path.join(d, SCHEDULE_TEMPLATE)
    if os.path.isfile(tmpl):
        try:
            import shutil
            shutil.copyfile(tmpl, live)
            print(f"  created {DEFAULT_SCHEDULE} from the template")
            return live
        except Exception as ex:
            print(f"  could not copy the template: {ex}")
    try:
        make_schedule_template(live)
        print(f"  created {DEFAULT_SCHEDULE}. Add a sheet per cable and tag each one with "
              f"CABLE / FROM / TO in its top rows.")
        return live
    except Exception as ex:
        print(f"  could not create {DEFAULT_SCHEDULE}: {ex}")
    return None


def _cache_path(name):
    return os.path.join(tool_dir(), f".cache_{name}.json")


def cache_load(name, max_age_h=24):
    import json
    p = _cache_path(name)
    try:
        if time.time() - os.path.getmtime(p) > max_age_h * 3600:
            return None
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def cache_save(name, data):
    import json
    try:
        with open(_cache_path(name), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:
        pass


SETTINGS_FILE = "settings.json"


def load_settings():
    """Per-install settings sitting next to the script.

    Holds nothing secret: the username, and preferences like binder size. The password is
    never written anywhere. Keeping it in a file rather than the launcher means the tool
    can be zipped and handed to someone else, and it will ask them for their own details
    on first run instead of arriving pre-filled with mine."""
    import json
    p = os.path.join(tool_dir(), SETTINGS_FILE)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_settings(d):
    import json
    try:
        with open(os.path.join(tool_dir(), SETTINGS_FILE), "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2)
        return True
    except Exception as ex:
        print(f"  could not save settings: {ex}")
        return False


def first_run(cfg):
    """Ask once, remember. Only runs when there is no settings file yet."""
    print("\n" + "-" * 68)
    print("  First run on this machine. Two questions, then never again.")
    print("-" * 68)
    who = ""
    while "@" not in who:
        who = input("  Your FMS username (email): ").strip()
        if "@" not in who:
            print("    that does not look like an email address")
    cfg["user"] = who
    pb = input("  Ribbons per binder [6]: ").strip()
    try:
        cfg["per_binder"] = int(pb) if pb else 6
    except ValueError:
        cfg["per_binder"] = 6
    save_settings(cfg)
    print(f"  Saved to {SETTINGS_FILE}. Delete that file to be asked again.")
    print("  Your password is never stored.\n")
    return cfg


def wizard(args):
    """Three questions: what report, which RTU, which ribbons. Everything else takes a
    sensible default and stays available as a command line flag."""
    banner()
    print("\nWhat are you reporting on?")
    print("  [1] Bi-directional   two RTUs, one at each end of the cable")
    print("  [2] Uni-directional  one RTU, tested from a single end")
    print("  [3] Backsplice       one RTU, far end looped back")
    while True:
        sel = input("Choice: ").strip()
        if sel in ("1", "2", "3"):
            break
        print("  Enter 1, 2 or 3")

    # End-to-end loss and connector loss now come with every report: they cost nothing
    # to produce and you either use the tab or you don't. Remedials come automatically
    # with anything bi-directional, because a work list needs an averaged figure.
    if sel == "1":
        args.mode = "bidir"
        args.reports = {"e2e", "bsplice", "odf", "remed"}
        print("  Bi-directional: E2E loss, splices, cable views, connector loss, remedials.")
    elif sel == "2":
        args.mode = "uni"
        args.reports = {"e2e", "usplice", "odf"}
        print("  Uni-directional: single-end pull. E2E loss, splices, connector loss.")
    else:
        args.mode = "uni"
        args.reports = {"e2e", "odf", "bsremed", "remed"}
        args.bs_by_desc = True
        args.bs_same_route = False
        print("  Backsplice: loop folded by location, per physical fibre, plus remedials.")
    picks = args.reports

    # ---- defaults that used to be questions ----
    if not args.test_type:
        args.test_type = "auto"          # newest result carrying events, whatever its type
    if picks & {"bsremed", "remed"} and not args.locations:
        sched = ensure_schedule()
        if sched:
            args.locations = sched
            print(f"  distances: {DEFAULT_SCHEDULE} (sheet chosen automatically)")
        else:
            print("  no Distances.xlsx found next to the tool, so locations will be "
                  "labelled by distance only")

    while True:
        args.ribbons = _ask("Ribbons to include (e.g. 3-6 or 3,6,8-11; blank or 'all' = every ribbon)")
        err = ribbon_spec_error(args.ribbons)
        if not err:
            break
        print(f"  {err}")

    # splice nominal: stands in for a direction that saw no event when averaging.
    # 0.02 matches the EXFO customer report; Enter keeps it, or type another value.
    dflt = args.splice_nominal if args.splice_nominal is not None else 0.02
    while True:
        v = _ask("Splice nominal in dB, used where one direction found no splice", f"{dflt:g}")
        try:
            nom = float(v)
        except ValueError:
            print("  Enter a number, e.g. 0.02")
            continue
        if not 0 <= nom <= 1:
            print("  Enter a value between 0 and 1 dB")
            continue
        break
    args.splice_nominal = nom
    print(f"  splice nominal: {nom:g} dB")

    return _finish_wizard(args)


def _finish_wizard(args):
    # customer, cable name, wavelength, nominal and the output filename all have good
    # defaults now: the cable name comes from the fibre IDs, the nominal is a live cell
    # on the Cover, and the file is named <dd-mm-yy>_<cable>_<ribbons>_<time>.xlsx
    args.customer = args.customer or "Motion"
    print("  file named automatically as <dd-mm-yy>_<cable>_<ribbons>_<time>.xlsx "
          "(--out to override)")
    return args


def uni_splices(rows_raw, threshold, wl=1550, margin=100.0):
    """One-way splice list (no averaging): flag events whose OWN loss at wl >= threshold.
    b-values mirror a-values so the sheet's average equals the one-way loss."""
    out = []
    for ra in rows_raw:
        length = ra.get("length")
        for e in ra.get("events") or []:
            if e.get("type") not in SPLICE_TYPES:
                continue
            pos = e.get("position")
            if pos is None:
                continue
            if length and not (margin < pos < length - margin):
                continue
            v = e.get(f"w{wl}")
            if v is None or v < threshold:
                continue
            rec = {"fid": ra["fid"], "position": pos, "type": e.get("type"),
                   "dirs": 1, "merged": 1}
            for w in (1310, 1550, 1625):
                rec[f"a{w}"] = e.get(f"w{w}")
                rec[f"b{w}"] = e.get(f"w{w}")
            out.append(rec)
    out.sort(key=lambda s: (s["fid"], s.get("position") or 0))
    return out


def backsplice_rows(rows_raw, pairs, reversed_map=False, per=12):
    """Overlay a backspliced loop tested from ONE end: fibre i of ribbon a and its partner
    in ribbon b are the same loop in opposite directions. Returns (rowsA, rowsB) where the
    partner's test is the 'B side', keyed to the A fibre so the standard bidirectional
    pairing/averaging applies."""
    by_num = {}
    for r in rows_raw:
        n = fibre_num(r.get("fid") or "")
        if n:
            by_num[n] = r
    rowsA, rowsB = [], []
    for a, b in pairs:
        for i in range(1, per + 1):
            na = (a - 1) * per + i
            nb = backsplice_partner(na, [(a, b)], reversed_map, per)
            ra, rb = by_num.get(na), by_num.get(nb)
            if not ra and not rb:
                continue
            key = f"F{na:04d}"
            A = dict(ra) if ra else {"fid": f"F{na:03d}", "loss": None, "length": 0, "events": []}
            B = dict(rb) if rb else {"fid": (ra["fid"] if ra else key), "loss": None, "length": 0, "events": []}
            A["_key"] = key
            B["_key"] = key
            rowsA.append(A)
            rowsB.append(B)
    return rowsA, rowsB



def backsplice_by_desc(rows_raw, verbose=True):
    """Pair each MAIN fibre with its LOOP-BACK partner using the FMS route description
    (e.g. 'RGAC:F197' main <-> 'RGAC-L:F209' loop-back). Both legs of a loop are the same
    light path measured in opposite directions, so this returns (rowsA, rowsB) keyed
    identically for the standard bidirectional pairing/averaging.
    Pairs are made in fibre-number order within each tag, so any fixed offset between the
    main and loop-back numbering (12, 24, ...) is handled automatically."""
    mains, loops = {}, {}
    for r in rows_raw:
        tag, num, is_loop = parse_desc(r.get("desc"))
        if num is None:
            continue
        base = re.sub(r'-\s*L$', '', tag or '', flags=re.I)
        (loops if is_loop else mains).setdefault(base, []).append((num, r))
    rowsA, rowsB, offsets = [], [], []
    for base, mlist in sorted(mains.items()):
        llist = loops.get(base, [])
        mlist.sort(key=lambda t: t[0])
        llist.sort(key=lambda t: t[0])
        for i, (mnum, mrow) in enumerate(mlist):
            if i >= len(llist):
                break
            lnum, lrow = llist[i]
            offsets.append(lnum - mnum)
            key = f"F{mnum:04d}"
            A = dict(mrow); B = dict(lrow)
            A["_key"] = key; B["_key"] = key
            A["fid"] = f"{mrow.get('fid')}  [main {mrow.get('desc')} <-> loop {lrow.get('desc')}]"
            B["fid"] = A["fid"]
            rowsA.append(A); rowsB.append(B)
    if verbose and rowsA:
        common = max(set(offsets), key=offsets.count) if offsets else None
        print(f"  paired {len(rowsA)} loops from descriptions"
              + (f" (main -> loop-back offset {common} fibres)" if common is not None else ""))
    return rowsA, rowsB



def all_events_table(rowsA_raw, rowsB_raw, single=False, tol=150.0, wl=1550, margin=0.0):
    """EVERY event of EVERY fibre in one flat list - the "one place" view that replaces
    opening each trace by hand. One row per fibre per event, with the location and the
    per-wavelength loss (and both one-way values when the cable was tested from both ends)."""
    evs = paired_events(rowsA_raw, rowsB_raw, tol=tol, wl=wl, merge=0.0, margin=margin,
                        single=single)
    meta = {}
    for r in rowsA_raw:
        meta[r.get("_key")] = {"date": r.get("date"), "len": r.get("length"),
                               "desc": r.get("desc"), "fid": r.get("fid")}
    rows = []
    for e in evs:
        m = meta.get(e.get("_key"), {})
        rows.append({
            "fid": e.get("fid"), "desc": m.get("desc"), "date": m.get("date"),
            "length": m.get("_len") or m.get("len"),
            "position": e.get("position"), "type": e.get("type"), "dirs": e.get("dirs"),
            "a1310": e.get("a1310"), "a1550": e.get("a1550"), "a1625": e.get("a1625"),
            "b1310": e.get("b1310"), "b1550": e.get("b1550"), "b1625": e.get("b1625"),
            "single": e.get("single", single),
        })
    rows.sort(key=lambda r: (r["fid"] or "", r["position"] if r["position"] is not None else 0))
    return rows



# --------------------------------------------------------------------------- #
# Location schedule: map measured distances to named physical locations
# --------------------------------------------------------------------------- #
def load_locations(path, sheet=None, verbose=True):
    """Read a backsplice/joint distance schedule (the 'Backsplice Distances' workbook).

    Expects a header row containing 'Location' and 'Leg', then distance columns whose
    headers name a fibre range and whether they are Design or Actual, e.g.
        'R1-2 Design\n(fibres 1-24)'   'R1-2 Actual\nmanual input'
    Returns [{location, leg, dists: {(lo,hi): {'design': m, 'actual': m}}}] per row.
    'Actual' is preferred when present; 'Design' is the fallback."""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    out = []
    sheets = [sheet] if sheet else wb.sheetnames
    for nm in sheets:
        if nm not in wb.sheetnames:
            continue
        ws = wb[nm]
        hrow = None
        for r in range(1, min(ws.max_row, 30) + 1):
            vals = [str(ws.cell(r, c).value or '').strip().lower() for c in range(1, 6)]
            if 'location' in vals and 'leg' in vals:
                hrow = r
                break
        if not hrow:
            continue
        cmap = {}                       # column -> (lo, hi, kind)
        lastrange = None
        for c in range(1, ws.max_column + 1):
            h = str(ws.cell(hrow, c).value or '')
            if not h:
                continue
            # Which fibres a distance column covers. Some sheets spell it out
            # ("fibres 25-216"), others only give the ribbon span ("R3-36"). Falling back
            # to the previous column's range when the ribbon form appears silently
            # assigns those distances to the wrong fibres, and then nothing matches.
            m = re.search(r'fibres?\s*(\d+)\s*[-\u2013]\s*(\d+)', h, re.I)
            if m:
                lastrange = (int(m.group(1)), int(m.group(2)))
            else:
                m = re.search(r'\bR\s*(\d+)\s*[-\u2013]\s*(\d+)', h, re.I)
                if m:
                    r1, r2 = int(m.group(1)), int(m.group(2))
                    lastrange = ((r1 - 1) * FIBRES_PER_RIBBON + 1, r2 * FIBRES_PER_RIBBON)
                else:
                    m = re.search(r'\bR\s*(\d+)\b', h, re.I)
                    if m:
                        rr = int(m.group(1))
                        lastrange = ((rr - 1) * FIBRES_PER_RIBBON + 1, rr * FIBRES_PER_RIBBON)
            kind = ('actual' if re.search(r'actual', h, re.I) else
                    ('design' if re.search(r'design', h, re.I) else None))
            if kind and lastrange:
                cmap[c] = (lastrange[0], lastrange[1], kind)
        if not cmap:
            continue
        for r in range(hrow + 1, ws.max_row + 1):
            name = ws.cell(r, 1).value
            if not name or not str(name).strip():
                continue
            leg = str(ws.cell(r, 2).value or '').strip()
            dists = {}
            for c, (lo, hi, kind) in cmap.items():
                v = _f(ws.cell(r, c).value)
                if v is None:
                    continue
                dists.setdefault((lo, hi), {})[kind] = v
            if dists:
                out.append({"location": str(name).strip(), "leg": leg,
                            "dists": dists, "sheet": nm})
    if verbose:
        legs = {}
        rng = set()
        for e in out:
            legs[e["leg"]] = legs.get(e["leg"], 0) + 1
            rng.update(e["dists"].keys())
        print(f"  location schedule: {len(out)} rows from {path} {legs}")
        if rng:
            print("    fibre ranges covered: "
                  + ", ".join(f"{lo}-{hi}" for lo, hi in sorted(rng)))
        # flag distances claimed by two different locations - a typo there silently
        # mis-labels events, so it is worth seeing
        seen = {}
        dupes = []
        for e in out:
            for rng, kinds in e["dists"].items():
                d = kinds.get('actual', kinds.get('design'))
                if d is None:
                    continue
                k = (e.get("sheet"), rng, round(d))
                if k in seen and seen[k] != e["location"]:
                    dupes.append((round(d), seen[k], e["location"], rng, e.get("sheet")))
                else:
                    seen[k] = e["location"]
        for d, a, b, rng, sh in dupes[:8]:
            print(f"  ** check schedule [{sh}]: {d} m is given for BOTH '{a}' and '{b}' "
                  f"(fibres {rng[0]}-{rng[1]}) - events there may be mis-labelled")
        # a schedule saved by a tool that strips cached formula values reads as empty
        if len(out) < 5:
            print("  ** the schedule returned very few rows - if its distance cells are "
                  "formulas, open it in Excel and save it once so the values are stored")
    return out


def _sched_points(schedule, fibre_no, prefer_actual=True):
    """Flatten the schedule to [(distance, location, leg)] for one fibre number."""
    pts = []
    for row in schedule:
        for (lo, hi), kinds in row["dists"].items():
            if fibre_no is not None and not (lo <= fibre_no <= hi):
                continue
            d = kinds.get('actual') if prefer_actual else None
            if d is None:
                d = kinds.get('design')
            if d is None:
                d = kinds.get('actual')
            if d is not None:
                pts.append((d, row["location"], row["leg"]))
    return sorted(pts)


def sheet_meta(ws, scan_rows=8):
    """CABLE / FROM / TO tags written above the header row of a distance sheet.

    Matching a sheet by trying it against the data works, but it is guesswork dressed as
    cleverness. If the sheet states which cable it belongs to, the tool can pick the right
    one outright, and say so, instead of inferring it every run."""
    meta = {}
    if str(getattr(ws, "title", "")).strip().lower().startswith("revision"):
        return meta
    for r in range(1, scan_rows + 1):
        for c in (1,):
            v = str(ws.cell(r, c).value or "").strip()
            m = re.match(r'^(cable|from|to|section|rev|revision|issued|version)'
                         r'\s*[:=]\s*(.+)$', v, re.I)
            if m:
                meta[m.group(1).lower()] = m.group(2).strip()
            else:
                k = v.rstrip(':').strip().lower()
                if k in ("cable", "from", "to", "section", "rev", "revision",
                         "issued", "version"):
                    nxt = ws.cell(r, c + 1).value
                    if nxt:
                        meta[k] = str(nxt).strip()
    return meta


def schedule_index(path):
    """What each sheet in the distance workbook covers."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)
    out = []
    for nm in wb.sheetnames:
        m = sheet_meta(wb[nm])
        out.append({"sheet": nm, "cable": m.get("cable"), "from": m.get("from"),
                    "to": m.get("to"), "section": m.get("section"),
                    "rev": m.get("rev") or m.get("revision") or m.get("version"),
                    "issued": m.get("issued")})
    wb.close()
    return out


def pick_sheet_for_cable(path, cable, site=None, verbose=True):
    """Sheet whose CABLE tag matches this pull, or (None, None) to fall back to best fit."""
    try:
        idx = schedule_index(path)
    except Exception:
        return None, None
    tagged = [e for e in idx if e.get("cable")]
    if not tagged:
        return None, None
    want = str(cable or "").strip().upper()
    hits = [e for e in tagged if want and want in str(e["cable"]).upper()]
    if not hits:
        if verbose:
            print(f"  no distance sheet tagged for cable {cable}. Tagged sheets: "
                  + ", ".join(f"{e['sheet']} ({e['cable']})" for e in tagged))
        return None, None
    e = hits[0]
    if len(hits) > 1 and site:
        for h in hits:
            if str(h.get("from") or "").upper() in str(site).upper():
                e = h
                break
    if verbose:
        print(f"  distance sheet '{e['sheet']}' tagged CABLE {e['cable']}"
              + (f", measured FROM {e['from']}" if e.get("from") else "")
              + (f", rev {e['rev']}" if e.get("rev") else "")
              + (f" issued {e['issued']}" if e.get("issued") else ""))
        if not e.get("rev"):
            print("     (no REV tag on this sheet: add one so every report can name the "
                  "distances it used)")
    return e["sheet"], e


def make_schedule_template(path):
    """Write a Distances workbook with the tagging rows already in place."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "EXAMPLE cable"
    bold = Font(name="Arial", size=10, bold=True)
    # Tags live in column A with the value in B. The revision travels inside the file, so
    # a sheet issued on its own is self-describing and any report built from it can name
    # the exact distances it used.
    for r, (k, v) in enumerate([("CABLE:", "F-RGAC-SNBC"), ("FROM:", "SNBC"),
                                ("TO:", "RGAC"), ("REV:", "1.0"),
                                ("ISSUED:", time.strftime("%Y-%m-%d"))], start=1):
        ws.cell(r, 1, k).font = bold
        ws.cell(r, 2, v)
    ws["F1"] = "Tag every sheet like this. CABLE is the route-name prefix the tool sees;"
    ws["F2"] = "FROM is the end the distances are measured from. One sheet per cable."
    ws["F3"] = "Bump REV whenever the distances change and log it on the Revisions sheet."
    ws["F4"] = "Delete this example sheet once you have added your own."
    for a in ("F1", "F2", "F3", "F4"):
        ws[a].font = Font(name="Arial", size=9, italic=True, color="808080")
    cols = ["Location", "Leg", "R1-2 Design\n(fibres 1-24)", "R1-2 Actual\nmanual input",
            "R3-36 Design\n(fibres 25-432)", "R3-36 Actual\nmanual input"]
    for c, h in enumerate(cols, 1):
        cc = ws.cell(7, c, h)
        cc.font = Font(name="Arial", size=9, bold=True, color="FFFFFF")
        cc.fill = PatternFill("solid", fgColor="1F4E79")
        cc.alignment = Alignment(horizontal="center", wrap_text=True)
    rows = [("SNBC (launch)", "Outbound", 0, 0), ("A-29 Spur West", "Outbound", 969, 975),
            ("A-21  \u25c4 BACKSPLICE", "Backsplice", 14426, 14430),
            ("A-29 Spur West", "Return", 27883, 27890), ("SNBC  \u25c4 END", "End", 28853, 28860)]
    for i, (loc, leg, d1, d2) in enumerate(rows):
        ws.cell(8 + i, 1, loc); ws.cell(8 + i, 2, leg)
        ws.cell(8 + i, 3, d1); ws.cell(8 + i, 4, d1)
        ws.cell(8 + i, 5, d2); ws.cell(8 + i, 6, d2)
    for c, w in ((1, 30), (2, 14), (3, 16), (4, 16), (5, 16), (6, 16)):
        ws.column_dimensions[chr(64 + c)].width = w
    ws.row_dimensions[7].height = 30

    rv = wb.create_sheet("Revisions")
    for c, h in enumerate(["Rev", "Issued", "Cable / sheet", "What changed", "By"], 1):
        cc = rv.cell(1, c, h)
        cc.font = Font(name="Arial", size=9, bold=True, color="FFFFFF")
        cc.fill = PatternFill("solid", fgColor="1F4E79")
    rv.cell(2, 1, "1.0")
    rv.cell(2, 2, time.strftime("%Y-%m-%d"))
    rv.cell(2, 3, "EXAMPLE cable")
    rv.cell(2, 4, "First issue")
    for c, w in ((1, 8), (2, 12), (3, 26), (4, 60), (5, 10)):
        rv.column_dimensions[chr(64 + c)].width = w
    rv["A5"] = ("Bump the REV cell on a sheet whenever its distances change, add a line "
                "here, and the reports built from it will name that revision.")
    rv["A5"].font = Font(name="Arial", size=9, italic=True, color="808080")
    wb.save(path)
    return path


def location_sheets(path):
    """Sheet names in the distance schedule. One workbook usually holds several cables,
    one per sheet, so loading them all at once mixes unrelated joint distances together."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)
    names = list(wb.sheetnames)
    wb.close()
    return names


def count_matches(events, schedule, tol=120.0, prefer_actual=True):
    """How many events this schedule would place, without tagging anything. Used to pick
    the right sheet by evidence rather than by guessing from the sheet name."""
    cache, hits = {}, 0
    for e in events:
        fno = fibre_num(e.get("fid") or "")
        if fno not in cache:
            cache[fno] = _sched_points(schedule, fno, prefer_actual)
        pts, pos = cache[fno], e.get("position")
        if not pts or pos is None:
            continue
        d, _, _ = min(pts, key=lambda p: abs(p[0] - pos))
        if abs(d - pos) <= tol:
            hits += 1
    return hits


def pick_location_sheet(path, events, tol=120.0, verbose=True):
    """Choose the sheet that actually fits this cable. Returns (sheet_name, schedule)."""
    names = location_sheets(path)
    if len(names) == 1:
        return names[0], load_locations(path, sheet=names[0], verbose=verbose)
    scored = []
    for nm in names:
        try:
            sch = load_locations(path, sheet=nm, verbose=False)
        except Exception:
            continue
        if not sch:
            continue
        scored.append((count_matches(events, sch, tol), nm, sch))
    if not scored:
        return None, None
    scored.sort(key=lambda x: -x[0])
    if verbose:
        print("  distance schedule has several sheets; matching each against the data:")
        for hits, nm, sch in scored:
            mark = "  <- using this one" if nm == scored[0][1] else ""
            print(f"      {nm:24s} {len(sch):3d} rows, places {hits}/{len(events)} events{mark}")
    return scored[0][1], scored[0][2]


def joint_names(joints, schedule, fibres=None, tol=120.0, prefer_actual=True):
    """Name each Cable_View joint after its scheduled physical location.

    The cable views key their columns on a distance in metres, which tells the reader
    nothing about where on the ground that splice is. This maps the joint distances back
    onto the schedule, closest pair first and one location per joint, so a cluster of
    spur points cannot take the same name twice. Returns {joint: location}."""
    if not joints or not schedule:
        return {}
    fno = None
    for fid in (fibres or []):
        fno = fibre_num(fid)
        if fno:
            break
    pts = _sched_points(schedule, fno or 1, prefer_actual)
    if not pts:
        return {}
    pairs = sorted((abs(d - j), ji, pi)
                   for ji, j in enumerate(joints)
                   for pi, (d, _n, _l) in enumerate(pts) if abs(d - j) <= tol)
    out, used_j, used_p = {}, set(), set()
    for gap, ji, pi in pairs:
        if ji in used_j or pi in used_p:
            continue
        used_j.add(ji); used_p.add(pi)
        out[joints[ji]] = pts[pi][1]
    return out


def map_locations(events, schedule, tol=120.0, prefer_actual=True, verbose=True):
    """Tag each event with its scheduled physical location, one location per event.

    Nearest-match on its own lets several events claim the same joint. That happened on
    the A-01 spur, where East, the ODF and West sit about 35 m apart in the schedule: all
    three measured events took "A-01 Spur West" and the sheet read West three times.

    So each fibre is matched closest-pair-first, and a location is claimed only once.
    An event with no location left within tolerance stays unnamed, which is honest: it
    then shows as an unscheduled joint rather than duplicating a name that belongs to a
    different splice. Same approach the splice pairing already uses."""
    cache = {}
    hits = dupes_avoided = 0
    by_fibre = {}
    for e in events:
        if e.get("position") is None:
            continue
        by_fibre.setdefault(fibre_num(e.get("fid") or ""), []).append(e)
    for fno, evs in by_fibre.items():
        if fno not in cache:
            cache[fno] = _sched_points(schedule, fno, prefer_actual)
        pts = cache[fno]
        if not pts:
            continue
        pairs = []
        for i, e in enumerate(evs):
            for j, (d, name, leg) in enumerate(pts):
                gap = abs(d - e["position"])
                if gap <= tol:
                    pairs.append((gap, i, j))
        pairs.sort()
        used_e, used_p = set(), set()
        for gap, i, j in pairs:
            if i in used_e or j in used_p:
                if i not in used_e:
                    dupes_avoided += 1
                continue
            used_e.add(i)
            used_p.add(j)
            e = evs[i]
            d, name, leg = pts[j]
            e["location"] = name
            e["leg"] = leg
            e["sched_m"] = d
            e["delta_m"] = round(e["position"] - d, 1)
            hits += 1
    if verbose and dupes_avoided:
        DBG.flag(f"{dupes_avoided} event(s) could not take their nearest location because a closer event already claimed it - likely real extra joints the schedule does not list, or spur detail; review them on the Cable View.") if dupes_avoided else None
        print(f"  {dupes_avoided} event(s) left unnamed because their nearest location was "
              f"already claimed by a closer event - they show as unscheduled joints")
    return hits



def leg_partners(rowsA, rowsB):
    """Map each main trace to the physical fibre its loop-back leg runs on.

    A backsplice loop uses TWO physical fibres: the light goes out on one and comes back
    on the other, joined at the backsplice point. The descriptions name both, e.g.
    'RGAC:F025' out and 'RGAC-L:F037' back."""
    out = {}
    for a, b in zip(rowsA or [], rowsB or []):
        _, fa, _ = parse_desc(a.get("desc"))
        _, fb, _ = parse_desc(b.get("desc"))
        if fa is None:
            fa = fibre_num(a.get("fid") or "")
        if fb is None:
            fb = fibre_num(b.get("fid") or "")
        out[a.get("fid")] = {"main": fa, "loop": fb}
    return out


def label_unmapped(events, tol=25.0, min_fibres=3, verbose=True):
    """Give every event that missed the schedule a location of its own.

    A joint the schedule does not know about is still a real joint, and dropping it means
    the remedial sheet quietly under-reports. These are clustered by distance across all
    fibres, so the same unlisted joint lines up in one column, and labelled by distance
    with '(unscheduled)' so nobody mistakes them for surveyed locations."""
    loose = [e for e in events if not e.get("location") and e.get("position") is not None]
    if not loose:
        return 0
    pts = sorted(e["position"] for e in loose)
    centres, run = [], [pts[0]]
    for p in pts[1:]:
        if p - run[-1] <= tol:
            run.append(p)
        else:
            centres.append(sum(run) / len(run))
            run = [p]
    centres.append(sum(run) / len(run))
    # A real joint shows up on most fibres at the same distance. A cluster seen on one or
    # two is a local feature or a detection artefact, and giving it a column turns the
    # sheet into mostly nominals dressed up as results. So it has to earn its place.
    members = {}
    for e in loose:
        c = min(centres, key=lambda x: abs(x - e["position"]))
        members.setdefault(round(c), set()).add(e.get("fid"))
    kept = {c for c, fids in members.items() if len(fids) >= max(1, min_fibres)}
    dropped_c = len(members) - len(kept)
    allpos = [e.get("sched_m") or e.get("position") or 0 for e in events]
    half = (max(allpos) / 2.0) if allpos else 0
    dropped_e = 0
    for e in loose:
        c = round(min(centres, key=lambda x: abs(x - e["position"])))
        if c not in kept:
            dropped_e += 1
            continue
        e["location"] = f"{c:,} m (unscheduled)"
        e["sched_m"] = c
        e["leg"] = e.get("leg") or ("Return" if e["position"] > half else "Outbound")
        e["unscheduled"] = True
    if verbose:
        print(f"  {len(loose)} event(s) matched no scheduled location: "
              f"{len(kept)} extra column(s) kept (seen on {min_fibres}+ fibres)")
        if dropped_c:
            print(f"     {dropped_c} thinner cluster(s) covering {dropped_e} event(s) left out "
                  f"as one-offs. --unmapped-min 1 keeps everything, --only-scheduled keeps none.")
    return len(kept)


def backsplice_physical(events, partners=None, nominal=0.06, wl=1550):
    """Backsplice remedials, one row per PHYSICAL fibre.

    This is the part that has to be right. A loop tested from one end passes each
    location twice, but those two passes are NOT two directions of one fibre: they are
    two different physical fibres, the out leg and the back leg, joined at the backsplice.
    Folding a single trace onto itself therefore averages fibre X's outbound reading with
    fibre Y's return reading, which is meaningless.

    The genuine bi-directional pair for one physical fibre comes from the TWO traces:
    the main trace and the loop-back trace, which the pairing engine has already matched
    and mirrored. So at each location:
        the outbound-leg event  -> the main fibre,  A-B = main trace, B-A = loop trace
        the return-leg event    -> the loop fibre,  A-B = loop trace, B-A = main trace
    Returns (locations, rows, distances)."""
    partners = partners or {}
    order, seen, dist = [], set(), {}
    for e in sorted(events, key=lambda x: (x.get("sched_m") or 0)):
        loc = e.get("location")
        if not loc:
            continue
        b = "ret" if (e.get("leg") or "").lower() == "return" else "out"
        if e.get("sched_m") is not None:
            dist.setdefault(loc, {}).setdefault(b, e["sched_m"])
        # Every location gets a column, whichever leg it turned up on. A scheduled
        # location appears on both legs under one name so it is listed once, but an
        # unscheduled joint on the return leg has a name of its own, and only listing
        # outbound-leg names dropped those from the sheet entirely.
        if loc not in seen:
            seen.add(loc)
            order.append(loc)
    per = {}
    for e in events:
        loc = e.get("location")
        if not loc:
            continue
        ret = (e.get("leg") or "").lower() == "return"
        p = partners.get(e.get("fid")) or {}
        no = p.get("loop" if ret else "main")
        if no is None:
            no = fibre_num(e.get("fid") or "")
        a, b = e.get(f"a{wl}"), e.get(f"b{wl}")
        ab, ba = (b, a) if ret else (a, b)
        rec = per.setdefault(no, {"fid": f"F{no:03d}" if isinstance(no, int) else str(no),
                                  "phys": no, "trace": e.get("fid"), "desc": e.get("desc"),
                                  "leg": "back" if ret else "out", "out": {}, "ret": {}})
        for key, val in (("out", ab), ("ret", ba)):
            if val is None:
                continue
            prev = rec[key].get(loc)
            if prev is None or abs(val) > abs(prev):
                rec[key][loc] = val
    rows = []
    for no in sorted(per, key=lambda x: (x if isinstance(x, int) else 1e9)):
        rec = per[no]
        bid, assumed = {}, {}
        for loc in order:
            a, b = rec["out"].get(loc), rec["ret"].get(loc)
            bid[loc] = ((a if a is not None else nominal) +
                        (b if b is not None else nominal)) / 2
            if a is None and b is None:
                assumed[loc] = "both"
            elif a is None:
                assumed[loc] = "out"
            elif b is None:
                assumed[loc] = "ret"
        rec["bidir"] = bid
        rec["assumed"] = assumed
        rows.append(rec)
    found = {}
    for e in events:
        loc = e.get("location")
        if not loc or e.get("position") is None:
            continue
        b = "ret" if (e.get("leg") or "").lower() == "return" else "out"
        found.setdefault(loc, {}).setdefault(b, []).append(e["position"])
    return order, rows, {"sched": dist, "found": found}


def _is_terminal(e):
    """Launch and end-of-fibre events are not field joints. The launch shows ~0.5 dB of
    connector loss that the far end cannot see at all, so it can never be averaged
    bi-directionally, and the end is the fibre stopping rather than a splice. Both belong
    out of a remedial work list."""
    leg = str(e.get("leg") or "").strip().lower()
    loc = str(e.get("location") or "").lower()
    typ = str(e.get("type") or "").lower()
    st = str(e.get("status") or "").lower()
    if leg == "end":
        return True
    if any(t in loc for t in ("launch", "end", "◄ end")):
        return True
    if any(t in typ for t in ("launch", "end of fiber", "end of analysis")):
        return True
    if any(t in st for t in ("spanstart", "spanend", "launchlevel")):
        return True
    pos = e.get("position")
    if pos is not None and abs(pos) < 60:          # within the launch dead zone
        return True
    return False


def drop_terminals(events, verbose=True, label=""):
    keep = [e for e in events if not _is_terminal(e)]
    n = len(events) - len(keep)
    if verbose and n:
        print(f"  dropped {n} launch/end event(s) from {label}remedial reporting "
              f"(not field joints, and the launch cannot be seen from the far end)")
    return keep


def backsplice_remedials(events, nominal=0.06, wl=1550):
    """Fold a backsplice loop by physical location.

    A loop tested from one end passes every location twice: once outbound and once on
    the return leg. Using the distance schedule each event is already tagged with its
    location and leg, so this pivots to one row per fibre with three blocks:
        A-B   = the outbound reading at each location
        B-A   = the return reading at each location
        Bidir = the overlay, mean of the two (the nominal stands in for a leg that
                found no splice, so a location is never silently dropped).
    Returns (locations_in_order, rows) where rows = [{fid, out{}, ret{}, bidir{}, ...}]."""
    order, seen = [], set()
    # scheduled distance for each location on each leg, so the sheet can show
    # "5125 / 23728" under the location name as a cross-check that the fold is right
    dist = {}
    for e in sorted(events, key=lambda x: (x.get("sched_m") or 0)):
        loc, leg = e.get("location"), (e.get("leg") or "")
        if not loc:
            continue
        bucket = "ret" if leg.lower() == "return" else "out"
        d = e.get("sched_m")
        if d is not None:
            dist.setdefault(loc, {}).setdefault(bucket, d)
        if leg.lower() == "return":
            continue
        if loc not in seen:
            seen.add(loc); order.append(loc)
    per = {}
    for e in events:
        loc, leg = e.get("location"), (e.get("leg") or "").lower()
        if not loc:
            continue
        rec = per.setdefault(e["fid"], {"fid": e["fid"], "desc": e.get("desc"),
                                        "out": {}, "ret": {}})
        val = e.get(f"a{wl}")
        if val is None:
            val = e.get(f"b{wl}")
        if val is None:
            continue
        bucket = "ret" if leg == "return" else "out"
        prev = rec[bucket].get(loc)
        if prev is None or abs(val) > abs(prev):
            rec[bucket][loc] = val
    rows = []
    for fid in sorted(per):
        rec = per[fid]
        bid, assumed = {}, {}
        for loc in order:
            a, b = rec["out"].get(loc), rec["ret"].get(loc)
            # every location gets a value so the grid lines up; the nominal stands in
            # for a leg that found no splice, and we record which side was assumed.
            bid[loc] = ((a if a is not None else nominal) +
                        (b if b is not None else nominal)) / 2
            if a is None and b is None:
                assumed[loc] = "both"
            elif a is None:
                assumed[loc] = "out"
            elif b is None:
                assumed[loc] = "ret"
        rec["bidir"] = bid
        rec["assumed"] = assumed
        # measured position per location, for checking the event really sat where the
        # schedule says the joint is
        rows.append(rec)
    found = {}
    for e in events:
        loc = e.get("location")
        if not loc or e.get("position") is None:
            continue
        b = "ret" if (e.get("leg") or "").lower() == "return" else "out"
        found.setdefault(loc, {}).setdefault(b, []).append(e["position"])
    return order, rows, {"sched": dist, "found": found}


def fill_from_curve(bs_rows, order, otdr_rows, partners=None, nominal=0.06, verbose=True):
    """Replace nominal placeholders with an actual reading off the trace.

    A direction reporting no event does NOT mean zero loss. It means nothing was flagged:
    either the step was under the detection threshold, or it was a gainer, which happens
    routinely when the fibres either side of a splice have different backscatter. A fixed
    nominal biases the average the same way every time; the curve gives the real one-way
    value, sign included.

    The mapping has to follow the loop the same way the event table does. A trace passes
    each location twice and those two passes are different physical fibres:
        main trace,  outbound leg -> the main fibre,  A-B
        main trace,  return leg   -> the loop fibre,  B-A
        loop trace,  outbound leg -> the loop fibre,  A-B
        loop trace,  return leg   -> the main fibre,  B-A
    Getting this wrong is silent, so it is spelled out."""
    partners = partners or {}
    idx = {}
    for r in otdr_rows or []:
        if r.get("loss") is None:
            continue
        ret = str(r.get("leg") or "").lower() == "return"
        p = partners.get(r.get("fid")) or {}
        main, loop = p.get("main"), p.get("loop")
        if main is None and loop is None:
            main = loop = fibre_num(r.get("fid") or "")
        if r.get("is_loop"):
            phys = main if ret else loop
        else:
            phys = loop if ret else main
        idx[(phys, r.get("location"), "ret" if ret else "out")] = r
    filled, missed = 0, 0
    for rec in bs_rows:
        curve = rec.setdefault("curve", {})
        for loc in order:
            for b in ("out", "ret"):
                if rec.get(b, {}).get(loc) is not None:
                    continue
                hit = idx.get((rec.get("phys"), loc, b))
                if not hit:
                    missed += 1
                    continue
                rec.setdefault(b, {})[loc] = hit["loss"]
                curve.setdefault(loc, []).append(b)
                filled += 1
        bid, assumed = {}, {}
        for loc in order:
            a, c = rec.get("out", {}).get(loc), rec.get("ret", {}).get(loc)
            bid[loc] = ((a if a is not None else nominal) +
                        (c if c is not None else nominal)) / 2
            if a is None and c is None:
                assumed[loc] = "both"
            elif a is None:
                assumed[loc] = "out"
            elif c is None:
                assumed[loc] = "ret"
        rec["bidir"] = bid
        rec["assumed"] = assumed
    if verbose:
        print(f"  filled {filled} missing direction(s) with a reading measured off the trace; "
              f"{missed} still fall back to the {nominal} dB nominal")
    return filled


def remedials_list(events, threshold=0.15, nominal=0.06, wl=1550, single=False):
    """Flat work list: every splice at or over the threshold, with its location."""
    out = []
    for e in events:
        if e.get("type") not in SPLICE_TYPES:
            continue
        a, b = e.get(f"a{wl}"), e.get(f"b{wl}")
        if single or e.get("single"):
            v = a if a is not None else b
        elif a is None and b is None:
            v = None
        else:
            v = ((a if a is not None else nominal) + (b if b is not None else nominal)) / 2
        if v is None or v < threshold:
            continue
        out.append({"fid": e.get("fid"), "desc": e.get("desc"),
                    "location": e.get("location"), "leg": e.get("leg"),
                    "position": e.get("position"), "sched_m": e.get("sched_m"),
                    "delta_m": e.get("delta_m"), "loss": v,
                    f"a{wl}": a, f"b{wl}": b, "wl": wl})
    out.sort(key=lambda r: -(r["loss"] or 0))
    return out



# --------------------------------------------------------------------------- #
# VERIFY: compare a manually-built results sheet against what FMS actually holds
# --------------------------------------------------------------------------- #
def _vnum(x):
    """'0.060 dB' / '5,125' / '-0.093' -> float."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return None if (isinstance(x, float) and math.isnan(x)) else float(x)
    s = str(x).replace(",", "").replace("dB", "").replace("m", "").strip()
    m = re.search(r'-?\d+(?:\.\d+)?', s)
    return float(m.group(0)) if m else None


def _vpair(x):
    """'5,125  /  23,728' -> (5125.0, 23728.0). A single number -> (n, None)."""
    if x is None:
        return (None, None)
    s = str(x).replace(",", "")
    nums = re.findall(r'-?\d+(?:\.\d+)?', s)
    if not nums:
        return (None, None)
    a = float(nums[0])
    b = float(nums[1]) if len(nums) > 1 else None
    return (a, b)


def load_manual(path, verbose=True):
    """Read a hand-built results sheet (.xlsx or .csv). Recognised columns (case /
    spacing insensitive, arrows optional): Joint · Physical Location · Distance A-B / B-A ·
    Physical Ribbon · Physical Fibre · A-B Loss · B-A Loss · Average."""
    rows_in = []
    if str(path).lower().endswith(".csv"):
        import csv
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows_in = [r for r in csv.reader(fh)]
    else:
        import openpyxl
        wb = openpyxl.load_workbook(path, data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows_in = [[ws.cell(r, c).value for c in range(1, ws.max_column + 1)]
                   for r in range(1, ws.max_row + 1)]

    def norm(s):
        return re.sub(r'[^a-z0-9]', '', str(s or '').lower().replace('→', '').replace('->', ''))

    hdr_i, cmap = None, {}
    for i, row in enumerate(rows_in[:20]):
        n = [norm(v) for v in row]
        if any('fibre' in v or 'fiber' in v for v in n) and any('loss' in v for v in n):
            hdr_i = i
            for c, v in enumerate(n):
                if not v:
                    continue
                if 'joint' in v and 'joint' not in cmap: cmap['joint'] = c
                elif ('physicallocation' in v or v == 'location') and 'loc' not in cmap: cmap['loc'] = c
                elif 'distance' in v and 'dist' not in cmap: cmap['dist'] = c
                elif 'ribbon' in v and 'ribbon' not in cmap: cmap['ribbon'] = c
                elif ('fibre' in v or 'fiber' in v) and 'fibre' not in cmap: cmap['fibre'] = c
                elif v.startswith('ab') and 'loss' in v and 'ab' not in cmap: cmap['ab'] = c
                elif v.startswith('ba') and 'loss' in v and 'ba' not in cmap: cmap['ba'] = c
                elif 'average' in v and 'avg' not in cmap: cmap['avg'] = c
                elif 'preremedial' in v and 'pre' not in cmap: cmap['pre'] = c
            break
    if hdr_i is None or 'fibre' not in cmap:
        raise ValueError("could not find a header row with a Fibre column and Loss columns")

    out = []
    for row in rows_in[hdr_i + 1:]:
        if not row or all(v in (None, '') for v in row):
            continue
        def g(k):
            c = cmap.get(k)
            return row[c] if (c is not None and c < len(row)) else None
        fib = str(g('fibre') or '').strip()
        if not fib:
            continue
        da, db = _vpair(g('dist'))
        out.append({"joint": g('joint'), "location": g('loc'),
                    "dist_ab": da, "dist_ba": db,
                    "ribbon": g('ribbon'), "fibre": fib,
                    "fno": fibre_num(fib),
                    "their_ab": _vnum(g('ab')), "their_ba": _vnum(g('ba')),
                    "their_avg": _vnum(g('avg')), "pre": _vnum(g('pre'))})
    if verbose:
        print(f"  manual sheet: {len(out)} rows, fibres "
              f"{min((r['fno'] for r in out if r['fno']), default='?')}-"
              f"{max((r['fno'] for r in out if r['fno']), default='?')}")
    return out


def verify_against_fms(manual, rows_raw, nominal=0.06, wl=1550, tol=60.0):
    """For each manual row, look up the FMS events nearest the stated A-B and B-A
    distances for that fibre and compare. Uses the distances from the manual sheet, so
    the location schedule is taken out of the equation entirely."""
    by_no = {}
    for r in rows_raw:
        n = fibre_num(r.get("fid") or "")
        if n:
            by_no.setdefault(n, r)
    out = []
    for m in manual:
        rec = dict(m)
        src = by_no.get(m["fno"])
        rec["fid"] = src.get("fid") if src else None
        rec["test_date"] = src.get("date") if src else None
        rec["length"] = src.get("length") if src else None
        evs = [e for e in ((src or {}).get("events") or [])
               if e.get("position") is not None]
        def nearest(d):
            if d is None or not evs:
                return (None, None, None)
            e = min(evs, key=lambda x: abs(x["position"] - d))
            gap = abs(e["position"] - d)
            if gap > tol:
                return (None, None, round(gap, 1))
            return (e.get(f"w{wl}"), e["position"], round(gap, 1))
        a, pa, ga = nearest(m["dist_ab"])
        b, pb, gb = nearest(m["dist_ba"])
        rec["mine_ab_raw"], rec["mine_ab_pos"], rec["mine_ab_gap"] = a, pa, ga
        rec["mine_ba_raw"], rec["mine_ba_pos"], rec["mine_ba_gap"] = b, pb, gb
        rec["mine_ab"] = nominal if a is None else a
        rec["mine_ba"] = nominal if b is None else b
        rec["mine_avg"] = (rec["mine_ab"] + rec["mine_ba"]) / 2
        rec["n_events"] = len(evs)
        def d(x, y):
            return None if (x is None or y is None) else round(x - y, 4)
        rec["d_ab"] = d(rec["mine_ab"], m["their_ab"])
        rec["d_ba"] = d(rec["mine_ba"], m["their_ba"])
        rec["d_avg"] = d(rec["mine_avg"], m["their_avg"])
        worst = max((abs(v) for v in (rec["d_ab"], rec["d_ba"], rec["d_avg"]) if v is not None),
                    default=None)
        if src is None:
            rec["status"] = "FIBRE NOT PULLED"
        elif not evs:
            rec["status"] = "NO EVENTS FROM FMS"
        elif worst is None:
            rec["status"] = "NO COMPARISON"
        elif worst <= 0.0011:
            rec["status"] = "MATCH"
        elif worst <= 0.02:
            rec["status"] = "CLOSE"
        else:
            rec["status"] = "DIFFERS"
        rec["worst"] = worst
        out.append(rec)
    return out



def dedupe_routes(rows, verbose=True, label=""):
    """A fibre number can have more than one route on an RTU (e.g. two ports, or a main
    and a loop-back entry). Without this their event lists get merged into one fibre and
    the trace looks like it has every joint twice. Keep one route per fibre - the newest
    test, then the one with the most events - and say what was dropped."""
    groups = {}
    for r in rows:
        groups.setdefault(r.get("_key"), []).append(r)
    out, dropped = [], []
    for key, g in groups.items():
        if len(g) == 1:
            out.append(g[0]); continue
        g2 = sorted(g, key=lambda r: (str(r.get("date") or ""), len(r.get("events") or [])),
                    reverse=True)
        out.append(g2[0])
        dropped.extend(g2[1:])
    if verbose and dropped:
        print(f"  ** {len(dropped)} duplicate route(s) {label}dropped so events are not merged:")
        shown = 0
        for r in dropped:
            if shown >= 3:
                break
            print(f"       {r.get('fid')}  desc={r.get('desc')!r}  "
                  f"test={str(r.get('date') or '')[:19]}  events={len(r.get('events') or [])}")
            shown += 1
        kept = {r.get("_key"): r for r in out}
        ex = dropped[0]
        k = kept.get(ex.get("_key"))
        if k is not None:
            print(f"       kept for that fibre: test={str(k.get('date') or '')[:19]} "
                  f"events={len(k.get('events') or [])}")
        print("       (use --route-desc to pick a specific set, e.g. --route-desc SNBC:)")
    return out



def split_legs_same_name(rows, verbose=True):
    """Two routes can share a route NAME while their descriptions name the two legs of a
    backsplice loop, e.g. both called F169 but described 'SNBC:F169' (main) and
    'SNBC-L:F181' (loop-back). They are the two directions of the same light path, so they
    must be PAIRED, not de-duplicated. Returns (mains, loops, paired_count)."""
    mains, loops = {}, {}
    for r in rows:
        _, _, is_loop = parse_desc(r.get("desc"))
        d = loops if is_loop else mains
        k = r.get("_key")
        prev = d.get(k)
        # within a leg, keep the newest test
        if prev is None or str(r.get("date") or "") > str(prev.get("date") or ""):
            d[k] = r
    both = sorted(set(mains) & set(loops))
    if verbose and both:
        ex = both[0]
        print(f"  loop legs found by description: {len(both)} fibres have BOTH a main and a "
              f"loop-back trace - pairing them as the two directions")
        print(f"       e.g. {mains[ex].get('fid')}: main {mains[ex].get('desc')!r} "
              f"<-> loop-back {loops[ex].get('desc')!r}")
    rowsA, rowsB = [], []
    for k in sorted(set(mains) | set(loops)):
        a, b = mains.get(k), loops.get(k)
        if a is None:
            a = dict(b, events=[])
        if b is None:
            b = dict(a, events=[])
        rowsA.append(a)
        rowsB.append(b)
    return rowsA, rowsB, len(both)


def norm_date(s, end=False):
    """Accepts '2026-08-13', '13/08/2026', optionally followed by 'HH:MM' (24h).
    Returns ISO for the results-window comparison. The window is FROM..TO inclusive;
    within it the tool uses each route's NEWEST result."""
    if not s:
        return None
    s = s.strip()
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}))?$', s)
    if m:
        d, mo, y, hh, mm = m.groups()
        s = f"{y}-{int(mo):02d}-{int(d):02d}" + (f"T{int(hh):02d}:{mm}:00" if hh else "")
    m = re.match(r'^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})$', s)
    if m:
        s = f"{m.group(1)}T{int(m.group(2)):02d}:{m.group(3)}:00"
    if len(s) <= 10:      # date only
        return s + ("T23:59:59.999Z" if end else "T00:00:00.000Z")
    if len(s) == 19 and "T" in s:
        return s + (".999Z" if end else ".000Z")
    return s


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Pull EXFO FMS results into the bidirectional workbook.")
    ap.add_argument("--rtu-a", help="End A: a distinctive token of that end's RTU name (e.g. 0057).")
    ap.add_argument("--rtu-b", help="End B: a distinctive token of that end's RTU name (e.g. RGAC-1991133).")
    ap.add_argument("--cable", default="", help="Pick the cable instead of the two RTUs (e.g. RGAC-SNBC). The tool finds the RTU at each end from the route names. --rtu-a/--rtu-b override this.")
    ap.add_argument("--list-rtus", action="store_true", help="List the tenant's RTUs and exit.")
    ap.add_argument("--scan", default="RTU", help="Search token used to enumerate RTUs for the menu/list (default 'RTU').")
    ap.add_argument("--from", dest="dfrom", help="Only results on/after this date (YYYY-MM-DD or full ISO).")
    ap.add_argument("--to", dest="dto", help="Only results on/before this date.")
    ap.add_argument("--headline-wl", type=int, default=1550, choices=[1310, 1550, 1625],
                    help="Wavelength used for the headline loss column (default 1550).")
    ap.add_argument("--limit", type=int, help="Only pull the first N fibres per end (for testing).")
    ap.add_argument("--splice-threshold", type=float, default=0.15, help="Flag splices with averaged loss >= this (dB) on the Splice Loss tab (default 0.15).")
    ap.add_argument("--splice-wl", type=int, default=1550, choices=[1310, 1550, 1625], help="Wavelength the splice threshold is judged on (default 1550, matching the client).")
    ap.add_argument("--splice-tol", type=float, default=150.0, help="Max position gap (m) when pairing a splice between the two ends (default 150).")
    ap.add_argument("--splice-merge", type=float, default=0.0, help="Combine an end's own events within this many metres before pairing (default 0 = off).")
    ap.add_argument("--splice-margin", type=float, default=100.0, help="Ignore events within this many metres of each cable end (ODF connectors) (default 100).")
    ap.add_argument("--splice-nominal", type=float, default=0.02, help="Nominal loss assumed for a direction that saw no event, when averaging (default 0.02, matching the EXFO customer report). The wizard asks for it, with 0.02 as the default.")
    ap.add_argument("--ribbons", help="Only these ribbons, 12 fibres each (e.g. '1-2' or '1-2,19-20'). Ribbon 1 = F001-F012.")
    ap.add_argument("--list-tests", action="store_true", help="List EVERY test held for the selected fibres (date, type, category, whether it carries splice events) so you can see which one a report will use.")
    ap.add_argument("--route-desc", help="Only use routes whose FMS description contains this (e.g. 'SNBC:' for main, '-L:' for loop-back).")
    ap.add_argument("--verify", metavar="PATH", help="Compare a hand-built results sheet (.xlsx/.csv) against what FMS holds; writes a Verify sheet showing your value vs the pulled value vs the difference.")
    ap.add_argument("--verify-tol", type=float, default=60.0, help="How near a stated distance an FMS event must be to count as that joint (m, default 60).")
    ap.add_argument("--locations", help="Path to the backsplice/joint distance schedule workbook (maps measured distances to named locations).")
    ap.add_argument("--loc-tol", type=float, default=120.0, help="How close a measured event must be to a scheduled distance to take its location name (m, default 120).")
    ap.add_argument("--view-wl", default="1550", help="Wavelength(s) shown in Cable_View / Detailed Cable View, e.g. 1550 or 1310,1550,1625 (default 1550 for readability).")
    ap.add_argument("--list-desc", action="store_true", help="List routes with their FMS description (shows which are main vs loop-back), then exit.")
    ap.add_argument("--loc-sheet", default="", help="Sheet name in the distance schedule "
                    "(a workbook often holds one cable per sheet). Blank = pick the sheet "
                    "that best fits the data.")
    ap.add_argument("--only-scheduled", action="store_true", help="Backsplice remedials: show only locations in the distance schedule. Off by default, so unlisted joints still appear, grouped by distance.")
    ap.add_argument("--unmapped-min", type=int, default=3, help="How many fibres an unlisted joint must appear on before it earns a column (default 3). 1 keeps every one-off.")
    ap.add_argument("--unmapped-tol", type=float, default=25.0, help="Metres within which unscheduled events from different fibres are treated as the same joint (default 25).")
    ap.add_argument("--per-binder", type=int, default=6, help="Ribbons per binder, used for the Binder column in the reports (default 6, so binder 1 = ribbons 1-6).")
    ap.add_argument("--split", default="", help="Build a SEPARATE workbook per ribbon group from ONE pull, e.g. --split \"1-2,3-18,19-20,21-36\". Use when ribbon groups take different routes, so their joints sit at different distances.")
    ap.add_argument("--find-route", default="", help="Diagnostic: show which RTU holds the routes whose names contain this text, e.g. SGIC-SNBC. Then exit.")
    ap.add_argument("-y", "--yes", action="store_true", help="Skip the data check prompt and build the report anyway.")
    ap.add_argument("--no-check", action="store_true", help="Do not show the data check at all.")
    ap.add_argument("--any-category", action="store_true", help="Include results the standard FMS filter leaves out (monitoring, nulling, non-adhoc OTDR). Use when a test you know exists is not being picked up.")
    ap.add_argument("--changelog", action="store_true", help="Show the version log and exit.")
    ap.add_argument("--no-cache", action="store_true", help="Ignore the cached RTU list and re-enumerate from the server.")
    ap.add_argument("--no-curve-fill", action="store_true", help="Do not measure missing directions off the raw trace. Faster, but blank directions fall back to the nominal.")
    ap.add_argument("--keep-ends", action="store_true", help="Keep launch and end-of-fibre events in the remedial reports. Off by default: they are tester artefacts, not field joints, and the launch cannot be seen from the far end.")
    ap.add_argument("--otdr-analyse", "--otdr-analyze", dest="otdr_analyse", action="store_true",
                    help="Measure splice loss directly from the RAW OTDR traces at every scheduled location. Use when no analysed iOLM exists. Needs --locations.")
    ap.add_argument("--otdr-csv", default="", help="Folder to write decoded traces to as CSV (distance_m,power_dB) so you can plot them in Excel.")
    ap.add_argument("--otdr-win", type=float, default=250.0, help="Length of the slope fit window either side of an event, metres (default 250).")
    ap.add_argument("--otdr-gap", type=float, default=30.0, help="Dead zone skipped either side of the event before fitting, metres (default 30).")
    ap.add_argument("--dump-otdr", action="store_true", help="Diagnostic: print one OTDR result record + otdr/extract response (needs --rtu-a; use --ribbons/--from to target), then exit.")
    ap.add_argument("--dump-raw", metavar="PATH", help="Pull both ends and write ALL raw events to a JSON file (for offline tuning), then exit.")
    ap.add_argument("--customer", default="")
    ap.add_argument("--partner", default="Motion Rail")
    ap.add_argument("--cable-name", default="")
    ap.add_argument("--budget-sys", type=float, default=7.5)
    ap.add_argument("--budget-1550", type=float, default=9.0)
    ap.add_argument("--max-km", type=float, default=0.25)
    ap.add_argument("--out", default="", help="Output .xlsx. Blank = auto-named <RTU>_<ribbons>_<date>_<time>.xlsx")
    ap.add_argument("--self-test", action="store_true", help="Build from synthetic data, no network.")
    ap.add_argument("--wizard", action="store_true", help="Interactive prompts (report mode, RTUs, test type, dates). Also runs when no arguments are given.")
    ap.add_argument("--mode", choices=["bidir", "uni", "backsplice"], default="bidir", help="Report mode (wizard asks if not given).")
    ap.add_argument("--test-type", choices=["iOLM", "OTDR", "auto"], default="auto", help="Which test results to use.")
    ap.add_argument("--bs-pairs", default="1-2", help="Backsplice ribbon pairs, e.g. '1-2' or '1-2,3-4'.")
    ap.add_argument("--bs-reversed", action="store_true", help="Backsplice fibre mapping reversed (fibre i <-> fibre 13-i).")
    ap.add_argument("--bs-by-desc", action="store_true", help="Backsplice: pair main and loop-back fibres using the FMS route description (RGAC:F197 <-> RGAC-L:F209). Auto-detected when descriptions contain -L.")
    ap.add_argument("--bs-same-route", action="store_true", help="Backsplice: both direction traces are saved under the SAME route name (e.g. both named fibre 001) — pull the two newest results per route.")
    ap.add_argument("--dump-elements", action="store_true", help="Print one fibre's splice/event table shape (needs --rtu-a), then exit.")
    ap.add_argument("--dump-pair", metavar="FID_SUBSTR", help="Print both ends' raw events for the fibre whose name contains this (needs --rtu-a and --rtu-b), then exit.")
    args = ap.parse_args()

    args.reports = None
    if args.wizard or len(sys.argv) == 1:
        wizard(args)
    if args.reports is None:   # CLI (non-wizard) defaults by mode
        args.reports = {"usplice"} if args.mode == "uni" else {"e2e", "bsplice", "odf"}

    settings = dict(budget_sys=args.budget_sys, budget_1550=args.budget_1550,
                    max_km=args.max_km, star_floor=3.0, nominal=args.splice_nominal, per_binder=args.per_binder, per_ribbon=FIBRES_PER_RIBBON)
    loss_label = f"Loss @{args.headline_wl}"
    if getattr(args, 'any_category', False):
        globals()['ANY_CATEGORY'] = True
        print("  using the relaxed result filter (--any-category)")

    if getattr(args, "changelog", False):
        show_changelog()
        return
    if not args.wizard:
        banner()
    if args.self_test:
        args.out = args.out or "Self_Test.xlsx"
        A = [{"fid": f"F-DEMO-F{i:03d}", "date": "2026-06-18T06:00:00Z", "loss": 7 + i * 0.03,
              "length": 32000 + i, "star": 4, "w1310": 13 + i * .02, "w1550": 8 + i * .02,
              "w1625": 8.4 + i * .02} for i in range(1, 13)]
        B = [{"fid": f"F-DEMO-F{i:03d}", "date": "2026-06-18T22:00:00Z", "loss": 7.1 + i * 0.02,
              "length": 32000 + i, "star": 4, "w1310": 13.2 + i * .02, "w1550": 8.1 + i * .02,
              "w1625": 8.5 + i * .02} for i in range(1, 13)]
        build_workbook(args.out, dict(customer="DEMO", cable="DEMO", partner=args.partner,
                       siteA="End A", siteB="End B", ribbons="1",
                       generated=time.strftime("%Y-%m-%d %H:%M"), device="iOLM", resultset="self-test"),
                       settings, A, B, loss_label=loss_label)
        print(f"Self-test workbook written: {args.out}")
        return

    cfg = load_settings()
    if not cfg.get("user") and not os.environ.get("FMS_USER"):
        cfg = first_run(cfg)
    if cfg.get("per_binder") and args.per_binder == 6:
        args.per_binder = int(cfg["per_binder"])
    user = os.environ.get("FMS_USER") or cfg.get("user")
    if not user:
        sys.stderr.write("FMS username (email): ")
        sys.stderr.flush()
        user = input("").strip()
    _env_pass = os.environ.get("FMS_PASS")
    password = _env_pass or getpass.getpass("FMS password: ")
    print("Authenticating...")
    session = FmsSession(user, password, retry_password=not _env_pass)

    if args.dump_elements:
        if not args.rtu_a:
            sys.exit("--dump-elements needs --rtu-a (an RTU token) to pick a fibre.")
        nodesA, _ = resolve_end(session, args.rtu_a)
        dump_elements(session, nodesA[0]["id"])
        return

    if getattr(args, "list_desc", False):
        if not args.rtu_a:
            sys.exit("--list-desc needs --rtu-a.")
        nodesA, _ = resolve_end(session, args.rtu_a)
        if args.ribbons:
            nodesA = filter_ribbons(nodesA, args.ribbons)
        main, loop = split_main_loop(nodesA)
        print(f"\n{len(nodesA)} routes: {len(main)} main, {len(loop)} loop-back")
        print(f"{'route name':32s} {'description':18s} {'leg':9s} {'desc fibre':>10s}")
        for n in nodesA:
            tag, num, is_loop = parse_desc(desc_of(n))
            print(f"{(n.get('name') or ''):32s} {desc_of(n):18s} "
                  f"{('LOOPBACK' if is_loop else 'main'):9s} {(num if num else ''):>10}")
        return

    if getattr(args, "list_tests", False):
        if not args.rtu_a:
            sys.exit("--list-tests needs --rtu-a.")
        nodesA, _ = resolve_end(session, args.rtu_a)
        if args.ribbons:
            nodesA = filter_ribbons(nodesA, args.ribbons)
        lim = args.limit or 4
        print(f"\nEvery test held for the first {min(lim, len(nodesA))} route(s):")
        for n in nodesA[:lim]:
            print(f"\n  {n.get('name')}   desc={n.get('description') or ''!r}")
            results, used = [], None
            for _f, _lab in ([(adhoc_filter(n["id"]), "standard filter")] +
                             [(x, "relaxed filter") for x in relaxed_filters(n["id"])]):
                try:
                    data = session.rest_get(RESULTS_URL, params={
                        "$filter": _f, "$orderby": "metadata/TestTime desc",
                        "$top": 30, "$skip": 0,
                        "$select": "resultid,metadata,brief/LinkResults,brief/Measurement/Elements"})
                except Exception:
                    continue
                got = data.get("results") or data.get("value") or []
                if len(got) > len(results):
                    results, used = got, _lab
                if _lab == "relaxed filter" and got:
                    break
            if used:
                print(f"    (most results came from the {used})")
            if not results:
                print("    no results at all"); continue
            print(f"    {'test time':21s} {'type':6s} {'category':12s} {'wl':>14s} {'events':>7s}")
            for r in results:
                md = r.get("metadata") or {}
                brief = r.get("brief") or {}
                lr = brief.get("LinkResults") or {}
                wls = [str(_to_nm(w.get("Wavelength"))) for w in (lr.get("Results") or [])]
                els = ((brief.get("Measurement") or {}).get("Elements")) or []
                print(f"    {str(md.get('TestTime') or '')[:19]:21s} "
                      f"{str(md.get('TestType') or ''):6s} "
                      f"{str(md.get('TestCategory') or ''):12s} "
                      f"{'/'.join(wls) or '-':>14s} {len(els):>7d}")
        print("\nThe report uses the NEWEST test that carries events (events > 0).")
        print("If a newer test shows 0 events it is a raw OTDR trace - no splice data via the API.")
        return

    if getattr(args, "dump_otdr", False):
        if not args.rtu_a:
            sys.exit("--dump-otdr needs --rtu-a.")
        import json as _j
        _dumpfh = open("otdr_dump.txt", "w", encoding="utf-8")

        def tee(msg=""):                          # screen + otdr_dump.txt
            print(msg)
            _dumpfh.write(str(msg) + "\n")
            _dumpfh.flush()
        nodesA, _ = resolve_end(session, args.rtu_a)
        if args.ribbons:
            nodesA = filter_ribbons(nodesA, args.ribbons)
        dfrom, dto = norm_date(args.dfrom), norm_date(args.dto, end=True)
        checked = 0
        for n in nodesA:
            try:
                data = session.rest_get(RESULTS_URL, params={
                    "$filter": adhoc_filter(n["id"]), "$orderby": "metadata/TestTime desc",
                    "$top": 10, "$skip": 0,
                    "$select": "resultid,metadata,brief,measurement"})
            except Exception:
                data = session.rest_get(RESULTS_URL, params={
                    "$filter": adhoc_filter(n["id"]), "$orderby": "metadata/TestTime desc",
                    "$top": 10, "$skip": 0,
                    "$select": "resultid,metadata,brief"})
            results = data.get("results") or data.get("value") or []
            checked += 1
            tts = [((r.get("metadata") or {}).get("TestType"), (r.get("metadata") or {}).get("TestTime", "")[:16]) for r in results]
            tee(f"  {n.get('name')}: {len(results)} results {tts[:4]}")
            for r in results:
                md = r.get("metadata") or {}
                tt = md.get("TestTime", "") or ""
                if md.get("TestType") != "OTDR":
                    continue
                if dfrom and tt < dfrom:
                    continue
                if dto and tt > dto:
                    continue
                rid = r.get("resultid")
                tee(f"===== OTDR RESULT RECORD  route {n.get('name')}  resultid {rid} =====")
                tee(_j.dumps(r, indent=2)[:6000])
                tee(f"\n===== otdr/extract RESPONSE for {rid} =====")
                try:
                    ex = session.rest_post(RESULTS_URL + f"{rid}/otdr/extract")
                    tee(_j.dumps(ex, indent=2)[:8000])
                except Exception as e2:
                    tee(f"extract error: {e2}")
                tee("\nSaved to otdr_dump.txt in this folder - upload that file to the chat.")
                _dumpfh.close()
                return
        sys.exit(f"No OTDR result matched on {checked} routes in that date window. "
                 "Loosen/remove --from/--to, or check --ribbons.")

    if args.dump_pair:
        if not (args.rtu_a and args.rtu_b):
            sys.exit("--dump-pair needs both --rtu-a and --rtu-b.")
        nodesA, _ = resolve_end(session, args.rtu_a)
        nodesB, _ = resolve_end(session, args.rtu_b)
        na = next((n for n in nodesA if args.dump_pair in (n.get("name") or "")), None)
        nb = next((n for n in nodesB if args.dump_pair in (n.get("name") or "")), None)
        if not na or not nb:
            sys.exit(f"Fibre '{args.dump_pair}' not found on both ends.")
        da = fetch_iolm(session, na["id"])
        db = fetch_iolm(session, nb["id"])
        for lbl, d in (("END A", da), ("END B", db)):
            print(f"\n===== {lbl}  {na['name'] if lbl=='END A' else nb['name']}  "
                  f"length={d.get('length')}  test={d.get('testtime')} =====")
            print(f"{'pos(m)':>10} {'type':<10} {'status':<10} {'1310':>8} {'1550':>8} {'1625':>8}")
            for ev in d.get("events", []):
                print(f"{(ev.get('position') or 0):>10.1f} {str(ev.get('type')):<10} "
                      f"{str(ev.get('status')):<10} "
                      f"{('' if ev.get('w1310') is None else round(ev['w1310'],3)):>8} "
                      f"{('' if ev.get('w1550') is None else round(ev['w1550'],3)):>8} "
                      f"{('' if ev.get('w1625') is None else round(ev['w1625'],3)):>8}")
        print("\nPaste BOTH END A and END B blocks back.")
        return

    # ---- choose the two ends ----
    if args.find_route:
        find_route(session, args.find_route, args.scan)
        return

    if args.list_rtus:
        print(f"Enumerating RTUs (scan token '{args.scan}')...")
        for r in collect_rtus(search_by_rtu(session, args.scan)):
            print(f"  {r['name']:28s}  site={str(r['site']):10s}  {r['count']:4d} routes  "
                  f"cable {r.get('cable') or '?':22s} id={r['rtuId']}")
        return

    two_rtus = (args.mode == "bidir")

    def _menu_rtus():
        # the RTU list is 1700+ routes over 35 pages and barely changes, so it is cached
        # for a day. Most of the old startup wait was re-fetching it every single run.
        cached = None if args.no_cache else cache_load(f"rtus_{args.scan}")
        if cached:
            age = int((time.time() - os.path.getmtime(_cache_path(f"rtus_{args.scan}"))) / 60)
            print(f"RTU list from cache ({len(cached['rtus'])} RTUs, {age} min old; "
                  f"--no-cache to refresh)")
            return cached["nodes"], cached["rtus"]
        print(f"Enumerating RTUs (scan token '{args.scan}')...")
        sn = search_by_rtu(session, args.scan)
        rt = collect_rtus(sn)
        if not rt:
            sys.exit("No RTUs found. Try a different --scan token.")
        cache_save(f"rtus_{args.scan}", {"nodes": sn, "rtus": rt})
        return sn, rt

    _rtu_of = lambda n: (n.get("rtu") or {}).get("name") or n.get("rtuName")
    if args.cable and not (args.rtu_a and args.rtu_b):
        # Pick the cable; the tool finds which RTU faces which end from the route names.
        scan_nodes, rtus = _menu_rtus()
        cab = cables_from_rtus(rtus)
        key = _match_cable_key(cab, args.cable)
        if not key:
            sys.exit(f"--cable '{args.cable}': no cable matched. Run --list-rtus to see the "
                     f"cables each RTU serves.")
        rtuA, rtuB = order_ends(key, cab[key])
        if two_rtus and not rtuB:
            sys.exit(f"--cable '{args.cable}': only one RTU serves {re.sub(r'^F-','',key)}, so "
                     f"it is not a two-ended job. Use --mode uni (or backsplice), or give "
                     f"--rtu-a/--rtu-b.")
        if not two_rtus:
            rtuB = rtuA
        print(f"Cable {re.sub(r'^F-','',key)}: End A = {rtuA['name']} ({rtuA['site']})"
              + (f", End B = {rtuB['name']} ({rtuB['site']})" if two_rtus else ""))
        nodesA = [n for n in scan_nodes if _rtu_of(n) == rtuA["name"]]
        nodesB = [n for n in scan_nodes if _rtu_of(n) == rtuB["name"]] if two_rtus else []
    elif two_rtus and args.rtu_a and args.rtu_b:
        print(f"Resolving End A ('{args.rtu_a}')...")
        nodesA, rtuA = resolve_end(session, args.rtu_a)
        print(f"Resolving End B ('{args.rtu_b}')...")
        nodesB, rtuB = resolve_end(session, args.rtu_b)
    elif not two_rtus and args.rtu_a:
        print(f"Resolving RTU ('{args.rtu_a}')...")
        nodesA, rtuA = resolve_end(session, args.rtu_a)
        nodesB, rtuB = [], rtuA
    else:
        scan_nodes, rtus = _menu_rtus()
        if two_rtus:
            rtuA, rtuB = _pick_cable(rtus, two_rtus=True)
            if rtuA is None:                      # no cables grouped: pick RTUs by hand
                rtuA = _pick_rtu(rtus, "End A (A->B)")
                rtuB = _pick_rtu(rtus, "End B (B->A)")
        else:
            rtuA = _pick_rtu(rtus, "the test RTU")
            rtuB = rtuA
        nodesA = [n for n in scan_nodes if _rtu_of(n) == rtuA["name"]]
        nodesB = [n for n in scan_nodes if _rtu_of(n) == rtuB["name"]] if two_rtus else []

    # ribbon scope: backsplice needs every ribbon named in the pairs
    ribbons_spec = args.ribbons
    if args.mode == "backsplice" and getattr(args, "bs_by_desc", False):
        print("Backsplice by description: pulling main and loop-back routes together")
        ribbons_spec = args.ribbons
    elif args.mode == "backsplice":
        pairs = parse_ribbon_pairs(args.bs_pairs)
        if getattr(args, "bs_same_route", False):
            ribs = sorted({p[0] for p in pairs})
            ribbons_spec = ",".join(str(r) for r in ribs)
            print(f"Backsplice pairs {pairs}, both traces per route -> pulling ribbons {ribbons_spec} (2 results each)")
        else:
            ribs = sorted({r for p in pairs for r in p})
            ribbons_spec = ",".join(str(r) for r in ribs)
            print(f"Backsplice pairs {pairs} -> pulling ribbons {ribbons_spec}")
    if ribbons_spec:
        nodesA = filter_ribbons(nodesA, ribbons_spec)
        if two_rtus:
            nodesB = filter_ribbons(nodesB, ribbons_spec)
        if two_rtus:
            print(f"  End B: {len(nodesB)} traces")

    print(f"\nRTU A = {rtuA['name']} ({rtuA['site']}), {len(nodesA)} fibres  [{args.test_type}]")
    if two_rtus:
        print(f"RTU B = {rtuB['name']} ({rtuB['site']}), {len(nodesB)} fibres")

    dfrom, dto = norm_date(args.dfrom), norm_date(args.dto, end=True)
    if dfrom or dto:
        print(f"Date window: {dfrom or 'any'}  ->  {dto or 'any'}")

    labelA = rtuA['site'] or rtuA['name']
    labelB = rtuB['site'] or rtuB['name']
    if args.test_type == "OTDR" and (R & {"bsplice", "usplice"}):
        print("\n*** NOTE: OTDR results are raw traces with no event table, so the splice")
        print("*** tabs and Cable_View will be empty. Re-run with test type Auto or iOLM")
        print("*** for splice-by-location data.\n")

    if args.mode == "backsplice" and getattr(args, "bs_by_desc", False):
        print("Pulling routes (main + loop-back)...")
        pulled_all = build_end_rows(session, nodesA, dfrom, dto, args.headline_wl, labelA,
                                    limit=args.limit, test_type=args.test_type)
        rowsA_raw, rowsB_raw = backsplice_by_desc(pulled_all)
        if not rowsA_raw:
            sys.exit("No main/loop-back pairs found in the route descriptions. "
                     "Run --list-desc to see what the descriptions look like.")
        labelA = f"{labelA} (main)"
        labelB = f"{rtuA['site'] or rtuA['name']} (loop-back)"
        pulled = None
    elif args.mode == "backsplice" and getattr(args, "bs_same_route", False):
        print("Pulling loop traces (2 newest results per route)...")
        rowsA_raw, rowsB_raw = build_loop_rows(
            session, nodesA, dfrom, dto, args.headline_wl, labelA,
            pairs=parse_ribbon_pairs(args.bs_pairs), reversed_map=args.bs_reversed,
            limit=args.limit, test_type=args.test_type)
        labelA = f"{labelA} (trace 1)"
        labelB = f"{labelB} (trace 2)"
        got2 = sum(1 for r in rowsB_raw if r.get("loss") is not None)
        print(f"  {len(rowsA_raw)} loops; second trace found for {got2}")
        pulled = None
    else:
        print("Pulling results..." if not two_rtus else "Pulling End A results...")
        pulled = build_end_rows(session, nodesA, dfrom, dto, args.headline_wl, labelA,
                                limit=args.limit, test_type=args.test_type)
    if pulled is None:
        pass
    elif two_rtus:
        print("Pulling End B results...")
        rowsA_raw = pulled
        rowsB_raw = build_end_rows(session, nodesB, dfrom, dto, args.headline_wl, labelB,
                                   limit=args.limit, test_type=args.test_type)
    elif args.mode == "backsplice":
        rowsA_raw, rowsB_raw = backsplice_rows(pulled, parse_ribbon_pairs(args.bs_pairs),
                                               reversed_map=args.bs_reversed)
        labelA = f"{labelA} (out)"
        labelB = f"{rtuA['site'] or rtuA['name']} (return)"
        print(f"Backsplice overlay: {len(rowsA_raw)} loop pairs")
    else:  # uni
        rowsA_raw = pulled
        rowsB_raw = [dict(r, events=list(r.get('events') or [])) for r in pulled]
        labelB = f"{labelA} (same end)"

    if args.route_desc:
        want = args.route_desc.lower()
        before = len(rowsA_raw)
        rowsA_raw = [r for r in rowsA_raw if want in str(r.get("desc") or "").lower()]
        rowsB_raw = [r for r in rowsB_raw if want in str(r.get("desc") or "").lower()]
        print(f"  route description filter '{args.route_desc}': kept {len(rowsA_raw)}/{before}")
    # Same route name, two descriptions (main + '-L' loop-back) = the two legs of a loop.
    # Pair them as the two directions instead of throwing one away.
    _mains, _loops, _npair = split_legs_same_name(rowsA_raw, verbose=True)
    if _npair and not args.route_desc:
        rowsA_raw, rowsB_raw = _mains, _loops
        labelA = f"{labelA} (main)"
        labelB = f"{rtuA['site'] or rtuA['name']} (loop-back)"
        SINGLE_OVERRIDE = False
    else:
        SINGLE_OVERRIDE = None
        rowsA_raw = dedupe_routes(rowsA_raw, label="on End A ")
        if args.mode == 'uni':
            rowsB_raw = [dict(r, events=list(r.get("events") or [])) for r in rowsA_raw]
        else:
            rowsB_raw = dedupe_routes(rowsB_raw, label="on End B ")

    if args.dump_raw:
        import json
        payload = {"siteA": labelA, "siteB": labelB,
                   "A": [{"fid": r["fid"], "length": r.get("length"), "events": r.get("events", [])} for r in rowsA_raw],
                   "B": [{"fid": r["fid"], "length": r.get("length"), "events": r.get("events", [])} for r in rowsB_raw]}
        with open(args.dump_raw, "w") as fh:
            json.dump(payload, fh)
        print(f"Wrote raw events for {len(rowsA_raw)}+{len(rowsB_raw)} fibres to {args.dump_raw}")
        return

    if not args.no_check:
        if not data_check(rowsA_raw, rowsB_raw, labelA, labelB, assume_yes=args.yes):
            print("Stopped. No workbook written.")
            return

    rowsA, rowsB, unmatched = pair_rows(rowsA_raw, rowsB_raw)
    got = sum(1 for r in rowsA_raw if r.get("loss") is not None) + \
          sum(1 for r in rowsB_raw if r.get("loss") is not None)

    # ---- one pull, several reports ----
    # Ribbon groups can take different routes, so their joints sit at different
    # distances. Merging them into one sheet lines up locations that are not the same
    # place. --split builds a separate workbook per group off the SAME pull, so the
    # server is hit once and each report only contains fibres that share a route.
    _groups = []
    if args.split:
        for part in str(args.split).split(','):
            part = part.strip()
            if not part:
                continue
            err = ribbon_spec_error(part)
            if err:
                sys.exit(f'--split: {err}')
            _groups.append(part)
    _all_A, _all_B = rowsA_raw, rowsB_raw
    _base_out = args.out
    _base_reports = set(args.reports)
    _base_ribbons = args.ribbons
    # With --split the first pass is the whole cable: end-to-end loss, connector loss and
    # the client verify are not location-dependent, so they belong in ONE workbook. The
    # per-group passes then carry only the splice-by-location material, which is the part
    # that genuinely differs between ribbon groups.
    _passes = ([None] + _groups) if _groups else [None]
    _splice_only = False
    for _gi, _grp in enumerate(_passes):
        args.reports = set(_base_reports)
        _splice_only = False
        if _grp:
            _want = parse_ribbons(_grp, FIBRES_PER_RIBBON)
            rowsA_raw = [r for r in _all_A if fibre_num(r.get('fid')) in _want]
            rowsB_raw = [r for r in _all_B if fibre_num(r.get('fid')) in _want]
            if not rowsA_raw:
                print(f'\n--split {_grp}: no fibres in this group, skipped')
                continue
            print('\n' + '=' * 68)
            print(f'  SPLICE REPORT {_gi} of {len(_groups)}: ribbons {_grp}  '
                  f'({len(rowsA_raw)} fibres)')
            print('=' * 68)
            args.out = ''
            args.ribbons = _grp
            args.reports = {r for r in _base_reports if r not in ('e2e', 'odf')}
            _splice_only = True
            rowsA, rowsB, unmatched = pair_rows(rowsA_raw, rowsB_raw)
        else:
            rowsA_raw, rowsB_raw = _all_A, _all_B
            args.ribbons = _base_ribbons
            args.out = _base_out
            if _groups:
                print('\n' + '=' * 68)
                print(f'  CABLE-WIDE REPORT: end-to-end loss, connector loss and the '
                      f'client verify')
                print(f'  ({len(rowsA_raw)} fibres, all groups together)')
                print('=' * 68)
                rowsA, rowsB, unmatched = pair_rows(rowsA_raw, rowsB_raw)
        R = args.reports
        SINGLE = (args.mode == 'uni')   # one RTU, one direction: no averaging
        if SINGLE_OVERRIDE is False:    # loop legs paired -> we really do have two directions
            SINGLE = False
        splices = None
        cable_view = None
        all_events = None
        remedials = None
        bs_remed = None
        schedule = None
        SCHEDULE_STAMP = None
        otdr_rows, otdr_diag = None, None
        extra_pts = []
        if "bsplice" in R:
            splices = bidir_splices(rowsA_raw, rowsB_raw, args.splice_threshold,
                                    tol=args.splice_tol, wl=args.splice_wl, merge=args.splice_merge,
                                    nominal=args.splice_nominal, margin=args.splice_margin,
                                    single=SINGLE)
            both = sum(1 for s in splices if s.get("dirs") == 2)
            print(f"Bidir splices >= {args.splice_threshold} dB @ {args.splice_wl}: "
                  f"{len(splices)} ({both} both directions)")
            # Cable_View / Detailed Cable View: joint-by-joint matrix of ALL events
            evs = [e for e in paired_events(rowsA_raw, rowsB_raw, tol=args.splice_tol,
                                            wl=args.splice_wl, merge=args.splice_merge,
                                            single=SINGLE)
                   if e.get("type") in ("Splice", "Group", "Connector")]
            joints = discover_joints(evs)
            lengths = [r.get("length") for r in rowsA_raw if r.get("length")]
            cable_view = dict(joints=joints, matrix=joint_matrix(evs, joints),
                              fibres=[r["fid"] for r in rowsA_raw],
                              length=(sum(lengths) / len(lengths)) if lengths else None)
            print(f"Cable view: {len(joints)} joints discovered")
            all_events = all_events_table(rowsA_raw, rowsB_raw, single=SINGLE,
                                          tol=args.splice_tol, wl=args.splice_wl)
            print(f"All Events sheet: {len(all_events)} events across {len(rowsA_raw)} fibres")
        elif "usplice" in R:
            splices = uni_splices(rowsA_raw, args.splice_threshold, wl=args.splice_wl,
                                  margin=args.splice_margin)
            print(f"Uni splices >= {args.splice_threshold} dB @ {args.splice_wl}: {len(splices)}")
            evs = [e for e in paired_events(rowsA_raw, rowsB_raw, tol=args.splice_tol,
                                            wl=args.splice_wl, merge=args.splice_merge,
                                            single=SINGLE)
                   if e.get("type") in ("Splice", "Group", "Connector")]
            joints = discover_joints(evs)
            lengths = [r.get("length") for r in rowsA_raw if r.get("length")]
            cable_view = dict(joints=joints, matrix=joint_matrix(evs, joints),
                              fibres=[r["fid"] for r in rowsA_raw],
                              length=(sum(lengths) / len(lengths)) if lengths else None)
            print(f"Cable view: {len(joints)} joints discovered")
            all_events = all_events_table(rowsA_raw, rowsB_raw, single=SINGLE,
                                          tol=args.splice_tol, wl=args.splice_wl)
            print(f"All Events sheet: {len(all_events)} events across {len(rowsA_raw)} fibres")
        odf = build_odf(rowsA_raw, rowsB_raw) if "odf" in R else None

        # ---- verify against a hand-built sheet ----
        verify = None
        client_rows, client_info = None, None
        if args.verify:
            args.verify = str(args.verify).strip().strip('"').strip("'")
            try:
                # A client Customer Cable Report has an E2E sheet and a different shape to the
                # hand-built tables, so detect it rather than making anyone re-key it.
                _client = None
                if args.verify.lower().endswith((".xlsx", ".xlsm")):
                    try:
                        _c = load_client_report(args.verify)
                        if _c.get("e2e"):
                            _client = _c
                    except Exception:
                        _client = None
                if _client:
                    client_info = {k: _client.get(k) for k in ("cable", "siteA", "siteB", "dates")}
                    client_rows = verify_client(_client, rowsA_raw, rowsB_raw,
                                                wl=args.splice_wl, tol=args.verify_tol,
                                                single=SINGLE, splice_tol=args.splice_tol,
                                                margin=args.splice_margin)
                    from collections import Counter
                    for kind in ("E2E", "Splice"):
                        part = [r for r in client_rows if r["kind"] == kind]
                        if part:
                            t = Counter(r["status"] for r in part)
                            print(f"Client verify {kind}: " +
                                  ", ".join(f"{k} {n}" for k, n in t.most_common()))
                    print(f"  their report: {client_info['cable']} {client_info['siteA']} to "
                          f"{client_info['siteB']}, tested {', '.join(client_info['dates'] or [])}")
                    raise StopIteration
                manual = load_manual(args.verify)
                verify = verify_against_fms(manual, rowsA_raw, nominal=args.splice_nominal,
                                            wl=args.splice_wl, tol=args.verify_tol)
                from collections import Counter
                tally = Counter(v["status"] for v in verify)
                print(f"Verify vs {args.verify}: " +
                      ", ".join(f"{k} {n}" for k, n in tally.most_common()))
                dts = {str(v.get("test_date") or "")[:19] for v in verify if v.get("test_date")}
                if dts:
                    print(f"  FMS test date(s) used: {', '.join(sorted(dts))}")
            except StopIteration:
                pass
            except FileNotFoundError:
                print(f"  verify: file not found -> {args.verify}")
                print("         save your table as .csv or .xlsx first, and give the full path")
                print("         (an example layout is in 'EXAMPLE - manual results for verify.csv')")
            except Exception as ex:
                print(f"  verify error: {ex}")

        # ---- location mapping + remedial reports ----
        if (R & {"remed", "bsremed"}) or args.locations:
            if all_events is None:
                all_events = all_events_table(rowsA_raw, rowsB_raw, single=SINGLE,
                                              tol=args.splice_tol, wl=args.splice_wl)
                print(f"All Events: {len(all_events)} events")
            if args.locations:
                args.locations = str(args.locations).strip().strip('"').strip("'")
                try:
                    if args.loc_sheet:
                        schedule = load_locations(args.locations, sheet=args.loc_sheet)
                    else:
                        # a sheet that names its cable beats guessing from the data
                        _cab = None
                        for _r in rowsA_raw:
                            _m = re.match(r'^(F-[A-Z0-9]+-[A-Z0-9]+)',
                                          str(_r.get("fid") or ""))
                            if _m:
                                _cab = _m.group(1)
                                break
                        _nm, _ent = pick_sheet_for_cable(args.locations, _cab,
                                                         site=(rtuA or {}).get("site"))
                        if _nm:
                            schedule = load_locations(args.locations, sheet=_nm)
                            _e = _ent or {}
                            SCHEDULE_STAMP = (
                                f"{os.path.basename(args.locations)} / {_nm}"
                                + (f" / {_e.get('cable')}" if _e.get("cable") else "")
                                + (f" rev {_e.get('rev')}" if _e.get("rev") else " (no rev)")
                                + (f", issued {_e.get('issued')}" if _e.get("issued") else ""))
                        else:
                            _nm, schedule = pick_location_sheet(args.locations, all_events,
                                                                tol=args.loc_tol)
                            if schedule is None:
                                schedule = load_locations(args.locations)
                    hit = map_locations(all_events, schedule, tol=args.loc_tol)
                    DBG.flag(f"Location matching: {hit} of {len(all_events)} events matched a named location within {args.loc_tol:.0f} m. {len(all_events)-hit} did not and show as unscheduled joints.")
                    print(f"  matched {hit}/{len(all_events)} events to named locations "
                          f"(within {args.loc_tol:g} m)")
                    if cable_view and cable_view.get("joints"):
                        cable_view["names"] = joint_names(
                            cable_view["joints"], schedule,
                            fibres=cable_view.get("fibres"), tol=args.loc_tol)
                        print(f"  cable views: named {len(cable_view['names'])}"
                              f"/{len(cable_view['joints'])} joint columns")
                    if all_events and hit < len(all_events) * 0.25:
                        print("  !! Only a small fraction matched. That usually means this "
                              "schedule belongs to a different cable or ribbon range,")
                        print("     or the distances are design values that no longer reflect "
                              "the build. Check the schedule before trusting the fold.")
                except Exception as ex:
                    print(f"  location schedule error: {ex}")
            else:
                print("  (no --locations schedule given: remedial reports will show "
                      "distances only, no location names)")
            if R & {"remed"}:
                remedials = remedials_list(
                    all_events if args.keep_ends else drop_terminals(all_events, label=""),
                    threshold=args.splice_threshold,
                                           nominal=args.splice_nominal, wl=args.splice_wl,
                                           single=SINGLE)
                print(f"Remedials: {len(remedials)} splices >= {args.splice_threshold} dB")
            if R & {"bsremed"}:
                if not any(e.get("location") for e in all_events):
                    # no usable schedule: fall back to the joints found in the data itself
                    jts = discover_joints(all_events, min_fibres=2)
                    if not jts:
                        jts = discover_joints(all_events, min_fibres=1)
                    half = (max(jts) / 2.0) if jts else None
                    for e in all_events:
                        p = e.get("position")
                        if p is None or not jts:
                            continue
                        j = min(jts, key=lambda x: abs(x - p))
                        if abs(j - p) <= args.splice_tol:
                            e["location"] = f"{round(j)} m"
                            e["sched_m"] = j
                            e["leg"] = ("Return" if (half and j > half) else "Outbound")
                    print(f"  no location names available - folding on {len(jts)} joints "
                          f"found in the data (labelled by distance)")
                bs_events = all_events if args.keep_ends else drop_terminals(all_events, label="backsplice ")
                if not args.only_scheduled:
                    label_unmapped(bs_events, tol=args.unmapped_tol,
                                   min_fibres=args.unmapped_min)
                    seen_x = {}
                    for _e in bs_events:
                        if _e.get("unscheduled") and _e.get("location") not in seen_x:
                            seen_x[_e["location"]] = (_e.get("sched_m"), _e["location"],
                                                      _e.get("leg"))
                    extra_pts = [v for v in seen_x.values() if v[0]]
                if _npair:
                    # two traces per loop: report per physical fibre, which is the only
                    # pairing that gives a real bi-directional average
                    parts = leg_partners(rowsA_raw, rowsB_raw)
                    bs_order, bs_rows, bs_dist = backsplice_physical(
                        bs_events, partners=parts,
                        nominal=args.splice_nominal, wl=args.splice_wl)
                    bs_labels = ("A-B  (out leg fibre)", "B-A  (from the other trace)",
                                 "Bi-directional average")
                    print(f"  paired per physical fibre: the out leg and the back leg are "
                          f"different fibres, joined at the backsplice")
                else:
                    bs_order, bs_rows, bs_dist = backsplice_remedials(bs_events,
                                                                      nominal=args.splice_nominal,
                                                                      wl=args.splice_wl)
                    bs_labels = None
                bs_remed = {"order": bs_order, "rows": bs_rows, "dist": bs_dist,
                            "labels": bs_labels,
                            "nominal": args.splice_nominal, "wl": args.splice_wl}
                print(f"Backsplice remedials: {len(bs_rows)} fibres x {len(bs_order)} locations")

        stale = [r for r in rowsA_raw if r.get("newer_no_events")]
        if stale:
            newest = max((r["newer_no_events"] for r in stale))
            used = min((str(r.get("date") or "") for r in stale if r.get("date")), default="?")
            print("\n" + "!" * 68)
            print(f"! {len(stale)} of {len(rowsA_raw)} fibres have a NEWER test than the one used.")
            print(f"! Used (has splice events): {str(used)[:19]}")
            print(f"! Newest available        : {str(newest[0])[:19]}  ({newest[1]}, no event table)")
            print("! A raw OTDR trace carries no splice events through the API, so any")
            print("! remediation done after the date above will NOT show in this report.")
            print("! Re-test those fibres as iOLM (or re-analyse in FMS) to capture it.")
            print("!" * 68 + "\n")
        ev_tot = sum(len(r.get("events") or []) for r in rowsA_raw) + sum(len(r.get("events") or []) for r in rowsB_raw)
        print(f"\nPaired {len(rowsA)} fibres; {got} one-way results with loss; unmatched {len(unmatched)}")
        print(f"Splice/events found: {ev_tot}" + ("  (0 = the chosen results carry no event table - use iOLM)" if ev_tot == 0 else ""))

        gaps = 0
        if bs_remed and bs_remed.get("rows") and not args.no_curve_fill:
            for rec in bs_remed["rows"]:
                for loc in bs_remed["order"]:
                    gaps += sum(1 for k in ("out", "ret") if rec.get(k, {}).get(loc) is None)
            if gaps and not args.otdr_analyse:
                print(f"\n{gaps} location/direction cells have no flagged event. Measuring those "
                      f"off the trace beats assuming the {args.splice_nominal} dB nominal.")
                args.otdr_analyse = True

        if getattr(args, "otdr_analyse", False):
            if not schedule:
                print("--otdr-analyse needs a distance schedule (--locations): it measures at "
                      "known distances rather than hunting for events.")
            else:
                print("\nMeasuring splice loss from the raw OTDR traces ...")
                csvdir = args.otdr_csv or None
                if csvdir and not os.path.isdir(csvdir):
                    try:
                        os.makedirs(csvdir, exist_ok=True)
                    except Exception:
                        csvdir = None
                otdr_rows, otdr_diag = otdr_measure_all(
                    session, nodesA, schedule, norm_date(args.dfrom), norm_date(args.dto, end=True),
                    win=args.otdr_win, gap=args.otdr_gap, csv_dir=csvdir,
                    extra_points=extra_pts)
                got_n = sum(1 for r in otdr_rows if r.get("loss") is not None)
                print(f"  {otdr_diag['traces']} traces decoded ({otdr_diag['failed']} failed); "
                      f"{got_n} location measurements")
                if otdr_diag.get("how"):
                    print(f"  trace format: {otdr_diag['how']}")
                for e in otdr_diag.get("errors", []):
                    print(f"  ! {e}")
                _sl, _tg = otdr_diag.get("slope_km"), otdr_diag.get("target_km")
                if _sl is not None and _tg and abs(_sl - _tg) > max(0.06, _tg * 0.3):
                    print(f"  !! CALIBRATION: the trace works out at {_sl} dB/km but the instrument "
                          f"recorded {_tg}. The distance axis or the sample")
                    print("     unit is probably wrong, so treat the measurements as suspect.")
                    print(f"     Spacing came from: {otdr_diag.get('spacing_src')}")
                if otdr_diag.get("range_m") and otdr_diag.get("furthest"):
                    _rng, _far = otdr_diag["range_m"], otdr_diag["furthest"]
                    if _rng < _far * 0.9:
                        print("\n" + "!" * 68)
                        print(f"! SHORT-RANGE TEST: the traces only reach {_rng:,} m, but the schedule "
                              f"puts joints out to {round(_far):,} m.")
                        print("! This is a short-range acquisition, not a full loop test, so anything "
                              "beyond that distance")
                        print("! simply is not in the data and will fall back to the nominal.")
                        print("! If someone is re-testing right now, use --to to pin the window to the "
                              "earlier full-range run.")
                        print("!" * 68)
                if otdr_rows and bs_remed and bs_remed.get("rows"):
                    fill_from_curve(bs_remed["rows"], bs_remed["order"], otdr_rows,
                                    partners=(leg_partners(rowsA_raw, rowsB_raw) if _npair else None),
                                    nominal=args.splice_nominal)
                if otdr_diag.get("fields"):
                    print(f"  Field names written to {otdr_diag['fields']} (no sample data in it).")
                if otdr_diag.get("dumped"):
                    print(f"  A map of the result record was written to {otdr_diag['dumped']} -")
                    print("  send me that file and I will point the decoder at the right field.")

        if stale:
            settings['_vintage'] = {"used": str(used)[:19], "newest": str(newest[0])[:19],
                                    "type": newest[1], "n": len(stale), "total": len(rowsA_raw)}
        latest_rows = latest_check(rowsA_raw, rowsB_raw if rowsB_raw is not rowsA_raw else None,
                                   wl=args.splice_wl, labelA=labelA, labelB=labelB)
        _improved = [x for x in latest_rows if isinstance(x.get("change"), float) and x["change"] <= -0.05]
        _worse = [x for x in latest_rows if isinstance(x.get("change"), float) and x["change"] >= 0.05]
        if any(x.get("latest_date") for x in latest_rows):
            print(f"Latest Test Check: newest trace compared for {sum(1 for x in latest_rows if x.get('latest_date'))} fibres "
                  f"({len(_improved)} improved, {len(_worse)} worse since the analysed test)")

        cable_id = None
        for _r in rowsA_raw:
            nm = _r.get("fid") or ""
            m = re.match(r'^F-(.+?)-F\d+', nm)
            if m:
                cable_id = m.group(1)
                break
        meta = dict(customer=args.customer or "Motion",
                    cable=args.cable_name or cable_id or f"{rtuA['site']} <-> {rtuB['site']}",
                    partner=args.partner, siteA=labelA, siteB=labelB,
                    ribbons=(args.ribbons or ""),
                    generated=time.strftime("%Y-%m-%d %H:%M"), device=args.test_type,
                    resultset=(args.dfrom or "latest"),
                    fms_user=(getattr(session, "_user", "") or user or ""),
                    schedule=SCHEDULE_STAMP)
        if not args.out:
            args.out = auto_filename(rtuA, rtuB, args.ribbons,
                                     tag=("" if _grp else ("CABLE" if _groups else "")),
                                     single=SINGLE_OVERRIDE is not False and args.mode == 'uni',
                                     cable=(cable_id or args.cable_name or ""))
        if not args.out.lower().endswith(".xlsx"):
            args.out += ".xlsx"
        build_workbook(args.out, meta, settings, rowsA, rowsB,
                       sections=[meta["cable"]], loss_label=loss_label,
                       splices=splices, splice_threshold=args.splice_threshold,
                       splice_wl=args.splice_wl, odf=odf, cable_view=cable_view,
                       all_events=all_events, remedials=remedials, bs_remed=bs_remed,
                       verify=verify,
                       client_verify=client_rows, client_info=client_info,
                       latest=[x for x in (latest_rows or [])
                               if x.get("latest_date") and x.get("latest_date")[:19] != x.get("ev_date")[:19]] or None,
                       otdr_measured=otdr_rows, otdr_diag=otdr_diag,
                       view_wls=tuple(int(x) for x in str(args.view_wl).replace(' ', '').split(',') if x),
                       include_e2e=(not _splice_only
                                    and ("e2e" in R or args.mode != "uni")))
        print(f"\nWorkbook written: {args.out}")
        try:
            import openpyxl as _px
            print("Tabs:", ", ".join(_px.load_workbook(args.out).sheetnames))
        except Exception:
            pass
        print("Open it in Excel; formulas calculate on open.")

        # ---- Debug run log: clear WORKED / ISSUES summary ----
        DBG.section("RUN RESULT")
        n_ok = len(DBG.fibre_ok)
        n_skip = len(DBG.fibre_skip)
        n_fail = len(DBG.fibre_fail)
        total = n_ok + n_skip + n_fail
        DBG.w(f"  WORKED:  {n_ok} of {total} fibre pulls exported with a result")
        if n_skip:
            DBG.w(f"  SKIPPED: {n_skip} had nothing to export (explained below)")
        if n_fail:
            DBG.w(f"  ISSUES:  {n_fail} should have exported but did not (explained below)")
        if not n_skip and not n_fail:
            DBG.w("  No issues - every selected fibre exported a result.")

        if DBG.fibre_fail:
            DBG.w("")
            DBG.w("  NOT EXPORTED - look at these:")
            for end, fibre, reason in DBG.fibre_fail:
                DBG.w(f"    {fibre} ({end}): {reason}")
            # Suggestions keyed to the reasons seen
            reasons_seen = {r for _, _, r in DBG.fibre_fail}
            DBG.w("")
            DBG.w("  WHAT TO CHECK:")
            if any("no test of any kind" in r for r in reasons_seen):
                DBG.w("    - 'no test found': confirm you picked the right cable for this RTU")
                DBG.w("      (an RTU is one end of several cables), and that the fibre has")
                DBG.w("      actually been tested. Run --find-route to see which RTU holds a cable.")
            if any("connection/parse" in r for r in reasons_seen):
                DBG.w("    - 'connection/parse error': usually the token expired mid-run or the")
                DBG.w("      server dropped the connection. Re-run; if it recurs, pull fewer ribbons.")

        if DBG.fibre_skip:
            DBG.w("")
            DBG.w("  SKIPPED - nothing to export, and why:")
            for end, fibre, reason in DBG.fibre_skip:
                DBG.w(f"    {fibre} ({end}): {reason}")
            DBG.w("")
            DBG.w("  These are usually fine: a fibre with only an OTDR test, or one tested")
            DBG.w("  outside your --from / --to dates, has no iOLM result to put in the sheets.")

        if DBG.flags:
            DBG.section("DATA QUALITY NOTES")
            for f in DBG.flags:
                DBG.w(f"  - {f}")
            DBG.w("")
            DBG.w("  Events not matched to a named location still appear in the report, labelled")
            DBG.w("  by distance as unscheduled joints. If a joint you expected is unscheduled,")
            DBG.w("  check its row in the distance schedule (Distances.xlsx), or widen the match")
            DBG.w("  tolerance with --loc-tol (default 120 m).")

        # Header at the very top of the log
        import time as _t
        header = [
            "FMS TEST RESULTS REPORT - RUN LOG",
            f"Tool {TOOL_VERSION} ({TOOL_BUILD})",
            f"Written {_t.strftime('%d/%m/%Y %H:%M:%S')}",
            f"Workbook: {args.out}",
            "",
            "This log lists every fibre the report tried to pull and says plainly",
            "where it worked, where it was skipped, and where there was an issue.",
            "Send this file to Alkis if results look missing.",
        ]
        DBG.lines = header + DBG.lines

        # Save next to the workbook, same stem
        import os as _os
        _stem = _os.path.splitext(args.out)[0]
        _logname = _stem + "_runlog.txt"
        if DBG.save(_logname):
            print("")
            print("=" * 68)
            print(f"  Run log saved: {_os.path.basename(_logname)}")
            print(f"  Location: {_os.path.abspath(_logname)}")
            if DBG.fibre_fail:
                print(f"  {len(DBG.fibre_fail)} fibre(s) did NOT export - open the log and send it to Alkis.")
            elif DBG.fibre_skip:
                print(f"  All results exported; {len(DBG.fibre_skip)} fibre(s) skipped with reasons in the log.")
            else:
                print("  Every selected fibre exported a result. No issues.")
            print("  Please send this log file to Alkis.")
            print("=" * 68)


if __name__ == "__main__":
    try:
        main()
    except FmsUnavailable as ex:
        print("\n" + "=" * 68)
        print(f"Could not reach the FMS server ({ex.what}).")
        print(f"Last error: {ex.cause}")
        print("=" * 68)
        print("Nothing is wrong with your settings. The server closed the connection or")
        print("did not answer, and retrying did not help. Usual causes:")
        print("  - the tenant is briefly down or being restarted")
        print("  - VPN or corporate proxy dropped out")
        print("  - too many requests in a short window, so wait a few minutes")
        print("Check you can load the FMS site in your browser, then run this again.")
        sys.exit(2)
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
