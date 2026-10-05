"""ctm CLI.

  python -m ctm.cli status
  python -m ctm.cli push-test --pct5 52 --pct7 29   # fake session file
  python -m ctm.cli push-test --clear               # delete fake session files

push-test writes <CTM_HOME>/sessions/test-<ts>.json with resets_at in the
future so a running bridge picks it up. Because of the merge rules (latest
resets_at wins) a test file usually *overrides* real sessions until it is
removed: always run `push-test --clear` afterwards.
"""
import argparse
import json
import os
import sys
import time

try:
    from ctm import merge as _merge
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ctm import merge as _merge


def ctm_home():
    return os.environ.get("CTM_HOME") or os.path.expanduser("~/.claude-token-meter")


def _rel(sec):
    sec = int(sec)
    sign = "in " if sec >= 0 else ""
    sec = abs(sec)
    h, m = divmod(sec // 60, 60)
    d, h = divmod(h, 24)
    s = ("%dd%02dh" % (d, h)) if d else ("%dh%02dm" % (h, m))
    return sign + s + ("" if sign else " ago")


def cmd_status(home, out=sys.stdout):
    now = time.time()
    sessions = _merge.load_sessions(home)
    windows = _merge.merge(sessions, now)
    out.write("home: %s  (%d session files)\n" % (home, len(sessions)))
    for k in _merge.WINDOW_KEYS:
        w = windows.get(k)
        if not w:
            out.write("  %-9s --\n" % k)
            continue
        out.write("  %-9s %s%%  resets %s  observed %s\n" % (
            k, w["pct"], _rel(w["resets_at"] - now), _rel(w["observed_at"] - now)))
    sp = os.path.join(home, "serial", "hold.state")
    try:
        with open(sp) as f:
            out.write("hold.state: %s" % f.read())
    except OSError:
        out.write("hold.state: (none)\n")
    return 0


def cmd_push_test(home, pct5, pct7, out=sys.stdout):
    now = time.time()
    ts = int(now)
    rl = {}
    if pct5 is not None:
        rl["five_hour"] = {"used_percentage": pct5, "resets_at": ts + 5 * 3600}
    if pct7 is not None:
        rl["seven_day"] = {"used_percentage": pct7, "resets_at": ts + 7 * 86400}
    if not rl:
        out.write("nothing to push (give --pct5 and/or --pct7)\n")
        return 1
    sid = "test-%d" % ts
    d = _merge.sessions_dir(home)
    os.makedirs(d, exist_ok=True)
    rec = {"session_id": sid, "observed_at": now, "cc_version": "push-test", "rate_limits": rl}
    path = os.path.join(d, sid + ".json")
    tmp = os.path.join(d, ".%s.tmp" % sid)
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, path)
    out.write("wrote %s\nremove with: python -m ctm.cli push-test --clear\n" % path)
    return 0


def cmd_clear(home, out=sys.stdout):
    d = _merge.sessions_dir(home)
    n = 0
    try:
        names = os.listdir(d)
    except OSError:
        names = []
    for name in names:
        if name.startswith("test-") and name.endswith(".json"):
            os.unlink(os.path.join(d, name))
            n += 1
    out.write("removed %d test session file(s)\n" % n)
    return 0


def _pct(s):
    v = float(s)
    if not 0 <= v <= 100:
        raise argparse.ArgumentTypeError("pct must be 0..100")
    return int(v) if v == int(v) else v


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ctm")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status", help="print merged state and hold.state")
    pt = sub.add_parser("push-test", help="write a fake session file (delete with --clear)")
    pt.add_argument("--pct5", type=_pct)
    pt.add_argument("--pct7", type=_pct)
    pt.add_argument("--clear", action="store_true", help="delete all test-*.json session files")
    args = ap.parse_args(argv)
    home = ctm_home()
    if args.cmd == "status":
        return cmd_status(home)
    if args.cmd == "push-test":
        if args.clear:
            return cmd_clear(home)
        return cmd_push_test(home, args.pct5, args.pct7)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
