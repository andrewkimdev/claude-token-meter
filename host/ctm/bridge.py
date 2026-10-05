#!/usr/bin/env python3
"""ctm-bridge (USB serial + WiFi HTTP transports). PROTOCOL v1 §2-§4.2.

Run:  cd host && ~/.platformio/penv/bin/python3 -m ctm.bridge [--transport auto|usb|wifi]
      ~/.platformio/penv/bin/python3 host/ctm/bridge.py --once

- Transport (§4.2): auto = USB while the target USB serial device is
  present, else WiFi if paired (device.json + Keychain key), else wait.
  WiFi: signed POST http://<host>.local/v1/state, min interval 30s,
  heartbeat 120s, exponential backoff (max 10 min) on failure.

- Owns the target serial port like tools/serial_hold.py: found by USB serial
  number (CTM_USB_SERIAL), opened with DTR/RTS low, then TIOCEXCL.
  `kill -USR1 $(cat ~/.claude-token-meter/serial/hold.pid)` toggles
  release / re-acquire (e.g. around a firmware upload).
- State: <CTM_HOME>/serial/hold.state, pid: hold.pid,
  log: <CTM_HOME>/serial/YYYYMMDD.log (RX lines raw, TX with ">> ",
  own events with "[bridge]").
- Polls <CTM_HOME>/sessions every POLL_S, merges, sends a state frame on
  change (min interval MIN_INTERVAL_S), every HEARTBEAT_S, on (re)connect,
  and immediately on a device "@{"t":"hello"}" line.

Python 3.9 compatible. pyserial is only imported when the port is used.
"""
import argparse
import errno
import json
import os
import signal
import sys
import time

try:
    from ctm import merge as _merge
    from ctm import proto as _proto
    from ctm import device as _device
    from ctm import keystore as _keystore
    from ctm import wifi as _wifi
except ImportError:  # run as a script: host/ctm/bridge.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ctm import merge as _merge
    from ctm import proto as _proto
    from ctm import device as _device
    from ctm import keystore as _keystore
    from ctm import wifi as _wifi

SERIAL_NO = os.environ.get("CTM_USB_SERIAL", "5915018996")
BAUD = 115200
READ_TIMEOUT_S = 0.5      # serial read blocks at most this long -> no busy loop
WRITE_TIMEOUT_S = 2.0
POLL_S = 2.0              # sessions dir poll period
MIN_INTERVAL_S = 2.0      # USB
WIFI_MIN_INTERVAL_S = 30.0  # PROTOCOL §4.2
WIFI_TICK_S = 1.0         # WiFi loop period (also USB presence re-check period)
WIFI_BACKOFF_BASE_S = 5.0
WIFI_BACKOFF_MAX_S = 600.0
KEY_RETRY_S = 60.0        # re-ask the Keychain this often when the key is missing
SEQ_MAX = 0xFFFFFFFF
TRANSPORTS = ("auto", "usb", "wifi")
NAK_RETRY_S = 1.0
HEARTBEAT_S = 120.0
PRUNE_EVERY_S = 3600.0
ABSENT_RETRY_S = 1.0
OPEN_FAIL_RETRY_S = 2.0
PAUSED_SLEEP_S = 0.2
MAX_RX_BUF = 4096


def ctm_home():
    return os.environ.get("CTM_HOME") or os.path.expanduser("~/.claude-token-meter")


# ---- real serial helpers (same approach as tools/serial_hold.py) ----------

def find_port(serial_no=None):
    from serial.tools import list_ports
    serial_no = serial_no or SERIAL_NO
    for p in list_ports.comports():
        if p.serial_number and p.serial_number.startswith(serial_no) and "cu." in p.device:
            return p.device
    return None


def open_port(dev):
    import fcntl
    import termios
    import serial
    s = serial.Serial()
    s.port, s.baudrate = dev, BAUD
    s.timeout, s.write_timeout = READ_TIMEOUT_S, WRITE_TIMEOUT_S
    s.dtr = False
    s.rts = False          # avoid reset pulse on open
    s.open()
    # pyserial exclusive=True is an advisory flock; TIOCEXCL is kernel-level [V].
    fcntl.ioctl(s.fileno(), termios.TIOCEXCL)
    return s


