"""`ctm install` / `ctm uninstall` (P6): LaunchAgent for the bridge and the
Claude Code statusLine entry.

  ctm install [--python P] [--replace] [--statusline [--force]] [--dry-run]
  ctm uninstall [--statusline] [--dry-run]

LaunchAgent: ~/Library/LaunchAgents/com.ctm.bridge.plist runs
  <python> -u -m ctm.bridge --transport auto   (WorkingDirectory = host/)
with RunAtLoad, KeepAlive, ThrottleInterval 10, stdout/stderr under
<CTM_HOME>/launchd/. Loaded with `launchctl bootstrap gui/<uid>` (bootout
first when already loaded).

A bridge started by hand (hold.pid alive, not the launchd job's pid) blocks
the install unless --replace, which SIGTERMs it right before bootstrap so the
serial port is unheld for as short a time as possible.

statusLine: ~/.claude/settings.json gets
  {"type":"command","command":"/usr/bin/python3 -S <abs>/host/ctm/statusline.py"}
idempotently, after a timestamped backup. A different existing statusLine is
only replaced with --force.

Every path (LaunchAgents dir, settings.json, CTM_HOME), the launchctl runner,
the pid lookup and kill are injectable (Paths / Ops) so tests never touch the
real system. Nothing here reads Anthropic credentials.
"""
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time

try:
    from ctm import bridge as _bridge
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ctm import bridge as _bridge

LABEL = "com.ctm.bridge"
LAUNCHCTL = "/bin/launchctl"
DEFAULT_PYTHON = "~/.platformio/penv/bin/python3"
STATUSLINE_PYTHON = "/usr/bin/python3"
HOST_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TERM_WAIT_S = 5.0


class InstallError(Exception):
    pass


def ctm_home():
    return os.environ.get("CTM_HOME") or os.path.expanduser("~/.claude-token-meter")


class Paths(object):
    def __init__(self, home=None, launch_agents=None, claude_settings=None,
                 host_dir=HOST_DIR, uid=None):
        self.home = home or ctm_home()
        self.launch_agents = launch_agents or os.path.expanduser("~/Library/LaunchAgents")
        self.claude_settings = claude_settings or os.path.expanduser("~/.claude/settings.json")
        self.host_dir = os.path.abspath(host_dir)
        self.uid = os.getuid() if uid is None else uid

    @property
    def plist(self):
        return os.path.join(self.launch_agents, LABEL + ".plist")

    @property
    def launchd_log_dir(self):
        return os.path.join(self.home, "launchd")

    @property
    def hold_pid(self):
        return os.path.join(self.home, "serial", "hold.pid")

    @property
    def statusline_script(self):
        return os.path.join(self.host_dir, "ctm", "statusline.py")

    @property
    def domain(self):
        return "gui/%d" % self.uid

    @property
    def service(self):
        return "%s/%s" % (self.domain, LABEL)


def statusline_command(paths):
    return "%s -S %s" % (STATUSLINE_PYTHON, paths.statusline_script)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == 1  # EPERM: alive, not ours
    return True


