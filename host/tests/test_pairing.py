import glob
import io
import json
import os
import signal
import tempfile
import unittest
from argparse import Namespace

from tests._util import wr
from ctm import cli, device, keystore, pairing

PASSWORD = "s3cret-Pa55word!"
KEY = bytes(range(100, 132))


class Clock(object):
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeDevice(object):
    """Fake serial port speaking PROTOCOL §4.3. Reads advance the fake clock
    like a blocking 0.5s read timeout."""

    def __init__(self, clock, events, hello=True, provision=None, scan=True, unpair=True):
        self.clock = clock
        self.events = events
        self.rx = []
        self.written = b""
        self.cmds = []
        self.closed = False
        self.provision = provision or {"t": "provision", "ok": True,
                                       "host": "ctmeter-a1b2", "ip": "192.168.1.77"}
        self.scan = scan
        self.unpair = unpair
        if hello:
            self.rx.append(b"\xff\x00ets Jun  8 2016 rst:0x1 (POWERON)\r\n[diag] boot\r\n"
                           b"garbage@{\"t\":\"hello\",\"fw\":\"0.2.0\",\"board\":\"t-display\"}\r\n")

    def write(self, b):
        self.written += b
        for line in b.split(b"\n"):
            if not line.strip():
                continue
            cmd = json.loads(line)
            self.cmds.append(cmd)
            t = cmd["t"]
            if t == "scan" and self.scan:
                self.reply({"t": "scan", "nets": [
                    {"ssid": "Neighbor", "rssi": -80, "open": False},
                    {"ssid": "HomeNet", "rssi": -48, "open": False},
                    {"ssid": "Cafe", "rssi": -70, "open": True}]})
            elif t == "provision":
                self.rx.append(b"[wifi] connecting...\r\n")
                self.reply(self.provision)
            elif t == "info":
                self.reply({"t": "info", "fw": "0.2.0", "wifi": "connected", "provisioned": True})
            elif t == "unpair" and self.unpair:
                self.reply({"t": "unpair", "ok": True})

    def reply(self, obj):
        self.rx.append(("@" + json.dumps(obj) + "\r\n").encode())

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def read(self, n):
        if self.rx:
            return self.rx.pop(0)
        self.clock.t += 0.5
        return b""

    def close(self):
        self.closed = True
        self.events.append("close")


class FakePauser(object):
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        self.events.append("pause")
        return self

    def __exit__(self, *exc):
        self.events.append("resume")
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.clock = Clock()
        self.events = []
        self.out = io.StringIO()
        self.answers = []
        self.getpass_calls = []
        self.ks = keystore.MemoryKeystore()
        self.present = True
        self.dev_kwargs = {}
        self.devs = []

        def inp(prompt):
            self.out.write(prompt)
            a = self.answers.pop(0)
            if isinstance(a, BaseException):
                raise a
            return a

        def gp(prompt):
            self.getpass_calls.append(prompt)
            return PASSWORD

        def opener(dev):
            self.events.append("open")
            d = FakeDevice(self.clock, self.events, **self.dev_kwargs)
            self.devs.append(d)
            return d

        self.env = pairing.Env(
            home=self.home, keystore=self.ks,
            find_port=lambda: "/dev/cu.fake" if self.present else None,
            open_port=opener, pauser=lambda: FakePauser(self.events),
            console=pairing.Console(inp, gp, self.out), clock=self.clock,
            token=lambda n: KEY[:n])

    def assert_no_secret_leak(self):
        text = self.out.getvalue()
        for root, _dirs, files in os.walk(self.home):
            for f in files:
                with open(os.path.join(root, f), "rb") as fh:
                    text += fh.read().decode("utf-8", "replace")
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn(KEY.hex(), text)

    def assert_paused_then_resumed_after_close(self):
        self.assertEqual(self.events[0], "pause")
        self.assertEqual(self.events[-1], "resume")
        if "open" in self.events:
            self.assertLess(self.events.index("close"), self.events.index("resume"))


