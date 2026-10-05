"""P5 bridge robustness: sleep/wake & clock jumps, IP change, USB boot race,
log hygiene. Fakes only: no serial port, no network, no Keychain."""
import errno
import json
import os
import socket
import tempfile
import time
import unittest

from tests._util import rd, wr
from tests.test_bridge import Base, write_session
from tests.test_bridge_wifi import WifiBase, KEY_HEX
from ctm import bridge, device, keystore, wifi


class WakeUsb(Base):
    def setUp(self):
        super(WakeUsb, self).setUp()
        self.mono_off = 0.0       # wall - mono; grows while the "Mac sleeps"
        self.b.mono = lambda: self.clock.t - self.mono_off

    def sleep_mac(self, seconds):
        """System sleep: wall clock advances, monotonic does not (macOS)."""
        self.clock.t += seconds
        self.mono_off += seconds

    def test_no_false_positive_in_normal_operation(self):
        T = self.clock.t
        write_session(self.home, "a", 50, int(T) + 3600, T)
        self.run_for(400)
        self.assertNotIn("wake/clock jump", self.logtext())

    def test_sleep_wake_resends_immediately(self):
        T = self.clock.t
        write_session(self.home, "a", 50, int(T) + 7200, T)
        self.run_for(10)
        self.assertEqual(len(self.all_frames()), 1)
        self.sleep_mac(60)        # short sleep: heartbeat (120s) not yet due
        self.b.step()
        fr = self.all_frames()
        self.assertEqual(len(fr), 2)
        self.assertEqual(self.b.last_reason, "force")
        self.assertIn("[bridge] wake/clock jump detected (wall +", self.logtext())
        self.assertTrue(self.ports[-1].tx[-1].startswith(b"\n{"))   # leading newline kept

    def test_wall_clock_set_backwards(self):
        self.run_for(10)
        self.clock.t -= 100       # e.g. NTP correction; mono unaffected
        self.mono_off -= 100
        self.b.step()
        self.assertIn("wake/clock jump detected (wall -", self.logtext())
        fr = self.all_frames()
        self.assertEqual(len(fr), 2)
        self.assertGreater(fr[1]["seq"], fr[0]["seq"])   # seq never goes backwards

    def test_long_gap_without_divergence(self):
        # process stopped (SIGSTOP / debugger): both clocks move, gap > GAP_S
        self.run_for(10)
        self.clock.t += bridge.GAP_S + 5
        self.b.step()
        self.assertIn("wake/clock jump detected", self.logtext())
        self.assertEqual(len(self.all_frames()), 2)

    def test_small_divergence_ignored(self):
        self.run_for(10)
        self.sleep_mac(bridge.JUMP_S - 5)
        self.b.step()
        self.assertNotIn("wake/clock jump", self.logtext())


class WakeWifi(WifiBase):
    def setUp(self):
        super(WakeWifi, self).setUp()
        self.mono_off = 0.0
        self.b.mono = lambda: self.clock.t - self.mono_off

    def test_wake_resets_backoff_and_distrusts_ip(self):
        self.pair()
        self.b.wifi_ready()
        self.w.stale_marks = 0

        def mark():
            self.w.stale_marks += 1
        self.w.mark_stale = mark
        self.w.replies.extend([wifi.Result("net", err="timeout", ip="192.168.1.40",
                                           errno_=errno.ETIMEDOUT)] * 4)
        self.run_for(40)          # fails at 0, 5, 15, 35 -> next try at 75
        n = len(self.w.bodies)
        self.assertEqual(n, 4)
        self.assertGreater(self.b.backoff.failures, 0)
        self.clock.t += 3600      # Mac slept for an hour
        self.mono_off += 3600
        self.b.step()
        self.assertEqual(len(self.w.bodies), n + 1)   # sent at once, no backoff wait
        self.assertEqual(self.b.backoff.failures, 0)  # ack reset it
        self.assertGreaterEqual(self.w.stale_marks, 1)
        self.assertIn("wake/clock jump detected", self.logtext())
        self.assertIn("ok seq=", self.state())


