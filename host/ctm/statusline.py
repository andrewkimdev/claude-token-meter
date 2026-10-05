#!/usr/bin/env python3
"""Claude Code statusLine entry (PROTOCOL v1 §1).

Register as:  /usr/bin/python3 -S /abs/path/host/ctm/statusline.py
Self-contained on purpose: stdlib only, no package imports, no network,
Python 3.9 compatible. Never crashes (always exits 0).

- rate_limits present & non-null -> atomically write
  $CTM_HOME/sessions/<session_id>.json (CTM_HOME default ~/.claude-token-meter)
- otherwise write nothing (keep the previous observation).
- prints e.g. "5h 52% · 7d 29%".
"""
import json
import os
import re
import sys
import time

WINDOWS = (("5h", "five_hour"), ("7d", "seven_day"))
_SID_OK = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def base_dir():
    return os.environ.get("CTM_HOME") or os.path.expanduser("~/.claude-token-meter")


def write_session(data, now, home=None):
    """Write the session file. Returns the path written, or None."""
    rl = data.get("rate_limits")
    if not isinstance(rl, dict) or not rl:
        return None
    sid = data.get("session_id")
    if not isinstance(sid, str) or not _SID_OK.match(sid) or sid.startswith("."):
        return None
    d = os.path.join(home or base_dir(), "sessions")
    os.makedirs(d, exist_ok=True)
    rec = {
        "session_id": sid,
        "observed_at": now,
        "cc_version": data.get("version"),
        "rate_limits": rl,
    }
    path = os.path.join(d, sid + ".json")
    tmp = os.path.join(d, ".%s.%d.tmp" % (sid, os.getpid()))
    try:
        with open(tmp, "w") as f:
            json.dump(rec, f, separators=(",", ":"))
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return path


def _fmt_pct(w, now):
    if not isinstance(w, dict):
        return "--"
    v = w.get("used_percentage")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return "--"
    r = w.get("resets_at")
    if isinstance(r, (int, float)) and not isinstance(r, bool) and r <= now:
        return "0%"
    return "%.0f%%" % v


def render(data, now):
    rl = data.get("rate_limits") if isinstance(data, dict) else None
    if not isinstance(rl, dict):
        rl = {}
    return " · ".join("%s %s" % (lbl, _fmt_pct(rl.get(k), now)) for lbl, k in WINDOWS)


def main(stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    now = time.time()
    try:
        data = json.load(stdin)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        stdout.write("ctm: --\n")
        return 0
    try:
        write_session(data, now)
    except Exception:
        pass  # display must never fail because of disk
    try:
        stdout.write(render(data, now) + "\n")
    except Exception:
        stdout.write("ctm: --\n")
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            print("ctm: --")
        except Exception:
            pass
    sys.exit(0)
