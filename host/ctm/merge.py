"""PROTOCOL v1 §2: merge per-session observations into one view per window.

Pure functions plus small file helpers. stdlib only, Python 3.9 compatible.
"""
import json
import os
import time

WINDOW_KEYS = ("five_hour", "seven_day")
PRUNE_AGE_S = 8 * 24 * 3600


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def merge(sessions, now):
    """sessions: list of session dicts (PROTOCOL §1). Returns
    {"five_hour": {"pct", "resets_at", "observed_at"}, ...}; windows with no
    valid, unexpired observation are omitted. Order-independent, idempotent."""
    out = {}
    for key in WINDOW_KEYS:
        best_reset = None
        best_pct = None
        best_obs = None
        for s in sessions:
            if not isinstance(s, dict):
                continue
            rl = s.get("rate_limits")
            if not isinstance(rl, dict):
                continue
            w = rl.get(key)
            if not isinstance(w, dict):
                continue
            pct = w.get("used_percentage")
            reset = w.get("resets_at")
            obs = s.get("observed_at")
            if not (_num(pct) and _num(reset)):
                continue
            if reset <= now:  # rule 1: window already reset
                continue
            obs = obs if _num(obs) else 0
            if best_reset is None or reset > best_reset:  # rule 2
                best_reset, best_pct, best_obs = reset, pct, obs
            elif reset == best_reset:  # rule 3/4
                best_pct = max(best_pct, pct)
                best_obs = max(best_obs, obs)
        if best_reset is not None:  # rule 5
            out[key] = {
                "pct": best_pct,
                "resets_at": int(best_reset),
                "observed_at": int(best_obs),
            }
    return out


def sessions_dir(home):
    return os.path.join(home, "sessions")


def _is_session_file(name):
    return name.endswith(".json") and not name.startswith(".")


def load_sessions(home):
    """Read all session files; unreadable/corrupt ones are skipped."""
    d = sessions_dir(home)
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in sorted(names):
        if not _is_session_file(n):
            continue
        try:
            with open(os.path.join(d, n)) as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return out


def snapshot_mtimes(home):
    """{name: (mtime_ns, size)} for change detection."""
    d = sessions_dir(home)
    out = {}
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        if not _is_session_file(n):
            continue
        try:
            st = os.stat(os.path.join(d, n))
        except OSError:
            continue
        out[n] = (st.st_mtime_ns, st.st_size)
    return out


def prune(home, now=None, max_age=PRUNE_AGE_S):
    """Delete session files (and stale tmp files) whose mtime is older than
    max_age. Returns list of removed names."""
    now = time.time() if now is None else now
    d = sessions_dir(home)
    removed = []
    try:
        names = os.listdir(d)
    except OSError:
        return removed
    for n in names:
        if not (_is_session_file(n) or n.endswith(".tmp")):
            continue
        p = os.path.join(d, n)
        try:
            if now - os.stat(p).st_mtime > max_age:
                os.unlink(p)
                removed.append(n)
        except OSError:
            continue
    return removed
