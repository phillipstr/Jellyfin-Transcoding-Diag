"""Scan a media library with ffprobe and flag files likely to fail or struggle to transcode.

Findings here are predictions from file metadata, not observed failures: whether
a file actually fails depends on the client, the server's hardware acceleration
and Jellyfin's settings. Each check looks at one ffprobe result (a dict parsed
from ``ffprobe -print_format json -show_format -show_streams``).
"""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence

from .rules import ERROR, INFO, SEVERITY_ORDER, WARNING

MEDIA_EXTENSIONS = frozenset({
    ".3gp", ".asf", ".avi", ".divx", ".dvr-ms", ".f4v", ".flv", ".m2ts", ".m2v",
    ".m4v", ".mk3d", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".ogm",
    ".ogv", ".rm", ".rmvb", ".ts", ".vob", ".webm", ".wmv", ".wtv", ".xvid",
})

PROBE_TIMEOUT = 60

IMAGE_SUBTITLE_CODECS = frozenset({"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"})
HDR_TRANSFERS = {"smpte2084": "HDR10/PQ", "arib-std-b67": "HLG"}
# Codecs FFmpeg decodes, but only in software and slowly; no GPU decodes them.
SOFTWARE_ONLY_VIDEO = frozenset({"prores", "dnxhd", "cfhd", "ffv1", "huffyuv", "utvideo", "rawvideo"})
# Codecs needing hardware that many servers still lack.
NEWER_VIDEO = {"av1": "AV1"}


@dataclass(frozen=True)
class Check:
    id: str
    severity: str
    title: str


@dataclass
class ScanFinding:
    check: Check
    detail: str


@dataclass
class ScanResult:
    path: Path
    findings: List[ScanFinding] = field(default_factory=list)

    @property
    def worst(self) -> Optional[str]:
        if not self.findings:
            return None
        return min((f.check.severity for f in self.findings), key=SEVERITY_ORDER.__getitem__)


PROBE_FAILED = Check("probe-failed", ERROR, "ffprobe could not read the file")
PROBE_ERRORS = Check("probe-errors", WARNING, "ffprobe reported errors while reading the file")
NO_VIDEO = Check("no-video-stream", ERROR, "No video stream found")
UNKNOWN_CODEC = Check("unknown-codec", ERROR, "A stream uses a codec FFmpeg does not recognise")
DOLBY_VISION_ONLY = Check(
    "dolby-vision-no-fallback", ERROR,
    "Dolby Vision without an HDR10 or SDR fallback layer",
)
HDR = Check("hdr-tone-mapping", WARNING, "HDR video needs tone mapping for SDR clients")
H264_HIGH_BIT_DEPTH = Check(
    "h264-10bit", WARNING, "10-bit H.264, which almost no hardware decoder supports",
)
CHROMA = Check("chroma-subsampling", WARNING, "4:2:2 or 4:4:4 video that most hardware cannot decode")
SOFTWARE_DECODE = Check("software-decode-only", WARNING, "Video codec with no hardware decoding")
NEWER_CODEC = Check("newer-codec", INFO, "Video codec that older GPUs cannot decode")
LARGE_FRAME = Check("large-frame", WARNING, "Resolution above 4K")
INTERLACED = Check("interlaced", INFO, "Interlaced video needs deinterlacing")
IMAGE_SUBTITLES = Check("image-subtitles", INFO, "Image subtitles force burn-in when selected")
NO_DURATION = Check("no-duration", WARNING, "File reports no duration; seeking may fail")

ALL_CHECKS = (
    PROBE_FAILED, PROBE_ERRORS, NO_VIDEO, UNKNOWN_CODEC, DOLBY_VISION_ONLY, HDR,
    H264_HIGH_BIT_DEPTH, CHROMA, SOFTWARE_DECODE, NEWER_CODEC, LARGE_FRAME,
    INTERLACED, IMAGE_SUBTITLES, NO_DURATION,
)


def iter_media_files(paths: Sequence[str]) -> Iterator[Path]:
    """Yield absolute paths of media files under each path, in a stable order.

    Explicit files are always included, whatever their extension.
    """
    for raw in paths:
        path = Path(os.path.abspath(raw))
        if path.is_file():
            yield path
        elif path.is_dir():
            for root, dirs, files in os.walk(path):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for name in sorted(files):
                    if not name.startswith(".") and Path(name).suffix.lower() in MEDIA_EXTENSIONS:
                        yield Path(root) / name
        else:
            raise FileNotFoundError(f"no such file or directory: {raw}")


@dataclass
class Probe:
    data: Optional[Dict[str, Any]]
    errors: str
    returncode: int


