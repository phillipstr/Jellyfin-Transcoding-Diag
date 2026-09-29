from jellyfin_transcode_diag.parser import parse_log


def test_parses_media_source_header_and_command(fixture_text):
    log = parse_log(fixture_text("vaapi_init_failure.log"))

    assert log.media_source["Container"] == "mkv"
    assert log.ffmpeg_path == "/usr/lib/jellyfin-ffmpeg/ffmpeg"
    assert log.ffmpeg_version == "6.0.1-Jellyfin"
    assert log.is_jellyfin_ffmpeg is True
    assert log.input_path == "/media/movies/Example Movie (2020)/Example Movie (2020).mkv"
    assert log.video_encoder == "h264_vaapi"
    assert log.audio_encoder == "libfdk_aac"
    assert log.hwaccel == "vaapi"
    assert "scale_vaapi" in log.video_filters


def test_parses_log_without_json_header(fixture_text):
    log = parse_log(fixture_text("nvenc_slow.log"))

    assert log.media_source is None
    assert log.video_encoder == "h264_nvenc"
    assert log.hwaccel == "nvenc"
    assert log.speeds[:3] == [0.2, 0.45, 0.6]
    assert len(log.speeds) == 9


def test_detects_stock_ffmpeg(fixture_text):
    log = parse_log(fixture_text("stock_ffmpeg_tonemap.log"))

    assert log.ffmpeg_version == "6.1.1-3ubuntu5"
    assert log.is_jellyfin_ffmpeg is False


def test_hwaccel_from_device_when_encoder_is_software():
    log = parse_log('ffmpeg -init_hw_device cuda=cu:0 -i "in.mkv" -c:v libx264 out.ts')

    assert log.video_encoder == "libx264"
    assert log.hwaccel == "nvenc"


def test_plain_stderr_and_exit_code():
    log = parse_log("[info] FFmpeg exited with code 137\n")

    assert log.command is None
    assert log.ffmpeg_version is None
    assert log.is_jellyfin_ffmpeg is None
    assert log.exit_code == 137


def test_unbalanced_quotes_do_not_crash():
    log = parse_log('ffmpeg -i "broken.mkv -c:v h264_qsv out.ts')

    assert log.hwaccel == "qsv"
