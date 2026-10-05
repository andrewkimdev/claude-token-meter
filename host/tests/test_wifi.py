import glob
import hashlib
import hmac
import json
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tests._util import FIXTURES
from ctm import device, keystore, wifi

KEY = bytes(range(32))


class Signing(unittest.TestCase):
    def test_matches_python_hmac(self):
        body = b'{"t":"state","v":1,"seq":1791180000,"sent_at":1791180000,"windows":{}}\n'
        want = hmac.new(KEY, body, hashlib.sha256).hexdigest()
        self.assertEqual(wifi.sign(KEY, body), want)
        self.assertEqual(len(want), 64)
        self.assertEqual(want, want.lower())
        self.assertTrue(wifi.verify(KEY, body, want))
        self.assertFalse(wifi.verify(KEY, body, want.upper()))  # §4.2: lowercase only
        self.assertFalse(wifi.verify(KEY, body + b" ", want))
        self.assertFalse(wifi.verify(b"\x00" * 32, body, want))
        self.assertFalse(wifi.verify(KEY, body, None))

    def test_fixtures_if_present(self):
        paths = sorted(glob.glob(os.path.join(FIXTURES, "hmac_*.json")))
        if not paths:
            self.skipTest("fixtures/hmac_*.json not present yet")
        for p in paths:
            with open(p) as f:
                fx = json.load(f)
            with self.subTest(os.path.basename(p)):
                key = bytes.fromhex(fx["key_hex"])
                body = fx["body"].encode("utf-8")
                ok = wifi.verify(key, body, fx["sig"])
                self.assertEqual("ok" if ok else "sig", fx["expected"])
                if fx["expected"] == "ok":
                    self.assertEqual(wifi.sign(key, body), fx["sig"])


class Interpret(unittest.TestCase):
    def test_statuses(self):
        r = wifi.interpret(200, b'{"t":"ack","seq":5}')
        self.assertTrue(r.ok)
        self.assertEqual(r.reply["seq"], 5)
        self.assertEqual(wifi.interpret(401, b'{"t":"nak","err":"sig"}').kind, "sig")
        self.assertEqual(wifi.interpret(401, b"").kind, "sig")
        r = wifi.interpret(400, b'{"t":"nak","seq":5,"err":"bad_v"}')
        self.assertEqual((r.kind, r.err), ("nak_parse", "bad_v"))
        self.assertEqual(wifi.interpret(409, b'{"t":"nak","seq":5,"err":"skew"}').kind, "skew")
        self.assertEqual(wifi.interpret(409, b'{"t":"nak","seq":5,"err":"old_seq"}').kind,
                         "old_seq")
        self.assertEqual(wifi.interpret(500, b"oops").kind, "http")
        self.assertEqual(wifi.interpret(404, b"").kind, "http")


class Backoff(unittest.TestCase):
    def test_exponential_capped(self):
        b = wifi.Backoff(5, 600)
        ds = [b.fail() for _ in range(12)]
        self.assertEqual(ds[:5], [5, 10, 20, 40, 80])
        self.assertEqual(max(ds), 600)
        self.assertEqual(ds[-1], 600)
        for _ in range(2000):   # no overflow
            b.fail()
        b.reset()
        self.assertEqual(b.fail(), 5)