class Setup(Base):
    def test_happy_path(self):
        self.answers = ["x", "2"]
        rc = pairing.run_setup(self.env)
        self.assertEqual(rc, 0, self.out.getvalue())
        d = self.devs[0]
        self.assertEqual([c["t"] for c in d.cmds], ["scan", "provision"])
        prov = d.cmds[1]
        self.assertEqual(prov["ssid"], "HomeNet")
        self.assertEqual(prov["pass"], PASSWORD)
        self.assertEqual(prov["key"], KEY.hex())
        self.assertTrue(d.written.startswith(b"\n{"))
        self.assertEqual(len(self.getpass_calls), 1)
        rec = device.load(self.home)
        self.assertEqual((rec["host"], rec["ip"]), ("ctmeter-a1b2", "192.168.1.77"))
        self.assertNotIn("key", rec)
        self.assertEqual(self.ks.get("ctmeter-a1b2"), KEY.hex())
        out = self.out.getvalue()
        self.assertIn("HomeNet", out)
        self.assertIn("\U0001F512", out)
        self.assertIn("▂▄▆█", out)    # -48 dBm: 4 bars
        self.assertIn("Invalid choice", out)
        self.assertIn("Paired: ctmeter-a1b2.local  ip 192.168.1.77", out)
        self.assertIn("fw 0.2.0", out)
        self.assert_no_secret_leak()
        self.assert_paused_then_resumed_after_close()
        self.assertTrue(d.closed)

    def test_open_network_skips_password(self):
        self.answers = ["3"]
        self.assertEqual(pairing.run_setup(self.env), 0)
        self.assertEqual(self.getpass_calls, [])
        self.assertEqual(self.devs[0].cmds[1]["pass"], "")

    def test_manual_hidden_ssid(self):
        self.answers = ["m", "Hidden Net", "n"]
        self.assertEqual(pairing.run_setup(self.env), 0)
        self.assertEqual(self.devs[0].cmds[1]["ssid"], "Hidden Net")
        self.assertEqual(len(self.getpass_calls), 1)
        self.assert_no_secret_leak()

    def test_no_hello_proceeds_after_timeout(self):
        self.dev_kwargs = {"hello": False}
        self.answers = ["2"]
        t0 = self.clock.t
        self.assertEqual(pairing.run_setup(self.env), 0)
        self.assertGreaterEqual(self.clock.t - t0, pairing.HELLO_TIMEOUT_S)

    def test_provision_failure_hints_nothing_saved(self):
        self.dev_kwargs = {"provision": {"t": "provision", "ok": False, "err": "auth"}}
        self.answers = ["2"]
        self.assertEqual(pairing.run_setup(self.env), 4)
        out = self.out.getvalue()
        self.assertIn("Provisioning failed: auth", out)
        self.assertIn("password is probably wrong", out)
        self.assertIsNone(device.load(self.home))
        self.assertEqual(self.ks.items, {})
        self.assert_no_secret_leak()
        self.assert_paused_then_resumed_after_close()

    def test_provision_no_reply(self):
        self.dev_kwargs = {"provision": {"t": "other"}}
        self.answers = ["2"]
        t0 = self.clock.t
        self.assertEqual(pairing.run_setup(self.env), 3)
        self.assertGreaterEqual(self.clock.t - t0, pairing.PROVISION_TIMEOUT_S)
        self.assertIsNone(device.load(self.home))
        self.assert_paused_then_resumed_after_close()

    def test_scan_no_reply_raises_and_resumes(self):
        self.dev_kwargs = {"scan": False}
        with self.assertRaises(pairing.SetupError):
            pairing.run_setup(self.env)
        self.assert_paused_then_resumed_after_close()
        self.assertTrue(self.devs[0].closed)

    def test_ctrl_c_resumes(self):
        self.answers = [KeyboardInterrupt()]
        args = Namespace(cmd="setup", dry_run=False, ssid=None, port_serial=None)
        self.assertEqual(cli.run_pairing_cmd(args, self.home, env=self.env), 130)
        self.assert_paused_then_resumed_after_close()
        self.assertEqual(self.ks.items, {})

    def test_ctrl_c_at_password_resumes(self):
        self.answers = ["2"]
        self.env.console.getpass = lambda p: (_ for _ in ()).throw(KeyboardInterrupt())
        args = Namespace(cmd="setup", dry_run=False, ssid=None, port_serial=None)
        self.assertEqual(cli.run_pairing_cmd(args, self.home, env=self.env), 130)
        self.assert_paused_then_resumed_after_close()
        self.assertEqual([c["t"] for c in self.devs[0].cmds], ["scan"])

    def test_quit(self):
        self.answers = ["q"]
        self.assertEqual(pairing.run_setup(self.env), 1)
        self.assertEqual([c["t"] for c in self.devs[0].cmds], ["scan"])
        self.assert_paused_then_resumed_after_close()

    def test_device_absent(self):
        self.present = False
        self.assertEqual(pairing.run_setup(self.env), 2)
        self.assertEqual(self.events, [])

    def test_open_failure_resumes(self):
        def bad_open(dev):
            raise OSError(16, "Resource busy")
        self.env.open_port = bad_open
        with self.assertRaises(pairing.SetupError):
            pairing.run_setup(self.env)
        self.assertEqual(self.events, ["pause", "resume"])

    def test_repair_replaces_old_keychain_item(self):
        device.save(self.home, "ctmeter-old0", "1.2.3.4")
        self.ks.set("ctmeter-old0", "11" * 32)
        self.answers = ["2"]
        self.assertEqual(pairing.run_setup(self.env), 0)
        self.assertIsNone(self.ks.get("ctmeter-old0"))
        self.assertEqual(device.load(self.home)["host"], "ctmeter-a1b2")

    def test_dry_run_masks_and_opens_nothing(self):
        out = io.StringIO()
        self.env.console.out = out
        self.assertEqual(pairing.run_setup(self.env, dry_run=True, ssid="HomeNet"), 0)
        self.assertEqual(self.events, [])
        text = out.getvalue()
        self.assertIn('"pass": "********"', text)
        self.assertIn(pairing.KEY_MASK, text)
        self.assertIn('"ssid": "HomeNet"', text)
        self.assertNotIn(KEY.hex(), text)

    def test_masked(self):
        s = pairing.masked({"t": "provision", "ssid": "a", "pass": PASSWORD, "key": KEY.hex()})
        self.assertNotIn(PASSWORD, s)
        self.assertNotIn(KEY.hex(), s)