def run_ffprobe(ffprobe: str, path: Path) -> Probe:
    command = [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, errors="replace", timeout=PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return Probe(None, f"ffprobe timed out after {PROBE_TIMEOUT}s", -1)
    try:
        data = json.loads(proc.stdout) if proc.stdout.strip() else None
    except ValueError:
        data = None
    return Probe(data, proc.stderr.strip(), proc.returncode)


def analyze(path: Path, probe: Probe) -> ScanResult:
    result = ScanResult(path)
    add = lambda check, detail: result.findings.append(ScanFinding(check, detail))  # noqa: E731

    if probe.returncode != 0 or not probe.data or not probe.data.get("streams"):
        add(PROBE_FAILED, _first_line(probe.errors) or "no streams found")
        return result
    if probe.errors:
        add(PROBE_ERRORS, _first_line(probe.errors))

    streams = probe.data.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video" and not _is_cover_art(s)]

    for stream in streams:
        if stream.get("codec_type") in ("video", "audio") and stream.get("codec_name") in (None, "", "none"):
            add(UNKNOWN_CODEC, f"stream {stream.get('index')} ({stream.get('codec_type')}) "
                               f"codec tag {stream.get('codec_tag_string', 'unknown')}")

    if not video:
        add(NO_VIDEO, "only audio, subtitle or attachment streams")
    for stream in video:
        _check_video(stream, add)

    image_subs = sorted({
        s.get("codec_name") for s in streams
        if s.get("codec_type") == "subtitle" and s.get("codec_name") in IMAGE_SUBTITLE_CODECS
    })
    if image_subs:
        add(IMAGE_SUBTITLES, ", ".join(image_subs))

    duration = _float(probe.data.get("format", {}).get("duration"))
    if video and not duration:
        add(NO_DURATION, "container has no duration")

    return result


def _check_video(stream: Dict[str, Any], add: Callable[[Check, str], None]) -> None:
    codec = stream.get("codec_name") or ""
    profile = stream.get("profile") or ""
    pix_fmt = stream.get("pix_fmt") or ""
    label = codec + (f" ({profile})" if profile else "")

    dovi = _dovi_record(stream)
    if dovi is not None:
        dv_profile = dovi.get("dv_profile")
        compat = dovi.get("dv_bl_signal_compatibility_id")
        name = f"Dolby Vision profile {dv_profile}" if dv_profile is not None else "Dolby Vision"
        if compat == 0 or dv_profile == 5:
            add(DOLBY_VISION_ONLY, f"{name}; colours will be wrong (often purple or green) "
                                   "unless the server tone maps Dolby Vision")
        elif compat is None and stream.get("color_transfer") not in HDR_TRANSFERS:
            add(HDR, f"{name}, fallback layer unknown")
        else:
            add(HDR, f"{name} with a compatible base layer")
    elif stream.get("color_transfer") in HDR_TRANSFERS:
        add(HDR, f"{HDR_TRANSFERS[stream['color_transfer']]} {label}")

    if codec == "h264" and ("10" in pix_fmt or "High 10" in profile or "4:2:2" in profile or "4:4:4" in profile):
        add(H264_HIGH_BIT_DEPTH, f"{label}, {pix_fmt}")
    elif pix_fmt.startswith(("yuv422", "yuv444", "yuvj422", "yuvj444")) and codec not in SOFTWARE_ONLY_VIDEO:
        add(CHROMA, f"{label}, {pix_fmt}")

    if codec in SOFTWARE_ONLY_VIDEO:
        add(SOFTWARE_DECODE, label)
    elif codec in NEWER_VIDEO:
        add(NEWER_CODEC, f"{NEWER_VIDEO[codec]}; needs a recent GPU for hardware decoding")

    width, height = stream.get("width") or 0, stream.get("height") or 0
    if width > 4096 or height > 2304:
        add(LARGE_FRAME, f"{width}x{height}")

    if stream.get("field_order") in ("tt", "bb", "tb", "bt"):
        add(INTERLACED, f"field order {stream['field_order']}")


def _dovi_record(stream: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for side_data in stream.get("side_data_list") or ():
        if "DOVI" in str(side_data.get("side_data_type", "")):
            return side_data
    if stream.get("codec_tag_string") in ("dvhe", "dvh1", "dav1", "dva1"):
        return {}
    return None


def _is_cover_art(stream: Dict[str, Any]) -> bool:
    return bool((stream.get("disposition") or {}).get("attached_pic"))


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0].strip() if text.strip() else ""


def scan(
    files: Iterable[Path],
    ffprobe: str,
    jobs: int = 4,
    on_result: Optional[Callable[[ScanResult], None]] = None,
    probe: Optional[Callable[[str, Path], Probe]] = None,
) -> List[ScanResult]:
    """Probe and analyze each file, keeping the input order."""
    run = probe or run_ffprobe

    def one(path: Path) -> ScanResult:
        return analyze(path, run(ffprobe, path))

    results: List[ScanResult] = []
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for result in pool.map(one, files):
            results.append(result)
            if on_result:
                on_result(result)
    return results
