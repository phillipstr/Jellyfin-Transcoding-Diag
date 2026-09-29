import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from jftd import cli

from .fake_server import KEY, FakeJellyfin
from .helpers import FakeRunner, fixture
from .test_hwtest import HW_FAIL_STDERR, OK_STDERR, PROBE_HEVC10

SKL = fixture("vainfo", "intel_skylake_ihd.txt")
BROKEN = fixture("encoding", "qsv_all_enabled.json")
FIXED = fixture("encoding", "qsv_matched_skylake.json")
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith(("JELLYFIN_", "JFTD_"))}


def run(*argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, {**CLEAN_ENV, **(env or {})}, clear=True), \
            redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Scan(unittest.TestCase):
    def test_text(self):
        code, out, _ = run("scan", "--log-dir", fixture("logs"))
        self.assertEqual(code, 1)
        self.assertIn("aborted       1", out)
        self.assertIn("hw_decode      2", out)
        self.assertIn("218 = -38 = ENOSYS", out)

    def test_json_and_redact(self):
        code, out, _ = run("scan", "--log-dir", fixture("logs"), "--json", "--redact", "-v")
        data = json.loads(out)
        self.assertEqual(data["status"], {"ok": 3, "aborted": 1, "failed": 9})
        self.assertNotIn("Example Series", out)
        self.assertEqual(len(data["logs"]), 13)

    def test_state_file_only_scans_new_logs(self):
        with tempfile.TemporaryDirectory() as d:
            logs = os.path.join(d, "logs")
            shutil.copytree(fixture("logs"), logs)
            state = os.path.join(d, "state.json")
            self.assertEqual(run("scan", "--log-dir", logs, "--state", state)[0], 1)
            code, out, _ = run("scan", "--log-dir", logs, "--state", state)
            self.assertEqual(code, 0)
            self.assertIn("Scanned 0 FFmpeg logs", out)

    def test_missing_dir_is_error(self):
        code, _, err = run("scan", "--log-dir", "/nonexistent/jftd")
        self.assertEqual(code, 2)
        self.assertIn("no such directory", err)

    def test_bad_since(self):
        self.assertEqual(run("scan", "--log-dir", fixture("logs"), "--since", "soon")[0], 2)


class CapsAndDrift(unittest.TestCase):
    def test_caps(self):
        code, out, _ = run("caps", "--vainfo-output", SKL)
        self.assertEqual(code, 0)
        self.assertRegex(out, r"HEVC 10bit\s+no\s+needs VAProfileHEVCMain10")

    def test_drift_broken(self):
        code, out, _ = run("drift", "--vainfo-output", SKL, "--encoding-json", BROKEN)
        self.assertEqual(code, 1)
        self.assertIn("untick: VP9, AV1, HEVC 10bit", out)
        self.assertIn('"EnableDecodingColorDepth10Hevc": false', out)

    def test_drift_clean(self):
        code, out, _ = run("drift", "--vainfo-output", SKL, "--encoding-json", FIXED)
        self.assertEqual(code, 0)
        self.assertIn("No drift", out)

    def test_drift_json(self):
        code, out, _ = run("drift", "--vainfo-output", SKL, "--encoding-json", BROKEN, "--json")
        data = json.loads(out)
        self.assertFalse(data["recommended_changes"]["EnableDecodingColorDepth10Hevc"])

    def test_drift_over_api(self):
        with open(BROKEN) as f, FakeJellyfin(json.load(f)) as srv:
            code, out, _ = run("drift", "--vainfo-output", SKL, "--url", srv.url, env={"JELLYFIN_API_KEY": KEY})
        self.assertEqual(code, 1)
        self.assertNotIn(KEY, out)

    def test_api_key_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".key", delete=False) as kf:
            kf.write(KEY + "\n")
        try:
            with open(FIXED) as f, FakeJellyfin(json.load(f)) as srv:
                code, _, _ = run("drift", "--vainfo-output", SKL, "--url", srv.url, "--api-key-file", kf.name)
            self.assertEqual(code, 0)
        finally:
            os.unlink(kf.name)


