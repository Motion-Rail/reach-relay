"""
fms_continuity.py  -  shared FMS calls for continuity (tone one end, OTDR the other).

Used by continuity_poc.py (runs on the PC) and relay_continuity.py (runs on the relay).
Only needs `requests`.

What this is built on (captured from the FMS Tasks history, 28 Sep 2026):
  * An AdHoc OTDR is a Conductor workflow `postBulkTests_On_Multiple_RTUs_ORs_Dynamic`
    whose input carries one `subworkflowInputs` entry per route:
        {RtuId, OpticalRouteId, OpticalRouteName, AdhocTestType:"OTDR",
         Payload:{validInput:true, payLoad:{spliceLossThreshold, reflectanceThreshold,
                  endOfFiberThreshold, wavelength:"0.00000155", duration, pulse,
                  autoSettings, range}}}
  * Each route's outcome lands in workflow.output as
        {"Rtu_<rtu>_OR_<route>": {"result": {status, completionStatus, message, ...},
                                  "subWorkflowId": ...}}
    A failed test reads status FAILED, message ADHOC_CALL_FAILED. The reason sits in
    the sub workflow's tasks, which is what `failure_detail()` digs out.
  * Measured pace on RGAC2/SNBC (PoC 28 Sep 2026, 5 s OTDR): a live fibre is
    refused in ~10 s; a dark fibre runs the full OTDR in ~35 s (first of the
    session 53 s). History: duration + ~14 s per OTDR, serial per RTU.

Starting: `start_otdr()` posts to the Conductor start call through the FMS gateway
(POST /workflow/server/api/workflow/<name>). Proven working 28 Sep 2026.

v24 (1 Oct 2026): EXFO asked us to stop creating a Task per OTDR. The default is now the
ad hoc call the FMS UI uses for "Test On Demand > Start Test" (captured 1 Oct 2026):
    POST /api/topology/control/remotetestunits/{rtu}/command/opticalroutes/{route}/otdr
    {"spliceLossThreshold":0.02,"reflectanceThreshold":-72,"endOfFiberThreshold":4,
     "wavelength":"0.00000155","duration":N,"autoSettings":true}
returns a promise id (GUID) and creates no Task. The outcome comes back two ways:
  * pushed over STOMP (SockJS websocket /api/topology/ws/connection, access_token query),
    topic /topic/monitoredassets/{route}/testsetups/adhoc/message/{promise}, body
    {promiseId, isError, body, lastTestResultId, testTime, ...}. A live fibre refusal is
    pushed with isError and the "Live fiber detected." text; it is not stored as a result.
  * a stored result in /api/measure/v1/results (metadata.PromiseId = promise,
    brief.LinkResults.Length in metres, metadata.TestTime UTC).
The relay listens on the websocket and also polls the results, whichever answers first.
OTDR_MODE=workflow switches back to the old Conductor path.
"""
from __future__ import annotations

import base64
import os
import json
import re
import time
from dataclasses import dataclass, field

import requests

