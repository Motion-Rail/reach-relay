"""Brunel.3: runs the FMS Test Results Report (fms_report/fms_pull.py, the same engine as the PC tool) for the relay.

Started by relay_fullreport.py as a separate process so a big report never holds the relay's memory, and so a crash
cannot take the relay down. It never sees a password: whenever it needs an FMS token it prints "NEED_TOKEN" and reads
the signed in user's current token from stdin (the relay refreshes it as usual).

Arguments come as one JSON object in the REPORT_JOB environment variable:
  {"rtuA": "RTU2-RGAC2-1991133", "rtuB": "RTU2-SNBC-1991132" or "", "ribbons": "1-2", "testType": "auto",
   "from": "", "to": "", "nominal": 0.02, "viewWl": "1550", "locations": "/path/Distances.xlsx" or "",
   "verify": "/path/client.xlsx" or "", "user": "alkis@...", "out": "/tmp/dir", "fmsBase": "https://..."}
Progress lines on stdout start with "PROG|", "STAGE|" or "DONE|".
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "fms_report"))

import requests  # noqa: E402

import fms_pull  # noqa: E402

CFG = json.loads(os.environ.get("REPORT_JOB") or "{}")
OUT = sys.__stdout__


def say(line: str):
    OUT.write(line.replace("\n", " ") + "\n")
    OUT.flush()


# FMS host (the mock in tests); every URL in the puller derives from BASE
if CFG.get("fmsBase"):
    b = CFG["fmsBase"].rstrip("/")
    fms_pull.BASE = b
    fms_pull.TOKEN_URL = f"{b}/auth/realms/Fiber/protocol/openid-connect/token"
    fms_pull.GRAPHQL_URL = f"{b}/topology/graphql/graphql"
    fms_pull.RESULTS_URL = f"{b}/api/measure/v1/results/"
    for k in dir(fms_pull):
        v = getattr(fms_pull, k)
        if k.endswith("_URL") and isinstance(v, str) and v.startswith("https://raman.ems.exfo-fms.com"):
            setattr(fms_pull, k, b + v[len("https://raman.ems.exfo-fms.com"):])


def ask_token() -> str:
    say("NEED_TOKEN")
    tok = sys.stdin.readline().strip()
    if not tok or tok.startswith("ERROR"):
        raise SystemExit("FMS sign in has ended. Sign in to the app again and run the report again.")
    return tok


class TokenSession(fms_pull.FmsSession):
    """The puller's session, but signed in with the app user's token instead of a password."""

    def __init__(self, user, password, verbose=True, retry_password=False):   # noqa: D401
        self._user, self._password, self.verbose = user, None, verbose
        self._retry_password, self._attempts = False, 0
        self.refresh_token = None
        self.s = requests.Session()
        self.access_token = ask_token()
        self._expiry = time.time() + 60

    def _login(self):
        self.access_token = ask_token()
        self._expiry = time.time() + 60

    def _refresh(self):
        self._login()


fms_pull.FmsSession = TokenSession
fms_pull.load_settings = lambda: {"user": CFG.get("user") or "app", "per_binder": 6}
fms_pull.save_settings = lambda d: None
fms_pull.cache_load = lambda name, max_age_h=24: None
fms_pull.cache_save = lambda name, data: None
os.environ["FMS_USER"] = CFG.get("user") or "app"
os.environ["FMS_PASS"] = "not-used"

# progress: every fibre the puller logs as exported, skipped or failed
_DBG = fms_pull.DBG
_end_total: dict[str, int] = {}
_end_done: dict[str, int] = {}
_w0, _ok0, _skip0, _fail0 = _DBG.w, _DBG.ok, _DBG.skip, _DBG.fail


def _w(text=""):
    t = str(text)
    if t.startswith("PULLING "):
        try:
            end = t.split()[1]
            n = int(t.split("(")[1].split()[0])
            _end_total[end] = n
            _end_done[end] = 0
            say(f"STAGE|pull|{end}|{n}")
        except Exception:                   # noqa: BLE001
            pass
    return _w0(text)


def _tick(end):
    _end_done[end] = _end_done.get(end, 0) + 1
    say(f"PROG|{end}|{_end_done[end]}|{_end_total.get(end, 0)}")


def _ok(end, fibre, detail=""):
    _tick(end)
    return _ok0(end, fibre, detail)


def _skip(end, fibre, reason):
    _tick(end)
    return _skip0(end, fibre, reason)


def _fail(end, fibre, reason):
    _tick(end)
    return _fail0(end, fibre, reason)


_DBG.w, _DBG.ok, _DBG.skip, _DBG.fail = _w, _ok, _skip, _fail


def _wizard(args):
    """What the PC wizard asks, answered from the app's settings."""
    two = bool(CFG.get("rtuB"))
    args.mode = "bidir" if two else "uni"
    args.reports = {"e2e", "bsplice", "odf", "remed"} if two else {"e2e", "usplice", "odf"}
    args.test_type = CFG.get("testType") or "auto"
    args.ribbons = CFG.get("ribbons") or ""
    args.splice_nominal = float(CFG.get("nominal", 0.02))
    args.customer = "Motion"
    return args


fms_pull.wizard = _wizard


def main():
    out = CFG["out"]
    os.makedirs(out, exist_ok=True)
    os.chdir(out)
    argv = ["fms_pull", "--wizard", "-y", "--rtu-a", CFG["rtuA"], "--view-wl", str(CFG.get("viewWl") or "1550")]
    if CFG.get("rtuB"):
        argv += ["--rtu-b", CFG["rtuB"]]
    if CFG.get("from"):
        argv += ["--from", CFG["from"]]
    if CFG.get("to"):
        argv += ["--to", CFG["to"]]
    if CFG.get("locations"):
        argv += ["--locations", CFG["locations"]]
    if CFG.get("verify"):
        argv += ["--verify", CFG["verify"]]
    sys.argv = argv
    say("STAGE|start|" + json.dumps({k: CFG.get(k) for k in ("rtuA", "rtuB", "ribbons", "testType")}))
    fms_pull.main()
    files = sorted(f for f in os.listdir(out) if f.lower().endswith(".xlsx") and not f.startswith("~"))
    say("DONE|" + json.dumps({"files": files}))


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            say("ERROR|" + str(e.code)[:600])
            sys.exit(1)
    except Exception as e:                  # noqa: BLE001
        import traceback
        traceback.print_exc()
        say("ERROR|" + f"{type(e).__name__}: {e}"[:600])
        sys.exit(1)
