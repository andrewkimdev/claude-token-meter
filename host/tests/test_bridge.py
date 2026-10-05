import glob
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tests._util import rd
from ctm import bridge, keystore, proto


class Clock(object):
    def __init__(self, t=1791180000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        assert s > 0, "busy loop: sleep(0)"
        self.t += s


class FakePort(object):
    def __init__(self, clock):
        self.clock = clock
        self.rx = []
        self.tx = []
        self.closed = False
        self.read_error = None
        self.write_error = None

    def write(self, b):
        if self.write_error:
            raise self.write_error
        self.tx.append(b)

    def flush(self):
        pass

    def read(self, n):
        if self.read_error:
            raise self.read_error
        if self.rx:
            return self.rx.pop(0)
        self.clock.t += bridge.READ_TIMEOUT_S  # simulate blocking timeout
        return b""

    def close(self):
        self.closed = True

    def frames(self):
        return [json.loads(b.decode()) for b in self.tx]


def write_session(home, sid, pct5, r5, obs, pct7=None, r7=None):
    d = os.path.join(home, "sessions")
    os.makedirs(d, exist_ok=True)
    rl = {"five_hour": {"used_percentage": pct5, "resets_at": r5}}
    if pct7 is not None:
        rl["seven_day"] = {"used_percentage": pct7, "resets_at": r7}
    p = os.path.join(d, sid + ".json")
    with open(p, "w") as f:
        json.dump({"session_id": sid, "observed_at": obs, "rate_limits": rl}, f)
    # bump mtime deterministically so change detection sees it
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1000 + int(obs * 1000) % 1000000))


S0 = 1791180000  # int(Clock().t): first seq = max(0 + 1, int(now))


