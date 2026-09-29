"""Render findings as readable text or JSON."""

from __future__ import annotations

import json
import re
import textwrap
from statistics import median
from typing import Callable, Iterable, List, Tuple

from .parser import TranscodeLog
from .rules import Finding

# Quoted strings that start with an absolute path, e.g. from 'file:/media/a b.mkv'.
_QUOTED_PATH_RE = re.compile(r"(['\"])(?:file:)?(?:/(?!dev/)|[A-Za-z]:\\)[^'\"]*\1")
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"']+", re.IGNORECASE)
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s\"']*")
# Absolute Unix paths with at least two components. Device nodes stay visible
# because they are usually the point of the finding.
_UNIX_PATH_RE = re.compile(r"(?<![\w.:/])/(?!dev/)[^\s\"':/()]+(?:/[^\s\"':()]*)+")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

Report = Tuple[TranscodeLog, List[Finding]]


def redact(text: str, known_paths: Iterable[str] = ()) -> str:
    """Hide URLs, file paths and IP addresses so a report can be shared publicly.

    Paths may contain spaces, which no pattern can bound reliably, so paths the
    parser already extracted are passed in and replaced verbatim first.
    """
    for known in sorted(filter(None, known_paths), key=len, reverse=True):
        text = text.replace(known, "<path>")
    text = _QUOTED_PATH_RE.sub(r"\1<path>\1", text)
    text = _URL_RE.sub("<url>", text)
    text = _WINDOWS_PATH_RE.sub("<path>", text)
    text = _UNIX_PATH_RE.sub("<path>", text)
    return _IPV4_RE.sub("<ip>", text)


def _cleaner(log: TranscodeLog, redact_output: bool) -> Callable[[str], str]:
    if not redact_output:
        return lambda text: text
    known = [log.input_path, log.ffmpeg_path]
    if log.media_source and isinstance(log.media_source.get("Path"), str):
        known.append(log.media_source["Path"])
    return lambda text: redact(text, known)


def render_text(reports: List[Report], redact_output: bool = False) -> str:
    blocks = [_render_one(log, findings, _cleaner(log, redact_output)) for log, findings in reports]
    return "\n\n".join(blocks) + "\n"


def _render_one(log: TranscodeLog, findings: List[Finding], clean: Callable[[str], str]) -> str:
    out = [f"== {clean(log.source)} =="]

    summary = []
    if log.ffmpeg_version or log.ffmpeg_path:
        version = log.ffmpeg_version or "unknown version"
        summary.append(("FFmpeg", f"{version} ({log.ffmpeg_path})" if log.ffmpeg_path else version))
    if log.video_encoder:
        hardware = f" (hardware: {log.hwaccel})" if log.hwaccel else " (software)"
        summary.append(("Video", log.video_encoder + hardware))
    if log.audio_encoder:
        summary.append(("Audio", log.audio_encoder))
    if log.input_path:
        summary.append(("Input", log.input_path))
    if log.speeds:
        summary.append(("Speed", f"median {median(log.speeds):.2f}x, last {log.speeds[-1]:.2f}x"))
    for label, value in summary:
        out.append(f"{label + ':':<8}{clean(value)}")
    if summary:
        out.append("")

    if not findings:
        out.append("No known problems found.")
        return "\n".join(out)

    for finding in findings:
        rule = finding.rule
        out.append(f"[{rule.severity.upper()}] {rule.title} ({rule.id})")
        out.extend(textwrap.wrap(rule.explanation, width=88, initial_indent="  ", subsequent_indent="  "))
        for number, line in finding.evidence:
            prefix = f"  line {number}: " if number else "  "
            out.append(prefix + clean(line))
        hidden = finding.count - len(finding.evidence)
        if hidden > 0:
            out.append(f"  ... and {hidden} more matching line{'s' if hidden != 1 else ''}")
        if finding.hints:
            out.append("  Try:")
            for hint in finding.hints:
                out.extend(textwrap.wrap(hint, width=88, initial_indent="    - ", subsequent_indent="      "))
        out.append("")

    return "\n".join(out).rstrip()


def render_json(reports: List[Report], redact_output: bool = False) -> str:
    payload = []
    for log, findings in reports:
        clean = _cleaner(log, redact_output)
        payload.append({
            "source": clean(log.source),
            "ffmpeg": {
                "path": clean(log.ffmpeg_path) if log.ffmpeg_path else None,
                "version": log.ffmpeg_version,
            },
            "input": clean(log.input_path) if log.input_path else None,
            "video_encoder": log.video_encoder,
            "audio_encoder": log.audio_encoder,
            "hwaccel": log.hwaccel,
            "speed": {
                "median": round(median(log.speeds), 3) if log.speeds else None,
                "last": log.speeds[-1] if log.speeds else None,
            },
            "exit_code": log.exit_code,
            "findings": [
                {
                    "id": f.rule.id,
                    "severity": f.rule.severity,
                    "title": f.rule.title,
                    "explanation": f.rule.explanation,
                    "count": f.count,
                    "evidence": [{"line": n or None, "text": clean(t)} for n, t in f.evidence],
                    "hints": f.hints,
                }
                for f in findings
            ],
        })
    return json.dumps(payload, indent=2) + "\n"
