import json
import unittest

from tests._util import fixtures
from ctm import proto


class FrameFixtures(unittest.TestCase):
    def test_device_reference_verdicts(self):
        for name, fx in fixtures("frame_"):
            with self.subTest(fixture=name):
                self.assertEqual(proto.device_accept(fx["line"], fx["last_seq"]), fx["expected"])

    def test_host_encoding_matches_valid_fixture(self):
        fx = dict(fixtures("frame_valid"))["frame_valid.json"]
        f = json.loads(fx["line"])
        b = proto.encode_frame(proto.build_frame(f["seq"], f["sent_at"], f["windows"]))
        self.assertEqual(b, (fx["line"] + "\n").encode())

    def test_host_frames_accepted_by_reference(self):
        for name, fx in fixtures("merge_"):
            from ctm import merge
            w = merge.merge(fx["sessions"], fx["now"])
            line = proto.encode_frame(proto.build_frame(1, fx["now"] + 0.7, w)).decode()
            self.assertTrue(line.endswith("\n") and line.count("\n") == 1)
            r = proto.device_accept(line.rstrip("\n"), None)
            self.assertEqual(r["result"], "ack", name)
            self.assertEqual(json.loads(line)["sent_at"], fx["now"])


class Build(unittest.TestCase):
    def test_shape(self):
        f = proto.build_frame(3, 100.9, {})
        self.assertEqual(f, {"t": "state", "v": 1, "seq": 3, "sent_at": 100, "windows": {}})

    def test_too_long(self):
        with self.assertRaises(ValueError):
            proto.encode_frame(proto.build_frame(1, 1, {"x": "y" * 2000}))


class DeviceLines(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(proto.parse_device_line('@{"t":"ack","seq":17}'), {"t": "ack", "seq": 17})
        self.assertEqual(proto.parse_device_line('@{"t":"hello","fw":"0.1.0","board":"t-display"}')["t"], "hello")
        self.assertIsNone(proto.parse_device_line("[diag] boot"))
        self.assertIsNone(proto.parse_device_line("@garbage"))
        self.assertIsNone(proto.parse_device_line("@[1]"))
        self.assertIsNone(proto.parse_device_line('@{"seq":1}'))


if __name__ == "__main__":
    unittest.main()


class TestBootJunkPrefix(unittest.TestCase):
    def test_hello_glued_to_rom_banner(self):
        from ctm import proto
        line = "��0x40�05�SH�@{\"t\":\"hello\",\"fw\":\"0.1.0\",\"board\":\"t-display\"}"
        self.assertEqual(proto.parse_device_line(line)["t"], "hello")

    def test_plain_log_still_ignored(self):
        from ctm import proto
        self.assertIsNone(proto.parse_device_line("[diag] boot ok"))