HOST = os.environ.get("FMS_HOST", "https://raman.ems.exfo-fms.com").rstrip("/")
TOKEN_URL = HOST + "/auth/realms/Fiber/protocol/openid-connect/token"
GQL_URL = HOST + "/topology/graphql/graphql"
WF_BASE = HOST + "/workflow/server/api"
WF_NAME = "postBulkTests_On_Multiple_RTUs_ORs_Dynamic"
SUB_WF_NAME = "postAdhocTest_On_Input_OR"
CLIENT_ID = "fg-topologyui"
OTDR_MODE = os.environ.get("OTDR_MODE", "adhoc").strip().lower()
ADHOC_URL = HOST + "/api/topology/control/remotetestunits/{rtu}/command/opticalroutes/{route}/otdr"
RESULTS_URL = HOST + "/api/measure/v1/results/"
# v37 (captured 7 Oct 2026, FMS "Launch Test On Demand" iOLM on SGIC, twice): one iOLM with no Task.
#   GET  /api/topology/testconfigurations/{setupId}   (the test setup, its payLoad is the full iOLM setup)
#   POST /api/topology/control/remotetestunits/{rtu}/command/opticalroutes/{route}/iolm
#        {"name":"iOLM test parameters","payload":"<the setup payLoad as text, MeasurementType set,
#         OtdrParameters cut to the chosen wavelengths, WavelengthsUsed []>"}
#   reply: a promise id (GUID), the same as the ad hoc OTDR; the result is stored with metadata.PromiseId.
IOLM_URL = HOST + "/api/topology/control/remotetestunits/{rtu}/command/opticalroutes/{route}/iolm"
TESTCONFIG_URL = HOST + "/api/topology/testconfigurations/{id}"
WS_URL = re.sub(r"^http", "ws", HOST) + "/api/topology/ws/connection"
ADHOC_TOPIC = "/topic/monitoredassets/{route}/testsetups/adhoc/message/{promise}"

# The RTU's own refusal when light is already on the fibre. Proven on RGAC2/SNBC
# 28 Sep 2026 (continuity_poc_20260928_174646.json): the sub workflow returns
#   messageKey "112_LiveFiberDetected", message "Live fiber detected.",
#   ExceptionTypeName Metrino.Otdr.Instrument.LiveFiberDetectedException
# Match only that. Any other failure is "failed" and gets retried, never a PASS.
LIVE_PATTERNS = re.compile(r"112_LiveFiberDetected|LiveFiberDetectedException|Live fiber detected")

GQL_ROUTES = """
query searchOpticalRouteByRtu($search: String, $condition: OpticalRouteSearchOutputCondition,
  $orderBy: OpticalRouteSearchOutputsOrderBy!, $first: Int!, $offset: Int!) {
  routeSearchResult: searchOpticalRouteByRtu(matchType: CONTAINS, search: $search,
    condition: $condition, orderBy: $orderBy, first: $first, offset: $offset) {
    totalCount
    nodes { id name rtuId rtuName portLabel description diagramName }
  }
}"""


class StartRefused(Exception):
    pass


