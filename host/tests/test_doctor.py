"""ctm doctor with fakes: temp settings.json / CTM_HOME / LaunchAgents dir,
fake launchctl, MemoryKeystore, fake USB scan and fake /v1/info."""
import io
import json
import os
import tempfile
import time
import unittest

from tests._util import wr
from tests.test_install import FakeOps
from ctm import board, device, doctor, install, keystore

KEY_HEX = "ab" * 32


class FakeInfoClient(object):
    def __init__(self, info, err=None):
        self.info, self.err = info, err

    def get_info(self):
        return self.info, "192.0.2.50", self.err


class Doctor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.home = os.path.join(t, "home")
        self.host = os.path.join(t, "repo", "host")
        os.makedirs(os.path.join(self.host, "ctm"))
        wr(os.path.join(self.host, "ctm", "statusline.py"), "# fake\n")
        self.paths = install.Paths(home=self.home, launch_agents=os.path.join(t, "LA"),
                                   claude_settings=os.path.join(t, "claude", "settings.json"),
                                   host_dir=self.host, uid=501)
        self.ks = keystore.MemoryKeystore()
        self.usb = None
        self.info = {"t": "info", "fw": "0.2.0", "rssi": -40, "uptime_s": 99}
        self.info_err = None
        self.make_healthy()

    def tearDown(self):
        self.tmp.cleanup()

    def env(self, ops=None):
        return doctor.Env(paths=self.paths, ops=ops or self.ops, keystore=self.ks,
                          find_port=lambda: self.usb,
                          wifi_factory=lambda h, ip: FakeInfoClient(
                              None if self.info_err else self.info, self.info_err))

    def make_healthy(self):
        now = time.time()
        os.makedirs(os.path.dirname(self.paths.claude_settings))
        wr(self.paths.claude_settings, json.dumps({"statusLine": {
            "type": "command", "command": install.statusline_command(self.paths)}}))
        sd = os.path.join(self.home, "sessions")
        os.makedirs(sd)
        wr(os.path.join(sd, "s1.json"), json.dumps({
            "session_id": "s1", "observed_at": now,
            "rate_limits": {"five_hour": {"used_percentage": 52, "resets_at": int(now) + 3600}}}))
        serial = os.path.join(self.home, "serial")
        os.makedirs(serial)
        wr(os.path.join(serial, "hold.pid"), str(os.getppid()))   # a live pid
        wr(os.path.join(serial, "hold.state"), "2026-10-05 10:00:00 WIFI ctmeter-a1b2 192.0.2.50 ok seq=5\n")
        wr(os.path.join(serial, time.strftime("%Y%m%d") + ".log"),
           "%s >> wifi {...}  (heartbeat)\n%s << wifi ack seq=5 ip=192.0.2.50\n"
           % ((time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 30)),) * 2))
        os.makedirs(self.paths.launch_agents)
        wr(self.paths.plist, "<plist/>")
        self.ops = FakeOps(loaded=True, job_pid=os.getppid())
        device.save(self.home, "ctmeter-a1b2", "192.0.2.50", 1791100000)
        self.ks.set("ctmeter-a1b2", KEY_HEX)
        board.save_config(self.home, usb_serial="0123456789")

    def run_doctor(self, ops=None):
        out = io.StringIO()
        rc = doctor.run(self.env(ops), out)
        return rc, out.getvalue()

    def line(self, text, name):
        for l in text.splitlines():
            if l[6:].startswith(name + " "):
                return l
        self.fail("no line for %s in\n%s" % (name, text))

    def test_all_pass(self):
        rc, o = self.run_doctor()
        self.assertEqual(rc, 0, o)
        self.assertIn("verdict: OK", o)
        self.assertEqual(o.count("PASS"), len(doctor.CHECKS))
        self.assertNotIn(KEY_HEX, o)
        self.assertIn("present for ctmeter-a1b2 (not shown)", o)
        self.assertIn("five_hour 52%", o)
        # read-only: only `launchctl print`
        self.assertEqual(set(self.ops.verbs()), {"print"})

    def test_statusline_elsewhere_fails(self):
        wr(self.paths.claude_settings, json.dumps({"statusLine": {"type": "command",
                                                                  "command": "other.sh"}}))
        rc, o = self.run_doctor()
        self.assertEqual(rc, 1)
        self.assertTrue(self.line(o, "statusline").startswith("FAIL"))
        self.assertIn("other.sh", o)
        self.assertIn("verdict: FAIL (statusline)", o)

    def test_missing_key_fails_without_printing(self):
        self.ks.delete("ctmeter-a1b2")
        rc, o = self.run_doctor()
        self.assertEqual(rc, 1)
        self.assertTrue(self.line(o, "pairing key").startswith("FAIL"))

    def test_bridge_dead_and_agent_not_loaded(self):
        wr(os.path.join(self.home, "serial", "hold.pid"), "999999")
        rc, o = self.run_doctor(FakeOps(loaded=False))
        self.assertEqual(rc, 1)
        self.assertTrue(self.line(o, "bridge").startswith("FAIL"))
        self.assertTrue(self.line(o, "launchagent").startswith("FAIL"))

    def test_hand_started_bridge_flagged(self):
        rc, o = self.run_doctor(FakeOps(loaded=True, job_pid=4242))
        self.assertTrue(self.line(o, "launchagent").startswith("FAIL"))
        self.assertIn("--replace", o)

    def test_stale_ack_fails(self):
        p = os.path.join(self.home, "serial", time.strftime("%Y%m%d") + ".log")
        old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 3600))
        wr(p, '%s @{"t":"ack","seq":3}\n' % old)
        rc, o = self.run_doctor()
        self.assertEqual(rc, 1)
        self.assertTrue(self.line(o, "last ack").startswith("FAIL"))

    def test_usb_ack_found(self):
        p = os.path.join(self.home, "serial", time.strftime("%Y%m%d") + ".log")
        wr(p, '%s @{"t":"ack","seq":3}\n%s [diag] x\n' % ((time.strftime("%Y-%m-%d %H:%M:%S"),) * 2))
        rc, o = self.run_doctor()
        self.assertTrue(self.line(o, "last ack").startswith("PASS"))

    def test_device_unreachable_wifi_but_usb_present(self):
        self.info_err = "timeout"
        self.usb = "/dev/cu.fake"
        rc, o = self.run_doctor()
        self.assertTrue(self.line(o, "device").startswith("PASS"))
        self.info_err = "timeout"
        self.usb = None
        rc, o = self.run_doctor()
        self.assertTrue(self.line(o, "device").startswith("FAIL"))

    def test_board_configured_absent_and_present(self):
        rc, o = self.run_doctor()
        self.assertEqual(rc, 0, o)
        self.assertIn("usb_serial 0123456789 (config): not plugged in", self.line(o, "board"))
        self.usb = "/dev/cu.usbserial-01234567891"
        rc, o = self.run_doctor()
        l = self.line(o, "board")
        self.assertTrue(l.startswith("PASS"), l)
        self.assertIn("/dev/cu.usbserial-01234567891", l)

    def test_no_board_selected_fails(self):
        os.unlink(board.config_path(self.home))
        rc, o = self.run_doctor()
        self.assertEqual(rc, 1)
        l = self.line(o, "board")
        self.assertTrue(l.startswith("FAIL"), l)
        self.assertIn("ctm select-board", l)

    def test_ambiguous_board_fails(self):
        def amb():
            raise board.BoardError("2 ports match USB serial 0123456789 (a, b); refusing to guess")
        e = self.env()
        e.find_port = amb
        out = io.StringIO()
        self.assertEqual(doctor.run(e, out), 1)
        self.assertTrue(self.line(out.getvalue(), "board").startswith("FAIL"))

    def test_crashing_check_means_no_verdict(self):
        def boom(env):
            raise RuntimeError("kaboom")
        out = io.StringIO()
        rc = doctor.run(self.env(), out, checks=doctor.CHECKS + (("extra", boom),))
        self.assertEqual(rc, 2)
        self.assertIn("SKIP  extra", out.getvalue())
        self.assertIn("verdict: NONE", out.getvalue())
        self.assertNotIn("verdict: OK", out.getvalue())

    def test_keychain_error_is_skip(self):
        class Broken(object):
            def get(self, host):
                raise RuntimeError("security exit 51")
        self.ks = Broken()
        rc, o = self.run_doctor()
        self.assertEqual(rc, 2)
        self.assertTrue(self.line(o, "pairing key").startswith("SKIP"))

    def test_no_sessions(self):
        os.unlink(os.path.join(self.home, "sessions", "s1.json"))
        rc, o = self.run_doctor()
        self.assertTrue(self.line(o, "sessions").startswith("FAIL"))

    def test_last_ack_scans_older_days(self):
        sd = os.path.join(self.home, "serial")
        os.unlink(os.path.join(sd, time.strftime("%Y%m%d") + ".log"))
        wr(os.path.join(sd, "20261001.log"), '2026-10-01 09:00:00 @{"t":"ack","seq":1}\n')
        t, _ = doctor.last_ack(sd)
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.localtime(t)), "2026-10-01 09:00")


if __name__ == "__main__":
    unittest.main()
