import glob
import os

from jftd.runner import Result

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(*parts: str) -> str:
    return os.path.join(FIXTURES, *parts)


def read(*parts: str) -> str:
    with open(fixture(*parts), encoding="utf-8") as f:
        return f.read()


def log_fixture(label: str) -> str:
    """Path of the FFmpeg log fixture whose name ends in _<label>.log."""
    (path,) = glob.glob(fixture("logs", f"FFmpeg.*_{label}.log"))
    return path


class FakeRunner:
    """Runner stand-in: returns canned results keyed on argv[0]."""

    def __init__(self, responses=None, files=None):
        self.responses = responses or {}
        self.files = files or set()
        self.calls = []
        self.container = None
        self.where = "this host"

    def run(self, argv, timeout=None):
        self.calls.append(list(argv))
        r = self.responses.get(argv[0])
        if callable(r):
            r = r(argv)
        if r is None:
            return Result(list(argv), 127, "", f"{argv[0]}: not found")
        rc, out, err = r
        return Result(list(argv), rc, out, err)

    def exists(self, path):
        return path in self.files