class Ops(object):
    """Side effects, injectable. `run` mimics subprocess.run."""

    def __init__(self, run=subprocess.run, kill=os.kill, alive=_pid_alive,
                 sleep=time.sleep, clock=time.time):
        self.run = run
        self.kill = kill
        self.alive = alive
        self.sleep = sleep
        self.clock = clock

    def launchctl(self, *args):
        r = self.run([LAUNCHCTL] + list(args), stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        out = r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else (r.stdout or "")
        err = r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else (r.stderr or "")
        return r.returncode, out, err

    def job(self, paths):
        """(loaded, pid or None) of the launchd job, via `launchctl print` (read-only)."""
        rc, out, _err = self.launchctl("print", paths.service)
        if rc != 0:
            return False, None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("pid = "):
                try:
                    return True, int(line[6:].strip())
                except ValueError:
                    pass
        return True, None

    def has_pyserial(self, python):
        try:
            r = self.run([python, "-c", "import serial"], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        except OSError:
            return False
        return r.returncode == 0


def plist_dict(paths, python):
    logd = paths.launchd_log_dir
    d = {
        "Label": LABEL,
        "ProgramArguments": [python, "-u", "-m", "ctm.bridge", "--transport", "auto"],
        "WorkingDirectory": paths.host_dir,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": os.path.join(logd, "bridge.out.log"),
        "StandardErrorPath": os.path.join(logd, "bridge.err.log"),
    }
    default_home = os.path.expanduser("~/.claude-token-meter")
    if os.path.abspath(paths.home) != default_home:
        d["EnvironmentVariables"] = {"CTM_HOME": os.path.abspath(paths.home)}
    return d


def _atomic_write(path, data):
    path = os.path.realpath(path)   # keep a symlinked settings.json a symlink
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.ctm-tmp" % os.path.basename(path))
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


# ---- statusLine -----------------------------------------------------------

def read_settings(path):
    """(dict, raw bytes or None). Raises InstallError on unreadable / bad JSON."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}, None
    except OSError as e:
        raise InstallError("cannot read %s: %s" % (path, e))
    try:
        d = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except ValueError as e:
        raise InstallError("%s is not valid JSON (%s); fix it first" % (path, e))
    if not isinstance(d, dict):
        raise InstallError("%s is not a JSON object" % path)
    return d, raw


def statusline_plan(paths, force=False):
    """('ok' | 'set' | 'replace', existing_command or None). Raises InstallError
    when a different statusLine exists and not force."""
    d, _raw = read_settings(paths.claude_settings)
    want = statusline_command(paths)
    sl = d.get("statusLine")
    cur = sl.get("command") if isinstance(sl, dict) else None
    if sl is None:
        return "set", None
    if isinstance(sl, dict) and sl.get("type") == "command" and cur == want:
        return "ok", cur
    if not force:
        raise InstallError("%s already has a different statusLine:\n  %s\n"
                           "Not changing it. Re-run with --force to replace it (a backup is kept)."
                           % (paths.claude_settings, json.dumps(sl) if cur is None else cur))
    return "replace", cur if cur is not None else json.dumps(sl)


def _backup(path, clock):
    if not os.path.exists(path):
        return None
    b = "%s.bak-ctm-%s" % (path, time.strftime("%Y%m%d-%H%M%S", time.localtime(clock())))
    n = 1
    while os.path.exists(b):
        n += 1
        b = "%s.bak-ctm-%s-%d" % (path, time.strftime("%Y%m%d-%H%M%S", time.localtime(clock())), n)
    shutil.copy2(os.path.realpath(path), b)
    return b


def apply_statusline(paths, action, ops, out, dry_run=False):
    want = statusline_command(paths)
    if action == "ok":
        out.write("statusLine: already set (%s)\n" % want)
        return
    if dry_run:
        out.write("[dry-run] would %s statusLine in %s (with backup) ->\n  %s\n"
                  % (action, paths.claude_settings, want))
        return
    d, _raw = read_settings(paths.claude_settings)
    b = _backup(paths.claude_settings, ops.clock)
    d["statusLine"] = {"type": "command", "command": want}
    _atomic_write(paths.claude_settings, (json.dumps(d, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    out.write("statusLine: %s -> %s\n" % (action, want))
    if b:
        out.write("  backup: %s\n" % b)


def remove_statusline(paths, ops, out, dry_run=False):
    d, _raw = read_settings(paths.claude_settings)
    sl = d.get("statusLine")
    if not (isinstance(sl, dict) and sl.get("command") == statusline_command(paths)):
        out.write("statusLine: not ours, left unchanged\n")
        return
    if dry_run:
        out.write("[dry-run] would remove statusLine from %s (with backup)\n" % paths.claude_settings)
        return
    b = _backup(paths.claude_settings, ops.clock)
    del d["statusLine"]
    _atomic_write(paths.claude_settings, (json.dumps(d, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    out.write("statusLine: removed (backup %s)\n" % b)


# ---- LaunchAgent ----------------------------------------------------------

def manual_bridge_pid(paths, ops):
    """pid of a live bridge recorded in hold.pid that is NOT the launchd job."""
    holder = _bridge.other_holder_alive(paths.hold_pid)
    if not holder:
        return None, None
    _loaded, job_pid = ops.job(paths)
    if job_pid == holder:
        return None, holder
    return holder, job_pid


def _wait_exit(pid, ops, timeout=TERM_WAIT_S):
    end = ops.clock() + timeout
    while ops.alive(pid):
        if ops.clock() >= end:
            return False
        ops.sleep(0.1)
    return True


def install(paths, ops, out=sys.stdout, python=None, replace=False, statusline=False,
            force=False, dry_run=False):
    python = os.path.abspath(os.path.expanduser(python or DEFAULT_PYTHON))
    # -- preflight: everything that can refuse runs before any change --
    if not (os.path.isfile(python) and os.access(python, os.X_OK)):
        raise InstallError("python not found or not executable: %s (use --python)" % python)
    if not ops.has_pyserial(python):
        raise InstallError("%s cannot import serial (pyserial); the bridge needs it for USB. "
                           "Use --python with an interpreter that has pyserial." % python)
    sl_action = statusline_plan(paths, force) if statusline else None
    manual, _ = manual_bridge_pid(paths, ops)
    if manual and not replace:
        raise InstallError("a bridge started by hand is running (pid %d, hold.pid). "
                           "Stop it, or re-run with --replace to stop it right before the "
                           "LaunchAgent starts." % manual)
    loaded, job_pid = ops.job(paths)
    plist = plistlib.dumps(plist_dict(paths, python))
    pre = "[dry-run] " if dry_run else ""

    if sl_action:
        apply_statusline(paths, sl_action[0], ops, out, dry_run)

    out.write("%swrite %s\n" % (pre, paths.plist))
    if dry_run:
        out.write(plist.decode("utf-8"))
    else:
        os.makedirs(paths.launchd_log_dir, exist_ok=True)
        _atomic_write(paths.plist, plist)
    if loaded:
        out.write("%slaunchctl bootout %s  (pid %s)\n" % (pre, paths.service, job_pid or "-"))
        if not dry_run:
            rc, _o, err = ops.launchctl("bootout", paths.service)
            if rc != 0:
                raise InstallError("launchctl bootout failed (exit %d): %s" % (rc, err.strip()))
            if job_pid and not _wait_exit(job_pid, ops):
                out.write("warning: old launchd bridge pid %d still alive after %ds\n"
                          % (job_pid, TERM_WAIT_S))
    if manual:
        out.write("%skill -TERM %d  (bridge started by hand)\n" % (pre, manual))
        if not dry_run:
            try:
                ops.kill(manual, signal.SIGTERM)
            except OSError as e:
                raise InstallError("cannot stop pid %d: %s" % (manual, e))
            if not _wait_exit(manual, ops):
                out.write("warning: pid %d did not exit within %ds; launchd will retry the "
                          "bridge every 10s until the port is free\n" % (manual, TERM_WAIT_S))
    out.write("%slaunchctl bootstrap %s %s\n" % (pre, paths.domain, paths.plist))
    if not dry_run:
        rc, _o, err = ops.launchctl("bootstrap", paths.domain, paths.plist)
        if rc != 0:
            raise InstallError("launchctl bootstrap failed (exit %d): %s" % (rc, err.strip()))
        out.write("installed %s (logs: %s). Check with: ctm doctor\n"
                  % (LABEL, paths.launchd_log_dir))
    return 0


def uninstall(paths, ops, out=sys.stdout, statusline=False, dry_run=False):
    pre = "[dry-run] " if dry_run else ""
    loaded, job_pid = ops.job(paths)
    if loaded:
        out.write("%slaunchctl bootout %s  (pid %s)\n" % (pre, paths.service, job_pid or "-"))
        if not dry_run:
            rc, _o, err = ops.launchctl("bootout", paths.service)
            if rc != 0:
                raise InstallError("launchctl bootout failed (exit %d): %s" % (rc, err.strip()))
    else:
        out.write("LaunchAgent %s not loaded\n" % LABEL)
    if os.path.exists(paths.plist):
        out.write("%sremove %s\n" % (pre, paths.plist))
        if not dry_run:
            os.unlink(paths.plist)
    if statusline:
        remove_statusline(paths, ops, out, dry_run)
    out.write("%sdone (logs, sessions, device.json and the Keychain item are kept; "
              "`ctm unpair` forgets the device)\n" % pre)
    return 0
