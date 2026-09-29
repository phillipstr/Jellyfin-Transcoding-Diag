"""Minimal Jellyfin API client for /System/Configuration/encoding."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from . import __version__

ENCODING_PATH = "/System/Configuration/encoding"


class JellyfinError(Exception):
    pass


class Client:
    def __init__(self, url: str, api_key: str | None, timeout: float = 15):
        self.url = url.rstrip("/")
        self._key = api_key
        self.timeout = timeout

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self._key:
            h["Authorization"] = (
                f'MediaBrowser Client="jftd", Device="jftd", DeviceId="jftd", '
                f'Version="{__version__}", Token="{self._key}"'
            )
        return h

    def _request(self, method: str, path: str, body: dict | None = None):
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                msg = "API key missing or rejected (401). Create one in Dashboard > API Keys."
                if not self._key:
                    msg = "no API key given. Set JELLYFIN_API_KEY or pass --api-key-file."
                raise JellyfinError(msg) from None
            if e.code == 403:
                raise JellyfinError("forbidden (403): the API key needs administrator rights.") from None
            raise JellyfinError(f"{method} {path} failed: HTTP {e.code} {e.reason}") from None
        except urllib.error.URLError as e:
            raise JellyfinError(f"cannot reach Jellyfin at {self.url}: {e.reason}") from None
        except TimeoutError:
            raise JellyfinError(f"timed out talking to Jellyfin at {self.url}") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise JellyfinError(f"{method} {path}: response was not JSON (is {self.url} a Jellyfin server?)") from None

    def public_info(self) -> dict:
        return self._request("GET", "/System/Info/Public") or {}

    def get_encoding(self) -> dict:
        cfg = self._request("GET", ENCODING_PATH)
        if not isinstance(cfg, dict):
            raise JellyfinError("unexpected response for encoding configuration")
        return cfg

    def post_encoding(self, cfg: dict) -> None:
        self._request("POST", ENCODING_PATH, cfg)


def check_types(current: dict, changes: dict) -> None:
    """Refuse changes whose type differs from what Jellyfin already stores."""
    for key, new in changes.items():
        if key not in current:
            raise JellyfinError(f"unknown setting '{key}' (not in this server's encoding config)")
        old = current[key]
        if isinstance(old, bool) or isinstance(new, bool):
            if not (isinstance(old, bool) and isinstance(new, bool)):
                raise JellyfinError(f"{key}: expected {type(old).__name__}, got {type(new).__name__}")
        elif isinstance(old, list):
            if not isinstance(new, list) or not all(isinstance(x, str) for x in new):
                raise JellyfinError(f"{key}: expected a list of strings")
        elif old is not None and type(old) is not type(new):
            raise JellyfinError(f"{key}: expected {type(old).__name__}, got {type(new).__name__}")


def default_backup_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "jftd")


def write_backup(cfg: dict, backup_dir: str) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("encoding-%Y%m%d-%H%M%S")
    for n in range(1000):
        path = os.path.join(backup_dir, f"{stamp}{'-' + str(n) if n else ''}.json")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            continue
    else:
        raise JellyfinError(f"cannot create a backup file in {backup_dir}")
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
        f.write("\n")
    return path


def apply_changes(client: Client, changes: dict, backup_dir: str) -> tuple[dict, dict, str]:
    """Write `changes` into the encoding config, verify by read-back.

    Returns (before, after, backup_path). Raises JellyfinError if the server
    did not store what was sent.
    """
    before = client.get_encoding()
    check_types(before, changes)
    backup = write_backup(before, backup_dir)
    client.post_encoding({**before, **changes})
    after = client.get_encoding()
    wrong = [k for k, v in changes.items() if after.get(k) != v]
    if wrong:
        raise JellyfinError(
            f"read-back mismatch for {', '.join(wrong)}; the server did not keep the change. "
            f"Previous config saved at {backup}."
        )
    return before, after, backup
