"""Board selection: config.json, resolution order, select-board, `ctm port`,
bridge with no board, tools/serial_hold.py resolution. Fake port lists only:
no test ever enumerates or opens a real serial port."""
import importlib.util
import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from unittest import mock

from tests._util import REPO, wr
from ctm import board, bridge, cli, device, keystore

SN = "0123456789"
SN2 = "9876543210"


class P(object):
    def __init__(self, device, serial=None, vid=None, pid=None, desc=""):
        self.device, self.serial_number, self.vid, self.pid = device, serial, vid, pid
        self.description = desc


def ports(*ps):
    return lambda: list(ps)


def no_scan():
    raise AssertionError("port scan must not happen")


CH9102 = P("/dev/cu.usbserial-%s1" % SN, SN, 0x1A86, 0x55D4, "USB Single Serial")
CH9102_TTY = P("/dev/tty.usbserial-%s1" % SN, SN, 0x1A86, 0x55D4)
CP2102 = P("/dev/cu.usbserial-0001", SN2, 0x10C4, 0xEA60, "CP2102")
S3 = P("/dev/cu.usbmodem101", "AA:BB:CC:DD:EE:FF", 0x303A, 0x1001, "USB JTAG/serial debug unit")
NOSERIAL = P("/dev/cu.wchusbserial10", None, 0x1A86, 0x7523, "CH340")
BT = P("/dev/cu.Bluetooth-Incoming-Port", None, None, None, "n/a")


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name


class Resolution(Tmp):
    def test_order_flag_env_config_none(self):
        self.assertEqual(board.resolve_serial(None, self.home, {}), (None, "none"))
        board.save_config(self.home, usb_serial=SN)
        self.assertEqual(board.resolve_serial(None, self.home, {}), (SN, "config"))
        env = {"CTM_USB_SERIAL": SN2}
        self.assertEqual(board.resolve_serial(None, self.home, env), (SN2, "env"))
        self.assertEqual(board.resolve_serial("42", self.home, env), ("42", "flag"))

    def test_blank_values_are_skipped(self):
        board.save_config(self.home, usb_serial=SN)
        self.assertEqual(board.resolve_serial("  ", self.home, {"CTM_USB_SERIAL": ""}),
                         (SN, "config"))
        board.save_config(self.home, usb_serial="")
        self.assertEqual(board.resolve_serial(None, self.home, {}), (None, "none"))

    def test_config_merge_and_ctm_home(self):
        wr(board.config_path(self.home), json.dumps({"other": 1}))
        board.save_config(self.home, usb_serial=SN)
        self.assertEqual(board.load_config(self.home), {"other": 1, "usb_serial": SN})
        with mock.patch.dict(os.environ, {"CTM_HOME": self.home}):
            self.assertEqual(board.config_path(), os.path.join(self.home, "config.json"))
            self.assertEqual(board.resolve_serial()[0], SN)

    def test_malformed_config_is_none(self):
        wr(board.config_path(self.home), "[1,2")
        self.assertEqual(board.resolve_serial(None, self.home, {}), (None, "none"))

    def test_no_builtin_default(self):
        import inspect
        import re
        self.assertFalse(hasattr(bridge, "SERIAL_NO"))
        for mod in (board, bridge, cli):   # no hardcoded USB serial literal
            self.assertIsNone(re.search(r"[\"']\d{8,}[\"']", inspect.getsource(mod)))