class Fix(unittest.TestCase):
    def setUp(self):
        with open(BROKEN) as f:
            self.cfg = json.load(f)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def fix(self, srv, *extra, tty=False):
        with mock.patch("sys.stdin.isatty", return_value=tty):
            return run("fix", "--vainfo-output", SKL, "--url", srv.url, "--backup-dir", self.tmp, *extra,
                       env={"JELLYFIN_API_KEY": KEY})

    def test_refuses_without_confirmation_when_not_interactive(self):
        with FakeJellyfin(self.cfg) as srv:
            code, _, err = self.fix(srv)
        self.assertEqual(code, 2)
        self.assertIn("--yes", err)
        self.assertEqual(srv.posts, [])

    def test_interactive_no_does_nothing(self):
        with FakeJellyfin(self.cfg) as srv, mock.patch("builtins.input", return_value="n"):
            code, out, _ = self.fix(srv, tty=True)
        self.assertEqual(code, 1)
        self.assertIn("Not applied", out)
        self.assertEqual(srv.posts, [])

    def test_yes_applies_verifies_and_restore_undoes(self):
        with FakeJellyfin(self.cfg) as srv:
            code, out, _ = self.fix(srv, "--yes")
            self.assertEqual(code, 0, out)
            self.assertIn("verified", out)
            self.assertFalse(srv.encoding["EnableDecodingColorDepth10Hevc"])
            self.assertEqual(srv.encoding["HardwareDecodingCodecs"], ["h264", "hevc", "mpeg2video", "vc1", "vp8"])
            # second run: nothing to do
            self.assertIn("Nothing to change", self.fix(srv, "--yes")[1])
            backup = os.path.join(self.tmp, os.listdir(self.tmp)[0])
            code, out, _ = self.fix(srv, "--yes", "--restore", backup)
            self.assertEqual(code, 0, out)
            self.assertEqual(srv.encoding, self.cfg)

    def test_fix_rejects_offline_config(self):
        code, _, err = run("fix", "--encoding-json", BROKEN, "--yes")
        self.assertEqual(code, 2)


class TestCommand(unittest.TestCase):
    def test_hw_decode_fails_verdict(self):
        def ffmpeg(argv):
            return (187, "", HW_FAIL_STDERR) if "-hwaccel" in argv else (0, "", OK_STDERR)
        fake = FakeRunner({"ffprobe": (0, PROBE_HEVC10, ""), "ffmpeg": ffmpeg}, files={"/m/a.mkv"})
        with mock.patch.object(cli, "make_runner", return_value=fake):
            code, out, _ = run("test", "/m/a.mkv", "--vainfo-output", SKL, "--seconds", "10")
        self.assertEqual(code, 1)
        self.assertIn("Verdict: HW_DECODE_FAILS", out)
        self.assertIn("missing VAProfileHEVCMain10", out)

    def test_missing_file(self):
        with mock.patch.object(cli, "make_runner", return_value=FakeRunner()):
            code, _, err = run("test", "/m/none.mkv")
        self.assertEqual(code, 2)


class Misc(unittest.TestCase):
    def test_exit_code(self):
        code, out, _ = run("exit-code", "218")
        self.assertEqual(code, 0)
        self.assertIn("ENOSYS", out)

    def test_version(self):
        with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()) as out:
            cli.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("jftd 0.1.0", out.getvalue())

    def test_docker_defaults(self):
        args = cli.build_parser().parse_args(["scan", "--docker", "jellyfin"])
        self.assertEqual(cli.log_dir(args), "/config/log")
        self.assertEqual(cli.make_runner(args).wrap(["vainfo"]), ["docker", "exec", "jellyfin", "vainfo"])
        self.assertEqual(cli.tool_path(args, cli.make_runner(args), "vainfo"), "/usr/lib/jellyfin-ffmpeg/vainfo")


if __name__ == "__main__":
    unittest.main()
