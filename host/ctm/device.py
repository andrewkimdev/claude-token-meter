"""Paired-device record (PROTOCOL v1 §4.2): <CTM_HOME>/device.json
{"host": "ctmeter-xxxx", "ip": "192.168.1.23", "paired_at": <epoch int>}

The pairing key is NOT stored here (it lives in the Keychain, ctm.keystore).
"""
import json
import os
import time


def ctm_home():
    return os.environ.get("CTM_HOME") or os.path.expanduser("~/.claude-token-meter")


def path(home):
    return os.path.join(home, "device.json")


def load(home):
    """Returns the record dict, or None if absent / unreadable / malformed."""
    try:
        with open(path(home)) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("host"), str) or not d["host"]:
        return None
    return d


def _write(home, rec):
    os.makedirs(home, exist_ok=True)
    p = path(home)
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f)
        f.write("\n")
    os.replace(tmp, p)


def save(home, host, ip, paired_at=None):
    rec = {"host": host, "ip": ip or "",
           "paired_at": int(paired_at if paired_at is not None else time.time())}
    _write(home, rec)
    return rec


def update_ip(home, ip):
    """Cache a new IP for the paired device. Returns True if the file changed."""
    rec = load(home)
    if rec is None or not ip or rec.get("ip") == ip:
        return False
    rec["ip"] = ip
    _write(home, rec)
    return True


def remove(home):
    try:
        os.unlink(path(home))
        return True
    except FileNotFoundError:
        return False
