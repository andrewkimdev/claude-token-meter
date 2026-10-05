"""`ctm doctor`: one-shot health report (P5/P6).

Each check prints PASS / FAIL / SKIP with a reason. Fail-closed (CLAUDE.md
rule 6): an overall verdict is printed only when every check actually ran;
a SKIP (could not evaluate, or the check itself crashed) means "no verdict".

Exit status: 0 all PASS, 1 at least one FAIL (and none skipped), 2 no verdict.

Read-only: ~/.claude/settings.json is only read, `launchctl print` only
reads, the pairing key is only tested for presence and never printed, the
device is asked GET /v1/info (unsigned, no secrets; IPv4 only via AF_INET).
"""
import glob
import os
import re
import sys
import time

try:
    from ctm import bridge as _bridge
    from ctm import device as _device
    from ctm import install as _install
    from ctm import merge as _merge
    from ctm import wifi as _wifi
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ctm import bridge as _bridge
    from ctm import device as _device
    from ctm import install as _install
    from ctm import merge as _merge
    from ctm import wifi as _wifi

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
ACK_MAX_AGE_S = 5 * 60      # heartbeat is 120s: an ack should be < ~4 min old
SESSION_STALE_S = 24 * 3600
ACK_RE = re.compile(r'"t":"ack"|<< wifi ack')
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")


class Env(object):
    """Injectable dependencies. Defaults are the real (read-only) ones."""

    def __init__(self, paths=None, ops=None, keystore=None, find_port=None,
                 wifi_factory=None, clock=time.time):
        self.paths = paths or _install.Paths()
        self.ops = ops or _install.Ops()
        self._keystore = keystore
        self.find_port = find_port
        self.wifi_factory = wifi_factory or (lambda host, ip: _wifi.WifiClient(host, b"", ip))
        self.clock = clock

    @property
    def home(self):
        return self.paths.home

    def keystore(self):
        if self._keystore is None:
            from ctm import keystore as _ks
            self._keystore = _ks.default()
        return self._keystore


def _read(p):
    try:
        with open(p) as f:
            return f.read()
    except OSError:
        return None


