"""`ctm setup` / `ctm unpair` / `ctm info` (PROTOCOL v1 §4.2-§4.3).

Interactive, run by the user in a terminal:

  cd host && ~/.platformio/penv/bin/python3 -m ctm.cli setup

Flow of setup:
  1. If a bridge (or tools/serial_hold.py) holds the port (hold.pid alive),
     send SIGUSR1, wait for hold.state "PAUSED" (5s). It is ALWAYS resumed at
     the end (finally; also on Ctrl-C / SIGTERM / SIGHUP).
  2. Find the target by USB serial number, open it with DTR/RTS low +
     TIOCEXCL (ctm.bridge.open_port). Opening may reset the board: wait for
     @{"t":"hello"} (<= 6s) or go on after the timeout.
  3. {"t":"scan"} -> numbered list -> pick (or type a hidden SSID) ->
     getpass for the password (skipped for open networks).
  4. key = secrets.token_bytes(32); {"t":"provision",...}; wait <= 25s.
     ok -> Keychain (ctm-pairing / host) + device.json.

Secrets: the WiFi password and the key are never printed, logged or put in
an exception message. The provision line is written straight to the port
(the bridge is paused, so it never reaches the bridge log). --dry-run
prints what would be sent with the secrets masked and touches no port.
"""
import json
import os
import re
import secrets
import signal
import sys
import time

try:
    from ctm import bridge as _bridge
    from ctm import device as _device
    from ctm import keystore as _keystore
    from ctm import proto as _proto
    from ctm import wifi as _wifi
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ctm import bridge as _bridge
    from ctm import device as _device
    from ctm import keystore as _keystore
    from ctm import proto as _proto
    from ctm import wifi as _wifi

PAUSE_TIMEOUT_S = 5.0
HELLO_TIMEOUT_S = 6.0
DRAIN_S = 0.3
SCAN_TIMEOUT_S = 15.0
PROVISION_TIMEOUT_S = 25.0
CMD_TIMEOUT_S = 5.0
MASK = "********"
KEY_MASK = "<64 hex chars, hidden>"

FAIL_HINTS = {
    "no_ap": ["The network was not found. The ESP32 only sees 2.4 GHz networks.",
              "Check the SSID spelling (case-sensitive) and move the device closer to the AP."],
    "auth": ["Authentication failed: the password is probably wrong.",
             "Run `ctm setup` again and retype it."],
    "timeout": ["The AP was found but no connection / IP within 15s.",
                "Check DHCP, MAC filtering, captive portals; WPA-Enterprise is not supported."],
    "nvs": ["The device could not save the settings (NVS). Retry; if it persists, reflash."],
    "bad_args": ["The device rejected the arguments (SSID 1-32 bytes, password <= 64 chars)."],
}


class SetupError(Exception):
    pass


# ---- bridge pause / resume ------------------------------------------------

def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


class BridgePauser(object):
    """Context manager: pause the process named in hold.pid with SIGUSR1 and
    always toggle it back on exit (exception, Ctrl-C included)."""

    def __init__(self, home, kill=os.kill, holder=None, clock=time.monotonic,
                 sleep=time.sleep, timeout=PAUSE_TIMEOUT_S, out=sys.stdout):
        self.serial_dir = os.path.join(home, "serial")
        self.pid_path = os.path.join(self.serial_dir, "hold.pid")
        self.state_path = os.path.join(self.serial_dir, "hold.state")
        self.kill = kill
        self.holder = holder or (lambda: _bridge.other_holder_alive(self.pid_path))
        self.clock = clock
        self.sleep = sleep
        self.timeout = timeout
        self.out = out
        self.signalled = None

    def paused(self):
        return "PAUSED" in _read(self.state_path)

    def pause(self):
        pid = self.holder()
        if not pid:
            return
        if self.paused():
            self.out.write("Port holder pid %d is already paused; leaving it as is.\n" % pid)
            return
        self.out.write("Pausing the bridge (pid %d) ...\n" % pid)
        self.kill(pid, signal.SIGUSR1)
        self.signalled = pid
        end = self.clock() + self.timeout
        while self.clock() < end:
            if self.paused():
                return
            self.sleep(0.1)
        raise SetupError("the bridge (pid %d) did not report PAUSED within %ds"
                         % (pid, self.timeout))

    def resume(self):
        pid, self.signalled = self.signalled, None
        if pid is None:
            return
        try:
            self.kill(pid, signal.SIGUSR1)
            self.out.write("Resumed the bridge (pid %d).\n" % pid)
        except OSError as e:
            self.out.write("Could not resume the bridge pid %d: %s\n" % (pid, e))

    def __enter__(self):
        try:
            self.pause()
        except BaseException:
            self.resume()
            raise
        return self

    def __exit__(self, *exc):
        self.resume()
        return False


