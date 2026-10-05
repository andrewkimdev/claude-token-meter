import errno
import json
import os
import unittest

from tests.test_bridge import Base, write_session
from ctm import bridge, device, keystore, wifi

KEY_HEX = "00" * 16 + "ff" * 16


class FakeWifi(object):
    """Stands in for wifi.WifiClient; records bodies, replies from a script."""

    def __init__(self, host, key, ip, clock):
        self.host = host
        self.key = key
        self.cached_ip = ip
        self.clock = clock
        self.bodies = []
        self.replies = []          # list of Result; default ack
        self.info = {"t": "info", "last_seq": None}

    def post_state(self, body):
        self.bodies.append(body)
        self.clock.t += 0.05       # a request takes a little time
        if self.replies:
            return self.replies.pop(0)
        seq = json.loads(body)["seq"]
        return wifi.Result("ack", 200, None, {"t": "ack", "seq": seq}, "192.0.2.50")

    def get_info(self):
        return self.info, self.cached_ip, None

    def mark_stale(self):
        pass

    def frames(self):
        return [json.loads(b) for b in self.bodies]


class WifiBase(Base):
    def setUp(self):
        super(WifiBase, self).setUp()
        self.ks = keystore.MemoryKeystore()
        self.clients = []

        def factory(host, key, ip):
            c = FakeWifi(host, key, ip, self.clock)
            self.clients.append(c)
            return c

        self.b.keystore = self.ks
        self.b.wifi_factory = factory
        self.present = False

    def pair(self, host="ctmeter-a1b2", ip="192.0.2.40", key=KEY_HEX):
        device.save(self.home, host, ip, 1791100000)
        if key:
            self.ks.set(host, key)

    @property
    def w(self):
        return self.clients[-1]


class Selection(WifiBase):
    def test_auto_unpaired_waits(self):
        self.run_for(5, max_steps=20)
        self.assertIn("not paired", self.state())
        self.assertEqual(self.clients, [])
        self.assertEqual(self.ports, [])

    def test_auto_paired_but_no_key(self):
        self.pair(key=None)
        self.run_for(5, max_steps=20)
        self.assertIn("no pairing key in Keychain", self.state())
        self.assertEqual(self.clients, [])
        self.ks.set("ctmeter-a1b2", KEY_HEX)
        self.run_for(bridge.KEY_RETRY_S + 2)
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(self.w.key, bytes.fromhex(KEY_HEX))

    def test_auto_wifi_when_usb_absent_then_usb_wins(self):
        self.pair()
        self.run_for(2)
        self.assertEqual(len(self.w.bodies), 1)
        self.assertEqual(self.b.active, "wifi")
        self.assertIn("WIFI ctmeter-a1b2", self.state())
        self.present = True
        self.run_for(2)
        self.assertEqual(self.b.active, "usb")
        self.assertEqual(len(self.ports), 1)
        self.assertEqual(len(self.ports[0].frames()), 1)  # sent on connect
        self.assertIn("HELD /dev/cu.fake", self.state())
        # unplug -> back to WiFi, immediate send
        self.ports[0].read_error = OSError(6, "Device not configured")
        self.present = False
        self.run_for(3)
        self.assertEqual(self.b.active, "wifi")
        self.assertEqual(len(self.w.bodies), 2)
        seqs = [f["seq"] for f in self.w.frames()] + [f["seq"] for f in self.ports[0].frames()]
        self.assertEqual(len(set(seqs)), 3)

    def test_usb_mode_never_wifi(self):
        self.pair()
        self.b.transport = "usb"
        self.run_for(5, max_steps=20)
        self.assertEqual(self.clients, [])
        self.assertEqual(self.state().split(" ", 2)[2].strip(), "WAITING (device not present)")

    def test_wifi_mode_never_touches_serial(self):
        self.pair()
        self.present = True
        self.b.transport = "wifi"
        self.b.find_port = lambda: self.fail("find_port called in wifi mode")
        self.run_for(3)
        self.assertEqual(self.ports, [])
        self.assertEqual(len(self.w.bodies), 1)

    def test_bad_transport(self):
        with self.assertRaises(ValueError):
            bridge.Bridge(home=self.home, transport="bluetooth")

    def test_unpair_drops_client(self):
        self.pair()
        self.run_for(2)
        device.remove(self.home)
        self.run_for(2)
        self.assertIsNone(self.b.wifi)
        self.assertIn("not paired", self.state())


