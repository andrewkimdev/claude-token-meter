"""ctm install / uninstall with fakes: temp dirs for LaunchAgents, settings.json
and CTM_HOME; a fake launchctl runner. Never runs real launchctl, never touches
~/Library or ~/.claude."""
import argparse
import io
import json
import os
import plistlib
import signal
import tempfile
import unittest

from tests._util import rd, wr
from ctm import cli, install

REAL_PLIST = os.path.expanduser("~/Library/LaunchAgents/com.ctm.bridge.plist")
REAL_SETTINGS = os.path.expanduser("~/.claude/settings.json")


class R(object):
    def __init__(self, rc=0, out=b"", err=b""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class FakeOps(install.Ops):
    """launchctl + python + kill fakes. Records every argv."""

    def __init__(self, loaded=False, job_pid=None, pyserial=True):
        self.loaded = loaded
        self.job_pid = job_pid
        self.pyserial = pyserial
        self.calls = []
        self.killed = []
        self.dead = set()
        self.t = 0.0
        super(FakeOps, self).__init__(run=self.fake_run, kill=self.fake_kill,
                                      alive=lambda pid: pid not in self.dead,
                                      sleep=self.fake_sleep, clock=lambda: 1791180000.0 + self.t)

    def fake_sleep(self, s):
        self.t += s

    def fake_kill(self, pid, sig):
        self.calls.append(["kill", pid, sig])
        self.killed.append((pid, sig))
        self.dead.add(pid)

    def fake_run(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[0] == install.LAUNCHCTL:
            verb = argv[1]
            if verb == "print":
                if not self.loaded:
                    return R(113, b"", b"Could not find service")
                body = "com.ctm.bridge = {\n\tstate = running\n"
                if self.job_pid:
                    body += "\tpid = %d\n" % self.job_pid
                return R(0, (body + "}\n").encode())
            if verb == "bootout":
                if self.job_pid:
                    self.dead.add(self.job_pid)
                self.loaded, self.job_pid = False, None
                return R(0)
            if verb == "bootstrap":
                self.loaded, self.job_pid = True, 4242
                return R(0)
            raise AssertionError("unexpected launchctl verb %s" % verb)
        if argv[1:] == ["-c", "import serial"]:
            return R(0 if self.pyserial else 1)
        raise AssertionError("unexpected command %r" % (argv,))

    def verbs(self):
        return [c[1] if c[0] == install.LAUNCHCTL else c[0] for c in self.calls
                if c[0] in (install.LAUNCHCTL, "kill")]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.home = os.path.join(t, "ctm-home")
        self.la = os.path.join(t, "LaunchAgents")
        self.settings = os.path.join(t, "claude", "settings.json")
        self.host = os.path.join(t, "repo", "host")
        os.makedirs(os.path.join(self.host, "ctm"))
        wr(os.path.join(self.host, "ctm", "statusline.py"), "# fake\n")
        self.py = os.path.join(t, "penv", "bin", "python3")
        os.makedirs(os.path.dirname(self.py))
        wr(self.py, "#!/bin/sh\n")
        os.chmod(self.py, 0o755)
        self.paths = install.Paths(home=self.home, launch_agents=self.la,
                                   claude_settings=self.settings, host_dir=self.host, uid=501)
        self.assertNotEqual(self.paths.plist, REAL_PLIST)
        self.assertNotEqual(self.paths.claude_settings, REAL_SETTINGS)
        self.out = io.StringIO()

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, ops, **kw):
        kw.setdefault("python", self.py)
        return install.install(self.paths, ops, self.out, **kw)

    def hold(self, pid):
        os.makedirs(os.path.join(self.home, "serial"), exist_ok=True)
        wr(self.paths.hold_pid, str(pid))


class LaunchAgent(Base):
    def test_plist_and_bootstrap(self):
        ops = FakeOps()
        self.assertEqual(self.install(ops), 0)
        with open(self.paths.plist, "rb") as f:
            p = plistlib.load(f)
        self.assertEqual(p["Label"], "com.ctm.bridge")
        self.assertEqual(p["ProgramArguments"],
                         [self.py, "-u", "-m", "ctm.bridge", "--transport", "auto"])
        self.assertEqual(p["WorkingDirectory"], self.host)
        self.assertIs(p["RunAtLoad"], True)
        self.assertIs(p["KeepAlive"], True)
        self.assertEqual(p["ThrottleInterval"], 10)
        self.assertEqual(p["StandardOutPath"], os.path.join(self.home, "launchd", "bridge.out.log"))
        self.assertEqual(p["StandardErrorPath"], os.path.join(self.home, "launchd", "bridge.err.log"))
        self.assertEqual(p["EnvironmentVariables"], {"CTM_HOME": self.home})  # non-default home
        self.assertTrue(os.path.isdir(os.path.join(self.home, "launchd")))
        self.assertIn([install.LAUNCHCTL, "bootstrap", "gui/501", self.paths.plist], ops.calls)
        self.assertNotIn("bootout", ops.verbs())
        self.assertFalse(os.path.exists(self.settings))     # no --statusline: untouched

    def test_default_python_path(self):
        self.assertEqual(install.DEFAULT_PYTHON, "~/.platformio/penv/bin/python3")

    def test_reload_when_loaded(self):
        # hold.pid liveness uses the real os.kill(pid, 0), so use a live pid;
        # launchd reports the same pid -> it is our job, not a manual bridge
        ops = FakeOps(loaded=True, job_pid=os.getppid())
        self.hold(os.getppid())
        self.assertEqual(self.install(ops), 0)
        self.assertEqual(ops.verbs(), ["print", "print", "bootout", "bootstrap"])
        self.assertEqual(ops.killed, [])

    def test_refuses_manual_bridge_without_replace(self):
        self.hold(os.getppid())       # live, and launchd says nothing is loaded
        ops = FakeOps()
        with self.assertRaises(install.InstallError) as cm:
            self.install(ops)
        self.assertIn("--replace", str(cm.exception))
        self.assertFalse(os.path.exists(self.paths.plist))
        self.assertNotIn("bootstrap", ops.verbs())
        self.assertEqual(ops.killed, [])

    def test_replace_stops_manual_bridge_right_before_bootstrap(self):
        pid = os.getppid()
        self.hold(pid)
        ops = FakeOps()
        self.assertEqual(self.install(ops, replace=True), 0)
        self.assertEqual(ops.killed, [(pid, signal.SIGTERM)])
        v = ops.verbs()
        self.assertEqual(v[-2:], ["kill", "bootstrap"])
        self.assertTrue(os.path.exists(self.paths.plist))

    def test_refuses_without_pyserial(self):
        ops = FakeOps(pyserial=False)
        with self.assertRaises(install.InstallError) as cm:
            self.install(ops)
        self.assertIn("pyserial", str(cm.exception))
        self.assertFalse(os.path.exists(self.paths.plist))

    def test_refuses_missing_python(self):
        with self.assertRaises(install.InstallError):
            self.install(FakeOps(), python=os.path.join(self.tmp.name, "nope"))

    def test_dry_run_changes_nothing(self):
        self.hold(os.getppid())
        ops = FakeOps()
        os.makedirs(os.path.dirname(self.settings))
        wr(self.settings, '{"a": 1}')
        self.assertEqual(self.install(ops, replace=True, statusline=True, dry_run=True), 0)
        self.assertFalse(os.path.exists(self.paths.plist))
        self.assertEqual(rd(self.settings), '{"a": 1}')
        self.assertEqual(ops.killed, [])
        self.assertEqual(set(ops.verbs()), {"print"})
        o = self.out.getvalue()
        self.assertIn("[dry-run] launchctl bootstrap gui/501", o)
        self.assertIn("[dry-run] kill -TERM %d" % os.getppid(), o)
        self.assertIn("<string>com.ctm.bridge</string>", o)

    def test_uninstall(self):
        ops = FakeOps()
        self.install(ops)
        ops.calls = []
        self.assertEqual(install.uninstall(self.paths, ops, self.out), 0)
        self.assertEqual(ops.verbs(), ["print", "bootout"])
        self.assertFalse(os.path.exists(self.paths.plist))

    def test_uninstall_dry_run(self):
        ops = FakeOps()
        self.install(ops)
        ops.calls = []
        install.uninstall(self.paths, ops, self.out, dry_run=True)
        self.assertTrue(os.path.exists(self.paths.plist))
        self.assertEqual(ops.verbs(), ["print"])

    def test_cli_wrapper_reports_error(self):
        self.hold(os.getppid())
        args = argparse.Namespace(cmd="install", python=self.py, replace=False, statusline=False,
                                  force=False, dry_run=False)
        import sys
        err = io.StringIO()
        old = sys.stderr
        sys.stderr = err
        try:
            rc = cli.run_install_cmd(args, self.paths, FakeOps(), self.out)
        finally:
            sys.stderr = old
        self.assertEqual(rc, 2)
        self.assertIn("--replace", err.getvalue())


class StatusLine(Base):
    def want(self):
        return "/usr/bin/python3 -S %s/ctm/statusline.py" % self.host

    def write_settings(self, d):
        os.makedirs(os.path.dirname(self.settings), exist_ok=True)
        wr(self.settings, json.dumps(d))

    def backups(self):
        d = os.path.dirname(self.settings)
        return sorted(n for n in os.listdir(d) if ".bak-ctm-" in n)

    def test_sets_and_preserves_other_keys_idempotently(self):
        self.write_settings({"model": "x", "permissions": {"allow": ["a"]}})
        self.assertEqual(self.install(FakeOps(), statusline=True), 0)
        d = json.loads(rd(self.settings))
        self.assertEqual(d["statusLine"], {"type": "command", "command": self.want()})
        self.assertEqual(d["model"], "x")
        self.assertEqual(d["permissions"], {"allow": ["a"]})
        self.assertEqual(len(self.backups()), 1)
        before = rd(self.settings)
        self.assertEqual(self.install(FakeOps(), statusline=True), 0)
        self.assertEqual(rd(self.settings), before)
        self.assertEqual(len(self.backups()), 1)          # no-op: no new backup
        self.assertIn("already set", self.out.getvalue())

    def test_creates_missing_settings(self):
        self.install(FakeOps(), statusline=True)
        self.assertEqual(json.loads(rd(self.settings))["statusLine"]["command"], self.want())

    def test_refuses_different_statusline_and_changes_nothing(self):
        other = {"type": "command", "command": "~/bin/my-statusline.sh"}
        self.write_settings({"statusLine": other})
        ops = FakeOps()
        with self.assertRaises(install.InstallError) as cm:
            self.install(ops, statusline=True)
        self.assertIn("~/bin/my-statusline.sh", str(cm.exception))
        self.assertIn("--force", str(cm.exception))
        self.assertEqual(json.loads(rd(self.settings))["statusLine"], other)
        self.assertFalse(os.path.exists(self.paths.plist))   # refused before any change
        self.assertNotIn("bootstrap", ops.verbs())

    def test_force_replaces_with_backup(self):
        orig = {"statusLine": {"type": "command", "command": "other"}, "k": 1}
        self.write_settings(orig)
        self.install(FakeOps(), statusline=True, force=True)
        self.assertEqual(json.loads(rd(self.settings))["statusLine"]["command"], self.want())
        b = self.backups()
        self.assertEqual(len(b), 1)
        self.assertEqual(json.loads(rd(os.path.join(os.path.dirname(self.settings), b[0]))), orig)

    def test_bad_json_refused(self):
        os.makedirs(os.path.dirname(self.settings))
        wr(self.settings, "{not json")
        with self.assertRaises(install.InstallError):
            self.install(FakeOps(), statusline=True)
        self.assertEqual(rd(self.settings), "{not json")

    def test_symlinked_settings_stays_symlink(self):
        real = os.path.join(self.tmp.name, "dotfiles-settings.json")
        wr(real, "{}")
        os.makedirs(os.path.dirname(self.settings))
        os.symlink(real, self.settings)
        self.install(FakeOps(), statusline=True)
        self.assertTrue(os.path.islink(self.settings))
        self.assertEqual(json.loads(rd(real))["statusLine"]["command"], self.want())

    def test_uninstall_removes_only_ours(self):
        self.write_settings({"statusLine": {"type": "command", "command": "other"}})
        install.uninstall(self.paths, FakeOps(), self.out, statusline=True)
        self.assertEqual(json.loads(rd(self.settings))["statusLine"]["command"], "other")
        self.write_settings({"statusLine": {"type": "command", "command": self.want()}, "k": 2})
        install.uninstall(self.paths, FakeOps(), self.out, statusline=True)
        self.assertEqual(json.loads(rd(self.settings)), {"k": 2})


if __name__ == "__main__":
    unittest.main()