class Server(object):
    """Minimal device /v1/state + /v1/info on 127.0.0.1 (PROTOCOL §4.2 order)."""

    def __init__(self, key):
        self.key = key
        self.last_seq = None
        self.force = None          # (status, body) override
        self.requests = []
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status, obj):
                b = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path == "/v1/info":
                    self._reply(200, {"t": "info", "host": "ctmeter-test", "last_seq": srv.last_seq})
                else:
                    self._reply(404, {})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                srv.requests.append((self.path, dict(self.headers), body))
                if srv.force:
                    return self._reply(*srv.force)
                if not wifi.verify(srv.key, body, self.headers.get("X-CTM-Sig")):
                    return self._reply(401, {"t": "nak", "err": "sig"})
                f = json.loads(body)
                if srv.last_seq is not None and f["seq"] <= srv.last_seq:
                    return self._reply(409, {"t": "nak", "seq": f["seq"], "err": "old_seq"})
                srv.last_seq = f["seq"]
                self._reply(200, {"t": "ack", "seq": f["seq"]})

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.t = threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True)
        self.t.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Http(unittest.TestCase):
    def setUp(self):
        self.srv = Server(KEY)
        self.addCleanup(self.srv.close)

    def client(self, key=KEY, resolver=lambda n: "127.0.0.1", cached=None):
        return wifi.WifiClient("ctmeter-test", key, cached, port=self.srv.port,
                               resolver=resolver, timeout=2.0)

    def test_signed_post_ack_then_old_seq(self):
        c = self.client()
        body = b'{"t":"state","v":1,"seq":100,"sent_at":100,"windows":{}}\n'
        r = c.post_state(body)
        self.assertEqual(r.kind, "ack")
        self.assertEqual(r.ip, "127.0.0.1")
        path, headers, got = self.srv.requests[0]
        self.assertEqual(path, "/v1/state")
        self.assertEqual(got, body)
        self.assertEqual(headers["X-CTM-Sig"], hmac.new(KEY, body, hashlib.sha256).hexdigest())
        self.assertEqual(headers["Host"], "ctmeter-test.local")
        self.assertEqual(c.post_state(body).kind, "old_seq")
        info, ip, err = c.get_info()
        self.assertEqual(info["last_seq"], 100)
        self.assertIsNone(err)

    def test_wrong_key_401(self):
        r = self.client(key=b"\x01" * 32).post_state(b'{"t":"state"}')
        self.assertEqual((r.kind, r.status), ("sig", 401))

    def test_forced_statuses(self):
        c = self.client()
        for status, obj, kind in ((400, {"t": "nak", "seq": 1, "err": "parse"}, "nak_parse"),
                                  (409, {"t": "nak", "seq": 1, "err": "skew"}, "skew"),
                                  (503, {}, "http")):
            self.srv.force = (status, obj)
            self.assertEqual(c.post_state(b"{}").kind, kind)

    def test_resolve_fallback_to_cache(self):
        c = self.client(resolver=lambda n: None, cached="127.0.0.1")
        self.assertEqual(c.address(), ("127.0.0.1", "cache"))
        self.assertEqual(c.post_state(b'{"t":"state","seq":1}').kind, "ack")

        def boom(n):
            raise socket.gaierror("nope")
        c = self.client(resolver=boom, cached=None)
        r = c.post_state(b"{}")
        self.assertEqual(r.kind, "resolve")

    def test_connection_refused_is_net(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        c = wifi.WifiClient("x", KEY, None, port=port, resolver=lambda n: "127.0.0.1")
        r = c.post_state(b"{}")
        self.assertEqual(r.kind, "net")

    def test_key_not_in_repr(self):
        c = self.client()
        self.assertNotIn(KEY.hex(), repr(c))
        self.assertNotIn(repr(KEY), repr(c))

    def test_ehostunreach_errno_kept(self):
        import errno

        def req(*a, **k):
            raise OSError(errno.EHOSTUNREACH, "No route to host")
        c = wifi.WifiClient("x", KEY, "10.0.0.9", resolver=lambda n: None, request=req)
        r = c.post_state(b"{}")
        self.assertEqual((r.kind, r.errno), ("net", errno.EHOSTUNREACH))
        # (after a failure c distrusts its cached ip; use a fresh client)
        c = wifi.WifiClient("x", KEY, "10.0.0.9", resolver=lambda n: None, request=req)
        info, ip, err = c.get_info()
        self.assertIn("Local Network", err)


class FakeRun(object):
    """Stand-in for subprocess.run with the `security` CLI semantics."""

    def __init__(self):
        self.items = {}
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        a = argv[1:]
        if a == ["-i"]:   # interactive mode: command arrives on stdin
            a = kw["input"].decode().split()
        args = dict(zip(a[1::2], a[2::2])) if a[0] != "add-generic-password" else None
        if a[0] == "add-generic-password":
            i = a.index
            self.items[(a[i("-s") + 1], a[i("-a") + 1])] = a[i("-w") + 1]
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        key = (args["-s"], args["-a"])
        if a[0] == "find-generic-password":
            if key not in self.items:
                return subprocess.CompletedProcess(argv, 44, b"", b"not found")
            return subprocess.CompletedProcess(argv, 0, (self.items[key] + "\n").encode(), b"")
        if a[0] == "delete-generic-password":
            if self.items.pop(key, None) is None:
                return subprocess.CompletedProcess(argv, 44, b"", b"")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        raise AssertionError(argv)


class Keystore(unittest.TestCase):
    def test_security_cli_commands(self):
        run = FakeRun()
        ks = keystore.SecurityKeystore(runner=run)
        k = KEY.hex()
        ks.set("ctmeter-a1b2", k)
        self.assertEqual(run.calls[0], ["/usr/bin/security", "-i"])   # key via stdin
        self.assertNotIn(k, " ".join(run.calls[0]))
        self.assertEqual(ks.get("ctmeter-a1b2"), k)
        self.assertEqual(run.calls[1], ["/usr/bin/security", "find-generic-password",
                                        "-s", "ctm-pairing", "-a", "ctmeter-a1b2", "-w"])
        self.assertIsNone(ks.get("ctmeter-zzzz"))
        self.assertTrue(ks.delete("ctmeter-a1b2"))
        self.assertFalse(ks.delete("ctmeter-a1b2"))
        self.assertIsNone(ks.get("ctmeter-a1b2"))

    def test_failure_message_has_no_key(self):
        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, b"", b"")
        ks = keystore.SecurityKeystore(runner=run)
        with self.assertRaises(keystore.KeystoreError) as cm:
            ks.set("h", KEY.hex())
        self.assertNotIn(KEY.hex(), str(cm.exception))
        with self.assertRaises(keystore.KeystoreError):
            ks.set("h", "nothex")

    def test_memory(self):
        ks = keystore.MemoryKeystore()
        ks.set("h", KEY.hex().upper())
        self.assertEqual(ks.get("h"), KEY.hex())
        self.assertTrue(ks.delete("h"))
        self.assertIsNone(ks.get("h"))


class DeviceStore(unittest.TestCase):
    def test_roundtrip_no_key(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertIsNone(device.load(home))
            device.save(home, "ctmeter-a1b2", "192.168.1.5", 1791180000)
            rec = device.load(home)
            self.assertEqual(rec, {"host": "ctmeter-a1b2", "ip": "192.168.1.5",
                                   "paired_at": 1791180000})
            self.assertEqual(os.stat(device.path(home)).st_mode & 0o777, 0o600)
            self.assertTrue(device.update_ip(home, "192.168.1.6"))
            self.assertFalse(device.update_ip(home, "192.168.1.6"))
            self.assertEqual(device.load(home)["ip"], "192.168.1.6")
            self.assertTrue(device.remove(home))
            self.assertFalse(device.remove(home))
            with open(device.path(home), "w") as f:
                f.write("garbage")
            self.assertIsNone(device.load(home))


if __name__ == "__main__":
    unittest.main()
