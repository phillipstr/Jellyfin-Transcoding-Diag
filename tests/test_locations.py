import os
import stat

import pytest

from jellyfin_transcode_diag import cli, locations
from jellyfin_transcode_diag.cli import EXIT_OK, EXIT_USAGE, main
from jellyfin_transcode_diag.locations import (
    FfprobeNotFound,
    LogDirNotFound,
    find_ffprobe,
    find_log_dir,
    log_dir_candidates,
)

from conftest import FIXTURES

real_running_jellyfin = locations.running_jellyfin


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Keep the real machine's Jellyfin, defaults files and home folder out of the tests."""
    monkeypatch.setattr(locations, "running_jellyfin", lambda: iter(()))
    monkeypatch.setattr(locations, "DEFAULTS_FILES", ())
    monkeypatch.setattr(locations, "JELLYFIN_FFPROBE_PATHS", ())
    monkeypatch.setattr(locations.shutil, "which", lambda name: None)
    for name in list(os.environ):
        if name.startswith("JELLYFIN_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


def make_log_dir(path):
    path.mkdir(parents=True)
    (path / "FFmpeg.Transcode-x.log").write_text((FIXTURES / "vaapi_init_failure.log").read_text())
    return path


def make_ffprobe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_log_dir_from_jellyfin_env(tmp_path):
    logs = make_log_dir(tmp_path / "custom" / "logs")
    found = find_log_dir(environ={"JELLYFIN_LOG_DIR": str(logs)})
    assert found.path == str(logs)
    assert "JELLYFIN_LOG_DIR" in found.source


def test_log_dir_from_data_dir_env(tmp_path):
    logs = make_log_dir(tmp_path / "data" / "log")
    found = find_log_dir(environ={"JELLYFIN_DATA_DIR": str(tmp_path / "data")})
    assert found.path == str(logs)


def test_log_dir_from_jellyfin_dir_option(tmp_path):
    logs = make_log_dir(tmp_path / "srv" / "jellyfin" / "log")
    assert find_log_dir(str(tmp_path / "srv" / "jellyfin"), environ={}).path == str(logs)


def test_log_dir_from_defaults_file(tmp_path, monkeypatch):
    logs = make_log_dir(tmp_path / "var" / "log")
    defaults = tmp_path / "jellyfin.default"
    defaults.write_text(f'# comment\nexport JELLYFIN_LOG_DIR="{logs}"\n')
    monkeypatch.setattr(locations, "DEFAULTS_FILES", (str(defaults),))
    found = find_log_dir(environ={})
    assert found.path == str(logs)
    assert str(defaults) in found.source


def test_log_dir_from_running_jellyfin(tmp_path, monkeypatch):
    logs = make_log_dir(tmp_path / "jf-logs")
    process = (["/opt/jellyfin/jellyfin", f"--logdir={logs}", "-d", "/elsewhere"], {})
    monkeypatch.setattr(locations, "running_jellyfin", lambda: iter([process]))
    found = find_log_dir(environ={})
    assert found.path == str(logs)
    assert "command line" in found.source


def test_running_jellyfin_reads_proc(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    for pid, cmdline in [("1", b"/sbin/init\0"), ("42", b"dotnet\0/opt/jf/jellyfin.dll\0-l\0/logs\0")]:
        (proc / pid).mkdir(parents=True)
        (proc / pid / "cmdline").write_bytes(cmdline)
    (proc / "42" / "environ").write_bytes(b"JELLYFIN_DATA_DIR=/data\0")
    monkeypatch.setattr(locations, "PROC", str(proc))
    assert list(real_running_jellyfin()) == [
        (["dotnet", "/opt/jf/jellyfin.dll", "-l", "/logs"], {"JELLYFIN_DATA_DIR": "/data"}),
    ]


def test_standard_places_are_listed():
    paths = [c.path for c in log_dir_candidates(environ={"ProgramData": "C:\\ProgramData"})]
    assert "/var/log/jellyfin" in paths
    assert "/config/log" in paths
    assert os.path.join("C:\\ProgramData", "Jellyfin", "Server", "log") in paths


def test_log_dir_not_found_lists_what_was_tried(tmp_path):
    (tmp_path / "empty" / "log").mkdir(parents=True)
    with pytest.raises(LogDirNotFound) as error:
        find_log_dir(str(tmp_path / "empty"), environ={})
    message = str(error.value)
    assert f"{tmp_path / 'empty' / 'log'} (--jellyfin-dir): no FFmpeg.*.log files" in message
    assert "/var/log/jellyfin (Debian, Ubuntu and RPM packages): not found" in message
    assert "JELLYFIN_LOG_DIR" in message


def test_ffprobe_beside_jellyfin_ffmpeg_env(tmp_path):
    ffprobe = make_ffprobe(tmp_path / "jf-ffmpeg" / "ffprobe")
    found = find_ffprobe(environ={"JELLYFIN_FFMPEG": str(tmp_path / "jf-ffmpeg" / "ffmpeg")})
    assert found.path == str(ffprobe)


def test_ffprobe_from_packaged_ffmpeg_opt(tmp_path, monkeypatch):
    ffprobe = make_ffprobe(tmp_path / "jf-ffmpeg" / "ffprobe")
    defaults = tmp_path / "jellyfin.default"
    defaults.write_text(f'JELLYFIN_FFMPEG_OPT="--ffmpeg={tmp_path / "jf-ffmpeg" / "ffmpeg"}"\n')
    monkeypatch.setattr(locations, "DEFAULTS_FILES", (str(defaults),))
    assert find_ffprobe(environ={}).path == str(ffprobe)


def test_ffprobe_from_encoding_xml(tmp_path):
    ffprobe = make_ffprobe(tmp_path / "opt" / "ffmpeg" / "ffprobe")
    config = tmp_path / "jf" / "config"
    config.mkdir(parents=True)
    (config / "encoding.xml").write_text(
        "<?xml version=\"1.0\"?>\n<EncodingOptions>"
        f"<EncoderAppPath>{tmp_path / 'opt' / 'ffmpeg' / 'ffmpeg'}</EncoderAppPath>"
        "</EncodingOptions>\n"
    )
    found = find_ffprobe(jellyfin_dir=str(tmp_path / "jf"), environ={})
    assert found.path == str(ffprobe)
    assert "encoding.xml" in found.source


def test_ffprobe_in_jellyfin_dir_and_explicit_folder(tmp_path):
    ffprobe = make_ffprobe(tmp_path / "jf" / "ffprobe")
    assert find_ffprobe(jellyfin_dir=str(tmp_path / "jf"), environ={}).path == str(ffprobe)
    assert find_ffprobe(str(tmp_path / "jf"), environ={}).path == str(ffprobe)


def test_ffprobe_prefers_jellyfins_over_path(tmp_path, monkeypatch):
    ffprobe = make_ffprobe(tmp_path / "jf" / "ffprobe")
    monkeypatch.setattr(locations.shutil, "which", lambda name: "/usr/bin/ffprobe")
    assert find_ffprobe(environ={"JELLYFIN_FFMPEG": str(tmp_path / "jf" / "ffmpeg")}).path == str(ffprobe)
    assert find_ffprobe(environ={}).path == "/usr/bin/ffprobe"


def test_ffprobe_not_found_lists_what_was_tried(tmp_path):
    with pytest.raises(FfprobeNotFound) as error:
        find_ffprobe(environ={"JELLYFIN_FFMPEG": "/nowhere/ffmpeg"})
    assert "/nowhere/ffprobe" in str(error.value)


def test_log_cli_finds_logs_without_a_path(tmp_path, monkeypatch, capsys):
    logs = make_log_dir(tmp_path / "custom")
    monkeypatch.setenv("JELLYFIN_LOG_DIR", str(logs))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    main([])
    out, err = capsys.readouterr()
    assert f"Reading logs in {logs} (from JELLYFIN_LOG_DIR in the environment)" in err
    assert "(hwaccel-init)" in out


def test_log_cli_jellyfin_dir_not_found(tmp_path, capsys):
    assert main(["--jellyfin-dir", str(tmp_path)]) == EXIT_USAGE
    assert "could not find Jellyfin's transcode logs" in capsys.readouterr().err


def test_scan_cli_reports_which_ffprobe(tmp_path, monkeypatch, capsys):
    make_ffprobe(tmp_path / "jf" / "ffprobe")
    monkeypatch.setattr(cli, "scan", lambda files, ffprobe, jobs, on_result: [])
    assert main(["scan", "--no-output", "--jellyfin-dir", str(tmp_path / "jf"), str(tmp_path)]) == EXIT_OK
    assert f"Using {tmp_path / 'jf' / 'ffprobe'} (from --jellyfin-dir)" in capsys.readouterr().err
