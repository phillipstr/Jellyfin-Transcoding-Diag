import unittest

from jftd import caps, profiles

from .helpers import FakeRunner, read


def parse(name):
    return caps.VaapiBackend().parse(read("vainfo", name), "/dev/dri/renderD128")


class VaapiParse(unittest.TestCase):
    def test_skylake_hd530(self):
        c = parse("intel_skylake_ihd.txt")
        yes = {f for f in profiles.FEATURES if c.supports(f)}
        self.assertEqual(yes, {"h264", "hevc", "mpeg2video", "vc1", "vp8"})
        self.assertIn("VAProfileHEVCMain", c.decode)
        self.assertNotIn("VAProfileHEVCMain10", c.decode)
        self.assertEqual(c.vendor, "intel")
        self.assertEqual(c.driver, "Intel iHD driver for Intel(R) Gen Graphics - 24.1.0")

    def test_encode_entrypoints_are_not_decode(self):
        c = parse("intel_skylake_ihd.txt")
        self.assertIn("VAProfileH264High", c.encode)
        self.assertIn("VAProfileJPEGBaseline", c.encode)
        self.assertNotIn("VAProfileNone", c.decode)

    def test_gen12_supports_10bit_vp9_av1_rext(self):
        c = parse("intel_gen12_ihd.txt")
        no = {f for f in profiles.FEATURES if not c.supports(f)}
        self.assertEqual(no, {"mpeg4", "vc1"})

    def test_amd_radeonsi(self):
        c = parse("amd_radeonsi.txt")
        self.assertEqual(c.vendor, "amd")
        self.assertTrue(c.supports("EnableDecodingColorDepth10Hevc"))
        self.assertTrue(c.supports("EnableDecodingColorDepth10Vp9"))
        self.assertFalse(c.supports("EnableDecodingColorDepth10HevcRext"))

    def test_flag_needs_its_codec(self):
        text = "vainfo: Supported profile and entrypoints\n  VAProfileVP9Profile2 : VAEntrypointVLD\n"
        c = caps.VaapiBackend().parse(text)
        self.assertFalse(c.supports("EnableDecodingColorDepth10Vp9"))

    def test_garbage_raises(self):
        with self.assertRaises(caps.CapsError):
            caps.VaapiBackend().parse("nothing useful")

    def test_to_dict_is_json_friendly(self):
        import json
        json.dumps(parse("amd_radeonsi.txt").to_dict())


class VaapiProbe(unittest.TestCase):
    def test_probe_runs_vainfo_on_device(self):
        r = FakeRunner({"vainfo": (0, read("vainfo", "intel_skylake_ihd.txt"), "")})
        c = caps.VaapiBackend().probe(r, "vainfo", "/dev/dri/renderD129")
        self.assertEqual(r.calls[0], ["vainfo", "--display", "drm", "--device", "/dev/dri/renderD129"])
        self.assertEqual(c.device, "/dev/dri/renderD129")

    def test_permission_error_mentions_render_group(self):
        r = FakeRunner({"vainfo": (1, "", read("vainfo", "permission_denied.txt"))})
        with self.assertRaises(caps.CapsError) as cm:
            caps.VaapiBackend().probe(r, "vainfo", "/dev/dri/renderD128")
        self.assertIn("render", str(cm.exception))

    def test_nvdec_not_implemented(self):
        with self.assertRaises(caps.CapsError):
            caps.get_backend("nvdec").probe(FakeRunner(), "vainfo", "/dev/dri/renderD128")

    def test_backend_for_accel(self):
        self.assertEqual(caps.backend_for_accel("qsv"), "vaapi")
        self.assertEqual(caps.backend_for_accel("nvenc"), "nvdec")
        self.assertEqual(caps.backend_for_accel(None), "vaapi")


if __name__ == "__main__":
    unittest.main()
