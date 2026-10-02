"""Find Jellyfin's transcode logs and the ffprobe it uses, wherever Jellyfin is installed.

Jellyfin's folders depend on how it was installed, and any of them can be moved
with its own environment variables (``JELLYFIN_LOG_DIR``, ``JELLYFIN_DATA_DIR``,
``JELLYFIN_CONFIG_DIR``, ``JELLYFIN_FFMPEG``) or command-line options
(``--logdir``, ``--datadir``, ``--configdir``, ``--ffmpeg``). Those settings are
read from, most specific first: ``--jellyfin-dir``, this process's environment,
a running Jellyfin process (Linux only), and the packages' defaults files. The
usual install locations are tried after that.
"""

from __future__ import annotations

import os
import shlex
import shutil
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

TRANSCODE_LOG_GLOB = "FFmpeg.*.log"

# Shell-style files the Debian/Ubuntu and RPM packages read Jellyfin's settings from.
DEFAULTS_FILES = ("/etc/default/jellyfin", "/etc/sysconfig/jellyfin")
PROC = "/proc"

ENV_KEYS = {
    "JELLYFIN_LOG_DIR": "log",
    "JELLYFIN_DATA_DIR": "data",
    "JELLYFIN_CONFIG_DIR": "config",
    "JELLYFIN_FFMPEG": "ffmpeg",
}
OPTION_KEYS = {
    "-l": "log", "--logdir": "log",
    "-d": "data", "--datadir": "data",
    "-c": "config", "--configdir": "config",
    "--ffmpeg": "ffmpeg",
}

# Where packaged installs put jellyfin-ffmpeg's ffprobe.
JELLYFIN_FFPROBE_PATHS = (
    "/usr/lib/jellyfin-ffmpeg/ffprobe",
    "/usr/share/jellyfin-ffmpeg/ffprobe",
    "/Applications/Jellyfin.app/Contents/MacOS/ffprobe",
)

# A Jellyfin setting: (value, where it was set).
Setting = Tuple[str, str]


@dataclass(frozen=True)
class Candidate:
    path: str
    source: str


class LogDirNotFound(Exception):
    pass


class FfprobeNotFound(Exception):
    pass