def _jwt_claims(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


class Fms:
    """Keycloak session. The password is used once for the grant and not kept."""

    def __init__(self, username: str, password: str, timeout: int = 60):
        self.s = requests.Session()
        self.timeout = timeout
        self.username = username
        self._grant({"grant_type": "password", "username": username, "password": password})

    # ---- auth ----
    def _grant(self, form: dict):
        r = self.s.post(TOKEN_URL, data={"client_id": CLIENT_ID, **form}, timeout=self.timeout)
        r.raise_for_status()
        j = r.json()
        self.token = j["access_token"]
        self.refresh = j.get("refresh_token")
        self.expires = time.time() + int(j.get("expires_in", 900)) - 60
        self.claims = _jwt_claims(self.token)

    @classmethod
    def from_token_provider(cls, get_token, username: str = "", claims: dict | None = None,
                            timeout: int = 60) -> "Fms":
        """For the relay: it already holds the Keycloak token, so pass a function
        returning the current access token instead of logging in again."""
        self = cls.__new__(cls)
        self.s, self.timeout, self.username = requests.Session(), timeout, username
        self._provider = get_token
        tok = get_token()
        self.token, self.refresh, self.expires = tok, None, float("inf")
        self.claims = claims or _jwt_claims(tok)
        return self

    def _auth(self) -> dict:
        if getattr(self, "_provider", None):
            self.token = self._provider()
            return {"Authorization": "Bearer " + self.token}
        if time.time() > self.expires and self.refresh:
            self._grant({"grant_type": "refresh_token", "refresh_token": self.refresh})
        return {"Authorization": "Bearer " + self.token}

    def _force_new_token(self):
        """v17: a 401 mid run means the token ran out. Ask the provider for a fresh one."""
        f = getattr(self, "_force", None)
        if f:
            f()

    def get(self, url, **kw):
        r = self.s.get(url, headers=self._auth(), timeout=self.timeout, **kw)
        if r.status_code == 401 and getattr(self, "_provider", None):
            self._force_new_token()
            r = self.s.get(url, headers=self._auth(), timeout=self.timeout, **kw)
        return r

    def post(self, url, **kw):
        extra = kw.pop("headers", {})
        r = self.s.post(url, headers={**self._auth(), **extra}, timeout=self.timeout, **kw)
        if r.status_code == 401 and getattr(self, "_provider", None):
            self._force_new_token()
            r = self.s.post(url, headers={**self._auth(), **extra}, timeout=self.timeout, **kw)
        return r

    # ---- topology ----
    def routes_for_rtu(self, rtu_name: str) -> list[dict]:
        """Every route on one RTU, exact RTU name match (the search itself is CONTAINS)."""
        out, offset = [], 0
        while True:
            body = {"operationName": "searchOpticalRouteByRtu", "query": GQL_ROUTES,
                    "variables": {"search": rtu_name, "condition": {}, "orderBy": "NAME_ASC",
                                  "offset": offset, "first": 50}}
            r = self.post(GQL_URL, json=body)
            r.raise_for_status()
            res = r.json()["data"]["routeSearchResult"]
            nodes = res["nodes"]
            out += [n for n in nodes if (n.get("rtuName") or "") == rtu_name]
            offset += len(nodes)
            if not nodes or offset >= res["totalCount"]:
                return out

    # ---- tests ----
    def user_fields(self) -> dict:
        c = self.claims
        first, last = c.get("given_name", ""), c.get("family_name", "")
        email = c.get("email") or c.get("preferred_username") or self.username
        return {
            "creatorName": (first + " " + last).strip() or email,
            "UserName": email,
            "UserScope": "diagrams",
            "UserRoles": json.dumps((c.get("realm_access") or {}).get("roles", [])),
            "loggedInUserEmail": email,
            "loggedInUserName": email,
        }

    def start_otdr(self, rtu_id: int, route_id: int, route_name: str, *,
                   duration: int = 5, range_m: int = 80000, comment: str = "continuity",
                   dry_run: bool = False) -> str | dict:
        if OTDR_MODE != "workflow":
            pid = self._start_adhoc(rtu_id, route_id, duration=duration, dry_run=dry_run)
            if pid is not None:
                return pid
            # No push channel: a live fibre refusal would never be seen, so use the old
            # workflow call for this one test rather than risk a false reading.
            self.ws_fallbacks = getattr(self, "ws_fallbacks", 0) + 1
        wf_input = build_otdr_input(rtu_id, route_id, route_name, duration=duration,
                                    range_m=range_m, comment=comment, user=self.user_fields())
        if dry_run:
            safe = dict(wf_input)
            safe["UserRoles"] = "[... your roles ...]"
            return safe
        tries = [
            (WF_BASE + "/workflow/" + WF_NAME, wf_input),
            (WF_BASE + "/workflow", {"name": WF_NAME, "version": 1, "input": wf_input}),
        ]
        last = None
        for url, body in tries:
            r = self.post(url, json=body)
            if r.status_code == 409:
                raise StartRefused("409 test already scheduled on this RTU: " + r.text[:200])
            if r.ok:
                wid = r.text.strip().strip('"')
                if re.fullmatch(r"[0-9a-f-]{36}", wid):
                    return wid
                try:
                    return r.json().get("workflowId") or wid
                except Exception:
                    return wid
            last = r
        raise StartRefused(f"{last.status_code} {last.text[:300]}")

    def workflow(self, wid: str, tasks: bool = False) -> dict:
        r = self.get(f"{WF_BASE}/workflow/{wid}", params={"includeTasks": str(tasks).lower()})
        r.raise_for_status()
        return r.json()

    def acquisition_start(self, wf: dict, sub_id: str | None, posted: float | None) -> tuple[float | None, list]:
        """v20: when did the RTU actually start the OTDR, on the relay's clock?
        The live fibre check happens at the start of acquisition, so this, not the time the
        result came back, says whether the tone was still on. FMS (Conductor) stamps tasks in
        epoch ms on its own clock; the parent workflow's startTime against the moment we posted
        it gives the clock offset. The acquisition is taken as the longest task in the sub workflow.
        Returns (local epoch seconds or None, a short task timeline for the logs)."""
        try:
            if not sub_id or not posted or not wf.get("startTime"):
                return None, []
            skew = wf["startTime"] / 1000.0 - posted
            sub = self.workflow(sub_id, tasks=True)
            tl, best = [], None
            for t in sub.get("tasks", []):
                st, en = t.get("startTime") or 0, t.get("endTime") or 0
                if not st:
                    continue
                dur = (en - st) / 1000.0 if en else 0.0
                tl.append({"ref": str(t.get("referenceTaskName") or t.get("taskType") or "")[:40],
                           "type": str(t.get("taskType") or "")[:24],
                           "startS": round(st / 1000.0 - skew - posted, 1), "durS": round(dur, 1)})
                if best is None or dur > best[1]:
                    best = (st / 1000.0 - skew, dur)
            return (best[0] if best else None), tl[:12]
        except Exception:                                   # noqa: BLE001
            return None, []

    # ---- v24: ad hoc OTDR (no Task) ----
    def _start_adhoc(self, rtu_id, route_id, *, duration: int, dry_run: bool = False):
        body = adhoc_payload(duration)
        url = ADHOC_URL.format(rtu=int(rtu_id), route=int(route_id))
        if dry_run:
            return {"url": url, "body": body}
        watch = StompWatch.open(self)            # listen before posting so a fast refusal is not missed
        if watch is None and OTDR_MODE != "adhoc-poll":
            return None
        try:
            r = self.post(url, json=body)
        except Exception:
            if watch:
                watch.close()
            raise
        if r.status_code == 409 or "AlreadyScheduled" in r.text:
            if watch:
                watch.close()
            raise StartRefused("409 test already scheduled on this RTU: " + r.text[:200])
        if not r.ok:
            if watch:
                watch.close()
            raise StartRefused(f"{r.status_code} {r.text[:300]}")
        pid = r.text.strip().strip('"')
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", pid):
            if watch:
                watch.close()
            raise StartRefused("unexpected start reply: " + r.text[:200])
        if watch:
            watch.subscribe(ADHOC_TOPIC.format(route=int(route_id), promise=pid))
        if not hasattr(self, "_adhoc"):
            self._adhoc = {}
        self._adhoc[pid] = {"route": int(route_id), "watch": watch, "duration": int(duration)}
        return pid

    def adhoc_results(self, route_id: int, top: int = 5, kind: str = "OTDR") -> list[dict]:
        params = {"$filter": f"metadata/AssetId eq {int(route_id)} and metadata/TestCategory eq 'Adhoc' "
                             f"and metadata/TestType eq '{kind}'",
                  "$orderby": "metadata/TestTime desc", "$top": str(top), "$skip": "0",
                  "$select": "resultid,brief/LinkResults,metadata"}
        r = self.get(RESULTS_URL, params=params)
        r.raise_for_status()
        j = r.json()
        return j.get("results", j if isinstance(j, list) else [])

    def _adhoc_from_result(self, res: dict, secs: float, pid: str) -> "Outcome":
        md = res.get("metadata") or {}
        link = ((res.get("brief") or {}).get("LinkResults") or {})
        if md.get("HasError"):
            reason = " | ".join(_strings(res, keys=("message", "error", "errorMessage", "ErrorMessage",
                                                     "reason", "detail", "messageKey", "Error"))) or "error result"
            return Outcome("clash" if LIVE_PATTERNS.search(reason) else "failed", reason[:1500], secs, pid, raw=md)
        acq = _utc_epoch(md.get("TestTime"))
        return Outcome("clean", "OTDR completed " + str(link.get("Length", "")) + " m", secs, pid,
                       raw={"linkLength": link.get("Length"), "completion": link.get("CompletionStatus"),
                            "resultId": md.get("ResultId"), "testTime": md.get("TestTime"), "acqStart": acq,
                            "timeline": [{"ref": "fms testTime", "type": "adhoc", "startS": None, "durS": None}]})

    def _wait_adhoc(self, pid: str, limit: float, poll_s: float = 3.0) -> "Outcome":
        info = self._adhoc.pop(pid)
        route, watch = info["route"], info["watch"]
        t0, next_poll = time.time(), time.time() + 2.0
        try:
            while True:
                if watch:
                    msg = watch.next_message(timeout=1.0)
                    if msg is not None and str(msg.get("promiseId") or msg.get("messageId")) == pid:
                        secs = time.time() - t0
                        if msg.get("isError") or msg.get("error"):
                            reason = json.dumps(msg.get("body")) if not isinstance(msg.get("body"), str) else msg["body"]
                            reason = (reason or "") + " " + " ".join(_strings(msg, keys=("message", "messageKey",
                                                                                          "error", "errorMessage")))
                            reason = reason.strip() or "ad hoc OTDR error"
                            return Outcome("clash" if LIVE_PATTERNS.search(reason) else "failed",
                                           reason[:1500], secs, pid, raw=msg)
                        # completed: read the stored result for the length
                        for _ in range(8):
                            for res in self.adhoc_results(route):
                                if (res.get("metadata") or {}).get("PromiseId") == pid:
                                    return self._adhoc_from_result(res, time.time() - t0, pid)
                            time.sleep(1.0)
                        return Outcome("clean", "OTDR completed (length not read)", secs, pid,
                                       raw={"acqStart": _utc_epoch(msg.get("testTime")), "timeline": []})
                else:
                    time.sleep(1.0)
                if time.time() >= next_poll:
                    next_poll = time.time() + poll_s
                    try:
                        for res in self.adhoc_results(route):
                            if (res.get("metadata") or {}).get("PromiseId") == pid:
                                return self._adhoc_from_result(res, time.time() - t0, pid)
                    except requests.RequestException:
                        pass
                if time.time() - t0 > limit:
                    return Outcome("timeout", "no ad hoc result after " + str(round(limit)) + " s"
                                   + ("" if watch else " (no websocket)"), time.time() - t0, pid)
        finally:
            if watch:
                watch.close()

    def wait(self, wid: str, poll: float = 1.0, limit: float = 90, posted: float | None = None) -> "Outcome":
        if wid in getattr(self, "_adhoc", {}):
            return self._wait_adhoc(wid, limit=limit)
        t0 = time.time()
        while True:
            wf = self.workflow(wid)
            if wf.get("status") in ("COMPLETED", "FAILED", "TERMINATED", "TIMED_OUT"):
                break
            if time.time() - t0 > limit:
                return Outcome("timeout", "workflow still " + str(wf.get("status")), time.time() - t0, wid)
            time.sleep(poll)
        secs = time.time() - t0
        entries = parse_output(wf.get("output"))
        if not entries:
            return Outcome("unknown", "no output; workflow " + str(wf.get("status")), secs, wid)
        res = entries[0]
        r = res.get("result") or {}
        if r.get("status") == "COMPLETED":
            acq, tl = self.acquisition_start(wf, res.get("subWorkflowId"), posted)
            return Outcome("clean", "OTDR completed " + str(r.get("linkLength", "")) + " m",
                           secs, wid, raw={**r, "acqStart": acq, "timeline": tl})
        reason = self.failure_detail(res.get("subWorkflowId")) or r.get("message") or "failed"
        verdict = "clash" if LIVE_PATTERNS.search(reason) else "failed"
        return Outcome(verdict, reason, secs, wid, raw=r)

    def failure_detail(self, sub_id: str | None) -> str:
        """Every reason / message string the sub workflow's tasks carry, joined."""
        if not sub_id:
            return ""
        try:
            wf = self.workflow(sub_id, tasks=True)
        except Exception as e:
            return f"(sub workflow unreadable: {e})"
        bits = []
        if wf.get("reasonForIncompletion"):
            bits.append(wf["reasonForIncompletion"])
        for t in wf.get("tasks", []):
            if t.get("reasonForIncompletion"):
                bits.append(f"{t.get('referenceTaskName')}: {t['reasonForIncompletion']}")
            bits += _strings(t.get("outputData"), keys=("message", "error", "errorMessage",
                                                           "reason", "detail", "title", "body",
                                                           "response", "code"))
        seen, out = set(), []
        for b in bits:
            b = str(b).strip()
            if b and b not in seen:
                seen.add(b)
                out.append(b)
        return " | ".join(out)[:1500]


@dataclass
class Outcome:
    verdict: str          # clash | clean | failed | timeout | unknown
    detail: str
    seconds: float
    workflow_id: str = ""
    raw: dict = field(default_factory=dict)


def iolm_body(cfg: dict, mode: str, wavelengths_nm: list[int]) -> dict:
    """What the FMS screen posts for an ad hoc iOLM: the stored test setup, with the acquisition mode set and
    OtdrParameters cut to the chosen wavelengths (WavelengthsUsed stays empty, as FMS sends it)."""
    raw = cfg.get("payLoad", cfg.get("payload"))
    p = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    if not p:
        raise RuntimeError("That FMS test setup has no iOLM settings.")
    want = {round(w * 1e-9, 12) for w in wavelengths_nm}
    ops = [o for o in (p.get("OtdrParameters") or []) if round(float(o.get("Wavelength") or 0), 12) in want]
    if not ops:
        raise RuntimeError("That FMS test setup has none of the chosen wavelengths.")
    p["OtdrParameters"] = ops
    p["MeasurementType"] = mode
    p["WavelengthsUsed"] = []
    return {"name": "iOLM test parameters", "payload": json.dumps(p, separators=(",", ":"))}


def adhoc_payload(duration: int) -> dict:
    """Exactly what the FMS UI sends for Test On Demand (1 Oct 2026), with our duration."""
    return {"spliceLossThreshold": 0.02, "reflectanceThreshold": -72, "endOfFiberThreshold": 4,
            "wavelength": "0.00000155", "duration": int(duration), "autoSettings": True}


def _utc_epoch(ts) -> float | None:
    """FMS TestTime, e.g. 2026-10-01T14:21:57.3693100Z (UTC, 7 decimal places)."""
    if not ts:
        return None
    try:
        from datetime import datetime, timezone
        m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?", str(ts))
        base = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        return base + (float(m.group(2)) if m.group(2) else 0.0)
    except Exception:
        return None


class StompWatch:
    """Minimal STOMP 1.2 over a SockJS websocket, the way the FMS UI listens for ad hoc
    results. Best effort: if it cannot connect, the caller falls back to polling results."""

    def __init__(self, ws):
        self.ws, self.buf, self.n = ws, [], 0

    @classmethod
    def open(cls, fms: "Fms") -> "StompWatch | None":
        if os.environ.get("ADHOC_WS", "1") == "0":
            return None
        try:
            import random
            import string
            from urllib.parse import quote
            from websockets.sync.client import connect
            tok = fms._auth()["Authorization"].split(" ", 1)[1]
            sess = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8))
            url = f"{WS_URL}/{random.randint(0, 999):03d}/{sess}/websocket?access_token={quote(tok)}"
            ws = connect(url, open_timeout=10, close_timeout=2, max_size=2 ** 22)
            self = cls(ws)
            if ws.recv(timeout=10) != "o":
                raise RuntimeError("no SockJS open frame")
            self._send("CONNECT\naccept-version:1.2,1.1,1.0\nheart-beat:0,0\n\n\x00")
            deadline = time.time() + 10
            while time.time() < deadline:
                for f in self._frames(timeout=deadline - time.time()):
                    if f.startswith("CONNECTED"):
                        return self
                    if f.startswith("ERROR"):
                        raise RuntimeError(f[:200])
            raise RuntimeError("STOMP not connected")
        except Exception:                                     # noqa: BLE001
            try:
                ws.close()                                    # type: ignore[name-defined]
            except Exception:
                pass
            return None

    def _send(self, frame: str):
        self.ws.send(json.dumps([frame]))

    def _frames(self, timeout: float) -> list[str]:
        try:
            raw = self.ws.recv(timeout=max(0.05, timeout))
        except TimeoutError:
            return []
        if raw.startswith("a"):
            try:
                return [str(x) for x in json.loads(raw[1:])]
            except Exception:
                return []
        if raw.startswith("c"):
            raise ConnectionError("SockJS closed " + raw[:120])
        return []                                             # "h" heartbeat, "o" open

    def subscribe(self, dest: str):
        self.n += 1
        self._send(f"SUBSCRIBE\nid:sub-{self.n}\ndestination:{dest}\nack:auto\n\n\x00")

    def next_message(self, timeout: float = 1.0) -> dict | None:
        """The next MESSAGE body as a dict, or None if nothing arrived in time."""
        if not self.buf:
            try:
                self.buf += [f for f in self._frames(timeout) if f.startswith("MESSAGE")]
            except Exception:                                 # noqa: BLE001
                return None
        while self.buf:
            f = self.buf.pop(0)
            body = f.split("\n\n", 1)[1] if "\n\n" in f else ""
            body = body.rstrip("\x00")
            try:
                return json.loads(body)
            except Exception:
                continue
        return None

    def close(self):
        try:
            self._send("DISCONNECT\n\n\x00")
        except Exception:
            pass
        try:
            self.ws.close()
        except Exception:
            pass


