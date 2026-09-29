"""Parse a Jellyfin FFmpeg transcode log into the facts the rules need.

Jellyfin writes one log per transcode (``FFmpeg.Transcode-<date>_<id>.log``).
Newer servers start it with a JSON line describing the media source, then the
full FFmpeg command line, then FFmpeg's own stderr. Older servers skip the JSON
line. Plain FFmpeg stderr and excerpts of the main server log also parse; any
facts that cannot be found are simply left empty.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import List, Optional

_PROGRESS_RE = re.compile(r"\bspeed=\s*([0-9]*\.?[0-9]+)x")
_VERSION_RE = re.compile(r"^ffmpeg version (\S+)")
_EXIT_RE = re.compile(r"exited with code:? (-?\d+)", re.IGNORECASE)

_VIDEO_CODEC_FLAGS = ("-codec:v:0", "-c:v:0", "-codec:v", "-c:v", "-vcodec")
_AUDIO_CODEC_FLAGS = ("-codec:a:0", "-c:a:0", "-codec:a", "-c:a", "-acodec")
_FILTER_FLAGS = ("-vf", "-filter:v", "-filter_complex", "-lavfi")

# Encoder name suffix -> hardware acceleration family.
_ENCODER_HWACCEL = {
    "_nvenc": "nvenc",
    "_qsv": "qsv",
    "_vaapi": "vaapi",
    "_videotoolbox": "videotoolbox",
    "_amf": "amf",
    "_rkmpp": "rkmpp",
    "_v4l2m2m": "v4l2m2m",
}
# -init_hw_device / -hwaccel type -> family.
_DEVICE_HWACCEL = {
    "cuda": "nvenc",
    "qsv": "qsv",
    "vaapi": "vaapi",
    "videotoolbox": "videotoolbox",
    "d3d11va": "amf",
    "rkmpp": "rkmpp",
}


@dataclass
class TranscodeLog:
    """Facts extracted from one log. ``lines`` keeps every line for rule matching."""

    source: str
    lines: List[str] = field(default_factory=list)
    command: Optional[str] = None
    ffmpeg_path: Optional[str] = None
    ffmpeg_version: Optional[str] = None
    input_path: Optional[str] = None
    video_encoder: Optional[str] = None
    audio_encoder: Optional[str] = None
    video_filters: Optional[str] = None
    hwaccel: Optional[str] = None
    media_source: Optional[dict] = None
    speeds: List[float] = field(default_factory=list)
    exit_code: Optional[int] = None

    @property
    def is_jellyfin_ffmpeg(self) -> Optional[bool]:
        if self.ffmpeg_version is None:
            return None
        return "jellyfin" in self.ffmpeg_version.lower()


def parse_log(text: str, source: str = "<stdin>") -> TranscodeLog:
    log = TranscodeLog(source=source, lines=text.splitlines())

    for line in log.lines[:10]:
        stripped = line.strip()
        if stripped.startswith("{") and log.media_source is None:
            try:
                parsed = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                log.media_source = parsed
        elif log.command is None and _looks_like_command(stripped):
            log.command = stripped
            _parse_command(log, stripped)

    for line in log.lines:
        if log.ffmpeg_version is None:
            match = _VERSION_RE.match(line.strip())
            if match:
                log.ffmpeg_version = match.group(1)
        for match in _PROGRESS_RE.finditer(line):
            log.speeds.append(float(match.group(1)))
        match = _EXIT_RE.search(line)
        if match:
            log.exit_code = int(match.group(1))

    return log


def _looks_like_command(line: str) -> bool:
    if " -i " not in line:
        return False
    first = line.split(" ", 1)[0].strip("\"'")
    return first.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower().startswith("ffmpeg")


def _split(command: str) -> List[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        # Unbalanced quotes: fall back to whitespace so the rest still works.
        return command.split()


def _value_after(tokens: List[str], flags) -> Optional[str]:
    for flag in flags:
        if flag in tokens:
            index = tokens.index(flag)
            if index + 1 < len(tokens):
                return tokens[index + 1]
    return None


def _parse_command(log: TranscodeLog, command: str) -> None:
    tokens = _split(command)
    if not tokens:
        return
    log.ffmpeg_path = tokens[0]

    input_path = _value_after(tokens, ("-i",))
    if input_path is not None:
        log.input_path = input_path[len("file:"):] if input_path.startswith("file:") else input_path

    log.video_encoder = _value_after(tokens, _VIDEO_CODEC_FLAGS)
    log.audio_encoder = _value_after(tokens, _AUDIO_CODEC_FLAGS)
    log.video_filters = _value_after(tokens, _FILTER_FLAGS)
    log.hwaccel = _detect_hwaccel(tokens, log.video_encoder)


def _detect_hwaccel(tokens: List[str], video_encoder: Optional[str]) -> Optional[str]:
    if video_encoder:
        for suffix, family in _ENCODER_HWACCEL.items():
            if video_encoder.endswith(suffix):
                return family
    for index, token in enumerate(tokens[:-1]):
        if token in ("-init_hw_device", "-hwaccel"):
            kind = re.split(r"[=:@]", tokens[index + 1], maxsplit=1)[0]
            if kind in _DEVICE_HWACCEL:
                return _DEVICE_HWACCEL[kind]
    return None
