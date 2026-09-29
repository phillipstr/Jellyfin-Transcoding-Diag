"""Parse and classify Jellyfin's per-run FFmpeg logs.

A Jellyfin FFmpeg log (FFmpeg.Transcode-*.log, FFmpeg.Remux-*.log, ...) is:

  1. a JSON header describing the media source (Jellyfin 10.9+),
  2. the full ffmpeg command line,
  3. FFmpeg's own output.

Only part 3 is classified. The command line routinely contains words like
`subtitles=` or `hwaccel` on runs that worked, so matching it produces false
failures.

A run is a failure only if FFmpeg printed a real failure marker (see MARKERS).
Two classes are exempt because their messages are unambiguous on their own:
disk_full and hw_device. Runs that Jellyfin stopped itself ("Exiting normally,
received signal 15": the client left or seeked) are "aborted", not failures.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shlex
from dataclasses import asdict, dataclass

from . import profiles

OK = "ok"
ABORTED = "aborted"
FAILED = "failed"

MARKERS = [
    re.compile(r"Conversion failed!"),
    re.compile(r"Task finished with error code"),
    re.compile(r"Error reinitializing filters"),
    re.compile(r"Terminating thread with return code -\d+"),
    re.compile(r"Error while opening encoder"),
    re.compile(r"Error opening (?:input|output) files?\b"),
]

ABORT = re.compile(r"Exiting normally, received signal (\d+)")


@dataclass(frozen=True)
class FailureClass:
    name: str
    summary: str
    advice: str
    patterns: tuple
    unambiguous: bool = False


def _p(*pats: str) -> tuple:
    return tuple(re.compile(p) for p in pats)


# Checked in order; the first class with a matching output line wins.
CLASSES = [
    FailureClass(
        "disk_full",
        "Disk full",
        "Free space on Jellyfin's transcode directory (Dashboard > Playback > Transcoding).",
        _p(r"No space left on device"),
        unambiguous=True,
    ),
    FailureClass(
        "hw_device",
        "GPU device could not be opened",
        "Check the render node exists and Jellyfin can open it: 'render' group on a native "
        "install, --device /dev/dri plus group_add in Docker. Run `jftd caps`.",
        _p(
            r"Failed to initiali[sz]e VAAPI connection",
            r"No VA display found",
            r"vaInitialize failed",
            r"Device creation failed",
            r"Failed to set value '[^']*' for option 'init_hw_device'",
            r"/dev/dri/\S+.*(?:Permission denied|No such file or directory)",
            r"Error creating a MFX session",
            r"Cannot load libcuda",
            r"CUDA_ERROR_NO_DEVICE",
        ),
        unambiguous=True,
    ),
    FailureClass(
        "hw_decode",
        "GPU could not decode the source",
        "Usually a codec or profile the GPU can't decode (e.g. HEVC 10-bit on older Intel). "
        "Run `jftd drift` and turn off the unsupported hardware-decoding settings.",
        _p(
            r"Failed setup for format \w+: hwaccel initiali[sz]ation returned error",
            r"hardware accelerator failed to decode picture",
            r"No support for codec \w+ profile",
            r"Failed to get HW surface format",
            r"Your platform doesn't support hardware accelerated \w+ decoding",
            r"Error initializing the MFX video decoder",
            r"\[\w+_(?:qsv|cuvid) @ [^\]]*\].*(?:Error|error) (?:during|while) decod",
        ),
    ),
    FailureClass(
        "hw_encode",
        "GPU encoder failed",
        "The hardware encoder rejected the job. Check the encoder options in Jellyfin "
        "(low-power mode, preset) and run `jftd test FILE`.",
        _p(
            r"\[\w+_(?:qsv|vaapi|nvenc|amf) @ [^\]]*\].*(?:[Ee]rror|[Ff]ailed|not supported|unsupported)",
            r"Error initializing the encoder",
            r"No usable encoding (?:profile|entrypoint) found",
            r"Error while opening encoder",
        ),
    ),
    FailureClass(
        "subtitle",
        "Subtitle burn-in failed",
        "The subtitle stream or file could not be read or rendered. Try another subtitle "
        "track, or check fonts under Dashboard > Playback > Transcoding.",
        _p(
            r"\[Parsed_subtitles_\d+ @ [^\]]*\].*(?:Unable|[Ee]rror|[Ff]ailed|[Cc]ould not)",
            r"Error initializing filter 'subtitles'",
            r"Unable to open \S*\.(?:srt|ass|ssa|vtt|sub|sup)\b",
            r"\[Parsed_(?:overlay|alphasrc)\w* @ [^\]]*\].*(?:[Ee]rror|[Ff]ailed)",
        ),
    ),
    FailureClass(
        "filter",
        "Filter graph failed",
        "A video filter (scaling, tone-mapping, format conversion) failed. Often a "
        "follow-on from a hardware decode problem; also try disabling tone mapping.",
        _p(
            r"Impossible to convert between the formats supported by the filter",
            r"Error (?:re)?initializing filters?\b",
            r"Failed to inject frame into filter network",
            r"Error configuring filter graph",
            r"Error while filtering",
        ),
    ),
    FailureClass(
        "input",
        "Source could not be read",
        "The media file is missing, unreadable or damaged. Check the path and "
        "permissions, and try playing it with ffprobe.",
        _p(
            r"Invalid data found when processing input",
            r"moov atom not found",
            r"Error opening input",
            r"No such file or directory",
            r"Input/output error",
            r"Server returned \d{3}",
            r"Connection (?:refused|timed out)",
        ),
    ),
]

CLASS_BY_NAME = {c.name: c for c in CLASSES}
OTHER = FailureClass(
    "other",
    "Unclassified failure",
    "Read the last lines of the log; please open an issue with a scrubbed copy.",
    (),
)
CLASS_BY_NAME["other"] = OTHER

_COMMAND = re.compile(r'^\s*(?:"[^"]*ffmpeg(?:\.exe)?"|[^"\s]*ffmpeg(?:\.exe)?)\s+-', re.I)
_ERRCODE = re.compile(r"(?:error code|return code)[:\s]+(-\d+)")
_INPUT_HDR = re.compile(r"^Input #0\b")
_OUTPUT_HDR = re.compile(r"^(?:Output #\d|Stream mapping:)")
_VSTREAM = re.compile(r"Stream #0:\d+\S*: Video: (\w+)(?: \(([^)]*)\))?[^,]*, (\w+)")
_KIND = re.compile(r"^FFmpeg\.(\w+)-")


@dataclass
class VideoInfo:
    codec: str | None = None
    profile: str | None = None
    pix_fmt: str | None = None
    bit_depth: int | None = None

    def describe(self) -> str:
        if not self.codec:
            return "unknown video"
        parts = [self.codec]
        if self.profile:
            parts.append(self.profile)
        if self.bit_depth:
            parts.append(f"({self.bit_depth}-bit)")
        return " ".join(parts)


@dataclass
class LogResult:
    path: str
    name: str
    kind: str | None
    mtime: float
    status: str
    cls: str | None
    evidence: str | None
    media_path: str | None
    video: VideoInfo
    hwaccel: str | None
    encoder: str | None
    error_code: int | None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["video"] = asdict(self.video)
        return d


@dataclass
class Classification:
    status: str
    cls: str | None
    evidence: str | None
    error_code: int | None


def split_log(text: str) -> tuple[dict | None, str | None, list[str]]:
    """Split a Jellyfin FFmpeg log into (json_header, command_line, output_lines)."""
    header = None
    rest = text.lstrip("﻿")
    stripped = rest.lstrip()
    if stripped.startswith("{"):
        try:
            obj, end = json.JSONDecoder().raw_decode(stripped)
            if isinstance(obj, dict):
                header, rest = obj, stripped[end:]
        except ValueError:
            pass
    lines = rest.splitlines()
    command = None
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if _COMMAND.match(line):
            command = line.strip()
            lines = lines[i + 1:]
        else:
            lines = lines[i:]
        break
    else:
        lines = []
    while lines and not lines[0].strip():
        lines = lines[1:]
    return header, command, lines


def classify_output(lines: list[str], command: str | None = None) -> Classification:
    """Classify FFmpeg output lines (never the command line)."""
    code = None
    for line in lines:
        m = _ERRCODE.search(line)
        if m:
            code = int(m.group(1))  # the first error is the root cause
            break

    def first_match(fc: FailureClass) -> str | None:
        for line in lines:
            for pat in fc.patterns:
                if pat.search(line):
                    return line.strip()
        return None

    for fc in CLASSES:
        if fc.unambiguous:
            ev = first_match(fc)
            if ev:
                return Classification(FAILED, fc.name, ev, code)

    for line in lines:
        if ABORT.search(line):
            return Classification(ABORTED, None, line.strip(), code)

    marker = None
    for line in lines:
        if any(p.search(line) for p in MARKERS):
            marker = line.strip()
            break
    if marker is None:
        return Classification(OK, None, None, code)

    for fc in CLASSES:
        if fc.unambiguous:
            continue
        ev = first_match(fc)
        if ev:
            # ENOSYS with hardware decoding requested and nothing more specific:
            # the classic "GPU can't decode this profile" failure.
            if fc.name in ("filter",) and code == -errno.ENOSYS and _hwaccel(command):
                return Classification(FAILED, "hw_decode", ev, code)
            return Classification(FAILED, fc.name, ev, code)
    if code == -errno.ENOSYS and _hwaccel(command):
        return Classification(FAILED, "hw_decode", marker, code)
    return Classification(FAILED, "other", marker, code)


def _argv(command: str | None) -> list[str]:
    if not command:
        return []
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _opt(argv: list[str], names: tuple) -> str | None:
    for i, a in enumerate(argv[:-1]):
        if a in names:
            return argv[i + 1]
    return None


def _hwaccel(command: str | None) -> str | None:
    return _opt(_argv(command), ("-hwaccel",))


def parse_command(command: str | None) -> dict:
    argv = _argv(command)
    inp = _opt(argv, ("-i",))
    if inp and inp.startswith("file:"):
        inp = inp[5:]
    return {
        "input": inp,
        "hwaccel": _opt(argv, ("-hwaccel",)),
        "encoder": _opt(argv, ("-codec:v:0", "-c:v:0", "-c:v", "-codec:v", "-vcodec")),
    }


def video_from_header(header: dict | None) -> VideoInfo | None:
    if not header:
        return None
    for s in header.get("MediaStreams") or []:
        if not isinstance(s, dict):
            continue
        if str(s.get("Type")).lower() in ("video", "1") and s.get("Codec"):
            pix = s.get("PixelFormat")
            depth = s.get("BitDepth") or profiles.bit_depth(pix, s.get("Profile"))
            return VideoInfo(str(s["Codec"]).lower(), s.get("Profile"), pix, depth)
    return None


def video_from_output(lines: list[str]) -> VideoInfo | None:
    in_input = False
    for line in lines:
        if _INPUT_HDR.match(line):
            in_input = True
        elif _OUTPUT_HDR.match(line):
            break
        elif in_input:
            m = _VSTREAM.search(line)
            if m:
                codec, prof, pix = m.groups()
                return VideoInfo(codec, prof, pix, profiles.bit_depth(pix, prof))
    return None


def analyze(text: str, path: str = "", mtime: float = 0.0) -> LogResult:
    header, command, lines = split_log(text)
    c = classify_output(lines, command)
    cmd = parse_command(command)
    video = video_from_header(header) or video_from_output(lines) or VideoInfo()
    media = (header or {}).get("Path") or cmd["input"]
    name = os.path.basename(path)
    km = _KIND.match(name)
    return LogResult(
        path=path,
        name=name,
        kind=km.group(1) if km else None,
        mtime=mtime,
        status=c.status,
        cls=c.cls,
        evidence=c.evidence,
        media_path=media,
        video=video,
        hwaccel=cmd["hwaccel"],
        encoder=cmd["encoder"],
        error_code=c.error_code,
    )


def is_ffmpeg_log(name: str) -> bool:
    return name.startswith("FFmpeg.") and name.endswith(".log")


_EXITED = re.compile(r"FFmpeg exited with code (-?\d+)")


def server_exit_codes(text: str) -> dict[int, int]:
    """Count 'FFmpeg exited with code N' lines in a Jellyfin server log."""
    counts: dict[int, int] = {}
    for m in _EXITED.finditer(text):
        n = int(m.group(1))
        counts[n] = counts.get(n, 0) + 1
    return counts


def explain_exit_code(code: int) -> str:
    """Turn an FFmpeg exit status into the errno it encodes.

    FFmpeg returns negative AVERROR codes; the OS truncates them to 0..255, so
    Jellyfin's "exited with code 218" is 218 - 256 = -38 = ENOSYS.
    """
    if code == 0:
        return "0: success"
    if code == 255:
        return "255: generic failure, or stopped by a signal (Jellyfin stopping the run)"
    if 0 < code <= 128:
        return f"{code}: generic FFmpeg error (see the FFmpeg log)"
    neg = code - 256 if code > 128 else code
    num = -neg
    if num > 0 and num in errno.errorcode:
        return f"{code} = {neg} = {errno.errorcode[num]} ({os.strerror(num)})"
    return f"{code}: unknown"