class Matching(unittest.TestCase):
    def test_exact_prefix_and_ambiguous(self):
        cp = ports(CH9102, CH9102_TTY, CP2102, BT)
        self.assertEqual(board.find_port(SN, cp), CH9102.device)        # cu. only
        self.assertEqual(board.find_port(SN2, cp), CP2102.device)
        self.assertEqual(board.find_port("01234567", cp), CH9102.device)  # unique prefix
        self.assertIsNone(board.find_port("555", cp))
        twin = P("/dev/cu.usbserial-other", SN, 0x1A86, 0x55D4)
        with self.assertRaises(board.BoardError):
            board.find_port(SN, ports(CH9102, twin))

    def test_no_serial_never_scans(self):
        self.assertIsNone(board.find_port(None, no_scan))
        self.assertIsNone(bridge.find_port(None))
        self.assertIsNone(bridge.find_port(""))

    def test_list_filters_known_chips(self):
        cp = ports(CH9102, CH9102_TTY, CP2102, S3, NOSERIAL, BT)
        devs = [d["device"] for d in board.list_ports(False, cp)]
        self.assertEqual(devs, sorted([CH9102.device, CP2102.device, S3.device, NOSERIAL.device]))
        alld = [d["device"] for d in board.list_ports(True, cp)]
        self.assertIn(BT.device, alld)
        self.assertNotIn(CH9102_TTY.device, alld)
        d = board.describe(CH9102)
        self.assertEqual((d["vidpid"], d["chip"]), ("1A86:55D4", "WCH CH9102"))
        self.assertEqual(board.describe(S3)["chip"], "Espressif native USB")


