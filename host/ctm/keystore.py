"""Pairing-key storage (PROTOCOL v1 §4.2): macOS Keychain generic password,
service "ctm-pairing", account = device host name, value = 64 hex chars.

The real backend shells out to the `security` CLI. Tests inject
MemoryKeystore (or a fake runner) and never touch the real Keychain.

Secrets rule: the key is never logged, printed or put into an exception
message. Errors raised here only carry the `security` exit status.
"""
import subprocess

SERVICE = "ctm-pairing"
SECURITY = "/usr/bin/security"
NOT_FOUND = 44  # security(1) exit status for "item could not be found"


class KeystoreError(Exception):
    pass


def _check_hex(key_hex):
    if not (isinstance(key_hex, str) and len(key_hex) == 64):
        raise KeystoreError("key must be 64 hex chars")
    try:
        bytes.fromhex(key_hex)
    except ValueError:
        raise KeystoreError("key must be 64 hex chars")


class SecurityKeystore(object):
    """Backend using /usr/bin/security. `runner` mimics subprocess.run."""

    def __init__(self, runner=subprocess.run, service=SERVICE):
        self.runner = runner
        self.service = service

    def _run(self, args):
        # capture everything; never let CalledProcessError (which embeds argv,
        # i.e. the key) escape.
        return self.runner([SECURITY] + args, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)

    def set(self, host, key_hex):
        _check_hex(key_hex)
        # The key goes through stdin (`security -i` reads commands from it), not
        # argv, so it never shows up in `ps` [V: verified with a throwaway item].
        if any(c in host for c in " \t\n\"'\\"):
            raise KeystoreError("unsupported host name")
        cmd = "add-generic-password -U -s %s -a %s -w %s\n" % (self.service, host, key_hex)
        r = self.runner([SECURITY, "-i"], input=cmd.encode("ascii"),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode != 0:
            raise KeystoreError("security add-generic-password failed (exit %d)" % r.returncode)

    def get(self, host):
        r = self._run(["find-generic-password", "-s", self.service, "-a", host, "-w"])
        if r.returncode == NOT_FOUND:
            return None
        if r.returncode != 0:
            raise KeystoreError("security find-generic-password failed (exit %d)" % r.returncode)
        out = r.stdout.decode("utf-8", "replace").strip() if isinstance(r.stdout, bytes) \
            else (r.stdout or "").strip()
        try:
            _check_hex(out)
        except KeystoreError:
            raise KeystoreError("Keychain item %s/%s is not a 64-hex key" % (self.service, host))
        return out.lower()

    def delete(self, host):
        r = self._run(["delete-generic-password", "-s", self.service, "-a", host])
        if r.returncode not in (0, NOT_FOUND):
            raise KeystoreError("security delete-generic-password failed (exit %d)" % r.returncode)
        return r.returncode == 0


class MemoryKeystore(object):
    """In-memory backend for tests."""

    def __init__(self):
        self.items = {}

    def set(self, host, key_hex):
        _check_hex(key_hex)
        self.items[host] = key_hex.lower()

    def get(self, host):
        return self.items.get(host)

    def delete(self, host):
        return self.items.pop(host, None) is not None


def default():
    return SecurityKeystore()
