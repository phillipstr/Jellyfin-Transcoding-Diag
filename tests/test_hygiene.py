"""Keep host-specific details out of the public repo."""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "__pycache__", "build", "dist", ".venv", "venv", ".eggs"}

FORBIDDEN = {
    "private IPv4 address": re.compile(r"\b(?:192\.168|10\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\b"),
    "email address": re.compile(r"\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b", re.I),
    "API token": re.compile(r"(?:api_key=|Token=\"|X-Emby-Token:\s*)[0-9a-f]{32}", re.I),
    "home directory path": re.compile(r"/home/\w+/"),
}


def repo_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for name in filenames:
            yield os.path.join(dirpath, name)


class Hygiene(unittest.TestCase):
    def test_no_host_specific_details(self):
        problems = []
        for path in repo_files():
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            for what, pat in FORBIDDEN.items():
                for m in pat.finditer(text):
                    problems.append(f"{os.path.relpath(path, ROOT)}: {what}: {m.group(0)}")
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
