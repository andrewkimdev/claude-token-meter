#!/usr/bin/python3 -S
"""P0 spike: can this process reach LAN hosts (macOS Local Network privacy)?

Usage: lan_probe.py <label> [gateway] [lan_host] [local_hostname]
Appends one JSON line to ~/.claude-token-meter/spike/lan_probe.log.
stdlib only. Never touches serial ports.
"""
import errno
import json
import os
import socket
import subprocess
import sys
import time

LOG = os.path.expanduser("~/.claude-token-meter/spike/lan_probe.log")


def tcp(host, port, timeout=3.0):
    t0 = time.time()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        r = {"ok": True, "result": "connected"}
    except socket.timeout:
        r = {"ok": False, "result": "timeout"}
    except OSError as e:
        r = {"ok": False, "result": errno.errorcode.get(e.errno, str(e.errno)), "msg": str(e)}
    finally:
        s.close()
    r.update(target="%s:%d" % (host, port), ms=int((time.time() - t0) * 1000))
    return r


def udp(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if host.startswith("224."):
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        n = s.sendto(b"ctm-spike", (host, port))
        r = {"ok": True, "result": "sent %d" % n}
    except OSError as e:
        r = {"ok": False, "result": errno.errorcode.get(e.errno, str(e.errno)), "msg": str(e)}
    finally:
        s.close()
    r["target"] = "%s:%d" % (host, port)
    return r


def resolve(name):
    t0 = time.time()
    try:
        addrs = sorted({ai[4][0] for ai in socket.getaddrinfo(name, 80, proto=socket.IPPROTO_TCP)})
        r = {"ok": True, "result": addrs}
    except OSError as e:
        r = {"ok": False, "result": str(e)}
    r.update(target=name, ms=int((time.time() - t0) * 1000))
    return r


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "unlabeled"
    gw = sys.argv[2] if len(sys.argv) > 2 else "192.168.219.1"
    lan = sys.argv[3] if len(sys.argv) > 3 else None
    lhn = sys.argv[4] if len(sys.argv) > 4 else None
    if not lhn:
        try:
            lhn = subprocess.check_output(["/usr/sbin/scutil", "--get", "LocalHostName"], text=True).strip()
        except Exception:
            lhn = None
    res = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "label": label,
        "exe": sys.executable,
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "gw_tcp_open": tcp(gw, 80),
        "gw_tcp_closed": tcp(gw, 81),
        "gw_udp9": udp(gw, 9),
        "mdns_udp": udp("224.0.0.251", 5353),
        "inet_tcp": tcp("1.1.1.1", 443),
    }
    if lan:
        res["lan_tcp_closed"] = tcp(lan, 81)
    if lhn:
        res["mdns_resolve"] = resolve(lhn + ".local")
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    line = json.dumps(res, ensure_ascii=False)
    with open(LOG, "a") as f:
        f.write(line + "\n")
    print(line)


if __name__ == "__main__":
    main()
