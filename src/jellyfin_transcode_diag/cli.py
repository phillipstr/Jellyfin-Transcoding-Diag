"""Command-line entry point: ``jf-transcode-diag``, also installed as ``jftd``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__
from .parser import parse_log
from .report import Report, render_json, render_text
from .rules import ALL_RULES, ERROR, diagnose

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_USAGE = 2

TRANSCODE_LOG_GLOB = "FFmpeg.*.log"

PROG = "jf-transcode-diag"
PROG_NAMES = (PROG, "jftd")


def _prog() -> str:
    """Name the command the way it was invoked, so ``jftd --help`` says ``jftd``."""
    name = os.path.splitext(os.path.basename(sys.argv[0]))[0] if sys.argv else ""
    return name if name in PROG_NAMES else PROG


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_prog(),
        description=(
            "Diagnose failed or struggling Jellyfin transcodes. Point it at an "
            "FFmpeg.Transcode-*.log file, at Jellyfin's log directory, or pipe a log in."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="log file, or directory containing FFmpeg.*.log files; '-' reads stdin",
    )
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


def _list_rules() -> str:
    width = max(len(rule.id) for rule in ALL_RULES)
    return "\n".join(f"{rule.id:<{width}}  {rule.severity:<7}  {rule.title}" for rule in ALL_RULES) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_rules:
        sys.stdout.write(_list_rules())
        return EXIT_OK
    if args.latest < 1:
        parser.error("--latest must be at least 1")

    paths = list(args.paths)
    if not paths:
        if sys.stdin.isatty():
            parser.print_usage(sys.stderr)
            sys.stderr.write(f"{parser.prog}: give a log file or directory, or pipe a log in\n")
            return EXIT_USAGE
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