def _age(sec):
    sec = int(max(0, sec))
    if sec < 120:
        return "%ds ago" % sec
    if sec < 7200:
        return "%dm ago" % (sec // 60)
    if sec < 172800:
        return "%dh ago" % (sec // 3600)
    return "%dd ago" % (sec // 86400)


# ---- checks: each returns (status, reason) --------------------------------

def check_statusline(env):
    p = env.paths
    if not os.path.exists(p.claude_settings):
        return FAIL, "%s not found (ctm install --statusline)" % p.claude_settings
    d, _raw = _install.read_settings(p.claude_settings)
    sl = d.get("statusLine")
    cmd = sl.get("command") if isinstance(sl, dict) else None
    if not cmd:
        return FAIL, "no statusLine command registered (ctm install --statusline)"
    if p.statusline_script not in cmd:
        return FAIL, "statusLine points elsewhere: %s" % cmd
    if not os.path.isfile(p.statusline_script):
        return FAIL, "statusLine script missing: %s" % p.statusline_script
    return PASS, cmd


def check_sessions(env):
    now = env.clock()
    files = glob.glob(os.path.join(_merge.sessions_dir(env.home), "*.json"))
    if not files:
        return FAIL, "no session files in %s (open Claude Code; the statusLine writes them)" \
            % _merge.sessions_dir(env.home)
    newest = max(os.path.getmtime(f) for f in files)
    w = _merge.merge(_merge.load_sessions(env.home), now)
    if not w:
        return FAIL, "%d session file(s), newest %s, but no current window (all expired?)" \
            % (len(files), _age(now - newest))
    parts = ["%s %s%%" % (k, w[k]["pct"]) for k in _merge.WINDOW_KEYS if k in w]
    note = "  (newest is old: no Claude Code activity?)" if now - newest > SESSION_STALE_S else ""
    tests = [os.path.basename(f) for f in files if os.path.basename(f).startswith("test-")]
    if tests:
        note += "  (push-test files present: ctm push-test --clear)"
    return PASS, "%d file(s), newest %s; merged: %s%s" % (
        len(files), _age(now - newest), ", ".join(parts), note)


def check_bridge(env):
    pid = _bridge.other_holder_alive(env.paths.hold_pid)
    if not pid:
        return FAIL, "no live bridge (hold.pid missing or stale)"
    st = (_read(os.path.join(env.home, "serial", "hold.state")) or "").strip()
    return PASS, "pid %d; hold.state: %s" % (pid, st or "(none)")


def check_launchagent(env):
    p = env.paths
    if not os.path.exists(p.plist):
        return FAIL, "%s not installed (ctm install)" % p.plist
    loaded, job_pid = env.ops.job(p)
    if not loaded:
        return FAIL, "plist present but %s not loaded (ctm install)" % p.service
    holder = _bridge.other_holder_alive(p.hold_pid)
    if job_pid is None:
        return FAIL, "%s loaded but not running (see %s)" % (p.service, p.launchd_log_dir)
    if holder and holder != job_pid:
        return FAIL, ("loaded (pid %d) but hold.pid is %d: a hand-started bridge owns the port "
                      "(ctm install --replace)" % (job_pid, holder))
    return PASS, "loaded, pid %d" % job_pid


def check_transport(env):
    st = (_read(os.path.join(env.home, "serial", "hold.state")) or "").strip()
    if not st:
        return FAIL, "no hold.state (bridge never ran?)"
    parts = st.split(" ", 2)
    s = parts[2] if len(parts) == 3 else st
    if s.startswith("HELD"):
        return PASS, "usb (%s)" % s
    if s.startswith("WIFI"):
        if "backoff" in s:
            return FAIL, "wifi failing: %s" % s
        return PASS, "wifi (%s)" % s
    return FAIL, s


def _usb_presence(env):
    """(present bool or None if unknown, detail)."""
    try:
        fp = env.find_port
        if fp is None:
            fp = _bridge.find_port
        dev = fp()
    except ImportError:
        return None, "pyserial not installed for this python"
    except Exception as e:
        return None, "port scan failed: %s" % e
    return bool(dev), (dev or "not plugged in")


def check_device(env):
    usb, usb_d = _usb_presence(env)
    rec = _device.load(env.home)
    wifi_ok, wifi_d = None, "not paired"
    if rec:
        c = env.wifi_factory(rec["host"], rec.get("ip") or None)
        info, ip, err = c.get_info()
        if info is not None:
            wifi_ok = True
            wifi_d = "%s.local %s fw=%s rssi=%s uptime=%ss" % (
                rec["host"], ip, info.get("fw"), info.get("rssi"), info.get("uptime_s"))
        else:
            wifi_ok = False
            wifi_d = "%s.local: %s" % (rec["host"], err)
    detail = "usb: %s; wifi: %s" % (usb_d, wifi_d)
    if usb or wifi_ok:
        return PASS, detail
    if usb is None and wifi_ok is None:
        return SKIP, detail
    return FAIL, detail


def check_pairing_key(env):
    rec = _device.load(env.home)
    if not rec:
        return FAIL, "not paired: no device.json (run ctm setup in a real terminal)"
    try:
        has = env.keystore().get(rec["host"])
    except Exception as e:
        return SKIP, "Keychain lookup failed: %s" % e
    if not has:
        return FAIL, "no Keychain item ctm-pairing/%s (rerun ctm setup)" % rec["host"]
    return PASS, "present for %s (not shown)" % rec["host"]


def last_ack(serial_dir):
    """(epoch, line) of the newest ack in the daily logs, or (None, None)."""
    for p in sorted(glob.glob(os.path.join(serial_dir, "[0-9]" * 8 + ".log")), reverse=True):
        try:
            with open(p, "rb") as f:
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if ACK_RE.search(line):
                m = TS_RE.match(line)
                if not m:
                    continue
                t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                return t, line
    return None, None


def check_last_ack(env):
    t, _line = last_ack(os.path.join(env.home, "serial"))
    if t is None:
        return FAIL, "no ack in the bridge logs"
    age = env.clock() - t
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    if age > ACK_MAX_AGE_S:
        return FAIL, "last ack %s (%s; expected every <=120s)" % (when, _age(age))
    return PASS, "last ack %s (%s)" % (when, _age(age))


CHECKS = (
    ("statusline", check_statusline),
    ("sessions", check_sessions),
    ("bridge", check_bridge),
    ("launchagent", check_launchagent),
    ("transport", check_transport),
    ("device", check_device),
    ("pairing key", check_pairing_key),
    ("last ack", check_last_ack),
)


def run(env, out=sys.stdout, checks=CHECKS):
    results = []
    for name, fn in checks:
        try:
            st, why = fn(env)
        except Exception as e:  # a crashed check did not run: no verdict
            st, why = SKIP, "check error: %s: %s" % (type(e).__name__, e)
        results.append((name, st, why))
        out.write("%s  %-12s %s\n" % (st, name, why))
    skipped = [n for n, s, _ in results if s == SKIP]
    failed = [n for n, s, _ in results if s == FAIL]
    if skipped:
        out.write("verdict: NONE (not all checks ran: %s)\n" % ", ".join(skipped))
        return 2
    if failed:
        out.write("verdict: FAIL (%s)\n" % ", ".join(failed))
        return 1
    out.write("verdict: OK\n")
    return 0
