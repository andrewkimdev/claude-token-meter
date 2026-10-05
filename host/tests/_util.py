import glob
import json
import os
import sys

HOST = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(HOST)
FIXTURES = os.path.join(REPO, "fixtures")
if HOST not in sys.path:
    sys.path.insert(0, HOST)


def fixtures(prefix):
    out = []
    for p in sorted(glob.glob(os.path.join(FIXTURES, prefix + "*.json"))):
        with open(p) as f:
            out.append((os.path.basename(p), json.load(f)))
    assert out, "no fixtures for %s (fail-closed)" % prefix
    return out


def rd(p):
    with open(p) as f:
        return f.read()


def wr(p, s):
    with open(p, "w") as f:
        f.write(s)


# Guard: no test may reach the real macOS Keychain (`security` CLI).
from ctm import keystore as _keystore  # noqa: E402


def _no_real_keychain(*_a, **_k):
    raise AssertionError("tests must not use the real Keychain; inject keystore.MemoryKeystore")


_keystore.default = _no_real_keychain
