import json
import os
import stat
import tempfile
import unittest

from jftd import jellyfin

from .fake_server import KEY, FakeJellyfin
from .helpers import fixture


def encoding():
    with open(fixture("encoding", "qsv_all_enabled.json")) as f:
        return json.load(f)


class TypeChecks(unittest.TestCase):
    def test_accepts_matching_types(self):
        jellyfin.check_types(encoding(), {"EnableDecodingColorDepth10Hevc": False, "HardwareDecodingCodecs": ["h264"]})

    def test_rejects(self):
        cur = encoding()
        for bad in (
            {"EnableDecodingColorDepth10Hevc": 0},
            {"EnableDecodingColorDepth10Hevc": "false"},
            {"HardwareDecodingCodecs": "h264"},
            {"HardwareDecodingCodecs": [1]},
            {"H264Crf": True},
            {"H264Crf": "23"},
            {"NoSuchSetting": True},
        ):
            with self.subTest(bad=bad), self.assertRaises(jellyfin.JellyfinError):
                jellyfin.check_types(cur, bad)


class ClientAgainstFakeServer(unittest.TestCase):
    def test_get_uses_token_header(self):
        with FakeJellyfin(encoding()) as srv:
            cfg = jellyfin.Client(srv.url, KEY).get_encoding()
        self.assertEqual(cfg["HardwareAccelerationType"], "qsv")
        self.assertTrue(srv.auth_headers[0].startswith("MediaBrowser "))

    def test_bad_key_message_does_not_leak_key(self):
        secret = "f" * 32
        with FakeJellyfin(encoding()) as srv, self.assertRaises(jellyfin.JellyfinError) as cm:
            jellyfin.Client(srv.url, secret).get_encoding()
        self.assertIn("401", str(cm.exception))
        self.assertNotIn(secret, str(cm.exception))

    def test_missing_key(self):
        with FakeJellyfin(encoding()) as srv, self.assertRaises(jellyfin.JellyfinError) as cm:
            jellyfin.Client(srv.url, None).get_encoding()
        self.assertIn("JELLYFIN_API_KEY", str(cm.exception))

    def test_unreachable(self):
        with self.assertRaises(jellyfin.JellyfinError):
            jellyfin.Client("http://127.0.0.1:9", KEY, timeout=2).get_encoding()

    def test_apply_writes_full_object_backs_up_and_verifies(self):
        changes = {"EnableDecodingColorDepth10Hevc": False, "HardwareDecodingCodecs": ["h264", "hevc"]}
        with tempfile.TemporaryDirectory() as d, FakeJellyfin(encoding()) as srv:
            before, after, backup = jellyfin.apply_changes(jellyfin.Client(srv.url, KEY), changes, d)
            self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)
            with open(backup) as f:
                self.assertEqual(json.load(f), before)
        self.assertEqual(len(srv.posts), 1)
        self.assertEqual(set(srv.posts[0]), set(before))  # full object, not a partial
        self.assertFalse(after["EnableDecodingColorDepth10Hevc"])
        self.assertEqual(after["HardwareDecodingCodecs"], ["h264", "hevc"])

    def test_readback_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as d, FakeJellyfin(encoding(), drop_keys={"EnableDecodingColorDepth10Hevc"}) as srv:
            with self.assertRaises(jellyfin.JellyfinError) as cm:
                jellyfin.apply_changes(jellyfin.Client(srv.url, KEY), {"EnableDecodingColorDepth10Hevc": False}, d)
        self.assertIn("read-back", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
