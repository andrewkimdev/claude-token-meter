import io
import json
import os
import tempfile
import time
import unittest

from tests._util import wr
from ctm import cli, merge


class Cli(unittest.TestCase):
    def test_push_status_clear(self):
        with tempfile.TemporaryDirectory() as home:
            out = io.StringIO()
            self.assertEqual(cli.cmd_push_test(home, 61, 33.5, out), 0)
            names = os.listdir(merge.sessions_dir(home))
            self.assertEqual(len(names), 1)
            self.assertTrue(names[0].startswith("test-"))
            w = merge.merge(merge.load_sessions(home), time.time())
            self.assertEqual(w["five_hour"]["pct"], 61)
            self.assertEqual(w["seven_day"]["pct"], 33.5)
            out = io.StringIO()
            cli.cmd_status(home, out, find_port=lambda: None)
            self.assertIn("five_hour 61%", out.getvalue())
            self.assertIn("hold.state: (none)", out.getvalue())
            wr(os.path.join(merge.sessions_dir(home), "real.json"), "{}")
            cli.cmd_clear(home, io.StringIO())
            self.assertEqual(os.listdir(merge.sessions_dir(home)), ["real.json"])


if __name__ == "__main__":
    unittest.main()
