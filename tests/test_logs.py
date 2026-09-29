import os
import unittest

from jftd import logs

from .helpers import log_fixture, read

EXPECTED = {
    "ok_subtitle_burnin": (logs.OK, None),
    "ok_legacy_no_header": (logs.OK, None),
    "ok_nonfatal_decode_errors": (logs.OK, None),
    "aborted_signal15": (logs.ABORTED, None),
    "hw_decode_hevc10": (logs.FAILED, "hw_decode"),
    "hw_decode_enosys_only": (logs.FAILED, "hw_decode"),
    "hw_device_no_display": (logs.FAILED, "hw_device"),
    "disk_full": (logs.FAILED, "disk_full"),
    "hw_encode_qsv": (logs.FAILED, "hw_encode"),
    "subtitle_unreadable": (logs.FAILED, "subtitle"),
    "filter_with_subtitles_in_cmd": (logs.FAILED, "filter"),
    "input_moov_missing": (logs.FAILED, "input"),
    "other_dts": (logs.FAILED, "other"),
}


def analyze(label):
    path = log_fixture(label)
    with open(path, encoding="utf-8") as f:
        return logs.analyze(f.read(), path)


class FixtureClassification(unittest.TestCase):
    def test_every_fixture(self):
        for label, (status, cls) in EXPECTED.items():
            with self.subTest(label=label):
                r = analyze(label)
                self.assertEqual((r.status, r.cls), (status, cls), r.evidence)

    def test_every_class_has_a_fixture(self):
        covered = {cls for _, cls in EXPECTED.values() if cls}
        self.assertEqual(covered, set(logs.CLASS_BY_NAME))

    def test_failures_carry_evidence_from_ffmpeg_output(self):
        for label, (status, _) in EXPECTED.items():
            if status != logs.FAILED:
                continue
            with self.subTest(label=label):
                r = analyze(label)
                self.assertTrue(r.evidence)
                self.assertFalse(r.evidence.startswith("/usr/lib/jellyfin-ffmpeg/ffmpeg"))


class CommandLineIsNotClassified(unittest.TestCase):
    """The command line mentions subtitles=, hwaccel, etc. on runs that worked."""

    def test_split_separates_header_command_and_output(self):
        header, command, lines = logs.split_log(read("logs", os.path.basename(log_fixture("ok_subtitle_burnin"))))
        self.assertIsInstance(header, dict)
        self.assertIn("subtitles=", command)
        self.assertTrue(command.startswith("/usr/lib/jellyfin-ffmpeg/ffmpeg "))
        self.assertFalse(any("subtitles=" in ln for ln in lines))
        self.assertTrue(lines[0].startswith("ffmpeg version"))

    def test_successful_burnin_is_ok(self):
        self.assertEqual(analyze("ok_subtitle_burnin").status, logs.OK)

    def test_failed_run_with_subtitles_in_command_is_not_subtitle(self):
        self.assertEqual(analyze("filter_with_subtitles_in_cmd").cls, "filter")

    def test_classifying_the_command_line_would_have_been_wrong(self):
        # Guard the guard: if the command line were treated as output, a
        # subtitle-looking error word in it must still not decide the class.
        cmd = "/usr/lib/jellyfin-ffmpeg/ffmpeg -i x.mkv -vf \"subtitles=f='Unable to open x.ass'\" -f null -"
        c = logs.classify_output(["Conversion failed!"], cmd)
        self.assertEqual(c.cls, "other")