def next_seq(last_seq, now):
    """PROTOCOL §3: seq never decreases across bridge restarts."""
    return min(SEQ_MAX, max(int(last_seq) + 1, int(now)))


def windows_key(windows):
    """What counts as a change: pct / resets_at per window, presence."""
    return tuple(sorted((k, w.get("pct"), w.get("resets_at")) for k, w in windows.items()))


class Bridge(object):
    def __init__(self, home=None, find_port=find_port, open_port=open_port,
                 clock=time.time, sleep=time.sleep, min_interval=MIN_INTERVAL_S,
                 transport="auto", keystore=None, wifi_factory=None,
                 wifi_min_interval=WIFI_MIN_INTERVAL_S):
        if transport not in TRANSPORTS:
            raise ValueError("transport must be one of %s" % (TRANSPORTS,))
        self.home = home or ctm_home()
        self.serial_dir = os.path.join(self.home, "serial")
        self.state_path = os.path.join(self.serial_dir, "hold.state")
        self.pid_path = os.path.join(self.serial_dir, "hold.pid")
        self.find_port = find_port
        self.open_port = open_port
        self.clock = clock
        self.sleep = sleep
        self.min_interval = min_interval
        self.wifi_min_interval = wifi_min_interval
        self.transport = transport
        self.keystore = keystore
        self.wifi_factory = wifi_factory or (
            lambda host, key, ip: _wifi.WifiClient(host, key, ip))
        self.active = None          # "usb" | "wifi" | None
        self.wifi = None            # WifiClient for the paired device
        self.pair_id = None         # (host, paired_at) the client was built for
        self.key_retry_at = None
        self.wifi_next_try = None
        self.backoff = _wifi.Backoff(WIFI_BACKOFF_BASE_S, WIFI_BACKOFF_MAX_S)
        self.port = None
        self.dev = None
        self.buf = b""
        self.seq = 0
        self.paused = False
        self.stop = False
        self.force_send = False
        self.retry_at = None
        self.last_reason = None
        self.last_sent_key = None
        self.last_sent_at = None
        self.last_poll = None
        self.last_prune = None
        self.mtimes = None
        self.sessions = []
        self.windows = {}
        self._state = None

    # ---- logging / state ----
    def log(self, line):
        try:
            os.makedirs(self.serial_dir, exist_ok=True)
            with open(os.path.join(self.serial_dir, time.strftime("%Y%m%d") + ".log"), "a") as f:
                f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
        except OSError:
            pass

    def event(self, msg):
        self.log("[bridge] " + msg)

    def set_state(self, s):
        if s == self._state:
            return
        self._state = s
        try:
            os.makedirs(self.serial_dir, exist_ok=True)
            with open(self.state_path, "w") as f:
                f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), s))
        except OSError:
            pass

    # ---- sessions ----
    def poll_sessions(self, now, force=False):
        if not force and self.last_poll is not None and now - self.last_poll < POLL_S:
            return
        self.last_poll = now
        if self.last_prune is None or now - self.last_prune >= PRUNE_EVERY_S:
            self.last_prune = now
            for n in _merge.prune(self.home, now):
                self.event("pruned session file %s" % n)
        m = _merge.snapshot_mtimes(self.home)
        if m != self.mtimes:
            self.mtimes = m
            self.sessions = _merge.load_sessions(self.home)
        # re-merge every poll: windows expire with time, not only on file change
        self.windows = _merge.merge(self.sessions, now)

    def current_frame(self, now):
        return _proto.build_frame(next_seq(self.seq, now), now, self.windows)

    def due(self, now):
        """Should a frame be sent now? Returns reason string or None."""
        if self.force_send:
            return "force"
        if self.retry_at is not None and now >= self.retry_at:
            return "retry"
        if self.last_sent_at is None:
            return "first"
        elapsed = now - self.last_sent_at
        mi = self.wifi_min_interval if self.active == "wifi" else self.min_interval
        if windows_key(self.windows) != self.last_sent_key and elapsed >= mi:
            return "change"
        if elapsed >= HEARTBEAT_S:
            return "heartbeat"
        return None

    def mark_sent(self, frame, now, reason):
        self.seq = frame["seq"]
        self.last_sent_at = now
        self.last_sent_key = windows_key(self.windows)
        self.force_send = False
        self.retry_at = None
        self.last_reason = reason

    def send(self, now, reason):
        frame = self.current_frame(now)
        data = _proto.encode_frame(frame)
        # Leading "\n" terminates any partial line left in the device's RX
        # buffer (e.g. a frame cut by a reset); the device ignores empty lines.
        self.port.write(b"\n" + data)
        try:
            self.port.flush()
        except Exception:
            pass
        self.mark_sent(frame, now, reason)
        self.log(">> " + data.decode("utf-8").rstrip("\n") + "  (" + reason + ")")

    # ---- device lines ----
    def handle_line(self, raw):
        line = raw.rstrip(b"\r").decode("utf-8", "replace")
        self.log(line)
        msg = _proto.parse_device_line(line)
        if msg is None:
            if line.startswith("@"):
                self.event("unparsable protocol line")
            return
        t = msg.get("t")
        if t == "hello":
            self.event("device hello fw=%s board=%s -> resend" % (msg.get("fw"), msg.get("board")))
            self.force_send = True
        elif t == "nak":
            self.event("NAK seq=%s err=%s" % (msg.get("seq"), msg.get("err")))
            # parse nak = likely line corruption: resend once (never retry a retry)
            if msg.get("err") == "parse" and self.last_reason != "retry":
                self.retry_at = self.clock() + NAK_RETRY_S
        elif t == "ack":
            if msg.get("seq") != self.seq:
                self.event("ack for seq=%s (last sent %s)" % (msg.get("seq"), self.seq))
        else:
            self.event("unknown protocol message t=%s" % t)

    def feed(self, chunk):
        self.buf += chunk
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            self.handle_line(line)
        if len(self.buf) > MAX_RX_BUF:  # runaway line without newline
            self.handle_line(self.buf)
            self.buf = b""

    # ---- port lifecycle ----
    def close_port(self, why):
        if self.port is not None:
            try:
                self.port.close()
            except Exception:
                pass
            self.event("released %s (%s)" % (self.dev, why))
        self.port = None
        self.buf = b""

    def step(self):
        """One loop iteration. Every path either blocks on serial read
        (READ_TIMEOUT_S) or sleeps, so the loop never busy-spins."""
        if self.paused:
            if self.port is not None:
                self.close_port("paused by SIGUSR1")
            self.set_state("PAUSED")
            self.sleep(PAUSED_SLEEP_S)
            return
        if self.port is None:
            if self.transport == "wifi":
                self.wifi_step()
                return
            try:
                dev = self.find_port()
            except Exception as e:
                if self.transport == "auto" and self.wifi_ready():
                    self.wifi_step()
                    return
                self.set_state("WAITING (port scan failed: %s)" % e)
                self.sleep(ABSENT_RETRY_S)
                return
            if not dev:
                if self.transport == "auto" and self.wifi_ready():
                    self.wifi_step()
                    return
                if self.transport == "usb":
                    self.set_state("WAITING (device not present)")
                else:
                    self.wait_state_unpaired()
                self.sleep(ABSENT_RETRY_S)
                return
            try:
                self.port = self.open_port(dev)
            except Exception as e:
                self.port = None
                self.set_state("OPEN FAILED %s: %s" % (dev, e))
                self.sleep(OPEN_FAIL_RETRY_S)
                return
            self.dev = dev
            self.buf = b""
            self.force_send = True
            if self.active != "usb":
                if self.active is not None:
                    self.event("transport -> usb")
                self.active = "usb"
            self.event("HELD %s" % dev)
            self.set_state("HELD %s" % dev)
        now = self.clock()
        self.poll_sessions(now)
        reason = self.due(now)
        if reason:
            try:
                self.send(now, reason)
            except Exception as e:
                self.event("write error: %s" % e)
                self.close_port("write error")
                self.sleep(ABSENT_RETRY_S)
                return
        try:
            chunk = self.port.read(512)
        except Exception as e:
            self.event("read error: %s" % e)
            self.close_port("read error")
            self.sleep(ABSENT_RETRY_S)
            return
        if chunk:
            self.feed(chunk)

    # ---- WiFi transport (§4.2) ----
    def _keystore(self):
        if self.keystore is None:
            self.keystore = _keystore.default()
        return self.keystore

    def wifi_ready(self):
        """Load / refresh the pairing (device.json + Keychain key). True when a
        WifiClient is available. Never logs the key."""
        rec = _device.load(self.home)
        if rec is None:
            if self.wifi is not None:
                self.event("device.json removed -> WiFi unpaired")
            self.wifi = None
            self.pair_id = None
            return False
        pid = (rec["host"], rec.get("paired_at"))
        if self.wifi is not None and pid == self.pair_id:
            if rec.get("ip") and not self.wifi.cached_ip:
                self.wifi.cached_ip = rec.get("ip")
            return True
        now = self.clock()
        if pid == self.pair_id and self.key_retry_at is not None and now < self.key_retry_at:
            return False
        self.pair_id = pid
        self.wifi = None
        try:
            key_hex = self._keystore().get(rec["host"])
        except Exception as e:
            key_hex = None
            self.event("keychain lookup failed for %s: %s" % (rec["host"], e))
        if not key_hex:
            self.key_retry_at = now + KEY_RETRY_S
            self.set_state("WAITING (paired %s but no pairing key in Keychain -- rerun ctm setup)"
                           % rec["host"])
            return False
        self.key_retry_at = None
        self.wifi = self.wifi_factory(rec["host"], bytes.fromhex(key_hex), rec.get("ip") or None)
        self.backoff.reset()
        self.wifi_next_try = None
        self.event("paired device %s (cached ip %s)" % (rec["host"], rec.get("ip") or "-"))
        return True

    def wait_state_unpaired(self):
        if self._state is None or not self._state.startswith("WAITING (paired"):
            self.set_state("WAITING (device not present, not paired for WiFi)")

    def wifi_step(self):
        """One WiFi iteration. Blocks at most ~HTTP timeout + resolve timeout
        when sending, otherwise sleeps WIFI_TICK_S."""
        if not self.wifi_ready():
            self.wait_state_unpaired()
            self.sleep(ABSENT_RETRY_S)
            return
        if self.active != "wifi":
            self.event("transport -> wifi (%s)" % self.wifi.host)
            self.active = "wifi"
            self.force_send = True
            self.set_state("WIFI %s" % self.wifi.host)
        now = self.clock()
        self.poll_sessions(now)
        if self.wifi_next_try is not None and now < self.wifi_next_try:
            self.sleep(min(WIFI_TICK_S, self.wifi_next_try - now))
            return
        reason = self.due(now)
        if reason:
            self.wifi_send(now, reason)
        self.sleep(WIFI_TICK_S)

    def wifi_fail(self, now, msg):
        d = self.backoff.fail()
        self.wifi_next_try = now + d
        self.event("%s -> retry in %ds" % (msg, d))
        self.set_state("WIFI %s backoff %ds (%s)" % (self.wifi.host, d, msg))

    def wifi_send(self, now, reason):
        frame = self.current_frame(now)
        # §4.2 body: the encoded frame, without the USB-only leading "\n"
        body = _proto.encode_frame(frame)
        self.log(">> wifi " + body.decode("utf-8").rstrip("\n") + "  (" + reason + ")")
        # the seq is spent whether or not the device saw it
        self.seq = frame["seq"]
        r = self.wifi.post_state(body)
        host = self.wifi.host
        if r.kind == "ack":
            self.mark_sent(frame, now, reason)
            self.backoff.reset()
            self.wifi_next_try = None
            if r.reply and r.reply.get("seq") not in (None, frame["seq"]):
                self.event("ack for seq=%s (last sent %s)" % (r.reply.get("seq"), frame["seq"]))
            if r.ip and r.ip != self.wifi.cached_ip:
                self.wifi.cached_ip = r.ip
                if _device.update_ip(self.home, r.ip):
                    self.event("cached ip for %s -> %s" % (host, r.ip))
            self.set_state("WIFI %s %s ok seq=%d" % (host, r.ip or "-", frame["seq"]))
            return
        if r.kind == "nak_parse":
            # delivered but rejected: do not hammer, normal policy resends later
            self.event("HTTP 400 NAK seq=%s err=%s" % (frame["seq"], r.err))
            self.mark_sent(frame, now, reason)
            return
        if r.kind == "old_seq":
            self.event("HTTP 409 NAK seq=%s err=old_seq" % frame["seq"])
            self.mark_sent(frame, now, reason)
            # resync with the device's last_seq, then retry once (never retry a retry)
            info, _ip, _err = self.wifi.get_info()
            ls = info.get("last_seq") if isinstance(info, dict) else None
            if isinstance(ls, int) and not isinstance(ls, bool) and ls >= self.seq:
                self.event("device last_seq=%d -> skipping ahead" % ls)
                self.seq = min(SEQ_MAX, ls)
            if reason != "retry":
                self.retry_at = now + NAK_RETRY_S
            return
        if r.kind == "skew":
            self.wifi_fail(now, "HTTP 409 skew: Mac clock and device clock differ by >300s")
            return
        if r.kind == "sig":
            self.wifi_fail(now, "HTTP 401: pairing key mismatch \u2014 rerun ctm setup")
            return
        if r.kind == "net":
            msg = "POST %s (%s) failed: %s" % (host, r.ip, r.err)
            if r.errno == errno.EHOSTUNREACH:
                self.event(_wifi.LOCAL_NETWORK_HINT)
            self.wifi_fail(now, msg)
            return
        if r.kind == "resolve":
            self.wifi_fail(now, r.err)
            return
        self.wifi_fail(now, "HTTP %s from %s" % (r.status, host))

    def run(self):
        os.makedirs(self.serial_dir, exist_ok=True)
        with open(self.pid_path, "w") as f:
            f.write(str(os.getpid()))
        self.event("start pid=%d serial_no=%s home=%s transport=%s"
                   % (os.getpid(), SERIAL_NO, self.home, self.transport))
        try:
            while not self.stop:
                self.step()
        finally:
            self.close_port("exit")
            self.set_state("STOPPED")
            self.event("stop")
            try:
                with open(self.pid_path) as f:
                    if f.read().strip() == str(os.getpid()):
                        os.unlink(self.pid_path)
            except OSError:
                pass