class UnpairInfo(Base):
    def test_unpair_usb(self):
        device.save(self.home, "ctmeter-a1b2", "1.2.3.4")
        self.ks.set("ctmeter-a1b2", KEY.hex())
        self.assertEqual(pairing.run_unpair(self.env), 0)
        self.assertEqual(self.devs[0].cmds, [{"t": "unpair"}])
        self.assertIsNone(device.load(self.home))
        self.assertIsNone(self.ks.get("ctmeter-a1b2"))
        self.assert_paused_then_resumed_after_close()

    def test_unpair_no_confirm_keeps_local(self):
        device.save(self.home, "ctmeter-a1b2", "1.2.3.4")
        self.ks.set("ctmeter-a1b2", KEY.hex())
        self.dev_kwargs = {"unpair": False}
        self.assertEqual(pairing.run_unpair(self.env), 3)
        self.assertIsNotNone(device.load(self.home))
        self.assertIsNotNone(self.ks.get("ctmeter-a1b2"))

    def test_unpair_absent_and_local(self):
        device.save(self.home, "ctmeter-a1b2", "1.2.3.4")
        self.ks.set("ctmeter-a1b2", KEY.hex())
        self.present = False
        self.assertEqual(pairing.run_unpair(self.env), 2)
        self.assertIsNotNone(device.load(self.home))
        self.assertEqual(pairing.run_unpair(self.env, local_only=True), 0)
        self.assertIsNone(device.load(self.home))
        self.assertEqual(self.ks.items, {})
        self.assertEqual(self.events, [])

    def test_info_usb(self):
        self.assertEqual(pairing.run_info(self.env), 0)
        self.assertEqual(self.devs[0].cmds, [{"t": "info"}])
        self.assertIn('"wifi": "connected"', self.out.getvalue())
        self.assert_paused_then_resumed_after_close()

    def test_info_wifi_when_usb_absent(self):
        self.present = False
        device.save(self.home, "ctmeter-a1b2", "1.2.3.4")
        calls = []

        class C(object):
            def __init__(self, host, ip):
                calls.append((host, ip))

            def get_info(self):
                return {"t": "info", "host": "ctmeter-a1b2", "rssi": -55}, "1.2.3.4", None

        self.env.wifi_factory = C
        self.assertEqual(pairing.run_info(self.env), 0)
        self.assertEqual(calls, [("ctmeter-a1b2", "1.2.3.4")])
        self.assertIn('"rssi": -55', self.out.getvalue())
        self.assertEqual(self.events, [])

    def test_info_nothing(self):
        self.present = False
        self.assertEqual(pairing.run_info(self.env), 2)


