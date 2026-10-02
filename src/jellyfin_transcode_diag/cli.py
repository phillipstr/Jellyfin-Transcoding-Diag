"""Command-line entry point: ``jf-transcode-diag``, also installed as ``jftd``.

Two modes: ``log`` (the default) diagnoses FFmpeg transcode logs after the fact,
and ``scan`` probes a media library for files likely to fail to transcode.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__
from .parser import parse_log
from .report import (
    Report,
    render_json,
    render_scan_result,
    render_scan_summary,
    render_text,
    write_scan_report,
)
from .rules import ALL_RULES, ERROR, SEVERITY_ORDER, diagnose
from .locations import TRANSCODE_LOG_GLOB, FfprobeNotFound, LogDirNotFound, find_ffprobe, find_log_dir
from .scanner import ALL_CHECKS, iter_media_files, scan

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_USAGE = 2

PROG = "jf-transcode-diag"
PROG_NAMES = (PROG, "jftd")
MODES = ("log", "scan")
DEFAULT_SCAN_OUTPUT = "transcode-scan.csv"


def _prog() -> str:
    """Name the command the way it was invoked, so ``jftd --help`` says ``jftd``."""
    name = os.path.splitext(os.path.basename(sys.argv[0]))[0] if sys.argv else ""
    return name if name in PROG_NAMES else PROG


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_prog(),
        usage="%(prog)s [log] [options] [PATH ...]\n       %(prog)s scan [options] PATH [PATH ...]",
        description=(
            "Diagnose failed or struggling Jellyfin transcodes. Point it at an "
            "FFmpeg.Transcode-*.log file, at Jellyfin's log directory, or pipe a log in. "
            "With no PATH it looks for Jellyfin's log directory itself. "
            "Run '%(prog)s scan --help' to check a media library for files likely to fail "
            "before anyone plays them."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="log file, or directory containing FFmpeg.*.log files; '-' reads stdin "
             "(default: find Jellyfin's log directory)",
    )
    _add_jellyfin_dir(parser)
    parser.add_argument(
        "-n", "--latest",
        type=int,
        default=1,
        metavar="N",
        help="for directories, check the N most recent transcode logs (default: 1)",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument(
        "--redact",
        action="store_true",
        help="hide file paths, URLs and IP addresses so the report can be shared",
    )
    parser.add_argument("--list-rules", action="store_true", help="list known problems and exit")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _add_jellyfin_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--jellyfin-dir",
        metavar="DIR",
        help="Jellyfin's data, config or install folder, when it is not in a standard "
             "place (JELLYFIN_LOG_DIR, JELLYFIN_DATA_DIR, JELLYFIN_CONFIG_DIR and "
             "JELLYFIN_FFMPEG are also read)",
    )


def _collect(paths: Sequence[str], latest: int) -> List[Path]:
    files: List[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            logs = sorted(path.glob(TRANSCODE_LOG_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
            if not logs:
                raise FileNotFoundError(f"no {TRANSCODE_LOG_GLOB} files in {raw}")
            files.extend(logs[:latest])
        elif path.is_file():
            files.append(path)
        else:
            raise FileNotFoundError(f"no such file or directory: {raw}")
    return files


def build_scan_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{_prog()} scan",
        description=(
            "Probe media files with ffprobe and list the ones likely to fail or struggle "
            "to transcode, with their full paths. Findings are predictions from file "
            "metadata, not observed failures. Requires ffprobe (jellyfin-ffmpeg's is "
            "used when installed)."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="media file, or library directory to search recursively",
    )
    parser.add_argument(
        "-o", "--output",
        default=DEFAULT_SCAN_OUTPUT,
        metavar="FILE",
        help=f"write findings here as CSV, or JSON if FILE ends in .json (default: {DEFAULT_SCAN_OUTPUT})",
    )
    parser.add_argument("--no-output", action="store_true", help="only print to the terminal")
    parser.add_argument(
        "--min-severity",
        choices=sorted(SEVERITY_ORDER, key=SEVERITY_ORDER.__getitem__),
        default="warning",
        help="lowest severity to report (default: warning; 'info' adds notes "
             "such as image subtitles and interlacing)",
    )
    parser.add_argument(
        "--ffprobe",
        metavar="PATH",
        help="ffprobe to run, or the folder holding it (default: the one Jellyfin uses, "
             "else the one on PATH)",
    )
    _add_jellyfin_dir(parser)
    parser.add_argument(
        "-j", "--jobs",
        type=int,
        default=4,
        metavar="N",
        help="files to probe at once (default: 4)",
    )
    parser.add_argument("--list-checks", action="store_true", help="list the checks and exit")
    return parser


def _list_rules() -> str:
    width = max(len(rule.id) for rule in ALL_RULES)
    return "\n".join(f"{rule.id:<{width}}  {rule.severity:<7}  {rule.title}" for rule in ALL_RULES) + "\n"


def _list_checks() -> str:
    width = max(len(check.id) for check in ALL_CHECKS)
    return "\n".join(
        f"{check.id:<{width}}  {check.severity:<7}  {check.title}" for check in ALL_CHECKS
    ) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "scan":
        return scan_main(argv[1:])
    if argv and argv[0] == "log":
        argv = argv[1:]
    return log_main(argv)


def log_main(argv: Sequence[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_rules:
        sys.stdout.write(_list_rules())
        return EXIT_OK
    if args.latest < 1:
        parser.error("--latest must be at least 1")

    paths = list(args.paths)
    if not paths:
        if args.jellyfin_dir or sys.stdin.isatty():
            try:
                log_dir = find_log_dir(args.jellyfin_dir)
            except LogDirNotFound as error:
                sys.stderr.write(f"{parser.prog}: {error}\n")
                return EXIT_USAGE
            sys.stderr.write(f"Reading logs in {log_dir.path} (from {log_dir.source})\n")
            paths = [log_dir.path]
        else:
            paths = ["-"]

    reports: List[Report] = []
    try:
        if "-" in paths:
            log = parse_log(sys.stdin.read(), source="<stdin>")
            reports.append((log, diagnose(log)))
        for path in _collect([p for p in paths if p != "-"], args.latest):
            log = parse_log(path.read_text(encoding="utf-8", errors="replace"), source=str(path))
            reports.append((log, diagnose(log)))
    except OSError as error:
        sys.stderr.write(f"{parser.prog}: {error}\n")
        return EXIT_USAGE

    render = render_json if args.json else render_text
    sys.stdout.write(render(reports, redact_output=args.redact))

    has_errors = any(f.rule.severity == ERROR for _, findings in reports for f in findings)
    return EXIT_PROBLEMS if has_errors else EXIT_OK


def scan_main(argv: Sequence[str]) -> int:
    parser = build_scan_parser()
    args = parser.parse_args(argv)

    if args.list_checks:
        sys.stdout.write(_list_checks())
        return EXIT_OK
    if not args.paths:
        parser.error("give a media file or library directory to scan")
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")

    try:
        found = find_ffprobe(args.ffprobe, args.jellyfin_dir)
        files = list(iter_media_files(args.paths))
    except (FfprobeNotFound, OSError) as error:
        sys.stderr.write(f"{parser.prog}: {error}\n")
        return EXIT_USAGE
    ffprobe = found.path
    sys.stderr.write(f"Using {ffprobe} (from {found.source})\n")

    progress = sys.stderr.isatty()
    done = 0

    def show(result) -> None:
        nonlocal done
        done += 1
        if progress:
            sys.stderr.write("\r\033[K")
        text = render_scan_result(result, args.min_severity)
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()
        if progress:
            sys.stderr.write(f"Scanned {done}/{len(files)}")
            sys.stderr.flush()

    results = scan(files, ffprobe, jobs=args.jobs, on_result=show)
    if progress:
        sys.stderr.write("\r\033[K")

    if any(render_scan_result(r, args.min_severity) for r in results):
        sys.stdout.write("\n")
    sys.stdout.write(render_scan_summary(results, args.min_severity))

    if not args.no_output:
        output = Path(args.output)
        try:
            rows = write_scan_report(results, output, args.min_severity)
        except OSError as error:
            sys.stderr.write(f"{parser.prog}: could not write {output}: {error}\n")
            return EXIT_USAGE
        sys.stdout.write(f"Wrote {rows} finding{'s' if rows != 1 else ''} to {output}\n")

    has_errors = any(f.check.severity == ERROR for r in results for f in r.findings)
    return EXIT_PROBLEMS if has_errors else EXIT_OK
