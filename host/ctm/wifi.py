"""WiFi HTTP transport (PROTOCOL v1 §4.2).

POST http://<host>.local/v1/state with X-CTM-Sig = hex(hmac_sha256(key, body)).
The host name is resolved with getaddrinfo (mDNS via the macOS resolver,
bounded by a thread timeout); on failure the cached IP from device.json is
used. Status codes are mapped to a small Result (see interpret()).

stdlib only, Python 3.9 compatible. The key is never logged or repr'd.
"""
import errno
import hashlib
import hmac
import http.client
import json
import socket
import threading

HTTP_TIMEOUT_S = 3.0
RESOLVE_TIMEOUT_S = 2.0
# P5: once a request to an address failed, the cached IP is not trusted:
# every attempt re-resolves <host>.local first. While mDNS keeps failing the
# stale cached IP is only probed on every STALE_PROBE_EVERY-th attempt, so a
# network whose multicast is broken (mDNS never works) cannot lock us out.
STALE_PROBE_EVERY = 3
MAX_BODY = 1024
LOCAL_NETWORK_HINT = ("EHOSTUNREACH: check macOS System Settings > Privacy & Security > "
                      "Local Network (allow the terminal / python running the bridge)")


def sign(key, body):
    """hex lowercase hmac_sha256(key, body). key: bytes, body: bytes."""
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def verify(key, body, sig):
    """Reference of the device check: exact lowercase hex, constant time
    (uppercase is rejected, fixtures/hmac_uppercase_sig.json)."""
    if not isinstance(sig, str):
        return False
    return hmac.compare_digest(sign(key, body).encode("ascii"), sig.encode("utf-8", "replace"))


def resolve(name, timeout=RESOLVE_TIMEOUT_S):
    """IPv4 address string for name, or None. getaddrinfo cannot be given a
    timeout, so it runs in a daemon thread that we stop waiting for."""
    box = {}

    def run():
        try:
            box["r"] = socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM)
        except Exception as e:  # gaierror, OSError
            box["e"] = e

    t = threading.Thread(target=run, name="ctm-resolve", daemon=True)
    t.start()
    t.join(timeout)
    r = box.get("r")
    if r:
        return r[0][4][0]
    return None


def http_request(ip, port, method, path, body=None, headers=None, timeout=HTTP_TIMEOUT_S):
    """Returns (status, body_bytes). Raises OSError / socket.timeout /
    http.client.HTTPException on transport failure."""
    c = http.client.HTTPConnection(ip, port, timeout=timeout)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read(4096)
    finally:
        c.close()


class Result(object):
    """kind: ack | nak_parse (400) | sig (401) | old_seq | skew (409) |
    http (other status) | net (transport error) | resolve (no address)."""

    def __init__(self, kind, status=None, err=None, reply=None, ip=None, errno_=None):
        self.kind = kind
        self.status = status
        self.err = err
        self.reply = reply
        self.ip = ip
        self.errno = errno_

    @property
    def ok(self):
        return self.kind == "ack"

    def __repr__(self):
        return "Result(%s status=%s err=%s ip=%s)" % (self.kind, self.status, self.err, self.ip)


def _json(b):
    try:
        d = json.loads(b.decode("utf-8", "replace"))
        return d if isinstance(d, dict) else None
    except ValueError:
        return None


def interpret(status, body, ip=None):
    """Map a /v1/state HTTP response to a Result (§4.2 processing order)."""
    reply = _json(body or b"")
    err = reply.get("err") if reply else None
    if status == 200:
        return Result("ack", status, None, reply, ip)
    if status == 401:
        return Result("sig", status, err or "sig", reply, ip)
    if status == 400:
        return Result("nak_parse", status, err or "parse", reply, ip)
    if status == 409:
        kind = "skew" if err == "skew" else "old_seq"
        return Result(kind, status, err or kind, reply, ip)
    return Result("http", status, err, reply, ip)