class Rules(unittest.TestCase):
    def test_no_marker_means_ok(self):
        c = logs.classify_output(["Invalid data found when processing input", "Error while filtering: x"])
        self.assertEqual(c.status, logs.OK)

    def test_each_marker_makes_a_failure(self):
        for m in [
            "Conversion failed!",
            "[vf#0:0 @ 0x1] Task finished with error code: -22 (Invalid argument)",
            "[vf#0:0 @ 0x1] Error reinitializing filters!",
            "[vf#0:0 @ 0x1] Terminating thread with return code -5 (Input/output error)",
            "Error while opening encoder for output stream #0:0",
            "Error opening input files: Invalid data found when processing input",
        ]:
            with self.subTest(marker=m):
                self.assertEqual(logs.classify_output([m]).status, logs.FAILED)

    def test_signal_15_is_aborted_even_with_error_lines(self):
        c = logs.classify_output([
            "Error while decoding stream #0:0: Invalid data found when processing input",
            "Conversion failed!",
            "Exiting normally, received signal 15.",
        ])
        self.assertEqual(c.status, logs.ABORTED)

    def test_unambiguous_classes_need_no_marker_and_beat_abort(self):
        c = logs.classify_output(["av_interleaved_write_frame(): No space left on device",
                                  "Exiting normally, received signal 15."])
        self.assertEqual((c.status, c.cls), (logs.FAILED, "disk_full"))
        c = logs.classify_output(["Failed to initialise VAAPI connection: -1 (unknown libva error)."])
        self.assertEqual((c.status, c.cls), (logs.FAILED, "hw_device"))

    def test_enosys_with_hwaccel_is_hw_decode_without_it_is_filter(self):
        lines = ["[vf#0:0 @ 0x1] Error reinitializing filters!",
                 "[vf#0:0 @ 0x1] Task finished with error code: -38 (Function not implemented)"]
        self.assertEqual(logs.classify_output(lines, "ffmpeg -hwaccel vaapi -i a.mkv out").cls, "hw_decode")
        self.assertEqual(logs.classify_output(lines, "ffmpeg -i a.mkv out").cls, "filter")

    def test_first_error_code_is_kept(self):
        self.assertEqual(analyze("hw_decode_hevc10").error_code, -38)

    def test_crlf_and_pretty_json_header(self):
        text = (
            '{\r\n  "Path": "/media/x.mkv",\r\n  "MediaStreams": [{"Type": "Video", "Codec": "hevc", '
            '"Profile": "Main 10", "PixelFormat": "yuv420p10le"}]\r\n}\r\n\r\n'
            "/usr/bin/ffmpeg -i file:/media/x.mkv -f null -\r\n\r\n"
            "ffmpeg version 7\r\nConversion failed!\r\n"
        )
        r = logs.analyze(text, "/logs/FFmpeg.Transcode-x.log")
        self.assertEqual(r.status, logs.FAILED)
        self.assertEqual(r.media_path, "/media/x.mkv")
        self.assertEqual((r.video.codec, r.video.bit_depth), ("hevc", 10))
        self.assertEqual(r.kind, "Transcode")

    def test_windows_command_line(self):
        text = '"C:\\Program Files\\Jellyfin\\Server\\ffmpeg.exe" -i file:"D:\\a.mkv" -vf subtitles=x -f null -\n\nConversion failed!\n'
        header, command, lines = logs.split_log(text)
        self.assertIsNone(header)
        self.assertIn("ffmpeg.exe", command)
        self.assertEqual(lines, ["Conversion failed!"])


class Metadata(unittest.TestCase):
    def test_header_video_and_path(self):
        r = analyze("hw_decode_hevc10")
        self.assertEqual((r.video.codec, r.video.profile, r.video.bit_depth), ("hevc", "Main 10", 10))
        self.assertTrue(r.media_path.endswith("Example Series - S01E02.mkv"))
        self.assertEqual((r.hwaccel, r.encoder), ("vaapi", "h264_qsv"))

    def test_legacy_log_uses_output_and_command(self):
        r = analyze("ok_legacy_no_header")
        self.assertEqual((r.video.codec, r.video.profile, r.video.bit_depth), ("h264", "High", 8))
        self.assertTrue(r.media_path.startswith("/media/movies/"))
        self.assertEqual(r.kind, "DirectStream")

    def test_output_stream_not_mistaken_for_input(self):
        v = logs.video_from_output([
            "Output #0, hls, to 'x.m3u8':",
            "  Stream #0:0: Video: h264, qsv(tv), 1920x1080",
        ])
        self.assertIsNone(v)


class ExitCodes(unittest.TestCase):
    def test_218_is_enosys(self):
        s = logs.explain_exit_code(218)
        self.assertIn("-38", s)
        self.assertIn("ENOSYS", s)

    def test_others(self):
        self.assertIn("EINVAL", logs.explain_exit_code(234))
        self.assertIn("signal", logs.explain_exit_code(255))
        self.assertIn("generic", logs.explain_exit_code(1))
        self.assertIn("ENOSYS", logs.explain_exit_code(-38))

    def test_server_log_counts(self):
        counts = logs.server_exit_codes(read("logs", "log_20260105.log"))
        self.assertEqual(counts, {0: 1, 218: 2, 234: 1, 1: 1})


if __name__ == "__main__":
    unittest.main()