class Pauser(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        os.makedirs(os.path.join(self.home, "serial"))
        self.state = os.path.join(self.home, "serial", "hold.state")
        wr(self.state, "2026-10-05 10:00:00 HELD /dev/cu.fake\n")
        self.kills = []
        self.clock = Clock()
        self.paused = False
        self.responsive = True

    def kill(self, pid, sig):
        self.kills.append((pid, sig))
        if self.responsive:
            self.paused = not self.paused
            wr(self.state, "x y %s\n" % ("PAUSED" if self.paused else "HELD /dev/cu.fake"))

    def pauser(self, holder=4242):
        return pairing.BridgePauser(self.home, kill=self.kill, holder=lambda: holder,
                                    clock=self.clock, sleep=self.clock.sleep,
                                    out=io.StringIO())

    def test_pause_resume(self):
        with self.pauser():
            self.assertTrue(self.paused)
        self.assertFalse(self.paused)
        self.assertEqual(self.kills, [(4242, signal.SIGUSR1)] * 2)

    def test_resume_on_exception(self):
        for exc in (RuntimeError("boom"), KeyboardInterrupt()):
            self.kills = []
            with self.assertRaises(type(exc)):
                with self.pauser():
                    raise exc
            self.assertFalse(self.paused)
            self.assertEqual(len(self.kills), 2)

    def test_timeout_still_resumes(self):
        self.responsive = False
        with self.assertRaises(pairing.SetupError):
            with self.pauser():
                self.fail("body must not run")
        self.assertEqual(len(self.kills), 2)   # pause + toggle back
        self.assertGreaterEqual(self.clock.t - 1000.0, pairing.PAUSE_TIMEOUT_S)

    def test_no_holder(self):
        with self.pauser(holder=None):
            pass
        self.assertEqual(self.kills, [])

    def test_already_paused_left_alone(self):
        wr(self.state, "x y PAUSED\n")
        with self.pauser():
            pass
        self.assertEqual(self.kills, [])


class Status(unittest.TestCase):
    def test_status_transport_and_pairing(self):
        with tempfile.TemporaryDirectory() as home:
            ks = keystore.MemoryKeystore()
            out = io.StringIO()
            cli.cmd_status(home, out, keystore=ks, find_port=lambda: None)
            self.assertIn("transport: none", out.getvalue())
            self.assertIn("usb device: not present", out.getvalue())
            self.assertIn("paired: no", out.getvalue())
            os.makedirs(os.path.join(home, "serial"))
            wr(os.path.join(home, "serial", "hold.state"),
               "2026-10-05 10:00:00 WIFI ctmeter-a1b2 192.168.1.5 ok seq=1791180000\n")
            device.save(home, "ctmeter-a1b2", "192.168.1.5")
            ks.set("ctmeter-a1b2", KEY.hex())
            out = io.StringIO()
            cli.cmd_status(home, out, keystore=ks, find_port=lambda: None)
            text = out.getvalue()
            self.assertIn("transport: wifi", text)
            self.assertIn("paired: ctmeter-a1b2.local  ip 192.168.1.5", text)
            self.assertIn("key in Keychain: yes", text)
            self.assertNotIn(KEY.hex(), text)
            wr(os.path.join(home, "serial", "hold.state"), "2026-10-05 10:00:00 HELD /dev/cu.x\n")
            out = io.StringIO()
            cli.cmd_status(home, out, keystore=ks, find_port=lambda: "/dev/cu.x")
            self.assertIn("transport: usb", out.getvalue())
            self.assertIn("usb device: /dev/cu.x", out.getvalue())


if __name__ == "__main__":
    unittest.main()
