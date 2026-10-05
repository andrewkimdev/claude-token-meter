#!/usr/bin/env python3
"""ctm-bridge (P1, USB serial transport). PROTOCOL v1 §2-§4.1.

Run:  cd host && ~/.platformio/penv/bin/python3 -m ctm.bridge
      ~/.platformio/penv/bin/python3 host/ctm/bridge.py --once

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
except ImportError:  # run as a script: host/ctm/bridge.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ctm import merge as _merge
    from ctm import proto as _proto

SERIAL_NO = os.environ.get("CTM_USB_SERIAL", "5915018996")
BAUD = 115200
READ_TIMEOUT_S = 0.5      # serial read blocks at most this long -> no busy loop
WRITE_TIMEOUT_S = 2.0
POLL_S = 2.0              # sessions dir poll period
MIN_INTERVAL_S = 2.0      # P1 (USB). ARCHITECTURE: 30s for WiFi (P4).
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


def windows_key(windows):
    """What counts as a change: pct / resets_at per window, presence."""
    return tuple(sorted((k, w.get("pct"), w.get("resets_at")) for k, w in windows.items()))


class Bridge(object):
    def __init__(self, home=None, find_port=find_port, open_port=open_port,
                 clock=time.time, sleep=time.sleep, min_interval=MIN_INTERVAL_S):
        self.home = home or ctm_home()
        self.serial_dir = os.path.join(self.home, "serial")
        self.state_path = os.path.join(self.serial_dir, "hold.state")
        self.pid_path = os.path.join(self.serial_dir, "hold.pid")
        self.find_port = find_port
        self.open_port = open_port
        self.clock = clock
        self.sleep = sleep
        self.min_interval = min_interval
        self.port = None
        self.dev = None
        self.buf = b""
        self.seq = 0
        self.paused = False
        self.stop = False
        self.force_send = False
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
        return _proto.build_frame(self.seq + 1, now, self.windows)

    def due(self, now):
        """Should a frame be sent now? Returns reason string or None."""
        if self.force_send:
            return "force"
        if self.last_sent_at is None:
            return "first"
        elapsed = now - self.last_sent_at
        if windows_key(self.windows) != self.last_sent_key and elapsed >= self.min_interval:
            return "change"
        if elapsed >= HEARTBEAT_S:
            return "heartbeat"
        return None

    def send(self, now, reason):
        frame = self.current_frame(now)
        data = _proto.encode_frame(frame)
        self.port.write(data)
        try:
            self.port.flush()
        except Exception:
            pass
        self.seq = frame["seq"]
        self.last_sent_at = now
        self.last_sent_key = windows_key(self.windows)
        self.force_send = False
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
            try:
                dev = self.find_port()
            except Exception as e:
                self.set_state("WAITING (port scan failed: %s)" % e)
                self.sleep(ABSENT_RETRY_S)
                return
            if not dev:
                self.set_state("WAITING (device not present)")
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

    def run(self):
        os.makedirs(self.serial_dir, exist_ok=True)
        with open(self.pid_path, "w") as f:
            f.write(str(os.getpid()))
        self.event("start pid=%d serial_no=%s home=%s" % (os.getpid(), SERIAL_NO, self.home))
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
    args = ap.parse_args(argv)
    b = Bridge()
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
