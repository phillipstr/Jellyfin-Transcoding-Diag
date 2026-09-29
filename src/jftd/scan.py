"""Scan a Jellyfin log directory and summarise FFmpeg failures."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import time
from collections import Counter, defaultdict

from . import logs
from .runner import Runner

EXIT_HINTS = {
    -38: "typically hardware decode of a codec/profile the GPU does not support",
    -28: "disk full",
    -12: "out of memory",
    -5: "I/O error; often the GPU device or a network share",
    -2: "a file or device was not found",
    -13: "permission denied",
}

_SERVER_LOG = re.compile(r"^(?:log_\d+.*|jellyfin.*)\.(?:log|txt)$")
_SINCE = re.compile(r"^(\d+(?:\.\d+)?)([smhdw])$")


def parse_since(value: str) -> float:
    """'30m', '24h', '7d', '2w' -> seconds."""
    m = _SINCE.match(value.strip().lower())
    if not m:
        raise ValueError(f"bad duration '{value}' (use e.g. 30m, 24h, 7d)")
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    return float(m.group(1)) * mult


def load_state(path: str | None) -> float:
    if not path or not os.path.exists(path):
        return 0.0
    try:
        with open(path) as f:
            return float(json.load(f).get("last_mtime", 0.0))
    except (OSError, ValueError, AttributeError):
        return 0.0


def save_state(path: str, last_mtime: float) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"last_mtime": last_mtime}, f)
    os.replace(tmp, path)


def scan_dir(runner: Runner, log_dir: str, newer_than: float = 0.0):
    """Analyze FFmpeg logs (and exit codes in server logs) newer than a timestamp."""
    entries = runner.listdir(log_dir)
    results = []
    exit_codes: Counter = Counter()
    for e in sorted(entries, key=lambda e: e.mtime):
        if e.mtime <= newer_than:
            continue
        if logs.is_ffmpeg_log(e.name):
            results.append(logs.analyze(runner.read_text(e.path), e.path, e.mtime))
        elif _SERVER_LOG.match(e.name):
            exit_codes.update(logs.server_exit_codes(runner.read_text(e.path)))
    return results, dict(exit_codes)


def redact(path: str | None) -> str | None:
    if not path:
        return path
    ext = os.path.splitext(path)[1]
    return "media-" + hashlib.sha256(path.encode()).hexdigest()[:10] + ext


def redact_results(results: list) -> list:
    """Copies of results with media paths (and their folders) hashed, also inside evidence lines."""
    out = []
    for r in results:
        ev = r.evidence
        if ev and r.media_path:
            ev = ev.replace(r.media_path, redact(r.media_path))
            folder = os.path.dirname(r.media_path)
            if len(folder) > 1:
                ev = ev.replace(folder, "<media-dir>")
        out.append(dataclasses.replace(r, media_path=redact(r.media_path), evidence=ev))
    return out


def summarise(results: list, exit_codes: dict[int, int] | None = None) -> dict:
    status = Counter(r.status for r in results)
    failures = [r for r in results if r.status == logs.FAILED]
    by_class = Counter(r.cls for r in failures)

    by_video: dict = defaultdict(Counter)
    for r in failures:
        key = r.video.describe() + (f", hw decode {r.hwaccel}" if r.hwaccel else ", CPU decode")
        by_video[key][r.cls] += 1

    by_file: dict = {}
    for r in failures:
        key = r.media_path or r.name
        f = by_file.setdefault(key, {"file": key, "count": 0, "classes": Counter(), "last": 0.0, "last_log": None})
        f["count"] += 1
        f["classes"][r.cls] += 1
        if r.mtime >= f["last"]:
            f["last"], f["last_log"] = r.mtime, r.name

    examples = {}
    for r in failures:
        examples.setdefault(r.cls, {"log": r.name, "line": r.evidence})

    return {
        "total": len(results),
        "status": {k: status.get(k, 0) for k in (logs.OK, logs.ABORTED, logs.FAILED)},
        "by_class": dict(by_class.most_common()),
        "by_video": {k: dict(v) for k, v in sorted(by_video.items(), key=lambda kv: -sum(kv[1].values()))},
        "by_file": [
            {**f, "classes": dict(f["classes"])}
            for f in sorted(by_file.values(), key=lambda f: (-f["count"], -f["last"]))
        ],
        "examples": examples,
        "server_exit_codes": {
            str(code): {"count": n, "meaning": logs.explain_exit_code(code), "hint": _hint(code)}
            for code, n in sorted((exit_codes or {}).items(), key=lambda kv: -kv[1])
            if code != 0
        },
    }


def _hint(code: int) -> str | None:
    neg = code - 256 if code > 128 else code
    return EXIT_HINTS.get(neg)


def format_summary(s: dict, source: str, max_files: int = 15) -> str:
    out = [f"Scanned {s['total']} FFmpeg logs in {source}", ""]
    st = s["status"]
    out.append(f"  ok        {st['ok']:>5}")
    out.append(f"  aborted   {st['aborted']:>5}   stopped by Jellyfin (client left or seeked); not failures")
    out.append(f"  failed    {st['failed']:>5}")

    if s["by_class"]:
        out += ["", "Failures by class:"]
        for cls, n in s["by_class"].items():
            fc = logs.CLASS_BY_NAME[cls]
            out.append(f"  {cls:<10} {n:>5}   {fc.summary}")

        out += ["", "Failures by source video:"]
        width = max(len(k) for k in s["by_video"])
        for key, classes in s["by_video"].items():
            n = sum(classes.values())
            detail = ", ".join(f"{c} x{k}" for c, k in classes.items())
            out.append(f"  {key:<{width}}  {n:>5}   {detail}")

        out += ["", "Failures by file:"]
        for f in s["by_file"][:max_files]:
            classes = ", ".join(f["classes"])
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(f["last"])) if f["last"] else "?"
            out.append(f"  {f['count']:>4}  {os.path.basename(f['file'])}  [{classes}]  last {when}")
        extra = len(s["by_file"]) - max_files
        if extra > 0:
            out.append(f"        ... and {extra} more (use --json for all)")

        out += ["", "Example per class:"]
        for cls, ex in s["examples"].items():
            out.append(f"  {cls}: {ex['log']}")
            out.append(f"      {ex['line']}")

        out += ["", "What to do:"]
        for cls in s["by_class"]:
            out.append(f"  {cls}: {logs.CLASS_BY_NAME[cls].advice}")

    if s["server_exit_codes"]:
        out += ["", "FFmpeg exit codes in the Jellyfin server log:"]
        for code, info in s["server_exit_codes"].items():
            line = f"  {info['count']:>5} x {info['meaning']}"
            if info["hint"]:
                line += f": {info['hint']}"
            out.append(line)
    return "\n".join(out)
