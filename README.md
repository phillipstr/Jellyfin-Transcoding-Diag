# jellyfin-transcode-diag

> [!NOTE]
> AI Info: This project is AI-assisted with Claude. I want to be open about that.

A small command-line tool that reads Jellyfin's FFmpeg transcode logs and tells
you, in plain language, why a transcode failed or struggled and what to try next.

Playback errors in Jellyfin usually end in a vague "playback failed" on the
client, while the real cause sits a few hundred lines into an
`FFmpeg.Transcode-*.log` file. This tool finds the lines that matter, names
the problem, and points at the setting or package to check.

It has no dependencies beyond Python 3.9+ and never contacts the network.

## Why Does This Exist?

I have recently begun migrating from Plex to Jellyfin, and admittedly, my
hardware (ThinkCentre M700) isn't the best. I just have an Intel iGPU so
sometimes Jellyfin will fail when attempting to transcode things. When that
happens, the Jellyfin UI will simply display a media playback error without
much else.

So I set off to figure out how I can make my self-hosting life a little
easier by making this little utility. It can do two things:

1. Checks log files for failed transcodes
1. Crawls a library to find media files that *might* fail a transcode

## Install

```sh
pip install git+https://github.com/phillipstr/Jellyfin-Transcoding-Diag
```

Or run from a checkout without installing:

```sh
PYTHONPATH=src python -m jellyfin_transcode_diag path/to/FFmpeg.Transcode-*.log
```

## Usage

```sh
# One log file
jf-transcode-diag FFmpeg.Transcode-2026-01-01_12-00-00_abc123.log

# Jellyfin's log directory: checks the most recent transcode log
jf-transcode-diag /var/log/jellyfin

# The five most recent transcode logs
jf-transcode-diag --latest 5 /config/log

# Pipe a log in, e.g. the newest one from a Docker container
docker exec jellyfin sh -c 'cat "$(ls -t /config/log/FFmpeg.Transcode-*.log | head -n 1)"' | jf-transcode-diag

# Hide paths, URLs and IP addresses before pasting into a forum post
jf-transcode-diag --redact /config/log

# Machine-readable output
jf-transcode-diag --json /config/log
```

Where Jellyfin keeps its logs depends on how it was installed. Common places
are `/var/log/jellyfin` (Debian and Ubuntu packages), `/config/log` (the
official Docker image) and `%ProgramData%\Jellyfin\Server\log` (Windows).

Example output:

```
== FFmpeg.Transcode-2026-01-01_12-00-00_abc123.log ==
FFmpeg: 6.0.1-Jellyfin (/usr/lib/jellyfin-ffmpeg/ffmpeg)
Video:  h264_vaapi (hardware: vaapi)
Audio:  aac
Input:  /media/movies/Example Movie (2020)/Example Movie (2020).mkv

[ERROR] Hardware acceleration device could not be opened (hwaccel-init)
  FFmpeg failed to open the GPU or its driver before transcoding started, so the
  transcode could not run with the configured hardware acceleration.
  line 9: [AVHWDeviceContext @ 0x55d0c8a1e2c0] Failed to initialise VAAPI connection: -1 (unknown libva error).
  line 10: Device creation failed: -5.
  Try:
    - Check the render node exists (usually /dev/dri/renderD128) and is passed through
      to the container or VM.
    - Make sure the Jellyfin user is in the group that owns the render node (often
      'render' or 'video').
    ...
```

### Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | No error-level problems found (warnings and notes may still be printed) |
| 1 | At least one error-level problem found |
| 2 | Bad arguments, or a log could not be read |

## What it detects

Run `jf-transcode-diag --list-rules` for the current list. Version 0.1 covers:

- Hardware acceleration that fails to start (VA-API, QSV, NVENC), with hints
  specific to the acceleration method in use
- NVIDIA drivers too old for the FFmpeg build, and NVENC session limits
- Encoders and decoders that reject the codec, profile or bit depth
- HDR tone mapping failures, including OpenCL problems and missing filters
- Pixel format mismatches in the filter chain
- Subtitle burn-in failures and missing subtitle fonts
- Missing, unreadable or corrupt source files; unreachable network sources
- A full transcode directory, out-of-memory errors, and FFmpeg being killed
- Audio channel layouts the encoder cannot handle
- Transcodes running slower than real time
- Jellyfin using a stock FFmpeg instead of jellyfin-ffmpeg

A stop signal from Jellyfin (playback ended, the client seeked) is reported as
a note, since it is normal and often the last line of a healthy log.

## Adding a rule

Rules live in [`src/jellyfin_transcode_diag/rules.py`](src/jellyfin_transcode_diag/rules.py).
Most are a list of regular expressions plus an explanation and hints:

```python
Rule(
    id="disk-full",
    severity=ERROR,
    title="The transcode directory ran out of space",
    explanation="FFmpeg could not write transcoded segments because the disk is full.",
    patterns=_p(r"No space left on device"),
    hints=("Free space on the disk holding the transcode path, ...",),
),
```

Add a test with the exact FFmpeg line to `tests/test_rules.py`. If you add a
sample log under `tests/fixtures`, replace real paths, hostnames and addresses
with made-up ones first.

## Development

```sh
pip install -e . pytest
pytest
```

## License

MIT, see [LICENSE](LICENSE). Not affiliated with the Jellyfin project.