def check_seqs(tc, frames, n):
    """PROTOCOL §3 seq rule: seq = max(last_seq + 1, int(now)), sent_at = int(now)."""
    tc.assertEqual(len(frames), n)
    last = 0
    for f in frames:
        tc.assertEqual(f["seq"], max(last + 1, f["sent_at"]))
        last = f["seq"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.clock = Clock()
        self.ports = []
        self.present = True
        self.open_error = None

        def find():
            return "/dev/cu.fake" if self.present else None

        def opener(dev):
            if self.open_error:
                raise self.open_error
            p = FakePort(self.clock)
            self.ports.append(p)
            return p

        self.b = bridge.Bridge(home=self.home, find_port=find, open_port=opener,
                               clock=self.clock, sleep=self.clock.sleep,
                               keystore=keystore.MemoryKeystore(),
                               usb_hello_wait=self.HELLO_WAIT)

    # Legacy P1-P4 tests send on connect; the P5 hello deferral is tested in
    # test_robustness.py with the real default.
    HELLO_WAIT = 0

    def tearDown(self):
        self.tmp.cleanup()

    def run_for(self, seconds, max_steps=100000):
        end = self.clock.t + seconds
        n = 0
        while self.clock.t < end:
            t0 = self.clock.t
            self.b.step()
            n += 1
            self.assertLess(n, max_steps)
            if self.clock.t == t0:
                # a step that neither blocked nor slept is only allowed when it consumed RX data
                pass

    def all_frames(self):
        return [f for p in self.ports for f in p.frames()]

    def logtext(self):
        out = ""
        for p in glob.glob(os.path.join(self.home, "serial", "*.log")):
            out += rd(p)
        return out

    def state(self):
        return rd(os.path.join(self.home, "serial", "hold.state"))


class SendPolicy(Base):
    def test_send_on_connect_and_heartbeat(self):
        T = self.clock.t
        write_session(self.home, "a", 52, int(T) + 3600, T - 10, 29, int(T) + 86400)
        self.run_for(1)
        fr = self.all_frames()
        self.assertEqual(len(fr), 1)
        self.assertEqual(fr[0]["seq"], S0)
        self.assertEqual(fr[0]["windows"]["five_hour"]["pct"], 52)
        self.assertEqual(proto.device_accept(json.dumps(fr[0]), None)["result"], "ack")
        self.run_for(118)
        self.assertEqual(len(self.all_frames()), 1)
        self.run_for(3)
        fr = self.all_frames()
        check_seqs(self, fr, 2)
        self.assertGreaterEqual(fr[1]["seq"], S0 + 119)
        self.assertIn("HELD /dev/cu.fake", self.state())
        log = self.logtext()
        self.assertIn('>> {"t":"state","v":1,"seq":%d' % S0, log)
        self.assertIn("(heartbeat)", log)

    def test_change_triggers_send_with_min_interval(self):
        T = self.clock.t
        write_session(self.home, "a", 50, int(T) + 3600, T)
        self.run_for(1)
        write_session(self.home, "a", 51, int(T) + 3600, T + 1)
        self.run_for(5)
        fr = self.all_frames()
        self.assertEqual([f["windows"]["five_hour"]["pct"] for f in fr], [50, 51])
        self.assertGreaterEqual(fr[1]["sent_at"] - fr[0]["sent_at"], bridge.MIN_INTERVAL_S - 1)
        # observed_at-only change does not trigger a send
        write_session(self.home, "b", 51, int(T) + 3600, T + 4)
        self.run_for(10)
        self.assertEqual(len(self.all_frames()), 2)
        # a new window appearing does
        write_session(self.home, "c", 51, int(T) + 3600, T + 20, 7, int(T) + 86400)
        self.run_for(5)
        self.assertIn("seven_day", self.all_frames()[-1]["windows"])

    def test_min_interval_coalesces(self):
        self.b.min_interval = 30.0
        T = self.clock.t
        write_session(self.home, "a", 1, int(T) + 3600, T)
        self.run_for(1)
        for i in range(2, 8):
            write_session(self.home, "a", i, int(T) + 3600, T + i)
            self.run_for(3)
        self.run_for(30)
        fr = self.all_frames()
        self.assertLessEqual(len(fr), 3)
        self.assertEqual(fr[-1]["windows"]["five_hour"]["pct"], 7)
        for a, b in zip(fr, fr[1:]):
            self.assertGreaterEqual(b["sent_at"] - a["sent_at"], 29)

    def test_window_expiry_is_a_change(self):
        T = self.clock.t
        write_session(self.home, "a", 50, int(T) + 10, T)
        self.run_for(1)
        self.run_for(15)
        fr = self.all_frames()
        self.assertEqual(len(fr), 2)
        self.assertEqual(fr[-1]["windows"], {})

    def test_hello_resends_immediately(self):
        T = self.clock.t
        write_session(self.home, "a", 50, int(T) + 3600, T)
        self.run_for(1)
        p = self.ports[0]
        p.rx.append(b'ets Jun  8 2016 00:22:57\r\n[diag] boot\r\n@{"t":"hel')
        p.rx.append(b'lo","fw":"0.1.0","board":"t-display"}\r\n')
        self.run_for(0.6)
        fr = self.all_frames()
        check_seqs(self, fr, 2)
        self.assertEqual(fr[1]["seq"], S0 + 1)   # same second -> last + 1
        log = self.logtext()
        self.assertIn("[diag] boot", log)
        self.assertIn('@{"t":"hello"', log)
        self.assertIn("[bridge] device hello", log)

    def test_ack_nak_logged(self):
        self.run_for(1)
        self.ports[0].rx.append(('@{"t":"ack","seq":%d}\n@{"t":"nak","seq":%d,"err":"old_seq"}\n'
                                 % (S0, S0)).encode())
        self.run_for(1)
        log = self.logtext()
        self.assertIn('@{"t":"ack","seq":%d}' % S0, log)
        self.assertIn("[bridge] NAK seq=%d err=old_seq" % S0, log)
        self.assertNotIn("ack for seq", log)


    def test_frames_start_with_newline(self):
        # a leading newline cuts any partial line left in the device RX buffer
        self.run_for(1)
        self.assertTrue(self.ports[0].tx[0].startswith(b"\n{"))

    def test_parse_nak_retries_once(self):
        T = self.clock.t
        write_session(self.home, "a", 50, int(T) + 3600, T)
        self.run_for(1)
        p = self.ports[0]
        p.rx.append(b'@{"t":"nak","seq":0,"err":"parse"}\n')
        self.run_for(2)
        check_seqs(self, self.all_frames(), 2)
        p.rx.append(b'@{"t":"nak","seq":0,"err":"parse"}\n')   # nak of the retry
        self.run_for(5)
        check_seqs(self, self.all_frames(), 2)  # no retry loop

    def test_old_seq_nak_no_retry(self):
        self.run_for(1)
        self.ports[0].rx.append(b'@{"t":"nak","seq":1,"err":"old_seq"}\n')
        self.run_for(5)
        self.assertEqual(len(self.all_frames()), 1)


class PortLifecycle(Base):
    def test_absent_waits_without_busy_loop(self):
        self.present = False
        self.run_for(5, max_steps=10)
        self.assertIn("WAITING", self.state())
        self.assertEqual(self.ports, [])
        self.present = True
        self.run_for(2)
        self.assertEqual(len(self.ports), 1)
        self.assertEqual(len(self.all_frames()), 1)

    def test_open_failed(self):
        self.open_error = OSError(16, "Resource busy")
        self.run_for(5, max_steps=10)
        self.assertIn("OPEN FAILED /dev/cu.fake", self.state())
        self.open_error = None
        self.run_for(3)
        self.assertIn("HELD", self.state())

    def test_read_error_reopens_and_resends(self):
        self.run_for(1)
        self.ports[0].read_error = OSError(6, "Device not configured")
        self.run_for(3)
        self.assertTrue(self.ports[0].closed)
        self.assertEqual(len(self.ports), 2)
        check_seqs(self, self.all_frames(), 2)
        self.assertIn("[bridge] read error", self.logtext())

    def test_write_error_reopens(self):
        self.run_for(1)
        self.ports[0].write_error = OSError(5, "EIO")
        self.b.force_send = True
        self.run_for(3)
        self.assertTrue(self.ports[0].closed)
        self.assertEqual(len(self.ports), 2)
        self.assertIn("[bridge] write error", self.logtext())

    def test_unplug_waits_then_reconnects(self):
        self.run_for(1)
        self.ports[0].read_error = OSError(6, "Device not configured")
        self.present = False
        self.run_for(5, max_steps=20)
        self.assertIn("WAITING", self.state())
        self.present = True
        self.run_for(2)
        self.assertIn("HELD", self.state())

    def test_sigusr1_pause_resume(self):
        self.run_for(1)
        self.b.paused = True
        self.run_for(1, max_steps=20)
        self.assertTrue(self.ports[0].closed)
        self.assertIn("PAUSED", self.state())
        self.assertIn("released /dev/cu.fake (paused by SIGUSR1)", self.logtext())
        self.b.paused = False
        self.run_for(1)
        self.assertEqual(len(self.ports), 2)
        self.assertIn("HELD", self.state())

    def test_runaway_line_bounded(self):
        self.run_for(1)
        self.ports[0].rx.append(b"x" * (bridge.MAX_RX_BUF + 10))
        self.run_for(1)
        self.assertEqual(self.b.buf, b"")


class Once(unittest.TestCase):
    def test_once_prints_frame_no_serial(self):
        with tempfile.TemporaryDirectory() as home:
            import time
            T = time.time()
            write_session(home, "a", 42, int(T) + 3600, T)
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"CTM_HOME": home}), \
                    mock.patch.object(bridge, "find_port", side_effect=AssertionError), \
                    mock.patch.object(bridge, "open_port", side_effect=AssertionError), \
                    redirect_stdout(out):
                self.assertEqual(bridge.main(["--once"]), 0)
            f = json.loads(out.getvalue())
            self.assertEqual(f["seq"], f["sent_at"])  # max(0 + 1, int(now))
            self.assertEqual(f["windows"]["five_hour"]["pct"], 42)
            self.assertFalse(os.path.exists(os.path.join(home, "serial", "hold.pid")))

    def test_refuses_when_other_holder_alive(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "serial"))
            with open(os.path.join(home, "serial", "hold.pid"), "w") as f:
                f.write(str(os.getppid()))
            with mock.patch.dict(os.environ, {"CTM_HOME": home}), \
                    mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(bridge.main([]), 2)


if __name__ == "__main__":
    unittest.main()
