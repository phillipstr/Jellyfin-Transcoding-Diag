import json
import unittest

from jftd import caps, hwtest

from .helpers import FakeRunner, read

PROBE_HEVC10 = json.dumps({"streams": [{"codec_name": "hevc", "profile": "Main 10", "pix_fmt": "yuv420p10le",
                                        "width": 3840, "height": 2160}]})
OK_STDERR = "frame=  719 fps=574 q=-0.0 Lsize=N/A time=00:00:30.01 bitrate=N/A speed=23.9x\n"
HW_FAIL_STDERR = (
    "[hevc @ 0x1] Failed setup for format vaapi: hwaccel initialisation returned error.\n"
    "[vf#0:0 @ 0x2] Error reinitializing filters!\n"
    "[vf#0:0 @ 0x2] Task finished with error code: -38 (Function not implemented)\n"
    "Conversion failed!\n"
)
ENC_FAIL_STDERR = (
    "[h264_qsv @ 0x1] Error initializing the encoder: unsupported (-3)\n"
    "Error while opening encoder - maybe incorrect parameters such as bit_rate, rate, width or height.\n"
    "Conversion failed!\n"
)


def runner(hw, sw):
    def ffmpeg(argv):
        return hw if "-hwaccel" in argv else sw
    return FakeRunner({"ffprobe": (0, PROBE_HEVC10, ""), "ffmpeg": ffmpeg})


class Commands(unittest.TestCase):
    def test_hw_and_sw_differ_only_in_decode(self):
        c = hwtest.build_commands("ffmpeg", "/m/a.mkv", 30, "/dev/dri/renderD128", "qsv")
        self.assertIn("-hwaccel", c["hw"])
        self.assertNotIn("-hwaccel", c["sw"])
        for argv in c.values():
            self.assertIn("h264_qsv", argv)
            self.assertIn("vaapi=va:/dev/dri/renderD128", argv)
            self.assertEqual(argv[argv.index("-t") + 1], "30")
            self.assertLess(argv.index("-i"), argv.index("-t"))  # -t limits output, not input

    def test_vaapi_mode(self):
        c = hwtest.build_commands("ffmpeg", "/m/a.mkv", 5, "/dev/dri/renderD128", "vaapi")
        self.assertIn("h264_vaapi", c["hw"])
        self.assertNotIn("qsv=qs@va", c["sw"])


class Verdicts(unittest.TestCase):
    def run_test(self, hw, sw):
        return hwtest.run_test(runner(hw, sw), "ffmpeg", "ffprobe", "/m/a.mkv", 30, "/dev/dri/renderD128", "qsv")

    def test_hw_decode_fails(self):
        res = self.run_test((187, "", HW_FAIL_STDERR), (0, "", OK_STDERR))
        self.assertEqual(res["verdict"], hwtest.HW_DECODE_FAILS)
        hw, sw = res["runs"]
        self.assertEqual(hw["cls"], "hw_decode")
        self.assertEqual(sw["speed"], 23.9)
        self.assertEqual(sw["media_seconds"], 30.01)
        self.assertEqual(res["needs"], ["hevc", "EnableDecodingColorDepth10Hevc"])

    def test_hw_ok(self):
        self.assertEqual(self.run_test((0, "", OK_STDERR), (0, "", OK_STDERR))["verdict"], hwtest.HW_OK)

    def test_both_fail(self):
        res = self.run_test((187, "", ENC_FAIL_STDERR), (187, "", ENC_FAIL_STDERR))
        self.assertEqual(res["verdict"], hwtest.BOTH_FAIL)
        self.assertEqual(res["runs"][1]["cls"], "hw_encode")

    def test_no_device(self):
        err = "[AVHWDeviceContext @ 0x1] No VA display found for device /dev/dri/renderD128.\n"
        self.assertEqual(self.run_test((234, "", err), (234, "", err))["verdict"], hwtest.NO_DEVICE)

    def test_nonzero_exit_without_known_marker_is_failure(self):
        res = self.run_test((1, "", "something odd\n"), (0, "", OK_STDERR))
        self.assertFalse(res["runs"][0]["ok"])
        self.assertEqual(res["runs"][0]["evidence"], "something odd")

    def test_format_uses_caps(self):
        res = self.run_test((187, "", HW_FAIL_STDERR), (0, "", OK_STDERR))
        c = caps.VaapiBackend().parse(read("vainfo", "intel_skylake_ihd.txt"))
        text = hwtest.format_result(res, c)
        self.assertIn("GPU: NO (missing VAProfileHEVCMain10)", text)
        self.assertIn("Verdict: HW_DECODE_FAILS", text)
        self.assertIn("23.9x realtime", text)


if __name__ == "__main__":
    unittest.main()
