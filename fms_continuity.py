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

    def wait(self, wid: str, poll: float = 1.0, limit: float = 90, posted: float | None = None) -> "Outcome":
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