class _NoPause(object):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---- device line protocol over the port -----------------------------------

class DeviceLink(object):
    """Line I/O with the device. Only '@{...}' lines are interpreted
    (proto.parse_device_line, '@{' anywhere in the line). Device log text is
    discarded, never printed."""

    def __init__(self, port, clock=time.monotonic):
        self.port = port
        self.clock = clock
        self.buf = b""
        self.pending = []

    def _pump(self):
        chunk = self.port.read(512)
        if not chunk:
            return
        self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            msg = _proto.parse_device_line(line.rstrip(b"\r").decode("utf-8", "replace"))
            if msg is not None:
                self.pending.append(msg)
        if len(self.buf) > _bridge.MAX_RX_BUF:
            self.buf = b""

    def wait_for(self, kinds, timeout):
        """First message whose t is in kinds, or None after timeout."""
        end = self.clock() + timeout
        while True:
            while self.pending:
                m = self.pending.pop(0)
                if m.get("t") in kinds:
                    return m
            if self.clock() >= end:
                return None
            self._pump()

    def drain(self, dur=DRAIN_S):
        end = self.clock() + dur
        while self.clock() < end:
            self._pump()
        self.pending = []
        try:
            self.port.reset_input_buffer()
        except Exception:
            pass

    def send(self, obj):
        data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(data) + 1 > _proto.MAX_LINE:
            raise SetupError("command exceeds %d bytes" % _proto.MAX_LINE)
        # leading "\n" ends any partial line in the device RX buffer (P3)
        self.port.write(b"\n" + data + b"\n")
        try:
            self.port.flush()
        except Exception:
            pass


# ---- presentation helpers --------------------------------------------------

def rssi_bars(rssi):
    try:
        r = int(rssi)
    except (TypeError, ValueError):
        return "    "
    n = 4 if r >= -55 else 3 if r >= -67 else 2 if r >= -75 else 1
    return "▂▄▆█"[:n] + " " * (4 - n)


def format_nets(nets):
    lines = []
    w = max([len(n.get("ssid", "")) for n in nets] + [4])
    for i, n in enumerate(nets, 1):
        lock = "  " if n.get("open") else "\U0001F512"
        lines.append("  %2d) %-*s  %s %4s dBm  %s" % (
            i, w, n.get("ssid", ""), rssi_bars(n.get("rssi")), n.get("rssi", "?"), lock))
    return "\n".join(lines)


def masked(obj):
    """Copy of a command with secrets masked, for display."""
    d = dict(obj)
    if "pass" in d:
        d["pass"] = MASK if d["pass"] else ""
    if "key" in d:
        d["key"] = KEY_MASK
    return json.dumps(d, ensure_ascii=False)


class Console(object):
    def __init__(self, input_fn=input, getpass_fn=None, out=sys.stdout):
        if getpass_fn is None:
            import getpass
            getpass_fn = getpass.getpass
        self.input = input_fn
        self.getpass = getpass_fn
        self.out = out

    def say(self, s=""):
        self.out.write(s + "\n")
        try:
            self.out.flush()
        except Exception:
            pass


# ---- environment (injectable for tests) ------------------------------------

class Env(object):
    def __init__(self, home=None, keystore=None, find_port=None, open_port=None,
                 pauser=None, console=None, clock=time.monotonic, wifi_factory=None,
                 serial_no=None, token=secrets.token_bytes):
        self.home = home or _device.ctm_home()
        self.keystore = keystore
        self.serial_no = serial_no or _bridge.SERIAL_NO
        self.find_port = find_port or (lambda: _bridge.find_port(self.serial_no))
        self.open_port = open_port or _bridge.open_port
        self.console = console or Console()
        self.pauser = pauser or (lambda: BridgePauser(self.home, out=self.console.out))
        self.clock = clock
        self.wifi_factory = wifi_factory or (lambda host, ip: _wifi.WifiClient(host, b"", ip))
        self.token = token

    def ks(self):
        if self.keystore is None:
            self.keystore = _keystore.default()
        return self.keystore


def _find(env):
    try:
        return env.find_port()
    except ImportError:
        raise SetupError("pyserial is not installed for this python; use "
                         "~/.platformio/penv/bin/python3")


