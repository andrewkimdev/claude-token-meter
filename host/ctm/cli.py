"""ctm CLI.

  python -m ctm.cli status
  python -m ctm.cli setup [--dry-run] [--port-serial N]   # interactive WiFi pairing (USB)
  python -m ctm.cli unpair [--local] [--dry-run] [--port-serial N]
  python -m ctm.cli info [--wifi] [--port-serial N]
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
    from ctm import device as _device
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ctm import merge as _merge
    from ctm import device as _device


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


def _usb_present(find_port):
    try:
        if find_port is None:
            from ctm import bridge as _bridge
            find_port = _bridge.find_port
        dev = find_port()
    except ImportError:
        return "unknown (pyserial not installed for this python)"
    except Exception as e:
        return "unknown (%s)" % e
    return dev or "not present"


def _transport_from_state(state):
    """hold.state is '<date> <time> <STATE ...>'."""
    parts = state.split(" ", 2)
    s = parts[2] if len(parts) == 3 else state
    for prefix, name in (("HELD", "usb"), ("WIFI", "wifi"), ("PAUSED", "paused"),
                         ("WAITING", "none (waiting)"), ("STOPPED", "none (bridge stopped)")):
        if s.startswith(prefix):
            return name
    return "none"


def cmd_status(home, out=sys.stdout, keystore=None, find_port=None):
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
    state = ""
    try:
        with open(sp) as f:
            state = f.read()
        out.write("hold.state: %s" % state)
    except OSError:
        out.write("hold.state: (none)\n")
    out.write("transport: %s\n" % _transport_from_state(state.strip()))
    out.write("usb device: %s\n" % _usb_present(find_port))
    rec = _device.load(home)
    if not rec:
        out.write("paired: no (run ctm setup)\n")
        return 0
    try:
        if keystore is None:
            from ctm import keystore as _ks
            keystore = _ks.default()
        has_key = "yes" if keystore.get(rec["host"]) else "NO (rerun ctm setup)"
    except Exception as e:
        has_key = "unknown (%s)" % e
    paired_at = rec.get("paired_at")
    out.write("paired: %s.local  ip %s  since %s  key in Keychain: %s\n" % (
        rec["host"], rec.get("ip") or "-",
        time.strftime("%Y-%m-%d %H:%M", time.localtime(paired_at)) if paired_at else "?",
        has_key))
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


def run_pairing_cmd(args, home, env=None):
    from ctm import pairing as _pairing
    env = env or _pairing.Env(home=home, serial_no=getattr(args, "port_serial", None))
    try:
        if args.cmd == "setup":
            return _pairing.run_setup(env, dry_run=args.dry_run, ssid=args.ssid)
        if args.cmd == "unpair":
            return _pairing.run_unpair(env, local_only=args.local, dry_run=args.dry_run)
        return _pairing.run_info(env, force_wifi=args.wifi)
    except _pairing.SetupError as e:
        env.console.say("ctm %s: %s" % (args.cmd, e))
        return 2
    except KeyboardInterrupt:
        env.console.say("\nInterrupted.")
        return 130


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ctm")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status", help="print merged state, hold.state, transport, pairing")
    st = sub.add_parser("setup", help="interactive WiFi provisioning over USB")
    st.add_argument("--dry-run", action="store_true",
                    help="print what would be sent (secrets masked); open nothing")
    st.add_argument("--ssid", help="(dry-run only) SSID to show in the provision line")
    up = sub.add_parser("unpair", help="clear WiFi + key on the device (USB) and on this Mac")
    up.add_argument("--local", action="store_true",
                    help="only forget the pairing on this Mac (no USB)")
    up.add_argument("--dry-run", action="store_true")
    inf = sub.add_parser("info", help="device info (USB if present, else GET /v1/info)")
    inf.add_argument("--wifi", action="store_true", help="use WiFi even if USB is present")
    for p in (st, up, inf):
        p.add_argument("--port-serial", metavar="N",
                       help="USB serial number of the target (default CTM_USB_SERIAL / 5915018996)")
    pt = sub.add_parser("push-test", help="write a fake session file (delete with --clear)")
    pt.add_argument("--pct5", type=_pct)
    pt.add_argument("--pct7", type=_pct)
    pt.add_argument("--clear", action="store_true", help="delete all test-*.json session files")
    args = ap.parse_args(argv)
    home = ctm_home()
    if args.cmd == "status":
        return cmd_status(home)
    if args.cmd in ("setup", "unpair", "info"):
        from ctm import pairing as _pairing
        _pairing.install_exit_signals()   # SIGTERM/SIGHUP -> finally -> bridge resumed
        return run_pairing_cmd(args, home)
    if args.cmd == "push-test":
        if args.clear:
            return cmd_clear(home)
        return cmd_push_test(home, args.pct5, args.pct7)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