class IpChange(unittest.TestCase):
    """WifiClient: after a failure the cached IP is not trusted; <host>.local
    is re-resolved before every retry."""

    def setUp(self):
        self.mdns = None            # what the resolver answers
        self.up = {"10.0.0.7"}      # addresses where the device answers
        self.calls = []

        def resolver(name):
            return self.mdns

        def request(ip, port, method, path, body, headers, timeout):
            self.calls.append(ip)
            if ip not in self.up:
                raise OSError(errno.ECONNREFUSED, "Connection refused")
            return 200, json.dumps({"t": "ack", "seq": 1}).encode()

        self.c = wifi.WifiClient("ctmeter-a1b2", b"k" * 32, "10.0.0.5",
                                 resolver=resolver, request=request)

    def test_failure_then_new_ip_via_mdns(self):
        r = self.c.post_state(b"{}")               # mDNS down -> cache -> refused
        self.assertEqual((r.kind, r.ip), ("net", "10.0.0.5"))
        self.assertTrue(self.c.stale)
        r = self.c.post_state(b"{}")               # mDNS still down: cache NOT used
        self.assertEqual(r.kind, "resolve")
        self.assertIn("not trusted", r.err)
        self.assertEqual(self.calls, ["10.0.0.5"])
        self.mdns = "10.0.0.7"                     # device answers mDNS at its new IP
        r = self.c.post_state(b"{}")
        self.assertEqual((r.kind, r.ip), ("ack", "10.0.0.7"))
        self.assertFalse(self.c.stale)

    def test_stale_cache_is_probed_periodically(self):
        self.c.post_state(b"{}")                   # fail -> stale
        self.up.add("10.0.0.5")                    # device came back at the same IP
        kinds = [self.c.post_state(b"{}").kind for _ in range(wifi.STALE_PROBE_EVERY)]
        self.assertEqual(kinds[-1], "ack")         # mDNS never works, probe still recovers
        self.assertTrue(all(k == "resolve" for k in kinds[:-1]))
        self.assertFalse(self.c.stale)

    def test_timeout_and_unreachable_mark_stale(self):
        for exc in (socket.timeout(), OSError(errno.EHOSTUNREACH, "No route to host")):
            def request(*a, **k):
                raise exc
            c = wifi.WifiClient("h", b"k" * 32, "10.0.0.5", resolver=lambda n: None,
                                request=request)
            self.assertEqual(c.post_state(b"{}").kind, "net")
            self.assertTrue(c.stale)

    def test_bridge_updates_device_json_after_ip_change(self):
        with tempfile.TemporaryDirectory() as home:
            device.save(home, "ctmeter-a1b2", "10.0.0.5", 1791100000)
            ks = keystore.MemoryKeystore()
            ks.set("ctmeter-a1b2", KEY_HEX)
            t = [1791180000.0]

            def sleep(s):
                t[0] += s

            def factory(host, key, ip):
                return wifi.WifiClient(host, key, ip, resolver=lambda n: self.mdns,
                                       request=self.c.request)
            b = bridge.Bridge(home=home, find_port=lambda: None, clock=lambda: t[0],
                              sleep=sleep, keystore=ks, wifi_factory=factory)
            for _ in range(3):
                b.step()
            self.assertIn("backoff", rd(b.state_path))
            self.mdns = "10.0.0.7"
            for _ in range(30):
                b.step()
            self.assertEqual(device.load(home)["ip"], "10.0.0.7")
            self.assertIn("ok seq=", rd(b.state_path))


class HelloDefer(Base):
    HELLO_WAIT = bridge.USB_HELLO_WAIT_S   # the real default

    def test_waits_for_hello(self):
        T = self.clock.t
        write_session(self.home, "a", 50, int(T) + 3600, T)
        self.run_for(1)
        self.assertEqual(self.all_frames(), [])      # board still booting
        self.ports[0].rx.append(b'\x00\xff@{"t":"hello","fw":"0.2.0","board":"t-display"}\r\n')
        self.run_for(0.6)
        fr = self.all_frames()
        self.assertEqual(len(fr), 1)                  # exactly one frame, not two
        self.assertTrue(self.ports[0].tx[0].startswith(b"\n{"))
        self.run_for(5)
        self.assertEqual(len(self.all_frames()), 1)

    def test_no_hello_sends_after_timeout(self):
        self.run_for(bridge.USB_HELLO_WAIT_S - 0.6)
        self.assertEqual(self.all_frames(), [])
        self.run_for(1.5)
        self.assertEqual(len(self.all_frames()), 1)
        self.assertIn("no hello within 2.5s", self.logtext())

    def test_reacquire_defers_again(self):
        self.run_for(4)
        self.assertEqual(len(self.all_frames()), 1)
        self.ports[0].read_error = OSError(6, "Device not configured")
        self.run_for(2)
        self.assertEqual(len(self.ports), 2)
        self.assertEqual(self.ports[1].tx, [])
        self.ports[1].rx.append(b'@{"t":"hello","fw":"0.2.0","board":"t-display"}\n')
        self.run_for(1)
        self.assertEqual(len(self.ports[1].frames()), 1)

    def test_late_hello_still_resends(self):
        self.run_for(4)
        self.ports[0].rx.append(b'@{"t":"hello","fw":"0.2.0","board":"t-display"}\n')
        self.run_for(1)
        self.assertEqual(len(self.all_frames()), 2)