class _Session(object):
    """Pause the bridge, open the port, wait for hello. Closes the port
    before the bridge is resumed."""

    def __init__(self, env, dev):
        self.env = env
        self.dev = dev
        self.pauser = None
        self.port = None

    def __enter__(self):
        c = self.env.console
        self.pauser = self.env.pauser()
        self.pauser.__enter__()
        try:
            try:
                self.port = self.env.open_port(self.dev)
            except Exception as e:
                raise SetupError("cannot open %s: %s (is another program using the port?)"
                                 % (self.dev, e))
            link = DeviceLink(self.port, self.env.clock)
            c.say("Opened %s; waiting for the device (it may reboot) ..." % self.dev)
            hello = link.wait_for(("hello",), HELLO_TIMEOUT_S)
            if hello:
                c.say("Device fw %s (%s)." % (hello.get("fw", "?"), hello.get("board", "?")))
            link.drain()
            return link
        except BaseException:
            self._close()
            raise

    def _close(self):
        try:
            if self.port is not None:
                self.port.close()
        except Exception:
            pass
        self.port = None
        self.pauser.__exit__(None, None, None)

    def __exit__(self, *exc):
        self._close()
        return False


# ---- setup -----------------------------------------------------------------

def _choose_network(link, c):
    """Returns (ssid, is_open) or None if the user quits."""
    while True:
        c.say("Scanning for WiFi networks ...")
        link.send({"t": "scan"})
        m = link.wait_for(("scan",), SCAN_TIMEOUT_S)
        if m is None:
            raise SetupError("no scan reply from the device (firmware without P4 support?)")
        nets = [n for n in (m.get("nets") or [])
                if isinstance(n, dict) and isinstance(n.get("ssid"), str) and n["ssid"]]
        if nets:
            c.say(format_nets(nets))
        else:
            c.say("  (no networks found)")
        while True:
            ans = c.input("Select [1-%d], m = type SSID (hidden network), r = rescan, q = quit: "
                          % len(nets)).strip().lower()
            if ans == "q":
                return None
            if ans == "r":
                break
            if ans == "m":
                ssid = c.input("SSID: ").strip("\r\n")
                if not 1 <= len(ssid.encode("utf-8")) <= 32:
                    c.say("SSID must be 1-32 bytes.")
                    continue
                is_open = c.input("Open network (no password)? [y/N]: ").strip().lower() \
                    .startswith("y")
                return ssid, is_open
            if ans.isdigit() and 1 <= int(ans) <= len(nets):
                n = nets[int(ans) - 1]
                return n["ssid"], bool(n.get("open"))
            c.say("Invalid choice.")


def _ask_password(c, ssid):
    for _ in range(3):
        pw = c.getpass("WiFi password for '%s' (hidden): " % ssid)
        if 8 <= len(pw) <= 64:
            return pw
        c.say("WPA passwords are 8-63 characters (or 64 hex). Try again.")
    raise SetupError("no valid password entered")


