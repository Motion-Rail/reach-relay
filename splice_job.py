"""Brunel 1.1: the Splice loss view's pull, run by relay_splice.py in its own process.

Uses the Cable Insight engine (ci_pull.py) with fms_pull's own functions, so the
numbers are the same as the PC tools. Never sees a password: it prints NEED_TOKEN and reads the
signed in user's FMS token from stdin, like report_job.py.

SPLICE_JOB (JSON env): {"mode": "pull"|"rebuild", "section": "RGAC-SNBC-A-R432", "rtuA", "rtuB",
  "ribbons": "" or "7" or "3-6", "testType": "iOLM", "data": "/tmp/rft-splice", "dist": "/path/Distances.xlsx" or "", "learned": [...] (no Distances: Brunel 1.0 learned locations),
  "user": "...", "fmsBase": "https://..."}
Lines out: MARK|end|fibre|state|date, PHASE|text, SETEND|end|label|n, DONE|{json}, ERROR|text.
"""
from __future__ import annotations

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "fms_report")
sys.path.insert(0, ENGINE)

import requests  # noqa: E402

import fms_pull  # noqa: E402
import ci_pull  # noqa: E402
import ci_store  # noqa: E402

CFG = json.loads(os.environ.get("SPLICE_JOB") or "{}")
OUT = sys.__stdout__


def say(line: str):
    OUT.write(line.replace("\n", " ") + "\n")
    OUT.flush()


if CFG.get("fmsBase"):
    b = CFG["fmsBase"].rstrip("/")
    for k in dir(fms_pull):
        v = getattr(fms_pull, k)
        if k.endswith("_URL") and isinstance(v, str) and v.startswith("https://raman.ems.exfo-fms.com"):
            setattr(fms_pull, k, b + v[len("https://raman.ems.exfo-fms.com"):])
    fms_pull.BASE = b


def ask_token() -> str:
    say("NEED_TOKEN")
    tok = sys.stdin.readline().strip()
    if not tok or tok.startswith("ERROR"):
        raise SystemExit("FMS sign in has ended. Sign in to the app again and update again.")
    return tok


class TokenSession(fms_pull.FmsSession):
    """fms_pull's session, signed in with the app user's token instead of a password."""

    def __init__(self, user, password, verbose=False, retry_password=False):   # noqa: D401
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


class LineProgress(ci_pull.Progress):
    """Progress that also reports every fibre to the relay."""

    def set_end(self, end, label, fnos):
        super().set_end(end, label, fnos)
        say(f"SETEND|{end}|{label}|{len(fnos)}")

    def mark(self, end, fno, state, date=None):
        super().mark(end, fno, state, date)
        if state not in ("check",):
            say(f"MARK|{end}|{fno}|{state}|{(date or '')[:16]}")

    def __setattr__(self, k, v):
        object.__setattr__(self, k, v)
        if k == "phase":
            say(f"PHASE|{v}")


def main():
    fms_pull.load_settings = lambda: {"user": CFG.get("user") or "app", "per_binder": 6}
    fms_pull.save_settings = lambda d: None
    fms_pull.cache_load = lambda name, max_age_h=24: None
    fms_pull.cache_save = lambda name, data: None
    ci_store.DIST_OVERRIDE = CFG.get("dist") or None
    ci_store.LEARNED_FOR = lambda cable: CFG.get("learned") or None
    store = ci_store.Store(CFG["data"])
    prog = LineProgress()
    log = lambda line: say("LOG|" + str(line))          # noqa: E731
    try:
        if CFG.get("mode") == "rebuild":
            res = ci_pull.rebuild_from_raw(store, ENGINE, CFG["section"], log=log, progress=prog)
        else:
            form = {"section": CFG["section"], "rtu_a": CFG["rtuA"], "rtu_b": CFG["rtuB"],
                    "ribbons": CFG.get("ribbons") or "", "test_type": CFG.get("testType") or "iOLM"}
            res = ci_pull.run_pull(store, ENGINE, form, "token", CFG.get("user") or "app", prog, log,
                                   session_factory=TokenSession)
        say("DONE|" + json.dumps(res, default=str))
    except SystemExit as e:
        say("ERROR|" + str(e))
        sys.exit(1)
    except RuntimeError as e:
        say("ERROR|" + str(e)[:300])
        sys.exit(1)
    except Exception as e:                  # noqa: BLE001
        say("ERROR|" + f"{type(e).__name__}: {e}"[:300])
        sys.exit(1)


if __name__ == "__main__":
    main()
