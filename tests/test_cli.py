import io
import json
import os

from jellyfin_transcode_diag.cli import EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE, main
from jellyfin_transcode_diag.report import redact

from conftest import FIXTURES


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def test_error_findings_exit_nonzero(capsys):
    code, out, _ = run(capsys, str(FIXTURES / "vaapi_init_failure.log"))

    assert code == EXIT_PROBLEMS
    assert "[ERROR] Hardware acceleration device could not be opened" in out
    assert "line 9:" in out


def test_warnings_only_exit_zero(capsys):
    code, out, _ = run(capsys, str(FIXTURES / "nvenc_slow.log"))

    assert code == EXIT_OK
    assert "[WARNING] Transcoding is slower than real time" in out


def test_json_output(capsys):
    code, out, _ = run(capsys, "--json", str(FIXTURES / "stock_ffmpeg_tonemap.log"))
    (report,) = json.loads(out)

    assert code == EXIT_PROBLEMS
    assert report["hwaccel"] == "vaapi"
    assert [f["id"] for f in report["findings"]] == ["tonemap-failed", "non-jellyfin-ffmpeg"]
    assert report["findings"][0]["evidence"][0]["line"] == 5


def test_redact_hides_paths_with_spaces(capsys):
    _, out, _ = run(capsys, "--redact", str(FIXTURES / "vaapi_init_failure.log"))

    assert "Example Movie" not in out
    assert "/config/" not in out
    # Device nodes stay visible; they are what the finding is about.
    assert "/dev/dri/renderD128" in out


def test_redact_patterns():
    assert redact("from 'file:/media/tv/A Show/ep 1.mkv':") == "from '<path>':"
    assert redact("open C:\\Media\\film.mkv failed") == "open <path> failed"
    assert redact("GET http://192.168.1.20:8096/x") == "GET <url>"
    assert redact("host 10.0.0.5 down") == "host <ip> down"
    assert redact("device /dev/dri/renderD128") == "device /dev/dri/renderD128"


def test_directory_picks_most_recent_logs(tmp_path, capsys):
    for age, name in enumerate(["FFmpeg.Transcode-new.log", "FFmpeg.Transcode-old.log"]):
        path = tmp_path / name
        path.write_text("No space left on device\n")
        os.utime(path, (1_000_000 - age, 1_000_000 - age))
    (tmp_path / "log_20260101.log").write_text("Permission denied\n")

    _, out, _ = run(capsys, str(tmp_path))
    assert "Transcode-new" in out and "Transcode-old" not in out

    _, out, _ = run(capsys, "--latest", "5", str(tmp_path))
    assert "Transcode-old" in out and "log_2026" not in out


def test_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("No space left on device\n"))
    code, out, _ = run(capsys, "-")

    assert code == EXIT_PROBLEMS
    assert "== <stdin> ==" in out
    assert "(disk-full)" in out


def test_missing_path_is_usage_error(capsys):
    code, _, err = run(capsys, "/nonexistent/FFmpeg.log")

    assert code == EXIT_USAGE
    assert "no such file or directory" in err


def test_list_rules(capsys):
    code, out, _ = run(capsys, "--list-rules")

    assert code == EXIT_OK
    assert "hwaccel-init" in out and "slow-transcode" in out
