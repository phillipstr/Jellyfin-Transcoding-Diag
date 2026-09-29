"""Known Jellyfin transcode failure signatures and the checks that find them.

Most rules are a list of regular expressions matched against every log line.
A few checks look at extracted facts instead (transcode speed, exit code, the
FFmpeg build), and live in :func:`_fact_checks`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import median
from typing import Dict, List, Optional, Pattern, Tuple

from .parser import TranscodeLog

ERROR = "error"
WARNING = "warning"
INFO = "info"
SEVERITY_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}

MAX_EVIDENCE = 3


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    title: str
    explanation: str
    hints: Tuple[str, ...] = ()
    patterns: Tuple[Pattern[str], ...] = ()
    # Extra hints shown only when the command used this hardware acceleration.
    hwaccel_hints: Dict[str, Tuple[str, ...]] = field(default_factory=dict)

    def hints_for(self, hwaccel: Optional[str]) -> List[str]:
        return list(self.hwaccel_hints.get(hwaccel or "", ())) + list(self.hints)


@dataclass
class Finding:
    rule: Rule
    hints: List[str]
    count: int = 0
    evidence: List[Tuple[int, str]] = field(default_factory=list)


def _p(*patterns: str) -> Tuple[Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


RULES: Tuple[Rule, ...] = (
    Rule(
        id="hwaccel-init",
        severity=ERROR,
        title="Hardware acceleration device could not be opened",
        explanation=(
            "FFmpeg failed to open the GPU or its driver before transcoding started, "
            "so the transcode could not run with the configured hardware acceleration."
        ),
        patterns=_p(
            r"Failed to initiali[sz]e VAAPI connection",
            r"No VA display found for device",
            r"Device creation failed",
            r"Failed to set value .* for option 'init_hw_device'",
            r"Error creating a MFX session",
            r"Error initializing an internal MFX session",
            r"Cannot load libcuda",
            r"Cannot load libnvidia-encode",
            r"CUDA_ERROR_NO_DEVICE",
            r"No capable devices found",
            r"hwaccel initialisation returned error",
        ),
        hwaccel_hints={
            "vaapi": (
                "Check the render node exists (usually /dev/dri/renderD128) and is passed "
                "through to the container or VM.",
                "Make sure the Jellyfin user is in the group that owns the render node "
                "(often 'render' or 'video').",
                "Confirm the VA-API driver is installed and run 'vainfo' as the Jellyfin user.",
            ),
            "qsv": (
                "QSV needs the Intel media driver (iHD); older CPUs may need VA-API instead.",
                "Check /dev/dri is passed through and readable by the Jellyfin user.",
            ),
            "nvenc": (
                "Check the NVIDIA driver is loaded ('nvidia-smi' should list the GPU).",
                "In Docker, use the NVIDIA container runtime and expose the 'video' and "
                "'compute' driver capabilities.",
            ),
        },
        hints=(
            "Temporarily switch Hardware acceleration to 'None' in Dashboard > Playback "
            "> Transcoding to confirm software transcoding works.",
        ),
    ),
    Rule(
        id="nvenc-driver-too-old",
        severity=ERROR,
        title="NVIDIA driver is too old for this FFmpeg build",
        explanation="The installed NVIDIA driver does not support the NVENC API version FFmpeg was built against.",
        patterns=_p(r"Driver does not support the required nvenc API version"),
        hints=("Update the NVIDIA driver on the host to a version supported by your jellyfin-ffmpeg release.",),
    ),
    Rule(
        id="nvenc-session-limit",
        severity=ERROR,
        title="NVENC could not open another encode session",
        explanation=(
            "The GPU refused a new encoder session. Consumer NVIDIA cards cap the number of "
            "simultaneous encodes, and running out of video memory produces the same error."
        ),
        patterns=_p(r"OpenEncodeSessionEx failed", r"out of memory \(10\)"),
        hints=(
            "Check how many transcodes are running at once and whether other software is using NVENC.",
            "Update the driver; newer drivers raise the concurrent session limit.",
        ),
    ),
    Rule(
        id="encoder-unsupported",
        severity=ERROR,
        title="The encoder rejected the requested output",
        explanation=(
            "FFmpeg could not start the encoder. Common causes are asking a GPU for a codec, "
            "profile or bit depth it cannot encode, or an encoder missing from this FFmpeg build."
        ),
        patterns=_p(
            r"Error while opening encoder",
            r"Unknown encoder",
            r"Encoder not found",
            r"10 bit encode not supported",
            r"doesn't support required NVENC features",
            r"No usable encoding profile found",
        ),
        hints=(
            "Check the GPU supports encoding the target codec; disable 'Allow encoding in HEVC/AV1' "
            "in the transcoding settings if it does not.",
            "Make sure Jellyfin is using jellyfin-ffmpeg rather than a distribution FFmpeg.",
        ),
    ),
    Rule(
        id="decoder-unsupported",
        severity=ERROR,
        title="The source video could not be decoded",
        explanation="Neither the hardware decoder nor this FFmpeg build could decode the source codec or profile.",
        patterns=_p(
            r"Decoder \(codec \S+\) not found",
            r"Your platform doesn't support hardware accelerated \S+ decoding",
            r"No support for codec \S+ profile",
            r"Failed setup for format \S+: hwaccel initialisation returned error",
        ),
        hints=(
            "Untick the source codec (and 10-bit variants) under 'Enable hardware decoding for' "
            "if your GPU cannot decode it; Jellyfin will fall back to software decoding.",
        ),
    ),
    Rule(
        id="tonemap-failed",
        severity=ERROR,
        title="HDR tone mapping failed",
        explanation=(
            "The tone mapping filter could not start, usually because OpenCL or CUDA is not "
            "available to FFmpeg or the FFmpeg build lacks the filter."
        ),
        patterns=_p(
            r"No such filter: 'tonemap_\w+'",
            r"Error initializing filter 'tonemap_\w+'",
            r"Failed to get number of OpenCL platforms",
            r"Failed to create OpenCL context",
            r"No matching devices found",
            r"Error (initiali[sz]ing|creating) (an )?OpenCL",
        ),
        hints=(
            "For Intel and AMD, install the OpenCL runtime (for example intel-opencl-icd) "
            "inside the Jellyfin environment.",
            "Temporarily disable 'Enable tone mapping' to confirm this is the only problem.",
        ),
    ),
    Rule(
        id="filter-format-mismatch",
        severity=ERROR,
        title="The filter chain could not agree on a pixel format",
        explanation=(
            "Two filters in the chain need incompatible pixel formats, typically when 10-bit or "
            "HDR video meets a hardware path that only handles 8-bit."
        ),
        patterns=_p(r"Impossible to convert between the formats supported by the filter"),
        hints=(
            "Check whether the source is 10-bit or HDR and whether your hardware decodes it.",
            "Try disabling hardware decoding for the source codec's 10-bit variant.",
        ),
    ),
    Rule(
        id="subtitle-burn-in",
        severity=ERROR,
        title="Subtitle burn-in failed",
        explanation=(
            "The client asked for subtitles to be burned into the video and FFmpeg could not "
            "render them, often because the extracted subtitle file or its fonts are missing."
        ),
        patterns=_p(
            r"Error initializing filter 'subtitles'",
            r"Parsed_subtitles_\d+ .*Unable to open",
            r"Unable to open .*\.(ass|ssa|srt)\b",
        ),
        hints=(
            "Check the Jellyfin cache and transcode directories are writable.",
            "Try a client that renders subtitles itself, or pick a text subtitle track.",
        ),
    ),
    Rule(
        id="font-fallback",
        severity=WARNING,
        title="Subtitle fonts are missing",
        explanation="libass could not find a font for some subtitle characters, so they may render as boxes or not at all.",
        patterns=_p(r"fontselect: failed to find any fallback", r"Glyph 0x[0-9A-F]+ not found"),
        hints=("Set a fallback font folder in Dashboard > Playback, or install fonts covering the subtitle language.",),
    ),
    Rule(
        id="input-missing",
        severity=ERROR,
        title="A file or device could not be found",
        explanation=(
            "FFmpeg was given a path that does not exist from Jellyfin's point of view: the media "
            "file, an extracted subtitle, or a GPU device node."
        ),
        patterns=_p(r"No such file or directory"),
        hints=(
            "Check the library path is mounted the same way inside the Jellyfin container or service.",
            "Rescan the library if the file was moved or renamed.",
            "If the path is under /dev, the GPU is not passed through to Jellyfin.",
        ),
    ),
    Rule(
        id="permission-denied",
        severity=ERROR,
        title="Permission denied",
        explanation="FFmpeg runs as the Jellyfin user and could not read the source or write the transcode output.",
        patterns=_p(r"Permission denied"),
        hints=(
            "Check the Jellyfin user can read the media file and write the transcode directory.",
            "For hardware acceleration, check access to the GPU device node as well.",
        ),
    ),
    Rule(
        id="input-corrupt",
        severity=ERROR,
        title="The source file looks damaged or incomplete",
        explanation="FFmpeg could not read the container, which usually means a truncated or corrupt file.",
        patterns=_p(r"Invalid data found when processing input", r"moov atom not found"),
        hints=("Play or probe the file directly with ffprobe; replace or remux it if ffprobe fails too.",),
    ),
    Rule(
        id="remote-input",
        severity=ERROR,
        title="A remote or network source could not be reached",
        explanation="The source is a URL (for example a .strm file or live TV tuner) and the request failed.",
        patterns=_p(
            r"Server returned \d{3}",
            r"Connection timed out",
            r"Connection refused",
            r"Failed to resolve hostname",
        ),
        hints=("Check the URL or tuner is reachable from the Jellyfin server itself.",),
    ),
    Rule(
        id="disk-full",
        severity=ERROR,
        title="The transcode directory ran out of space",
        explanation="FFmpeg could not write transcoded segments because the disk is full.",
        patterns=_p(r"No space left on device"),
        hints=(
            "Free space on the disk holding the transcode path, or move it in Dashboard > Playback.",
            "If the transcode path is a RAM disk, make it larger or move it to disk.",
        ),
    ),
    Rule(
        id="out-of-memory",
        severity=ERROR,
        title="FFmpeg ran out of memory",
        explanation="An allocation failed while transcoding.",
        patterns=_p(r"Cannot allocate memory"),
        hints=("Check memory limits on the Jellyfin container or service, and the number of simultaneous transcodes.",),
    ),
    Rule(
        id="audio-layout",
        severity=ERROR,
        title="The audio encoder rejected the channel layout",
        explanation="The source audio has a channel layout the target audio encoder cannot handle.",
        patterns=_p(r"Unsupported channel layout", r"Specified channel layout .* is not supported"),
        hints=("Try limiting audio channels on the client, or enable audio downmixing in the transcoding settings.",),
    ),
    Rule(
        id="stopped-by-client",
        severity=INFO,
        title="The transcode was stopped by Jellyfin",
        explanation=(
            "FFmpeg received a stop signal, which Jellyfin sends when playback ends, the client "
            "seeks, or the stream is restarted. On its own this is not a failure."
        ),
        patterns=_p(r"received signal 15", r"Exiting normally, received signal"),
    ),
)

SLOW_TRANSCODE = Rule(
    id="slow-transcode",
    severity=WARNING,
    title="Transcoding is slower than real time",
    explanation="FFmpeg's reported speed stayed below 1.0x, so playback will buffer.",
    hints=(
        "Enable hardware acceleration if it is available, or lower the client's streaming quality.",
        "Check CPU load and whether tone mapping or subtitle burn-in is running in software.",
    ),
)

KILLED = Rule(
    id="ffmpeg-killed",
    severity=ERROR,
    title="FFmpeg was killed or crashed",
    explanation=(
        "FFmpeg ended with an exit code that means it was killed (often by the out-of-memory "
        "killer) or crashed rather than failing with an error message."
    ),
    hints=(
        "Check the system log (dmesg or journalctl) for out-of-memory kills.",
        "Update jellyfin-ffmpeg and the GPU driver; crashes are often driver bugs.",
    ),
)

NON_JELLYFIN_FFMPEG = Rule(
    id="non-jellyfin-ffmpeg",
    severity=WARNING,
    title="Jellyfin is not using jellyfin-ffmpeg",
    explanation=(
        "This FFmpeg build is not jellyfin-ffmpeg. Stock builds lack filters Jellyfin relies on "
        "for hardware tone mapping and some hardware pipelines."
    ),
    hints=("Install jellyfin-ffmpeg and point Dashboard > Playback > FFmpeg path at it.",),
)

ALL_RULES: Tuple[Rule, ...] = RULES + (SLOW_TRANSCODE, KILLED, NON_JELLYFIN_FFMPEG)

# Exit codes that mean killed or crashed: SIGKILL/SIGSEGV/SIGABRT, raw or as 128+n.
_KILLED_EXIT_CODES = {-9, -11, -6, 134, 137, 139}
# Ignore the first few progress samples; FFmpeg is always slow while starting up.
_SPEED_WARMUP = 3
_MIN_SPEED_SAMPLES = 5


def diagnose(log: TranscodeLog) -> List[Finding]:
    """Return findings for ``log``, most severe first."""
    findings: List[Finding] = []
    for rule in RULES:
        finding = Finding(rule=rule, hints=rule.hints_for(log.hwaccel))
        for number, line in enumerate(log.lines, start=1):
            if any(p.search(line) for p in rule.patterns):
                finding.count += 1
                if len(finding.evidence) < MAX_EVIDENCE:
                    finding.evidence.append((number, line.strip()))
        if finding.count:
            findings.append(finding)

    findings.extend(_fact_checks(log))
    findings.sort(key=lambda f: SEVERITY_ORDER[f.rule.severity])
    return findings


def _fact_checks(log: TranscodeLog) -> List[Finding]:
    findings = []

    samples = log.speeds[_SPEED_WARMUP:]
    if len(samples) >= _MIN_SPEED_SAMPLES and median(samples) < 1.0:
        findings.append(Finding(
            rule=SLOW_TRANSCODE,
            hints=list(SLOW_TRANSCODE.hints),
            count=1,
            evidence=[(0, f"median speed {median(samples):.2f}x over {len(samples)} progress updates")],
        ))

    if log.exit_code in _KILLED_EXIT_CODES:
        findings.append(Finding(
            rule=KILLED,
            hints=list(KILLED.hints),
            count=1,
            evidence=[(0, f"exit code {log.exit_code}")],
        ))

    if log.is_jellyfin_ffmpeg is False:
        findings.append(Finding(
            rule=NON_JELLYFIN_FFMPEG,
            hints=list(NON_JELLYFIN_FFMPEG.hints),
            count=1,
            evidence=[(0, f"ffmpeg version {log.ffmpeg_version}")],
        ))

    return findings
