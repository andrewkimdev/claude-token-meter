import os
import random
import tempfile
import time
import unittest

from tests._util import wr, fixtures
from ctm import merge


def S(obs, fh=None, sd=None):
    rl = {}
    if fh:
        rl["five_hour"] = {"used_percentage": fh[0], "resets_at": fh[1]}
    if sd:
        rl["seven_day"] = {"used_percentage": sd[0], "resets_at": sd[1]}
    return {"session_id": "x", "observed_at": obs, "rate_limits": rl}


class MergeFixtures(unittest.TestCase):
    def test_fixtures(self):
        for name, fx in fixtures("merge_"):
            with self.subTest(fixture=name):
                self.assertEqual(merge.merge(fx["sessions"], fx["now"]), fx["expected"])

    def test_order_independent_and_idempotent(self):
        rnd = random.Random(0)
        for name, fx in fixtures("merge_"):
            for _ in range(10):
                ss = list(fx["sessions"])
                rnd.shuffle(ss)
                with self.subTest(fixture=name):
                    self.assertEqual(merge.merge(ss, fx["now"]), fx["expected"])
                    self.assertEqual(merge.merge(ss + ss, fx["now"]), fx["expected"])


class MergeRules(unittest.TestCase):
    now = 1000

    def test_later_reset_wins(self):
        w = merge.merge([S(1, (90, 1100)), S(2, (3, 1200))], self.now)
        self.assertEqual(w["five_hour"]["pct"], 3)
        self.assertEqual(w["five_hour"]["resets_at"], 1200)

    def test_same_reset_max(self):
        w = merge.merge([S(5, (40, 1100)), S(1, (52, 1100))], self.now)
        self.assertEqual(w["five_hour"], {"pct": 52, "resets_at": 1100, "observed_at": 5})

    def test_expired_dropped(self):
        self.assertEqual(merge.merge([S(1, (40, 1000)), S(1, (50, 999))], self.now), {})

    def test_missing_window(self):
        w = merge.merge([S(1, sd=(13, 5000))], self.now)
        self.assertNotIn("five_hour", w)
        self.assertEqual(w["seven_day"]["pct"], 13)

    def test_int_float(self):
        w = merge.merge([S(1, (41.5, 1100)), S(1, (41, 1100))], self.now)
        self.assertEqual(w["five_hour"]["pct"], 41.5)
        w = merge.merge([S(1, (41.0, 1100)), S(1, (42, 1100))], self.now)
        self.assertEqual(w["five_hour"]["pct"], 42)
        self.assertIsInstance(w["five_hour"]["pct"], int)

    def test_float_times_become_int(self):
        w = merge.merge([S(7.9, (1, 1100.6))], self.now)
        self.assertEqual(w["five_hour"], {"pct": 1, "resets_at": 1100, "observed_at": 7})

    def test_garbage_ignored(self):
        bad = [None, 3, {}, {"rate_limits": None}, {"rate_limits": {"five_hour": None}},
               {"rate_limits": {"five_hour": {"used_percentage": "5", "resets_at": 1100}}},
               {"rate_limits": {"five_hour": {"used_percentage": True, "resets_at": 1100}}},
               {"rate_limits": {"five_hour": {"used_percentage": 5}}}]
        self.assertEqual(merge.merge(bad, self.now), {})


class FilesAndPrune(unittest.TestCase):
    def test_load_skips_tmp_and_corrupt_and_prunes(self):
        with tempfile.TemporaryDirectory() as home:
            d = merge.sessions_dir(home)
            os.makedirs(d)
            wr(os.path.join(d, "a.json"), '{"session_id":"a"}')
            wr(os.path.join(d, "bad.json"), "{nope")
            wr(os.path.join(d, ".a.123.tmp"), "{}")
            wr(os.path.join(d, "old.json"), "{}")
            now = time.time()
            old = now - 9 * 86400
            os.utime(os.path.join(d, "old.json"), (old, old))
            os.utime(os.path.join(d, ".a.123.tmp"), (old, old))
            self.assertEqual(len(merge.load_sessions(home)), 2)
            self.assertEqual(set(merge.snapshot_mtimes(home)), {"a.json", "bad.json", "old.json"})
            self.assertEqual(sorted(merge.prune(home, now)), [".a.123.tmp", "old.json"])
            self.assertEqual(sorted(os.listdir(d)), ["a.json", "bad.json"])

    def test_missing_dir(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(merge.load_sessions(home), [])
            self.assertEqual(merge.prune(home), [])


if __name__ == "__main__":
    unittest.main()