def other_holder_alive(pid_path):
    """pid of a live process recorded in hold.pid (e.g. serial_hold.py), or None."""
    try:
        with open(pid_path) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    if pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
    except OSError as e:
        return pid if e.errno == errno.EPERM else None
    return pid


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ctm-bridge")
    ap.add_argument("--once", action="store_true",
                    help="merge sessions, print the frame that would be sent, exit (no serial)")
    ap.add_argument("--force", action="store_true",
                    help="start even if hold.pid names another live process")
    ap.add_argument("--transport", choices=TRANSPORTS, default="auto",
                    help="auto: USB if the target device is plugged in, else WiFi if paired")
    args = ap.parse_args(argv)
    b = Bridge(transport=args.transport)
    if args.once:
        now = time.time()
        b.poll_sessions(now, force=True)
        print(json.dumps(b.current_frame(now), separators=(",", ":")))
        return 0
    holder = other_holder_alive(b.pid_path)
    if holder and not args.force:
        sys.stderr.write("ctm-bridge: hold.pid names live pid %d (serial_hold.py?). "
                         "Stop it first or pass --force.\n" % holder)
        return 2

    def on_usr1(*_):
        b.paused = not b.paused

    def on_term(*_):
        b.stop = True

    signal.signal(signal.SIGUSR1, on_usr1)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    b.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