class Logs(unittest.TestCase):
    def test_prune_logs_by_name_date(self):
        with tempfile.TemporaryDirectory() as d:
            names = ["20260901.log", "20260920.log", "20260921.log", "20261005.log",
                     "hold.state", "notes.log", "2026091.log"]
            for n in names:
                wr(os.path.join(d, n), "x\n")
            now = time.mktime((2026, 10, 5, 12, 0, 0, 0, 0, -1))
            gone = bridge.prune_logs(d, now)
            self.assertEqual(gone, ["20260901.log", "20260920.log"])
            self.assertEqual(sorted(os.listdir(d)),
                             sorted(set(names) - {"20260901.log", "20260920.log"}))

    def test_bridge_prunes_old_logs(self):
        with tempfile.TemporaryDirectory() as home:
            sd = os.path.join(home, "serial")
            os.makedirs(sd)
            wr(os.path.join(sd, "20200101.log"), "old\n")
            b = bridge.Bridge(home=home, find_port=lambda: None, clock=time.time,
                              sleep=lambda s: None, keystore=keystore.MemoryKeystore())
            b.poll_sessions(time.time(), force=True)
            self.assertFalse(os.path.exists(os.path.join(sd, "20200101.log")))
            self.assertIn("pruned log 20200101.log", rd(os.path.join(sd, time.strftime("%Y%m%d") + ".log")))

    def test_daily_cap_keeps_events(self):
        with tempfile.TemporaryDirectory() as home:
            b = bridge.Bridge(home=home, find_port=lambda: None, clock=time.time,
                              sleep=lambda s: None, keystore=keystore.MemoryKeystore(),
                              log_cap=2000)
            for i in range(100):
                b.log("[diag] junk line %d %s" % (i, "y" * 40))
            b.log('@{"t":"ack","seq":5}', kind="proto")
            b.log(">> {frame}", kind="tx")
            b.event("still here")
            p = os.path.join(home, "serial", time.strftime("%Y%m%d") + ".log")
            text = rd(p)
            self.assertLess(os.path.getsize(p), 2000 + 400)
            self.assertNotIn("junk line 99", text)
            self.assertNotIn(">> {frame}", text)
            self.assertIn('@{"t":"ack","seq":5}', text)    # protocol lines kept until 2x
            self.assertIn("[bridge] still here", text)
            self.assertEqual(text.count("log cap reached"), 1)
            for i in range(100):
                b.log('@{"t":"ack","seq":%d}' % i, kind="proto")
            self.assertLess(os.path.getsize(p), 2 * 2000 + 200)   # hard cap for @ lines
            b.event("after hard cap")
            self.assertIn("after hard cap", rd(p))

    def test_cap_counts_existing_file(self):
        with tempfile.TemporaryDirectory() as home:
            sd = os.path.join(home, "serial")
            os.makedirs(sd)
            p = os.path.join(sd, time.strftime("%Y%m%d") + ".log")
            wr(p, "z" * 3000)          # bridge restarted on a day already over the cap
            b = bridge.Bridge(home=home, find_port=lambda: None, keystore=keystore.MemoryKeystore(),
                              log_cap=2000)
            b.log("junk")
            self.assertNotIn("junk", rd(p))

    def test_wifi_ack_logged_for_doctor(self):
        with tempfile.TemporaryDirectory() as home:
            device.save(home, "ctmeter-a1b2", "10.0.0.7", 1791100000)
            ks = keystore.MemoryKeystore()
            ks.set("ctmeter-a1b2", KEY_HEX)

            def request(*a, **k):
                return 200, b'{"t":"ack"}'
            b = bridge.Bridge(home=home, find_port=lambda: None, sleep=lambda s: None,
                              keystore=ks, transport="wifi",
                              wifi_factory=lambda h, k, ip: wifi.WifiClient(
                                  h, k, ip, resolver=lambda n: "10.0.0.7", request=request))
            b.step()
            log = rd(os.path.join(home, "serial", time.strftime("%Y%m%d") + ".log"))
            self.assertIn("<< wifi ack seq=", log)


if __name__ == "__main__":
    unittest.main()
