"""jftd command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__, caps as capsmod, hwtest, jellyfin, logs, profiles, scan
from .runner import Runner, RunnerError

JELLYFIN_FFMPEG_DIR = "/usr/lib/jellyfin-ffmpeg"
DEFAULT_URL = "http://127.0.0.1:8096"
DEFAULT_DEVICE = "/dev/dri/renderD128"

EXIT_OK, EXIT_PROBLEM, EXIT_ERROR = 0, 1, 2


class CliError(Exception):
    pass


# ---------------------------------------------------------------- settings

def _env(name: str, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def make_runner(args) -> Runner:
    return Runner(container=args.docker, docker_bin=args.docker_bin)


def tool_path(args, runner: Runner, name: str) -> str:
    explicit = getattr(args, name)
    if explicit:
        return explicit
    bundled = f"{JELLYFIN_FFMPEG_DIR}/{name}"
    if args.docker or runner.exists(bundled):
        return bundled
    return name


def log_dir(args) -> str:
    if args.log_dir:
        return args.log_dir
    return "/config/log" if args.docker else "/var/log/jellyfin"


def api_key(args) -> str | None:
    if args.api_key_file:
        try:
            with open(os.path.expanduser(args.api_key_file)) as f:
                return f.read().strip() or None
        except OSError as e:
            raise CliError(f"cannot read API key file: {e.strerror}") from None
    return _env("JELLYFIN_API_KEY")


def client(args) -> jellyfin.Client:
    return jellyfin.Client(args.url, api_key(args))


def load_encoding(args) -> dict:
    if args.encoding_json:
        try:
            with open(args.encoding_json) as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            raise CliError(f"cannot load {args.encoding_json}: {e}") from None
    return client(args).get_encoding()


def jellyfin_device(encoding: dict | None) -> str | None:
    if not encoding:
        return None
    accel = str(encoding.get("HardwareAccelerationType") or "").lower()
    if accel == "qsv":
        return encoding.get("QsvDevice") or encoding.get("VaapiDevice") or None
    return encoding.get("VaapiDevice") or None


def load_caps(args, runner: Runner, encoding: dict | None = None) -> capsmod.Capabilities:
    backend_name = args.backend or capsmod.backend_for_accel((encoding or {}).get("HardwareAccelerationType"))
    backend = capsmod.get_backend(backend_name)
    device = args.device or jellyfin_device(encoding) or DEFAULT_DEVICE
    if args.vainfo_output:
        if not hasattr(backend, "parse"):
            raise CliError(f"--vainfo-output is only valid for the vaapi backend, not {backend.name}")
        try:
            with open(args.vainfo_output, encoding="utf-8", errors="replace") as f:
                return backend.parse(f.read(), device)
        except OSError as e:
            raise CliError(f"cannot read {args.vainfo_output}: {e.strerror}") from None
    return backend.probe(runner, tool_path(args, runner, "vainfo"), device)


def dump(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=False, default=list))


# ---------------------------------------------------------------- caps

def encode_codecs(c: capsmod.Capabilities) -> list[str]:
    table = getattr(capsmod.get_backend(c.backend), "CODEC_PROFILES", {})
    return [profiles.label(codec) for codec, profs in table.items() if any(p in c.encode for p in profs)]


def format_caps(c: capsmod.Capabilities) -> str:
    out = [
        f"Backend: {c.backend}   Device: {c.device or '?'}",
        f"Driver:  {c.driver or 'unknown'}",
        "",
        f"  {'Jellyfin setting':<20} {'GPU decode':<11} Evidence",
    ]
    for feat in profiles.FEATURES:
        reqs = c.requires(feat)
        if c.supports(feat):
            have = [p for p in reqs if p in c.decode]
            out.append(f"  {profiles.label(feat):<20} {'yes':<11} {', '.join(have)}")
        else:
            why = "needs " + " or ".join(reqs)
            par = profiles.parent(feat)
            if par and not c.supports(par):
                why = f"needs {profiles.label(par)} decode first"
            out.append(f"  {profiles.label(feat):<20} {'no':<11} {why}")
    enc = encode_codecs(c)
    out += ["", f"GPU encode: {', '.join(enc) if enc else 'none found'}"]
    return "\n".join(out)


def cmd_caps(args) -> int:
    runner = make_runner(args)
    c = load_caps(args, runner)
    if args.json:
        dump({**c.to_dict(), "encode_codecs": encode_codecs(c)})
    else:
        print(format_caps(c))
    return EXIT_OK


# ---------------------------------------------------------------- drift

STATUS_TEXT = {
    profiles.BROKEN: "BROKEN  transcodes of this format will fail",
    profiles.LATENT: "latent  harmless now; fails if its codec is turned on",
    profiles.UNUSED: "unused  GPU could decode this; CPU is used instead",
    profiles.OK: "ok",
}


def format_drift(report: profiles.DriftReport, c: capsmod.Capabilities, changes: dict) -> str:
    out = [
        f"Jellyfin: hardware acceleration '{report.accel}'",
        f"GPU:      {c.driver or 'unknown driver'} ({c.backend}, {c.device})",
        "",
        f"  {'Setting':<20} {'Jellyfin':<9} {'GPU':<5} Status",
    ]
    for i in report.items:
        out.append(
            f"  {i.label:<20} {'on' if i.enabled else 'off':<9} {'yes' if i.supported else 'no':<5} {STATUS_TEXT[i.status]}"
        )
    for n in report.notes:
        out += ["", f"Note: {n}"]

    broken = report.by_status(profiles.BROKEN)
    latent = report.by_status(profiles.LATENT)
    if not (broken or latent):
        out += ["", "No drift: Jellyfin only asks the GPU to decode formats it supports."]
        unused = report.by_status(profiles.UNUSED)
        if unused:
            out.append(
                "Optional: the GPU could also decode "
                + ", ".join(i.label for i in unused)
                + " (`jftd fix --also-enable` to turn them on)."
            )
        return "\n".join(out)

    out.append("")
    if broken:
        out.append(
            f"Problem: Jellyfin asks the GPU to decode {len(broken)} format(s) it can't "
            f"({', '.join(i.label for i in broken)}). Those transcodes fail with errors like "
            "'Failed setup for format vaapi: hwaccel initialisation returned error' and "
            "'Error reinitializing filters' (Jellyfin: 'FFmpeg exited with code 218')."
        )
    if latent:
        out.append(
            f"Also enabled but unsupported (inactive for now): {', '.join(i.label for i in latent)}."
        )
    untick = [i.label for i in broken + latent]
    out += [
        "",
        "Fix: Dashboard > Playback > Transcoding > 'Enable hardware decoding for':",
        f"  untick: {', '.join(untick)}",
        "  then Save. Those formats are then decoded on the CPU and still encoded on the GPU.",
        "Or run `jftd fix` (shows this change and asks before writing).",
        "",
        "API change (POST /System/Configuration/encoding, merged into the current config):",
        "  " + json.dumps(changes),
    ]
    return "\n".join(out)


def cmd_drift(args) -> int:
    runner = make_runner(args)
    encoding = load_encoding(args)
    c = load_caps(args, runner, encoding)
    report = profiles.config_drift(encoding, c)
    changes = profiles.recommended_settings(encoding, c)
    if args.json:
        dump({"drift": report.to_dict(), "caps": c.to_dict(), "recommended_changes": changes})
    else:
        print(format_drift(report, c, changes))
    return EXIT_PROBLEM if report.has_problems else EXIT_OK


# ---------------------------------------------------------------- fix

def _confirm(args, prompt: str) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        raise CliError("refusing to write settings without confirmation; re-run interactively or add --yes")
    try:
        answer = input(f"{prompt} Type 'yes' to apply: ")
    except EOFError:
        return False
    return answer.strip().lower() == "yes"


def _show_changes(current: dict, changes: dict) -> None:
    print("Changes to /System/Configuration/encoding:")
    for k, v in changes.items():
        print(f"  {k}: {json.dumps(current.get(k))} -> {json.dumps(v)}")


def cmd_fix(args) -> int:
    if args.encoding_json:
        raise CliError("fix writes to a live server; drop --encoding-json")
    cl = client(args)
    current = cl.get_encoding()

    if args.restore:
        try:
            with open(args.restore) as f:
                saved = json.load(f)
        except (OSError, ValueError) as e:
            raise CliError(f"cannot load {args.restore}: {e}") from None
        changes = {k: v for k, v in saved.items() if k in current and current[k] != v}
        if not changes:
            print("Server already matches the backup; nothing to change.")
            return EXIT_OK
    else:
        c = load_caps(args, make_runner(args), current)
        changes = profiles.recommended_settings(current, c, enable_supported=args.also_enable)
        if not changes:
            print("Nothing to change: Jellyfin only asks the GPU for formats it supports.")
            return EXIT_OK

    jellyfin.check_types(current, changes)
    _show_changes(current, changes)
    if not _confirm(args, "Write these settings to Jellyfin?"):
        print("Not applied.")
        return EXIT_PROBLEM
    _, _, backup = jellyfin.apply_changes(cl, changes, args.backup_dir or jellyfin.default_backup_dir())
    print(f"Applied and verified by reading back. Previous config saved to {backup}")
    print(f"Undo with: jftd fix --restore {backup}")
    return EXIT_OK


# ---------------------------------------------------------------- scan

def cmd_scan(args) -> int:
    runner = make_runner(args)
    directory = log_dir(args)
    newer = 0.0
    if args.since:
        try:
            newer = time.time() - scan.parse_since(args.since)
        except ValueError as e:
            raise CliError(str(e)) from None
    if args.state:
        newer = max(newer, scan.load_state(args.state))
    results, exit_codes = scan.scan_dir(runner, directory, newer)
    newest = max((r.mtime for r in results), default=0.0)
    if args.redact:
        results = scan.redact_results(results)
    summary = scan.summarise(results, exit_codes)
    if args.json:
        payload = {**summary}
        if args.verbose:
            payload["logs"] = [r.to_dict() for r in results]
            if args.redact:
                for d in payload["logs"]:
                    d["path"] = d["name"]
        dump(payload)
    else:
        source = directory + (f" ({runner.where})" if args.docker else "")
        if args.since:
            source += f", last {args.since}"
        print(scan.format_summary(summary, source, args.max_files))
        if args.verbose:
            failed = [r for r in results if r.status == logs.FAILED]
            if failed:
                print("\nFailed runs:")
            for r in failed:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.mtime))
                print(f"  {when}  {r.cls:<10} {r.name}")
                print(f"      {r.video.describe()}  {r.media_path or ''}")
                print(f"      {r.evidence}")
    if args.state and results:
        scan.save_state(args.state, newest)
    return EXIT_PROBLEM if summary["status"]["failed"] else EXIT_OK


# ---------------------------------------------------------------- test

def cmd_test(args) -> int:
    runner = make_runner(args)
    if not runner.exists(args.file):
        raise CliError(f"file not found on {runner.where}: {args.file}")
    device = args.device or DEFAULT_DEVICE
    res = hwtest.run_test(
        runner,
        tool_path(args, runner, "ffmpeg"),
        tool_path(args, runner, "ffprobe"),
        args.file,
        args.seconds,
        device,
        args.mode,
    )
    c = None
    try:
        args.device = device
        c = load_caps(args, runner)
    except (capsmod.CapsError, RunnerError, CliError):
        pass
    if args.json:
        if c is not None:
            res["gpu_supports_needs"] = all(c.supports(f) for f in res["needs"])
        dump(res)
    else:
        print(hwtest.format_result(res, c))
    return EXIT_OK if res["verdict"] == hwtest.HW_OK else EXIT_PROBLEM


# ---------------------------------------------------------------- exit-code

def cmd_exit_code(args) -> int:
    print(logs.explain_exit_code(args.code))
    hint = scan._hint(args.code)
    if hint:
        print(f"  {hint}")
    return EXIT_OK


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("environment")
    g.add_argument("--url", default=_env("JELLYFIN_URL", DEFAULT_URL),
                   help="Jellyfin server URL (env JELLYFIN_URL, default %(default)s)")
    g.add_argument("--api-key-file", default=_env("JELLYFIN_API_KEY_FILE"),
                   help="file holding an admin API key (env JELLYFIN_API_KEY_FILE); "
                        "or set JELLYFIN_API_KEY")
    g.add_argument("--docker", metavar="CONTAINER", default=_env("JFTD_DOCKER"),
                   help="run ffmpeg/vainfo and read logs inside this container via "
                        "`docker exec` (env JFTD_DOCKER)")
    g.add_argument("--docker-bin", default=_env("JFTD_DOCKER_BIN", "docker"),
                   help="container CLI, e.g. podman (env JFTD_DOCKER_BIN, default %(default)s)")
    g.add_argument("--log-dir", default=_env("JFTD_LOG_DIR"),
                   help="Jellyfin log directory (env JFTD_LOG_DIR; default /var/log/jellyfin, "
                        "or /config/log with --docker)")
    g.add_argument("--ffmpeg", default=_env("JFTD_FFMPEG"), help="ffmpeg binary (env JFTD_FFMPEG)")
    g.add_argument("--ffprobe", default=_env("JFTD_FFPROBE"), help="ffprobe binary (env JFTD_FFPROBE)")
    g.add_argument("--vainfo", default=_env("JFTD_VAINFO"), help="vainfo binary (env JFTD_VAINFO)")
    g.add_argument("--device", default=_env("JFTD_DEVICE"),
                   help=f"GPU render node (env JFTD_DEVICE; default Jellyfin's setting or {DEFAULT_DEVICE})")
    g.add_argument("--backend", choices=sorted(capsmod.BACKENDS), default=_env("JFTD_BACKEND"),
                   help="capability backend (default: from Jellyfin's acceleration type, else vaapi)")
    g.add_argument("--vainfo-output", metavar="FILE",
                   help="use saved `vainfo` output instead of running vainfo")
    g.add_argument("--encoding-json", metavar="FILE",
                   help="use a saved /System/Configuration/encoding JSON instead of the API")
    g.add_argument("--json", action="store_true", help="machine-readable output")

    p = argparse.ArgumentParser(
        prog="jftd",
        description="Diagnose Jellyfin transcoding failures. Read-only unless you run `fix`.",
        epilog="Exit status: 0 = fine, 1 = problem found, 2 = could not run.",
    )
    p.add_argument("--version", action="version", version=f"jftd {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    s = sub.add_parser("caps", parents=[common], help="GPU decode capabilities in Jellyfin terms")
    s.set_defaults(func=cmd_caps)

    s = sub.add_parser("drift", parents=[common],
                       help="compare Jellyfin's hardware-decoding settings with the GPU")
    s.set_defaults(func=cmd_drift)

    s = sub.add_parser("scan", parents=[common], help="classify FFmpeg logs and summarise failures")
    s.add_argument("--since", help="only logs modified in this window, e.g. 24h, 7d")
    s.add_argument("--state", metavar="FILE",
                   help="remember the newest log seen and only scan newer ones next time")
    s.add_argument("--redact", action="store_true", help="replace media paths with short hashes")
    s.add_argument("--max-files", type=int, default=15, help="files to list (default %(default)s)")
    s.add_argument("-v", "--verbose", action="store_true", help="list every failed run")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("test", parents=[common], help="encode FILE with GPU decode and CPU decode; compare")
    s.add_argument("file", help="media file (path as seen inside the container with --docker)")
    s.add_argument("--seconds", type=int, default=30, help="seconds to encode (default %(default)s)")
    s.add_argument("--mode", choices=hwtest.MODES, default="qsv",
                   help="GPU encoder family: qsv (h264_qsv) or vaapi (h264_vaapi); default %(default)s")
    s.set_defaults(func=cmd_test)

    s = sub.add_parser("fix", parents=[common],
                       help="write the drift fix to Jellyfin (asks first; backs up; verifies)")
    s.add_argument("--also-enable", action="store_true",
                   help="also turn ON formats the GPU supports but Jellyfin has off")
    s.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    s.add_argument("--backup-dir", help=f"where to save the previous config (default {jellyfin.default_backup_dir()})")
    s.add_argument("--restore", metavar="BACKUP", help="write a saved backup back instead")
    s.set_defaults(func=cmd_fix)

    s = sub.add_parser("exit-code", help="explain an FFmpeg exit code, e.g. 218")
    s.add_argument("code", type=int)
    s.set_defaults(func=cmd_exit_code)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CliError, capsmod.CapsError, jellyfin.JellyfinError, RunnerError) as e:
        print(f"jftd: {e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130