class WifiClient(object):
    def __init__(self, host, key, cached_ip=None, port=80, resolver=resolve,
                 request=http_request, timeout=HTTP_TIMEOUT_S):
        self.host = host
        self._key = key          # bytes; never logged
        self.cached_ip = cached_ip or None
        self.port = port
        self.resolver = resolver
        self.request = request
        self.timeout = timeout
        self.stale = False       # an attempt failed: re-resolve, do not trust cached_ip
        self._stale_skips = 0

    def mark_stale(self):
        """Distrust the cached IP until the next success (failure, wake)."""
        self.stale = True

    def __repr__(self):
        return "WifiClient(host=%r, ip=%r)" % (self.host, self.cached_ip)

    def address(self):
        """(ip, source) where source is 'mdns', 'cache' or 'stale-probe';
        (None, None) if neither. mDNS is always asked first."""
        ip = None
        try:
            ip = self.resolver(self.host + ".local")
        except Exception:
            ip = None
        if ip:
            return ip, "mdns"
        if self.cached_ip:
            if not self.stale:
                return self.cached_ip, "cache"
            self._stale_skips += 1
            if self._stale_skips >= STALE_PROBE_EVERY:
                self._stale_skips = 0
                return self.cached_ip, "stale-probe"
        return None, None

    def _no_address(self):
        if self.cached_ip and self.stale:
            return ("cannot resolve %s.local (cached ip %s failed earlier, not trusted)"
                    % (self.host, self.cached_ip))
        return "cannot resolve %s.local and no cached ip" % self.host

    def headers(self, body):
        return {"Content-Type": "application/json",
                "Host": "%s.local" % self.host,
                "X-CTM-Sig": sign(self._key, body)}

    def post_state(self, body):
        if len(body) > MAX_BODY:
            raise ValueError("body exceeds %d bytes" % MAX_BODY)
        ip, _src = self.address()
        if not ip:
            return Result("resolve", err=self._no_address())
        try:
            status, data = self.request(ip, self.port, "POST", "/v1/state", body,
                                        self.headers(body), self.timeout)
        except socket.timeout:
            self.mark_stale()
            return Result("net", err="timeout", ip=ip, errno_=errno.ETIMEDOUT)
        except OSError as e:
            self.mark_stale()
            return Result("net", err=e.strerror or str(e), ip=ip, errno_=e.errno)
        except http.client.HTTPException as e:
            self.mark_stale()
            return Result("net", err="http: %s" % type(e).__name__, ip=ip)
        r = interpret(status, data, ip)
        if r.ok:
            # cached_ip itself is updated by the caller (bridge) together with device.json
            self.stale = False
            self._stale_skips = 0
        return r

    def get_info(self):
        """GET /v1/info (unsigned). Returns (dict|None, ip, error string|None)."""
        ip, _src = self.address()
        if not ip:
            return None, None, self._no_address()
        try:
            status, data = self.request(ip, self.port, "GET", "/v1/info", None,
                                        {"Host": "%s.local" % self.host}, self.timeout)
        except socket.timeout:
            return None, ip, "timeout"
        except (OSError, http.client.HTTPException) as e:
            if isinstance(e, OSError) and e.errno == errno.EHOSTUNREACH:
                return None, ip, LOCAL_NETWORK_HINT
            return None, ip, str(e)
        if status != 200:
            return None, ip, "HTTP %d" % status
        d = _json(data)
        return d, ip, (None if d is not None else "bad JSON")


class Backoff(object):
    """Exponential backoff: base, base*2, ... capped at cap seconds."""

    def __init__(self, base=5.0, cap=600.0):
        self.base = base
        self.cap = cap
        self.failures = 0

    def fail(self):
        d = min(self.cap, self.base * (2 ** self.failures))
        if d < self.cap:
            self.failures += 1
        return d

    def reset(self):
        self.failures = 0