def build_otdr_input(rtu_id, route_id, route_name, *, duration, range_m, comment, user) -> dict:
    ref = f"Rtu_{rtu_id}_OR_{route_id}"
    return {
        "subworkflows": [{"subWorkflowParam": {"name": SUB_WF_NAME},
                          "type": "SUB_WORKFLOW", "taskReferenceName": ref}],
        "type": "BULK_TEST.OPTICAL_TEST",
        "subType": "BULK_TEST.ADHOC_TEST_OTDR",
        "totalOrsCount": 1,
        "comment": comment,
        **user,
        "subworkflowInputs": {ref: {
            "RtuId": int(rtu_id), "OpticalRouteId": int(route_id),
            "OpticalRouteName": route_name, "AdhocTestType": "OTDR",
            "Payload": {"validInput": True, "payLoad": {
                "spliceLossThreshold": 0.02, "reflectanceThreshold": -72,
                "endOfFiberThreshold": 4, "wavelength": "0.00000155",
                "duration": int(duration), "pulse": "5e-8",
                "autoSettings": False, "range": int(range_m)}}}},
    }


def parse_output(out) -> list[dict]:
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except Exception:
            return []
    items = []
    for v in (out or {}).values():
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                continue
        if isinstance(v, dict) and "result" in v:
            items.append(v)
    return items


def _strings(obj, keys, depth=0) -> list[str]:
    if depth > 6 or obj is None:
        return []
    if isinstance(obj, str):
        try:
            return _strings(json.loads(obj), keys, depth + 1)
        except Exception:
            return []
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, (str, int)) and str(v).strip():
                out.append(f"{k}={v}")
            elif isinstance(v, (dict, list, str)):
                out += _strings(v, keys, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            out += _strings(v, keys, depth + 1)
    return out


def fibre_name(stem: str, n: int) -> str:
    return f"{stem}-F{n:03d}"
