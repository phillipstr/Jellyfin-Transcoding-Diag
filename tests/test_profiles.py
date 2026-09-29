import json
import unittest

from jftd import caps, profiles

from .helpers import fixture, read


def load_caps(name):
    return caps.VaapiBackend().parse(read("vainfo", name), "/dev/dri/renderD128")


def encoding(name):
    with open(fixture("encoding", name)) as f:
        return json.load(f)


SKYLAKE_BROKEN = {
    "vp9", "av1",
    "EnableDecodingColorDepth10Hevc", "EnableDecodingColorDepth10Vp9",
    "EnableDecodingColorDepth10HevcRext", "EnableDecodingColorDepth12HevcRext",
}


class Drift(unittest.TestCase):
    def setUp(self):
        self.skl = load_caps("intel_skylake_ihd.txt")

    def test_the_real_case(self):
        rep = profiles.config_drift(encoding("qsv_all_enabled.json"), self.skl)
        self.assertEqual({i.feature for i in rep.by_status(profiles.BROKEN)}, SKYLAKE_BROKEN)
        self.assertTrue(rep.has_problems)
        self.assertEqual(rep.notes, [])

    def test_recommended_fix(self):
        changes = profiles.recommended_settings(encoding("qsv_all_enabled.json"), self.skl)
        self.assertEqual(changes, {
            "HardwareDecodingCodecs": ["h264", "hevc", "mpeg2video", "vc1", "vp8"],
            "EnableDecodingColorDepth10Hevc": False,
            "EnableDecodingColorDepth10Vp9": False,
            "EnableDecodingColorDepth10HevcRext": False,
            "EnableDecodingColorDepth12HevcRext": False,
        })

    def test_fixed_config_has_no_drift(self):
        cfg = encoding("qsv_matched_skylake.json")
        rep = profiles.config_drift(cfg, self.skl)
        self.assertFalse(rep.has_problems)
        self.assertEqual(profiles.recommended_settings(cfg, self.skl), {})

    def test_applying_the_fix_removes_drift(self):
        cfg = encoding("qsv_all_enabled.json")
        cfg.update(profiles.recommended_settings(cfg, self.skl))
        self.assertFalse(profiles.config_drift(cfg, self.skl).has_problems)

    def test_flag_without_its_codec_is_latent(self):
        cfg = {"HardwareAccelerationType": "vaapi", "HardwareDecodingCodecs": ["h264"],
               "EnableDecodingColorDepth10Hevc": True}
        rep = profiles.config_drift(cfg, self.skl)
        self.assertEqual([i.feature for i in rep.by_status(profiles.LATENT)], ["EnableDecodingColorDepth10Hevc"])
        self.assertEqual(rep.by_status(profiles.BROKEN), [])

    def test_accel_none_is_latent_with_note(self):
        cfg = dict(encoding("qsv_all_enabled.json"), HardwareAccelerationType="none")
        rep = profiles.config_drift(cfg, self.skl)
        self.assertEqual(rep.by_status(profiles.BROKEN), [])
        self.assertTrue(rep.by_status(profiles.LATENT))
        self.assertIn("off", rep.notes[0])

    def test_unused_and_also_enable(self):
        gen12 = load_caps("intel_gen12_ihd.txt")
        cfg = dict(encoding("qsv_matched_skylake.json"), HardwareDecodingCodecs=["h264", "hevc"])
        rep = profiles.config_drift(cfg, gen12)
        self.assertIn("av1", {i.feature for i in rep.by_status(profiles.UNUSED)})
        self.assertEqual(profiles.recommended_settings(cfg, gen12), {})
        ch = profiles.recommended_settings(cfg, gen12, enable_supported=True)
        self.assertEqual(ch["HardwareDecodingCodecs"], ["h264", "hevc", "mpeg2video", "vp8", "vp9", "av1"])
        self.assertTrue(ch["EnableDecodingColorDepth10Hevc"])

    def test_codec_case_and_order_preserved(self):
        cfg = {"HardwareAccelerationType": "qsv", "HardwareDecodingCodecs": ["HEVC", "vp9", "h264"]}
        self.assertEqual(profiles.recommended_settings(cfg, self.skl)["HardwareDecodingCodecs"], ["HEVC", "h264"])

    def test_i965_with_qsv_note(self):
        c = load_caps("intel_skylake_ihd.txt")
        c.driver = "Intel i965 driver for Intel(R) Skylake - 2.4.1"
        rep = profiles.config_drift({"HardwareAccelerationType": "qsv"}, c)
        self.assertTrue(any("iHD" in n for n in rep.notes))

    def test_device_mismatch_note(self):
        rep = profiles.config_drift({"HardwareAccelerationType": "vaapi", "VaapiDevice": "/dev/dri/renderD129"}, self.skl)
        self.assertTrue(any("renderD129" in n for n in rep.notes))

    def test_nvenc_against_vaapi_note(self):
        rep = profiles.config_drift({"HardwareAccelerationType": "nvenc"}, self.skl)
        self.assertTrue(any("nvenc" in n for n in rep.notes))


class RequiredFeatures(unittest.TestCase):
    def test_cases(self):
        rf = profiles.required_features
        self.assertEqual(rf("h264", "yuv420p"), ["h264"])
        self.assertEqual(rf("hevc", "yuv420p"), ["hevc"])
        self.assertEqual(rf("hevc", "yuv420p10le"), ["hevc", "EnableDecodingColorDepth10Hevc"])
        self.assertEqual(rf("hevc", "yuv422p10le"), ["hevc", "EnableDecodingColorDepth10HevcRext"])
        self.assertEqual(rf("hevc", "yuv444p12le"), ["hevc", "EnableDecodingColorDepth12HevcRext"])
        self.assertEqual(rf("vp9", "yuv420p10le"), ["vp9", "EnableDecodingColorDepth10Vp9"])
        self.assertEqual(rf("av1", "yuv420p10le"), ["av1"])
        self.assertEqual(rf("hevc", None, "Main 10"), ["hevc", "EnableDecodingColorDepth10Hevc"])
        self.assertEqual(rf("prores", "yuv422p10le"), [])
        self.assertEqual(rf(None), [])


if __name__ == "__main__":
    unittest.main()
