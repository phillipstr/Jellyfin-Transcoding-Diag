import pytest

from jellyfin_transcode_diag.parser import parse_log
from jellyfin_transcode_diag.rules import ALL_RULES, ERROR, INFO, MAX_EVIDENCE, WARNING, diagnose


def ids(text):
    return [f.rule.id for f in diagnose(parse_log(text))]


def test_rule_ids_are_unique():
    rule_ids = [rule.id for rule in ALL_RULES]
    assert len(rule_ids) == len(set(rule_ids))


def test_vaapi_failure_gets_vaapi_specific_hints(fixture_text):
    findings = diagnose(parse_log(fixture_text("vaapi_init_failure.log")))

    assert [f.rule.id for f in findings] == ["hwaccel-init"]
    finding = findings[0]
    assert finding.count == 3
    assert finding.evidence[0][0] == 9
    assert any("vainfo" in hint for hint in finding.hints)
    assert not any("nvidia-smi" in hint for hint in finding.hints)


def test_stock_ffmpeg_tonemap(fixture_text):
    assert ids(fixture_text("stock_ffmpeg_tonemap.log")) == ["tonemap-failed", "non-jellyfin-ffmpeg"]


def test_slow_transcode_and_fonts(fixture_text):
    findings = diagnose(parse_log(fixture_text("nvenc_slow.log")))

    assert {f.rule.id for f in findings} == {"slow-transcode", "font-fallback"}
    assert all(f.rule.severity == WARNING for f in findings)


def test_healthy_log_only_reports_the_stop(fixture_text):
    findings = diagnose(parse_log(fixture_text("healthy.log")))

    assert [(f.rule.id, f.rule.severity) for f in findings] == [("stopped-by-client", INFO)]


def test_findings_are_sorted_by_severity():
    text = "Glyph 0x3042 not found\nNo space left on device\nreceived signal 15\n"
    severities = [f.rule.severity for f in diagnose(parse_log(text))]

    assert severities == [ERROR, WARNING, INFO]


def test_evidence_is_capped_but_counted():
    text = "Permission denied\n" * (MAX_EVIDENCE + 2)
    (finding,) = diagnose(parse_log(text))

    assert finding.count == MAX_EVIDENCE + 2
    assert len(finding.evidence) == MAX_EVIDENCE


def test_startup_slowness_alone_is_not_flagged():
    progress = "".join(f"frame={i} speed=0.{i}x\n" for i in range(1, 4))
    progress += "frame=9 speed=3.0x\n" * 5

    assert "slow-transcode" not in ids(progress)


@pytest.mark.parametrize(
    "line, rule_id",
    [
        ("[AVHWDeviceContext @ 0x1] No VA display found for device /dev/dri/renderD128.", "hwaccel-init"),
        ("[AVHWDeviceContext @ 0x1] Error creating a MFX session: -9.", "hwaccel-init"),
        ("[AVHWDeviceContext @ 0x1] Cannot load libcuda.so.1", "hwaccel-init"),
        ("[h264_nvenc @ 0x1] Driver does not support the required nvenc API version. Required: 12.1 Found: 12.0", "nvenc-driver-too-old"),
        ("[h264_nvenc @ 0x1] OpenEncodeSessionEx failed: out of memory (10): (no details)", "nvenc-session-limit"),
        ("[hevc_nvenc @ 0x1] 10 bit encode not supported", "encoder-unsupported"),
        ("Unknown encoder 'libfdk_aac'", "encoder-unsupported"),
        ("Decoder (codec av1) not found for input stream #0:0", "decoder-unsupported"),
        ("[av1 @ 0x1] Your platform doesn't support hardware accelerated AV1 decoding.", "decoder-unsupported"),
        ("[AVHWDeviceContext @ 0x1] Failed to get number of OpenCL platforms: -1001.", "tonemap-failed"),
        ("Impossible to convert between the formats supported by the filter 'Parsed_null_0' and the filter 'auto_scale_0'", "filter-format-mismatch"),
        ("[Parsed_subtitles_0 @ 0x1] Unable to open /cache/subtitles/abc.ass", "subtitle-burn-in"),
        ("/media/missing.mkv: No such file or directory", "input-missing"),
        ("/config/transcodes/x.ts: Permission denied", "permission-denied"),
        ("[mov,mp4,m4a,3gp,3g2,mj2 @ 0x1] moov atom not found", "input-corrupt"),
        ("[http @ 0x1] HTTP error 404 Not Found\nServer returned 404 Not Found", "remote-input"),
        ("av_interleaved_write_frame(): No space left on device", "disk-full"),
        ("Cannot allocate memory", "out-of-memory"),
        ("[libmp3lame @ 0x1] Specified channel layout '5.1(side)' is not supported", "audio-layout"),
        ("FFmpeg exited with code 139", "ffmpeg-killed"),
    ],
)
def test_signatures(line, rule_id):
    assert rule_id in ids(line)


def test_clean_exit_code_is_not_flagged():
    assert ids("FFmpeg exited with code 0") == []