def run_setup(env, dry_run=False, ssid=None):
    c = env.console
    if dry_run:
        dev = None
        try:
            dev = _find(env)
        except SetupError as e:
            c.say("(dry-run) %s" % e)
        holder = _bridge.other_holder_alive(os.path.join(env.home, "serial", "hold.pid"))
        c.say("(dry-run) nothing is opened or sent.")
        c.say("(dry-run) port holder: %s" % ("pid %d -> SIGUSR1 pause, resume at end" % holder
                                            if holder else "none"))
        c.say("(dry-run) target USB serial %s: %s" % (env.serial_no, dev or "NOT FOUND"))
        c.say("(dry-run) would send: %s" % json.dumps({"t": "scan"}))
        c.say("(dry-run) would send: %s" % masked(
            {"t": "provision", "ssid": ssid or "<chosen ssid>", "pass": "x", "key": "x"}))
        c.say("(dry-run) on ok: Keychain service=%s account=<host>, %s"
              % (_keystore.SERVICE, _device.path(env.home)))
        return 0

    dev = _find(env)
    if not dev:
        c.say("Target device (USB serial %s) not found. Connect it by USB and retry."
              % env.serial_no)
        return 2
    with _Session(env, dev) as link:
        choice = _choose_network(link, c)
        if choice is None:
            c.say("Aborted; nothing changed.")
            return 1
        net, is_open = choice
        pw = "" if is_open else _ask_password(c, net)
        key = env.token(32)
        c.say("Provisioning '%s' (the device tries to connect for up to 15s) ..." % net)
        link.send({"t": "provision", "ssid": net, "pass": pw, "key": key.hex()})
        pw = None
        r = link.wait_for(("provision",), PROVISION_TIMEOUT_S)
    # port closed and bridge resumed from here on
    if r is None:
        c.say("No provision reply within %ds. Nothing was saved on this Mac." % PROVISION_TIMEOUT_S)
        c.say("Check the device screen / reflash P4 firmware, then rerun ctm setup.")
        return 3
    if not r.get("ok"):
        err = r.get("err", "?")
        c.say("Provisioning failed: %s" % err)
        for h in FAIL_HINTS.get(err, []):
            c.say("  - " + h)
        c.say("  - The device keeps the saved settings and retries; rerun ctm setup to change them.")
        return 4
    host, ip = r.get("host"), r.get("ip") or ""
    if not isinstance(host, str) or not re.match(r"^[A-Za-z0-9-]{1,63}$", host):
        c.say("Device replied ok but with an invalid host name; nothing saved.")
        return 4
    old = _device.load(env.home)
    try:
        env.ks().set(host, key.hex())
    except Exception as e:
        c.say("Device is provisioned, but storing the key in the Keychain failed: %s" % e)
        c.say("Rerun ctm setup (a new key will be generated).")
        return 5
    if old and old.get("host") != host:
        try:
            env.ks().delete(old["host"])
        except Exception:
            pass
    _device.save(env.home, host, ip)
    c.say("Paired: %s.local  ip %s" % (host, ip or "?"))
    c.say("Key stored in Keychain (service %s, account %s); device info in %s."
          % (_keystore.SERVICE, host, _device.path(env.home)))
    c.say("The bridge uses USB while plugged in and WiFi otherwise (--transport auto).")
    return 0


def run_unpair(env, local_only=False, dry_run=False):
    c = env.console
    rec = _device.load(env.home)
    if dry_run:
        c.say("(dry-run) would send: %s" % json.dumps({"t": "unpair"}))
        c.say("(dry-run) would remove Keychain %s/%s and %s" % (
            _keystore.SERVICE, rec["host"] if rec else "<none>", _device.path(env.home)))
        return 0
    if not local_only:
        dev = _find(env)
        if not dev:
            c.say("Target device (USB serial %s) not found. Connect it by USB, "
                  "or use --local to forget the pairing on this Mac only." % env.serial_no)
            return 2
        with _Session(env, dev) as link:
            link.send({"t": "unpair"})
            r = link.wait_for(("unpair",), CMD_TIMEOUT_S)
        if not (r and r.get("ok")):
            c.say("Device did not confirm unpair; local pairing kept.")
            return 3
        c.say("Device cleared its WiFi settings and key.")
    if rec:
        try:
            env.ks().delete(rec["host"])
        except Exception as e:
            c.say("Keychain delete failed: %s" % e)
        _device.remove(env.home)
        c.say("Forgot %s on this Mac." % rec["host"])
    else:
        c.say("No local pairing record.")
    return 0


def run_info(env, force_wifi=False):
    c = env.console
    dev = None
    if not force_wifi:
        try:
            dev = _find(env)
        except SetupError:
            dev = None
    if dev:
        with _Session(env, dev) as link:
            link.send({"t": "info"})
            r = link.wait_for(("info",), CMD_TIMEOUT_S)
        if r is None:
            c.say("No info reply over USB.")
            return 3
        c.say("USB %s:" % dev)
        c.say(json.dumps(r, indent=2, sort_keys=True))
        return 0
    rec = _device.load(env.home)
    if not rec:
        c.say("USB device not present and no pairing (device.json). Run ctm setup.")
        return 2
    cl = env.wifi_factory(rec["host"], rec.get("ip") or None)
    info, ip, err = cl.get_info()
    if info is None:
        c.say("GET http://%s.local/v1/info (%s) failed: %s" % (rec["host"], ip or "-", err))
        return 3
    c.say("WiFi %s.local (%s):" % (rec["host"], ip))
    c.say(json.dumps(info, indent=2, sort_keys=True))
    return 0


def install_exit_signals():
    """SIGTERM / SIGHUP -> SystemExit so finally blocks resume the bridge."""
    def _exit(signum, _frame):
        raise SystemExit(128 + signum)
    for s in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(s, _exit)
        except (ValueError, OSError):
            pass
