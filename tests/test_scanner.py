import csv
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from jellyfin_transcode_diag import scanner
from jellyfin_transcode_diag.cli import EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE, main
from jellyfin_transcode_diag.scanner import Probe, analyze, iter_media_files

H264 = {"index": 0, "codec_type": "video", "codec_name": "h264", "profile": "High",
        "pix_fmt": "yuv420p", "width": 1920, "height": 1080}
AAC = {"index": 1, "codec_type": "audio", "codec_name": "aac"}
FORMAT = {"duration": "5400.0"}


def probe(*streams, fmt=FORMAT, errors="", returncode=0):
    return Probe({"streams": list(streams), "format": fmt}, errors, returncode)


def ids(result):
    return [f.check.id for f in result.findings]


def test_plain_h264_has_no_findings():
    assert ids(analyze(Path("/media/a.mkv"), probe(H264, AAC))) == []


def test_unreadable_file():
    result = analyze(Path("/media/a.mp4"), Probe(None, "moov atom not found\nmore", 1))
    assert ids(result) == ["probe-failed"]
    assert result.findings[0].detail == "moov atom not found"
    assert result.worst == "error"


def test_read_errors_on_otherwise_readable_file():
    result = analyze(Path("/media/a.mkv"), probe(H264, AAC, errors="Invalid NAL unit size"))
    assert ids(result) == ["probe-errors"]


def test_hdr10():
    stream = dict(H264, codec_name="hevc", profile="Main 10", pix_fmt="yuv420p10le",
                  color_transfer="smpte2084")
    assert ids(analyze(Path("/m.mkv"), probe(stream, AAC))) == ["hdr-tone-mapping"]


def test_dolby_vision_profile_5_has_no_fallback():
    stream = dict(H264, codec_name="hevc", pix_fmt="yuv420p10le", side_data_list=[
        {"side_data_type": "DOVI configuration record", "dv_profile": 5,
         "dv_bl_signal_compatibility_id": 0},
    ])
    result = analyze(Path("/m.mp4"), probe(stream, AAC))
    assert ids(result) == ["dolby-vision-no-fallback"]
    assert "profile 5" in result.findings[0].detail


def test_dolby_vision_profile_8_is_hdr():
    stream = dict(H264, codec_name="hevc", color_transfer="smpte2084", side_data_list=[
        {"side_data_type": "DOVI configuration record", "dv_profile": 8,
         "dv_bl_signal_compatibility_id": 1},
    ])
    assert ids(analyze(Path("/m.mkv"), probe(stream, AAC))) == ["hdr-tone-mapping"]


def test_h264_10bit_and_chroma():
    ten_bit = dict(H264, profile="High 10", pix_fmt="yuv420p10le")
    assert ids(analyze(Path("/m.mkv"), probe(ten_bit))) == ["h264-10bit"]
    hevc_444 = dict(H264, codec_name="hevc", profile="Rext", pix_fmt="yuv444p")
    assert ids(analyze(Path("/m.mkv"), probe(hevc_444))) == ["chroma-subsampling"]


def test_codec_checks():
    prores = dict(H264, codec_name="prores", profile="4444", pix_fmt="yuv444p10le")
    assert ids(analyze(Path("/m.mov"), probe(prores))) == ["software-decode-only"]
    av1 = dict(H264, codec_name="av1", profile="Main")
    assert ids(analyze(Path("/m.mkv"), probe(av1))) == ["newer-codec"]
    unknown = dict(H264, codec_name=None, codec_tag_string="XYZ1")
    assert "unknown-codec" in ids(analyze(Path("/m.avi"), probe(unknown)))


def test_no_video_ignores_cover_art():
    cover = dict(H264, codec_name="mjpeg", disposition={"attached_pic": 1})
    assert ids(analyze(Path("/m.mkv"), probe(cover, AAC))) == ["no-video-stream"]