class SelectBoard(Tmp):
    def sel(self, cp, answers, **kw):
        out = io.StringIO()
        asked = []

        def inp(prompt):
            asked.append(prompt)
            return answers.pop(0)
        rc, sn = board.select_board(self.home, input_fn=inp, out=out, comports=cp,
                                    environ={}, **kw)
        return rc, sn, out.getvalue(), asked

    def saved(self):
        return board.load_config(self.home).get("usb_serial")

    def test_single_candidate_requires_yes(self):
        rc, sn, o, asked = self.sel(ports(CH9102, BT), ["y"])
        self.assertEqual((rc, sn, self.saved()), (0, SN, SN))
        self.assertEqual(len(asked), 1)
        self.assertIn("[y/N]", asked[0])
        self.assertIn("1A86:55D4", o)
        self.assertIn("WCH CH9102", o)

    def test_single_candidate_default_is_no(self):
        for ans in ("", "n", "no"):
            rc, sn, o, _ = self.sel(ports(CH9102), [ans])
            self.assertEqual((rc, sn), (1, None))
            self.assertIsNone(self.saved())
            self.assertIn("Nothing changed", o)

    def test_multiple_pick(self):
        rc, sn, o, asked = self.sel(ports(CH9102, CP2102), ["x", "2"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.saved(), SN)   # sorted: usbserial-0001 first, CH9102 second
        self.assertIn("Invalid choice", o)
        rc, sn, o, _ = self.sel(ports(CH9102, CP2102), ["q"])
        self.assertEqual(rc, 1)
        self.assertIn("(current)", o)

    def test_refuses_port_without_serial(self):
        rc, sn, o, asked = self.sel(ports(NOSERIAL), [])
        self.assertEqual(rc, 2)
        self.assertEqual(asked, [])
        self.assertIn("no USB serial number", o)
        self.assertIsNone(self.saved())
        rc, sn, o, _ = self.sel(ports(NOSERIAL, CH9102), ["2"])  # sorted: wchusbserial last
        self.assertEqual(rc, 2)
        self.assertIn("Refused", o)
        self.assertIsNone(self.saved())

    def test_refuses_duplicate_serial(self):
        twin = P("/dev/cu.usbserial-twin", SN, 0x1A86, 0x55D4)
        rc, _sn, o, _ = self.sel(ports(CH9102, twin), ["1"])
        self.assertEqual(rc, 2)
        self.assertIn("same serial", o)
        self.assertIsNone(self.saved())

    def test_nothing_found_suggests_all(self):
        rc, _sn, o, _ = self.sel(ports(BT), [])
        self.assertEqual(rc, 1)
        self.assertIn("--all", o)

    def test_all_lists_other_ports(self):
        rc, _sn, o, _ = self.sel(ports(BT, CH9102), ["q"], show_all=True)
        self.assertIn("Bluetooth", o)

    def test_noninteractive_serial(self):
        rc, sn, o, asked = self.sel(ports(CH9102), [], serial=SN2)
        self.assertEqual((rc, sn, self.saved(), asked), (0, SN2, SN2, []))
        self.assertIn("not plugged in", o)

    def test_noninteractive_present(self):
        rc, sn, o, _ = self.sel(ports(CP2102), [], serial=SN2)
        self.assertIn(CP2102.device, o)

    def test_cli_requires_tty(self):
        err = io.StringIO()
        rc = cli.cmd_select_board(self.home, isatty=lambda: False, err=err, comports=no_scan,
                                  input_fn=lambda p: self.fail("prompted"))
        self.assertEqual(rc, 2)
        self.assertIn("interactive terminal", err.getvalue())
        self.assertIn("--serial", err.getvalue())
        self.assertIsNone(self.saved())
        # --serial works without a TTY
        rc = cli.cmd_select_board(self.home, serial=SN, isatty=lambda: False,
                                  out=io.StringIO(), comports=ports(), environ={})
        self.assertEqual((rc, self.saved()), (0, SN))


class PortCmd(Tmp):
    def run_port(self, cp, serial=None, environ=None):
        out, err = io.StringIO(), io.StringIO()
        rc = cli.cmd_port(self.home, serial=serial, out=out, err=err, comports=cp,
                          environ=environ or {})
        return rc, out.getvalue(), err.getvalue()

    def test_no_board(self):
        rc, o, e = self.run_port(no_scan)
        self.assertEqual((rc, o), (2, ""))
        self.assertIn("select-board", e)

    def test_present_absent_override(self):
        board.save_config(self.home, usb_serial=SN)
        self.assertEqual(self.run_port(ports(CH9102, CP2102))[:2], (0, CH9102.device + "\n"))
        rc, o, e = self.run_port(ports(CP2102))
        self.assertEqual((rc, o), (1, ""))
        self.assertIn("not plugged in", e)
        self.assertEqual(self.run_port(ports(CH9102, CP2102), serial=SN2)[:2],
                         (0, CP2102.device + "\n"))
        self.assertEqual(self.run_port(ports(CH9102, CP2102),
                                       environ={"CTM_USB_SERIAL": SN2})[:2],
                         (0, CP2102.device + "\n"))

    def test_ambiguous(self):
        board.save_config(self.home, usb_serial=SN)
        twin = P("/dev/cu.usbserial-twin", SN, 0x1A86, 0x55D4)
        rc, o, e = self.run_port(ports(CH9102, twin))
        self.assertEqual((rc, o), (2, ""))
        self.assertIn("refusing to guess", e)


class SetupSelectsFirst(Tmp):
    def test_setup_runs_select_when_no_board(self):
        calls = []
        args = Namespace(cmd="setup", dry_run=False, ssid=None, port_serial=None)
        with mock.patch("sys.stdout", io.StringIO()):
            rc = cli.run_pairing_cmd(args, self.home, env=object(), isatty=lambda: True,
                                     select=lambda: calls.append(1) or 1)
        self.assertEqual((rc, calls), (1, [1]))   # aborted select: setup stops, env untouched

    def test_setup_skips_select_with_flag(self):
        from ctm import pairing
        args = Namespace(cmd="setup", dry_run=False, ssid=None, port_serial=SN)
        with mock.patch.object(pairing, "run_setup", return_value=0) as rs:
            rc = cli.run_pairing_cmd(args, self.home, env=mock.Mock(),
                                     isatty=lambda: True,
                                     select=lambda: self.fail("select-board ran"))
        self.assertEqual(rc, 0)
        rs.assert_called_once()

    def test_pairing_env_resolves_config(self):
        from ctm import pairing
        board.save_config(self.home, usb_serial=SN)
        self.assertEqual(pairing.Env(home=self.home, keystore=keystore.MemoryKeystore()).serial_no,
                         SN)
        self.assertEqual(pairing.Env(home=self.home, serial_no=SN2).serial_no, SN2)


class FakeClock(object):
    def __init__(self):
        self.t = 1791180000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class BridgeNoBoard(Tmp):
    def make(self, **kw):
        c = FakeClock()
        b = bridge.Bridge(home=self.home, clock=c, sleep=c.sleep,
                          open_port=lambda dev: self.fail("open_port(%s) called" % dev),
                          keystore=keystore.MemoryKeystore(), **kw)
        return b, c

    def logtext(self):
        out = ""
        d = os.path.join(self.home, "serial")
        for n in sorted(os.listdir(d)):
            if n.endswith(".log"):
                with open(os.path.join(d, n)) as f:
                    out += f.read()
        return out

    def test_never_scans_or_opens_and_logs_once(self):
        with mock.patch.object(board, "_comports", side_effect=AssertionError("scanned")):
            for transport in ("auto", "usb"):
                b, _c = self.make(transport=transport)
                for _ in range(20):
                    b.step()
                self.assertIsNone(b.port)
                self.assertIn("no board selected", b._state)
        self.assertEqual(self.logtext().count("no board selected"), 2)   # once per bridge

    def test_wifi_only_when_paired(self):
        class W(object):
            host, cached_ip = "ctmeter-a1b2", None
            sent = []

            def post_state(self, body):
                from ctm import wifi
                self.sent.append(body)
                return wifi.Result("ack", 200, None, None, "192.0.2.50")

            def mark_stale(self):
                pass
        device.save(self.home, "ctmeter-a1b2", "192.0.2.50")
        b, _c = self.make(wifi_factory=lambda h, k, ip: W())
        b.keystore.set("ctmeter-a1b2", "ab" * 32)
        with mock.patch.object(board, "_comports", side_effect=AssertionError("scanned")):
            for _ in range(5):
                b.step()
        self.assertEqual(b.active, "wifi")
        self.assertTrue(W.sent)

    def test_picks_up_select_board_without_restart(self):
        b, _c = self.make(transport="usb")
        opened = []
        b.open_port = lambda dev: opened.append(dev) or self.fail("stop here")
        with mock.patch.object(board, "_comports", side_effect=AssertionError("scanned")):
            b.step()
        board.save_config(self.home, usb_serial=SN)
        with mock.patch.object(board, "_comports", ports(CP2102)):
            b.step()      # board selected but not plugged in: other boards untouched
        self.assertEqual(opened, [])
        self.assertEqual(b.serial_no, SN)
        self.assertIn("usb_serial none -> %s (config)" % SN, self.logtext())

    def test_flag_beats_env_and_config(self):
        board.save_config(self.home, usb_serial=SN)
        with mock.patch.dict(os.environ, {"CTM_USB_SERIAL": "1111"}):
            self.assertEqual(self.make()[0].serial_no, "1111")
            self.assertEqual(self.make(usb_serial=SN2)[0].serial_no, SN2)
        self.assertEqual(self.make()[0].serial_no, SN)

    def test_main_accepts_usb_serial_flag(self):
        with mock.patch.dict(os.environ, {"CTM_HOME": self.home}), \
                mock.patch.object(bridge, "Bridge") as B, \
                mock.patch("sys.stdout", io.StringIO()):
            B.return_value.current_frame.return_value = {}
            bridge.main(["--once", "--usb-serial", SN])
        self.assertEqual(B.call_args[1]["usb_serial"], SN)


class SerialHold(Tmp):
    def load(self):
        spec = importlib.util.spec_from_file_location(
            "serial_hold_under_test", os.path.join(REPO, "tools", "serial_hold.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)   # pyserial is imported lazily: nothing opened here
        return m

    def test_resolution(self):
        m = self.load()
        self.assertIsNone(m.resolve_serial([], {}, self.home))
        board.save_config(self.home, usb_serial=SN)
        self.assertEqual(m.resolve_serial([], {}, self.home), SN)
        self.assertEqual(m.resolve_serial([], {"CTM_USB_SERIAL": SN2}, self.home), SN2)
        self.assertEqual(m.resolve_serial(["--serial", "42"], {"CTM_USB_SERIAL": SN2},
                                          self.home), "42")
        self.assertEqual(m.resolve_serial(["--serial=43"], {}, self.home), "43")

    def test_exits_without_board(self):
        m = self.load()
        with mock.patch.object(m, "resolve_serial", return_value=None), \
                mock.patch("sys.stderr", io.StringIO()) as err, \
                self.assertRaises(SystemExit) as cm:
            m.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("select-board", err.getvalue())


if __name__ == "__main__":
    unittest.main()