class WifiPolicy(WifiBase):
    def test_body_seq_and_min_interval(self):
        self.pair()
        T = self.clock.t
        write_session(self.home, "a", 10, int(T) + 3600, T)
        self.run_for(2)
        body = self.w.bodies[0]
        self.assertTrue(body.startswith(b"{"))           # no leading newline
        self.assertTrue(body.endswith(b"}\n"))
        f = json.loads(body)
        self.assertEqual(f["seq"], max(1, int(T)))
        for i in range(11, 20):
            write_session(self.home, "a", i, int(T) + 3600, T + i)
            self.run_for(3)
        self.run_for(30)
        fr = self.w.frames()
        self.assertEqual(fr[-1]["windows"]["five_hour"]["pct"], 19)
        for a, b in zip(fr, fr[1:]):
            self.assertGreaterEqual(b["sent_at"] - a["sent_at"], bridge.WIFI_MIN_INTERVAL_S - 1)
            self.assertGreater(b["seq"], a["seq"])
        self.assertIn(">> wifi {", self.logtext())

    def test_heartbeat_120(self):
        self.pair()
        self.run_for(2)
        self.run_for(117)
        self.assertEqual(len(self.w.bodies), 1)
        self.run_for(4)
        self.assertEqual(len(self.w.bodies), 2)

    def test_ack_updates_cached_ip(self):
        self.pair(ip="192.0.2.40")
        self.run_for(2)
        self.assertEqual(device.load(self.home)["ip"], "192.0.2.50")
        self.assertIn("cached ip for ctmeter-a1b2 -> 192.0.2.50", self.logtext())
        self.assertEqual(len(self.clients), 1)   # own ip update does not re-pair

    def test_401_logs_mismatch_and_backs_off(self):
        self.pair()
        self.b.wifi_ready()
        for _ in range(10):
            self.w.replies.append(wifi.Result("sig", 401, "sig"))
        self.run_for(1)
        self.assertIn("pairing key mismatch — rerun ctm setup", self.logtext())
        self.run_for(200)
        n = len(self.w.bodies)
        # attempts at 0, 5, 15, 35, 75, 155 -> 6 in ~200s
        self.assertLessEqual(n, 7)
        self.assertGreaterEqual(n, 5)
        self.assertIn("backoff", self.state())

    def test_backoff_capped_and_reset(self):
        self.pair()
        self.b.wifi_ready()
        self.w.replies.extend([wifi.Result("net", err="timeout", ip="1.2.3.4")] * 20)
        self.run_for(4 * 3600, max_steps=10 ** 6)
        sent = [f["sent_at"] for f in self.w.frames()]
        gaps = [b - a for a, b in zip(sent, sent[1:21])]
        self.assertEqual(gaps[:4], [5, 10, 20, 40])
        self.assertEqual(max(gaps), 600)               # capped at 10 minutes
        self.assertEqual(self.w.replies, [])
        self.assertEqual(self.b.backoff.failures, 0)    # the first ack resets it
        self.assertIn("ok seq=", self.state())

    def test_ehostunreach_hint(self):
        self.pair()
        self.b.wifi_ready()
        self.w.replies.append(wifi.Result("net", err="No route to host", ip="192.0.2.40",
                                          errno_=errno.EHOSTUNREACH))
        self.run_for(1)
        self.assertIn("Local Network", self.logtext())

    def test_400_no_hammering(self):
        self.pair()
        self.b.wifi_ready()
        self.w.replies.append(wifi.Result("nak_parse", 400, "parse"))
        self.run_for(60)
        self.assertEqual(len(self.w.bodies), 1)
        self.assertIn("HTTP 400 NAK", self.logtext())

    def test_409_old_seq_resync_retry_once(self):
        self.pair()
        self.b.wifi_ready()
        self.w.info = {"t": "info", "last_seq": 4000000000}
        self.w.replies.extend([wifi.Result("old_seq", 409, "old_seq"),
                               wifi.Result("old_seq", 409, "old_seq")])
        self.run_for(10)
        fr = self.w.frames()
        self.assertEqual(len(fr), 2)                  # one retry only
        self.assertEqual(fr[1]["seq"], 4000000001)    # skipped past device last_seq
        self.run_for(120)
        self.assertEqual(len(self.w.frames()), 3)     # heartbeat continues

    def test_409_skew(self):
        self.pair()
        self.b.wifi_ready()
        self.w.replies.append(wifi.Result("skew", 409, "skew"))
        self.run_for(1)
        self.assertIn("skew", self.logtext())
        self.assertIn("backoff", self.state())

    def test_key_never_logged(self):
        self.pair()
        self.run_for(5)
        self.w.replies.append(wifi.Result("sig", 401, "sig"))
        self.b.force_send = True
        self.run_for(5)
        log = self.logtext() + self.state()
        self.assertNotIn(KEY_HEX, log)
        self.assertNotIn(KEY_HEX.upper(), log)


class SeqRule(unittest.TestCase):
    def test_next_seq(self):
        self.assertEqual(bridge.next_seq(0, 1791180000.7), 1791180000)
        self.assertEqual(bridge.next_seq(1791180000, 1791180000.2), 1791180001)
        self.assertEqual(bridge.next_seq(5, 100), 100)
        self.assertEqual(bridge.next_seq(2000000000, 100), 2000000001)
        self.assertEqual(bridge.next_seq(0xFFFFFFFF, 100), 0xFFFFFFFF)   # uint32 bound
        self.assertEqual(bridge.next_seq(1, 2 ** 40), 0xFFFFFFFF)

    def test_restart_never_decreases(self):
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            b1 = bridge.Bridge(home=home, keystore=keystore.MemoryKeystore())
            f1 = b1.current_frame(1791180000)
            b2 = bridge.Bridge(home=home, keystore=keystore.MemoryKeystore())  # "restart"
            f2 = b2.current_frame(1791180001)
            self.assertGreater(f2["seq"], f1["seq"])


if __name__ == "__main__":
    unittest.main()