def test_notes_and_shape_checks():
    stream = dict(H264, width=7680, height=4320, field_order="tt")
    pgs = {"index": 2, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"}
    srt = {"index": 3, "codec_type": "subtitle", "codec_name": "subrip"}
    result = analyze(Path("/m.mkv"), probe(stream, AAC, pgs, srt, fmt={}))
    assert ids(result) == ["large-frame", "interlaced", "image-subtitles", "no-duration"]


def test_iter_media_files(tmp_path):
    (tmp_path / "Show" / "Season 1").mkdir(parents=True)
    (tmp_path / ".trash").mkdir()
    for name in ["Show/Season 1/e1.MKV", "Show/e0.mp4", "Show/poster.jpg", ".trash/x.mkv",
                 "Show/.hidden.mkv", "extra.bin"]:
        (tmp_path / name).write_text("x")

    found = list(iter_media_files([str(tmp_path), str(tmp_path / "extra.bin")]))
    assert [p.relative_to(tmp_path).as_posix() for p in found] == [
        "Show/e0.mp4", "Show/Season 1/e1.MKV", "extra.bin",
    ]
    assert all(p.is_absolute() for p in found)


@pytest.fixture
def fake_library(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    for name in ["broken.mkv", "fine.mkv", "subs.mkv"]:
        (library / name).write_text("x")
    probes = {
        "broken.mkv": Probe(None, "Invalid data found when processing input", 1),
        "fine.mkv": probe(H264, AAC),
        "subs.mkv": probe(H264, AAC, {"index": 2, "codec_type": "subtitle",
                                      "codec_name": "dvd_subtitle"}),
    }
    monkeypatch.setattr(scanner, "find_ffprobe", lambda explicit=None: "ffprobe")
    monkeypatch.setattr(scanner, "run_ffprobe", lambda ffprobe, path: probes[path.name])
    monkeypatch.chdir(tmp_path)
    return library


def test_scan_cli_writes_csv_with_full_paths(fake_library, capsys):
    code = main(["scan", str(fake_library)])
    out = capsys.readouterr().out

    assert code == EXIT_PROBLEMS
    assert f"{fake_library / 'broken.mkv'}\n  [ERROR] ffprobe could not read the file" in out
    assert "subs.mkv" not in out  # info only, hidden by default
    assert "predictions" in out
    with open("transcode-scan.csv", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [(r["path"], r["check"]) for r in rows] == [(str(fake_library / "broken.mkv"), "probe-failed")]


def test_scan_cli_json_and_min_severity(fake_library, capsys):
    code = main(["scan", "--min-severity", "info", "-o", "out.json", str(fake_library)])
    out = capsys.readouterr().out

    assert code == EXIT_PROBLEMS
    assert "subs.mkv" in out
    payload = json.loads(Path("out.json").read_text())
    assert payload["scanned"] == 3
    assert [f["check"] for f in payload["findings"]] == ["probe-failed", "image-subtitles"]


def test_scan_cli_no_output(fake_library, capsys):
    main(["scan", "--no-output", str(fake_library / "fine.mkv")])
    assert not Path("transcode-scan.csv").exists()
    assert "Scanned 1 file: 0 likely to fail" in capsys.readouterr().out


def test_scan_cli_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scanner.shutil, "which", lambda name: None)
    monkeypatch.setattr(scanner, "JELLYFIN_FFPROBE_PATHS", ())
    assert main(["scan", str(tmp_path)]) == EXIT_USAGE
    assert "ffprobe not found" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        main(["scan"])


def test_scan_list_checks(capsys):
    assert main(["scan", "--list-checks"]) == EXIT_OK
    assert "dolby-vision-no-fallback" in capsys.readouterr().out


def test_log_subcommand_matches_default(capsys):
    assert main(["log", "--list-rules"]) == EXIT_OK
    assert "hwaccel-init" in capsys.readouterr().out


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                    reason="needs ffmpeg and ffprobe")
def test_scan_real_files(tmp_path):
    good = tmp_path / "good.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=5",
                    "-t", "1", "-c:v", "mpeg4", str(good)], check=True)
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00" * 4096)

    results = scanner.scan([good, bad], shutil.which("ffprobe"))
    assert [ids(r) for r in results] == [[], ["probe-failed"]]
