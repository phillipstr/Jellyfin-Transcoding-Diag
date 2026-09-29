"""GPU decode capabilities, mapped to Jellyfin's codec names and flags.

Each backend knows how to probe one kind of hardware and which of its native
profiles back each Jellyfin feature (see profiles.FEATURES). Only VA-API is
implemented (Intel iHD/i965 and AMD radeonsi all report through vainfo).
NVDEC has a placeholder so the CLI can say so clearly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import profiles
from .runner import Runner, RunnerError


class CapsError(Exception):
    pass


@dataclass
class Capabilities:
    backend: str
    device: str | None
    driver: str | None
    decode: set[str]
    encode: set[str]
    features: dict[str, bool]
    requirements: dict[str, list[str]]
    raw: str = field(default="", repr=False)

    def supports(self, feature: str) -> bool:
        return self.features.get(feature, False)

    def requires(self, feature: str) -> list[str]:
        return self.requirements.get(feature, [])

    @property
    def vendor(self) -> str:
        d = (self.driver or "").lower()
        if "intel" in d or "ihd" in d or "i965" in d:
            return "intel"
        if "radeon" in d or "amd" in d or "mesa" in d:
            return "amd"
        return "unknown"

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "device": self.device,
            "driver": self.driver,
            "vendor": self.vendor,
            "features": self.features,
            "requirements": self.requirements,
            "decode_profiles": sorted(self.decode),
            "encode_profiles": sorted(self.encode),
        }


class VaapiBackend:
    name = "vaapi"

    # Jellyfin codec -> VA profiles; any one of them is enough.
    CODEC_PROFILES = {
        "h264": ["VAProfileH264Main", "VAProfileH264High", "VAProfileH264ConstrainedBaseline"],
        "hevc": ["VAProfileHEVCMain"],
        "mpeg2video": ["VAProfileMPEG2Main", "VAProfileMPEG2Simple"],
        "mpeg4": ["VAProfileMPEG4Simple", "VAProfileMPEG4AdvancedSimple", "VAProfileMPEG4Main"],
        "vc1": ["VAProfileVC1Advanced", "VAProfileVC1Main", "VAProfileVC1Simple"],
        "vp8": ["VAProfileVP8Version0_3"],
        "vp9": ["VAProfileVP9Profile0"],
        "av1": ["VAProfileAV1Profile0"],
    }

    # Jellyfin flag -> VA profiles; any one of them is enough.
    FLAG_PROFILES = {
        "EnableDecodingColorDepth10Hevc": ["VAProfileHEVCMain10"],
        "EnableDecodingColorDepth10Vp9": ["VAProfileVP9Profile2"],
        "EnableDecodingColorDepth10HevcRext": [
            "VAProfileHEVCMain422_10",
            "VAProfileHEVCMain444",
            "VAProfileHEVCMain444_10",
        ],
        "EnableDecodingColorDepth12HevcRext": ["VAProfileHEVCMain422_12", "VAProfileHEVCMain444_12"],
    }

    DECODE_ENTRYPOINTS = {"VAEntrypointVLD"}
    ENCODE_ENTRYPOINTS = {"VAEntrypointEncSlice", "VAEntrypointEncSliceLP", "VAEntrypointEncPicture"}

    _line = re.compile(r"^\s*(VAProfile\w+)\s*:\s*(VAEntrypoint\w+)")
    _driver = re.compile(r"Driver version:\s*(.+)$", re.M)

    def parse(self, text: str, device: str | None = None) -> Capabilities:
        decode, encode = set(), set()
        for line in text.splitlines():
            m = self._line.match(line)
            if not m:
                continue
            prof, entry = m.groups()
            if entry in self.DECODE_ENTRYPOINTS:
                decode.add(prof)
            elif entry in self.ENCODE_ENTRYPOINTS:
                encode.add(prof)
        if not decode and not encode:
            raise CapsError("no VA-API profiles found in vainfo output")
        dm = self._driver.search(text)
        requirements = {**self.CODEC_PROFILES, **self.FLAG_PROFILES}
        features = {}
        for feat in profiles.FEATURES:
            ok = any(p in decode for p in requirements[feat])
            par = profiles.parent(feat)
            if par is not None:
                ok = ok and any(p in decode for p in requirements[par])
            features[feat] = ok
        return Capabilities(
            backend=self.name,
            device=device,
            driver=re.sub(r"\s*\(\)$", "", dm.group(1).strip()) if dm else None,
            decode=decode,
            encode=encode,
            features=features,
            requirements={k: list(v) for k, v in requirements.items()},
            raw=text,
        )

    def probe(self, runner: Runner, vainfo: str, device: str) -> Capabilities:
        try:
            r = runner.run([vainfo, "--display", "drm", "--device", device], timeout=30)
        except RunnerError as e:
            raise CapsError(f"{e}. Install vainfo (or jellyfin-ffmpeg, which ships one) or pass --vainfo PATH.") from None
        text = r.stdout + "\n" + r.stderr
        if not r.ok or "VAProfile" not in text:
            hint = ""
            low = text.lower()
            if "permission denied" in low or "failed to open" in low or "cannot open" in low:
                hint = (
                    f" Can't open {device}: add this user to the 'render' group (and 'video' on "
                    "some distros), or in Docker pass the device with --device /dev/dri."
                )
            elif "no such file" in low:
                hint = f" {device} does not exist; list /dev/dri to find your render node."
            tail = "\n".join(text.strip().splitlines()[-5:])
            raise CapsError(f"vainfo failed on {runner.where} (exit {r.returncode}).{hint}\n{tail}")
        return self.parse(text, device)


class NvdecBackend:
    name = "nvdec"

    def probe(self, runner: Runner, vainfo: str, device: str) -> Capabilities:
        raise CapsError(
            "NVIDIA NVDEC capability detection is not implemented yet. "
            "`jftd scan` still works. Contributions welcome."
        )


BACKENDS = {b.name: b for b in (VaapiBackend(), NvdecBackend())}


def get_backend(name: str):
    try:
        return BACKENDS[name]
    except KeyError:
        raise CapsError(f"unknown capability backend '{name}' (have: {', '.join(BACKENDS)})") from None


def backend_for_accel(accel: str | None) -> str:
    return profiles.ACCEL_BACKENDS.get((accel or "").lower(), "vaapi")