def _parse_defaults_file(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        try:
            words = shlex.split(raw, comments=True)
        except ValueError:
            continue
        values[key.strip()] = " ".join(words)
    return values


def _from_options(args: Sequence[str], source: str) -> Dict[str, Setting]:
    settings: Dict[str, Setting] = {}
    for index, arg in enumerate(args):
        name, eq, value = arg.partition("=")
        key = OPTION_KEYS.get(name)
        if key is None:
            continue
        if not eq:
            if index + 1 >= len(args):
                continue
            value = args[index + 1]
        if value:
            settings.setdefault(key, (value, f"{name} in {source}"))
    return settings


def _from_env(environ: Mapping[str, str], source: str) -> Dict[str, Setting]:
    settings: Dict[str, Setting] = {}
    for name, key in ENV_KEYS.items():
        if environ.get(name):
            settings[key] = (environ[name], f"{name} in {source}")
    # The packages pass FFmpeg's path as an option: JELLYFIN_FFMPEG_OPT="--ffmpeg=...".
    if "ffmpeg" not in settings and environ.get("JELLYFIN_FFMPEG_OPT"):
        try:
            opts = shlex.split(environ["JELLYFIN_FFMPEG_OPT"])
        except ValueError:
            opts = []
        settings.update(_from_options(opts, f"JELLYFIN_FFMPEG_OPT in {source}"))
    return settings


def _read_proc_file(path: str) -> Optional[List[str]]:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    return [part.decode("utf-8", "replace") for part in data.split(b"\0") if part]


def running_jellyfin() -> Iterator[Tuple[List[str], Dict[str, str]]]:
    """Yield the command line and (when readable) environment of each running Jellyfin."""
    try:
        pids = sorted(name for name in os.listdir(PROC) if name.isdigit())
    except OSError:
        return
    for pid in pids:
        args = _read_proc_file(os.path.join(PROC, pid, "cmdline"))
        if not args:
            continue
        program = os.path.basename(args[0]).lower()
        if program not in ("jellyfin", "jellyfin.exe") and not any(
            a.lower().endswith("jellyfin.dll") for a in args
        ):
            continue
        environ = _read_proc_file(os.path.join(PROC, pid, "environ")) or []
        yield args, dict(item.partition("=")[::2] for item in environ)


def jellyfin_settings(environ: Optional[Mapping[str, str]] = None) -> List[Dict[str, Setting]]:
    """Jellyfin's folder settings from every place they can be set, most specific first."""
    environ = os.environ if environ is None else environ
    found = [_from_env(environ, "the environment")]
    for args, process_env in running_jellyfin():
        found.append(_from_options(args, "running Jellyfin's command line"))
        found.append(_from_env(process_env, "running Jellyfin's environment"))
    for path in DEFAULTS_FILES:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found.append(_from_env(_parse_defaults_file(text), path))
    return [settings for settings in found if settings]


def _dedupe(candidates: Iterable[Candidate]) -> List[Candidate]:
    seen = set()
    unique = []
    for candidate in candidates:
        key = os.path.normcase(os.path.normpath(candidate.path))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def log_dir_candidates(
    jellyfin_dir: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    settings: Optional[List[Dict[str, Setting]]] = None,
) -> List[Candidate]:
    """Folders that may hold Jellyfin's transcode logs, most likely first."""
    environ = os.environ if environ is None else environ
    settings = jellyfin_settings(environ) if settings is None else settings
    found: List[Candidate] = []
    if jellyfin_dir:
        found.append(Candidate(os.path.join(jellyfin_dir, "log"), "--jellyfin-dir"))
        found.append(Candidate(jellyfin_dir, "--jellyfin-dir"))
    for setting in settings:
        if "log" in setting:
            found.append(Candidate(*setting["log"]))
        if "data" in setting:
            value, source = setting["data"]
            found.append(Candidate(os.path.join(value, "log"), source))

    home = os.path.expanduser("~")
    found.append(Candidate("/var/log/jellyfin", "Debian, Ubuntu and RPM packages"))
    found.append(Candidate("/config/log", "official Docker image"))
    found.append(Candidate("/var/lib/jellyfin/log", "packaged data folder"))
    xdg_data = environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    found.append(Candidate(os.path.join(xdg_data, "jellyfin", "log"), "Linux tarball and macOS"))
    found.append(Candidate("/mnt/user/appdata/jellyfin/log", "Unraid Docker appdata"))
    if environ.get("ProgramData"):
        found.append(Candidate(os.path.join(environ["ProgramData"], "Jellyfin", "Server", "log"), "Windows installer"))
    if environ.get("LOCALAPPDATA"):
        found.append(Candidate(os.path.join(environ["LOCALAPPDATA"], "jellyfin", "log"), "Windows portable"))
    return _dedupe(found)


def find_log_dir(
    jellyfin_dir: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Candidate:
    """Return the first candidate folder holding transcode logs, or raise LogDirNotFound."""
    tried = []
    for candidate in log_dir_candidates(jellyfin_dir, environ):
        path = Path(candidate.path)
        if not path.is_dir():
            tried.append((candidate, "not found"))
        elif not os.access(path, os.R_OK | os.X_OK):
            tried.append((candidate, "permission denied; try as Jellyfin's user or with sudo"))
        elif next(path.glob(TRANSCODE_LOG_GLOB), None) is None:
            tried.append((candidate, f"no {TRANSCODE_LOG_GLOB} files"))
        else:
            return candidate
    lines = "\n".join(f"  {c.path} ({c.source}): {why}" for c, why in tried)
    raise LogDirNotFound(
        "could not find Jellyfin's transcode logs. Looked in:\n"
        f"{lines}\n"
        "Give the log folder as PATH, point --jellyfin-dir at Jellyfin's data folder, "
        "or set JELLYFIN_LOG_DIR."
    )


def _ffprobe_beside(ffmpeg: str) -> str:
    name = "ffprobe.exe" if ffmpeg.lower().endswith(".exe") else "ffprobe"
    return os.path.join(os.path.dirname(ffmpeg), name)


def _encoder_paths(config_dir: str) -> Iterator[str]:
    """FFmpeg paths Jellyfin saved in its encoding.xml, if the file is there."""
    try:
        root = ElementTree.parse(os.path.join(config_dir, "encoding.xml")).getroot()
    except (OSError, ElementTree.ParseError):
        return
    for tag in ("EncoderAppPath", "EncoderAppPathDisplay"):
        value = (root.findtext(tag) or "").strip()
        if value:
            yield value


def ffprobe_candidates(
    jellyfin_dir: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    settings: Optional[List[Dict[str, Setting]]] = None,
) -> List[Candidate]:
    """ffprobe binaries Jellyfin may be using, most likely first (not counting PATH)."""
    environ = os.environ if environ is None else environ
    settings = jellyfin_settings(environ) if settings is None else settings
    found: List[Candidate] = []
    for setting in settings:
        if "ffmpeg" in setting:
            value, source = setting["ffmpeg"]
            found.append(Candidate(_ffprobe_beside(value), f"next to FFmpeg set by {source}"))

    config_dirs: List[str] = []
    if jellyfin_dir:
        config_dirs += [os.path.join(jellyfin_dir, "config"), jellyfin_dir]
    for setting in settings:
        if "config" in setting:
            config_dirs.append(setting["config"][0])
        if "data" in setting:
            config_dirs.append(os.path.join(setting["data"][0], "config"))
    config_dirs += ["/etc/jellyfin", "/config/config", "/config"]
    config_dirs.append(os.path.join(os.path.expanduser("~"), ".config", "jellyfin"))
    if environ.get("ProgramData"):
        config_dirs.append(os.path.join(environ["ProgramData"], "Jellyfin", "Server", "config"))
    for config_dir in config_dirs:
        for ffmpeg in _encoder_paths(config_dir):
            source = f"next to FFmpeg in {os.path.join(config_dir, 'encoding.xml')}"
            found.append(Candidate(_ffprobe_beside(ffmpeg), source))

    if jellyfin_dir:
        for name in ("ffprobe", "ffprobe.exe"):
            found.append(Candidate(os.path.join(jellyfin_dir, name), "--jellyfin-dir"))
            found.append(Candidate(os.path.join(jellyfin_dir, "jellyfin-ffmpeg", name), "--jellyfin-dir"))
    for path in JELLYFIN_FFPROBE_PATHS:
        found.append(Candidate(path, "jellyfin-ffmpeg install"))
    if environ.get("ProgramFiles"):
        found.append(Candidate(
            os.path.join(environ["ProgramFiles"], "Jellyfin", "Server", "ffprobe.exe"),
            "Windows installer",
        ))
    return _dedupe(found)


def _executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def find_ffprobe(
    explicit: Optional[str] = None,
    jellyfin_dir: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Candidate:
    """Return the ffprobe to run, preferring the one Jellyfin uses, or raise FfprobeNotFound.

    ``explicit`` may name the program, a path to it, or the folder holding it.
    """
    if explicit:
        if os.path.isdir(explicit):
            for name in ("ffprobe", "ffprobe.exe"):
                if _executable(os.path.join(explicit, name)):
                    return Candidate(os.path.join(explicit, name), "--ffprobe")
        found = shutil.which(explicit)
        if not found:
            raise FfprobeNotFound(f"ffprobe not found at {explicit}")
        return Candidate(found, "--ffprobe")

    candidates = ffprobe_candidates(jellyfin_dir, environ)
    for candidate in candidates:
        if _executable(candidate.path):
            return candidate
    found = shutil.which("ffprobe")
    if found:
        return Candidate(found, "PATH")
    tried = "".join(f"\n  {c.path} ({c.source})" for c in candidates)
    raise FfprobeNotFound(
        f"ffprobe not found on PATH or in any of:{tried}\n"
        "Install FFmpeg (or jellyfin-ffmpeg), pass --ffprobe PATH, "
        "or point --jellyfin-dir at Jellyfin's install or data folder."
    )
