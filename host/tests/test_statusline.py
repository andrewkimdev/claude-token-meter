import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests._util import rd, HOST
from ctm import statusline

SCRIPT = os.path.join(HOST, "ctm", "statusline.py")
SYS_PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable


def payload(sid="sess-1", rl="default", now=None):
    now = now or time.time()
    d = {"session_id": sid, "version": "2.1.289", "model": {"id": "m"}, "cwd": "/x"}
    if rl == "default":
        rl = {"five_hour": {"used_percentage": 52, "resets_at": int(now) + 3600},
              "seven_day": {"used_percentage": 29.4, "resets_at": int(now) + 86400}}
    if rl != "absent":
        d["rate_limits"] = rl
    return d


def run_script(home, stdin_text):
    env = dict(os.environ, CTM_HOME=home)
    t0 = time.time()
    p = subprocess.run([SYS_PY, "-S", SCRIPT], input=stdin_text.encode(), env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    return p, time.time() - t0


class Script(unittest.TestCase):
    """Runs the real script with the system python, -S, like Claude Code will."""

    def test_writes_and_prints(self):
        with tempfile.TemporaryDirectory() as home:
            p, _ = run_script(home, json.dumps(payload()))
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout.decode().strip(), "5h 52% · 7d 29%")
            self.assertEqual(p.stderr, b"")
            with open(os.path.join(home, "sessions", "sess-1.json")) as f:
                rec = json.load(f)
            self.assertEqual(rec["session_id"], "sess-1")
            self.assertEqual(rec["cc_version"], "2.1.289")
            self.assertEqual(rec["rate_limits"]["five_hour"]["used_percentage"], 52)
            self.assertIsInstance(rec["observed_at"], float)
            self.assertEqual(set(rec), {"session_id", "observed_at", "cc_version", "rate_limits"})
            self.assertEqual(os.listdir(os.path.join(home, "sessions")), ["sess-1.json"])

    def test_no_rate_limits_no_write(self):
        for rl in ("absent", None, {}):
            with tempfile.TemporaryDirectory() as home:
                p, _ = run_script(home, json.dumps(payload(rl=rl)))
                self.assertEqual(p.returncode, 0)
                self.assertEqual(p.stdout.decode().strip(), "5h -- · 7d --")
                self.assertFalse(os.path.exists(os.path.join(home, "sessions", "sess-1.json")))

    def test_null_does_not_overwrite_previous(self):
        with tempfile.TemporaryDirectory() as home:
            run_script(home, json.dumps(payload()))
            path = os.path.join(home, "sessions", "sess-1.json")
            before = rd(path)
            run_script(home, json.dumps(payload(rl=None)))
            self.assertEqual(rd(path), before)

    def test_garbage_input_exit0(self):
        for text in ("", "not json", "[1,2]", '{"rate_limits": 5}', '{"session_id": "../evil", "rate_limits": {"five_hour": {}}}'):
            with tempfile.TemporaryDirectory() as home:
                p, _ = run_script(home, text)
                self.assertEqual(p.returncode, 0, text)
                self.assertTrue(p.stdout.strip(), text)
                self.assertEqual(p.stderr, b"", text)
                self.assertFalse(os.path.exists(os.path.join(os.path.dirname(home), "evil.json")))

    def test_unwritable_home_still_prints(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "file")
            open(home, "w").close()  # a file where a dir is expected
            p, _ = run_script(home, json.dumps(payload()))
            self.assertEqual(p.returncode, 0)
            self.assertEqual(p.stdout.decode().strip(), "5h 52% · 7d 29%")

    def test_fast(self):
        with tempfile.TemporaryDirectory() as home:
            run_script(home, json.dumps(payload()))  # warm
            best = min(run_script(home, json.dumps(payload()))[1] for _ in range(5))
            # CLAUDE.md target is 20ms for the script; subprocess spawn adds overhead.
            self.assertLess(best, 0.2)


class Render(unittest.TestCase):
    def test_render(self):
        now = 1000.0
        self.assertEqual(statusline.render(payload(now=now), now), "5h 52% · 7d 29%")
        d = payload(rl={"five_hour": {"used_percentage": 80, "resets_at": 999}}, now=now)
        self.assertEqual(statusline.render(d, now), "5h 0% · 7d --")
        d = payload(rl={"seven_day": {"used_percentage": 99.6, "resets_at": 5000}}, now=now)
        self.assertEqual(statusline.render(d, now), "5h -- · 7d 100%")
        self.assertEqual(statusline.render({}, now), "5h -- · 7d --")


class Atomic(unittest.TestCase):
    def test_failed_write_keeps_old_file_and_no_tmp(self):
        with tempfile.TemporaryDirectory() as home:
            p = statusline.write_session(payload(), 1.0, home)
            before = rd(p)
            with mock.patch.object(statusline.json, "dump", side_effect=IOError("disk full")):
                with self.assertRaises(IOError):
                    statusline.write_session(payload(), 2.0, home)
            self.assertEqual(rd(p), before)
            self.assertEqual(os.listdir(os.path.dirname(p)), ["sess-1.json"])

    def test_uses_rename(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.object(statusline.os, "replace", wraps=os.replace) as rep:
                p = statusline.write_session(payload(), 1.0, home)
            rep.assert_called_once()
            self.assertEqual(rep.call_args[0][1], p)

    def test_main_never_raises(self):
        out = io.StringIO()
        with mock.patch.object(statusline, "write_session", side_effect=RuntimeError):
            self.assertEqual(statusline.main(io.StringIO(json.dumps(payload())), out), 0)
        self.assertIn("5h 52%", out.getvalue())


if __name__ == "__main__":
    unittest.main()
