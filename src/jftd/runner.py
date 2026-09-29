"""Run commands and read files on this host or inside a Jellyfin container.

Everything that touches the system goes through a Runner, so the same code
works for a native install and for `docker exec` into a container.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass


class RunnerError(Exception):
    """A command or file could not be reached at all."""


@dataclass
class Result:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class FileEntry:
    name: str
    path: str
    mtime: float
    size: int


class Runner:
    def __init__(self, container: str | None = None, docker_bin: str = "docker"):
        self.container = container
        self.docker_bin = docker_bin

    @property
    def where(self) -> str:
        return f"container {self.container}" if self.container else "this host"

    def wrap(self, argv: list[str]) -> list[str]:
        if self.container:
            return [self.docker_bin, "exec", self.container, *argv]
        return list(argv)

    def run(self, argv: list[str], timeout: float | None = None) -> Result:
        full = self.wrap(argv)
        try:
            p = subprocess.run(
                full,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise RunnerError(f"command not found: {full[0]}") from None
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            return Result(full, 124, out, err + f"\n[jftd] timed out after {timeout}s\n")
        return Result(full, p.returncode, p.stdout, p.stderr)

    def exists(self, path: str) -> bool:
        if not self.container:
            return os.path.exists(path)
        return self.run(["test", "-e", path]).ok

    def listdir(self, path: str) -> list[FileEntry]:
        if not self.container:
            try:
                names = os.listdir(path)
            except PermissionError:
                raise RunnerError(
                    f"permission denied reading {path}. Jellyfin's log directory is "
                    "usually readable by the 'adm' or 'jellyfin' group; add yourself "
                    "to it or run with sudo."
                ) from None
            except FileNotFoundError:
                raise RunnerError(f"no such directory: {path}") from None
            out = []
            for name in names:
                full = os.path.join(path, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if os.path.isfile(full):
                    out.append(FileEntry(name, full, st.st_mtime, st.st_size))
            return out
        r = self.run(["find", path, "-maxdepth", "1", "-type", "f", "-printf", r"%T@ %s %f\n"])
        if not r.ok:
            raise RunnerError(f"cannot list {path} in {self.where}: {r.stderr.strip()}")
        out = []
        for line in r.stdout.splitlines():
            parts = line.split(" ", 2)
            if len(parts) == 3:
                out.append(FileEntry(parts[2], f"{path.rstrip('/')}/{parts[2]}", float(parts[0]), int(parts[1])))
        return out

    def read_text(self, path: str) -> str:
        if not self.container:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    return f.read()
            except PermissionError:
                raise RunnerError(f"permission denied reading {path}") from None
        r = self.run(["cat", path])
        if not r.ok:
            raise RunnerError(f"cannot read {path} in {self.where}: {r.stderr.strip()}")
        return r.stdout
