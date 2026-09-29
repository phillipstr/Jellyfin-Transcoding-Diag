"""Jellyfin's hardware-decoding settings, and how they compare to a GPU.

Jellyfin keeps hardware decoding in /System/Configuration/encoding:

  HardwareDecodingCodecs               list of codec names, e.g. ["h264", "hevc"]
  EnableDecodingColorDepth10Hevc       "HEVC 10bit" checkbox
  EnableDecodingColorDepth10Vp9        "VP9 10bit"
  EnableDecodingColorDepth10HevcRext   "HEVC RExt 8/10bit" (4:2:2 / 4:4:4)
  EnableDecodingColorDepth12HevcRext   "HEVC RExt 12bit"

A "feature" below is either a codec name or one of those flag names. The
capability backends (caps.py) say which features the GPU can decode; this
module compares that with the settings and works out the fix.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CODECS = ["h264", "hevc", "mpeg2video", "mpeg4", "vc1", "vp8", "vp9", "av1"]

CODEC_LABELS = {
    "h264": "H264",
    "hevc": "HEVC",
    "mpeg2video": "MPEG2",
    "mpeg4": "MPEG4",
    "vc1": "VC1",
    "vp8": "VP8",
    "vp9": "VP9",
    "av1": "AV1",
}

# flag -> (codec it depends on, dashboard label)
FLAGS = {
    "EnableDecodingColorDepth10Hevc": ("hevc", "HEVC 10bit"),
    "EnableDecodingColorDepth10Vp9": ("vp9", "VP9 10bit"),
    "EnableDecodingColorDepth10HevcRext": ("hevc", "HEVC RExt 8/10bit"),
    "EnableDecodingColorDepth12HevcRext": ("hevc", "HEVC RExt 12bit"),
}

FEATURES = CODECS + list(FLAGS)

# Jellyfin HardwareAccelerationType -> capability backend name.
# QSV on Linux decodes through VA-API, so the same vainfo profiles apply.
ACCEL_BACKENDS = {
    "vaapi": "vaapi",
    "qsv": "vaapi",
    "amf": "vaapi",
    "nvenc": "nvdec",
}


def label(feature: str) -> str:
    if feature in FLAGS:
        return FLAGS[feature][1]
    return CODEC_LABELS.get(feature, feature)


def parent(feature: str) -> str | None:
    return FLAGS[feature][0] if feature in FLAGS else None


def enabled_features(encoding: dict) -> dict[str, bool]:
    """Which features the Jellyfin encoding config turns on."""
    codecs = {str(c).lower() for c in encoding.get("HardwareDecodingCodecs") or []}
    out = {c: c in codecs for c in CODECS}
    for flag in FLAGS:
        out[flag] = bool(encoding.get(flag, False))
    return out


# Status of one feature after comparing settings with the GPU.
BROKEN = "broken"      # enabled, GPU can't do it, and it is live: transcodes will fail
LATENT = "latent"      # enabled flag the GPU can't do, but its codec is off: fails if the codec is turned on
UNUSED = "unused"      # GPU can do it but it is off: CPU decodes instead (works, slower)
OK = "ok"


@dataclass
class DriftItem:
    feature: str
    enabled: bool
    supported: bool
    requires: list[str]
    status: str

    @property
    def label(self) -> str:
        return label(self.feature)

    def to_dict(self) -> dict:
        return {
            "feature": self.feature,
            "label": self.label,
            "enabled": self.enabled,
            "supported": self.supported,
            "requires": self.requires,
            "status": self.status,
        }


@dataclass
class DriftReport:
    accel: str
    hw_decode_active: bool
    items: list[DriftItem]
    notes: list[str] = field(default_factory=list)

    def by_status(self, status: str) -> list[DriftItem]:
        return [i for i in self.items if i.status == status]

    @property
    def has_problems(self) -> bool:
        return bool(self.by_status(BROKEN) or self.by_status(LATENT))

    def to_dict(self) -> dict:
        return {
            "accel": self.accel,
            "hw_decode_active": self.hw_decode_active,
            "items": [i.to_dict() for i in self.items],
            "notes": self.notes,
        }


def config_drift(encoding: dict, caps) -> DriftReport:
    """Compare Jellyfin's decode settings with what `caps` says the GPU can do."""
    accel = str(encoding.get("HardwareAccelerationType") or "none").lower()
    active = accel not in ("", "none")
    enabled = enabled_features(encoding)
    items = []
    for feat in FEATURES:
        on = enabled[feat]
        can = caps.supports(feat)
        par = parent(feat)
        if on and not can:
            live = active and (par is None or enabled[par])
            status = BROKEN if live else LATENT
        elif can and not on:
            status = UNUSED
        else:
            status = OK
        items.append(DriftItem(feat, on, can, caps.requires(feat), status))

    notes = []
    if not active:
        notes.append(
            "Hardware acceleration is off in Jellyfin (HardwareAccelerationType=none), "
            "so these settings have no effect right now."
        )
    elif ACCEL_BACKENDS.get(accel) != caps.backend:
        notes.append(
            f"Jellyfin uses '{accel}' but capabilities came from the '{caps.backend}' "
            "backend; the comparison may not apply."
        )
    if accel == "qsv" and caps.driver and "i965" in caps.driver.lower():
        notes.append(
            "QSV needs Intel's iHD (intel-media-driver) VA-API driver, but vainfo "
            "loaded i965. Set LIBVA_DRIVER_NAME=iHD or install intel-media-driver."
        )
    dev_key = {"vaapi": "VaapiDevice", "qsv": "QsvDevice"}.get(accel)
    jf_dev = encoding.get(dev_key) if dev_key else None
    if accel == "qsv" and not jf_dev:
        jf_dev = encoding.get("VaapiDevice")
    if jf_dev and caps.device and jf_dev != caps.device:
        notes.append(
            f"Jellyfin is set to use {jf_dev} but capabilities were read from "
            f"{caps.device}; pass --device {jf_dev} to compare the right GPU."
        )
    return DriftReport(accel, active, items, notes)


def recommended_settings(encoding: dict, caps, enable_supported: bool = False) -> dict:
    """The encoding-config fields to change so Jellyfin only asks the GPU for what it can do.

    Returns only fields whose value changes. By default this only turns things
    off; with enable_supported=True it also turns on everything the GPU supports.
    """
    enabled = enabled_features(encoding)
    current = [str(c) for c in encoding.get("HardwareDecodingCodecs") or []]
    keep = []
    for c in current:
        lc = c.lower()
        if lc in CODECS and not caps.supports(lc):
            continue
        keep.append(c)
    if enable_supported:
        have = {c.lower() for c in keep}
        keep += [c for c in CODECS if caps.supports(c) and c not in have]

    changes: dict = {}
    if keep != current:
        changes["HardwareDecodingCodecs"] = keep
    for flag in FLAGS:
        want = caps.supports(flag) and (enabled[flag] or enable_supported)
        if want != enabled[flag]:
            changes[flag] = want
    return changes


def required_features(codec: str | None, pix_fmt: str | None = None, profile: str | None = None) -> list[str]:
    """Features Jellyfin must have enabled to hardware-decode a stream."""
    if not codec:
        return []
    codec = codec.lower()
    aliases = {"mpeg2": "mpeg2video", "wmv3": "vc1", "avc": "h264", "h265": "hevc"}
    codec = aliases.get(codec, codec)
    if codec not in CODECS:
        return []
    need = [codec]
    depth = bit_depth(pix_fmt, profile)
    chroma = chroma_of(pix_fmt, profile)
    if codec == "hevc":
        if chroma in ("422", "444"):
            need.append("EnableDecodingColorDepth12HevcRext" if depth >= 12 else "EnableDecodingColorDepth10HevcRext")
        elif depth >= 10:
            need.append("EnableDecodingColorDepth10Hevc")
    elif codec == "vp9" and depth >= 10:
        need.append("EnableDecodingColorDepth10Vp9")
    return need


def bit_depth(pix_fmt: str | None, profile: str | None = None) -> int:
    pf = (pix_fmt or "").lower()
    for d in (16, 14, 12, 10):
        if f"p{d}" in pf or f"{d}le" in pf or f"{d}be" in pf:
            return d
    prof = (profile or "").lower()
    if "12" in prof:
        return 12
    if "10" in prof:
        return 10
    return 8


def chroma_of(pix_fmt: str | None, profile: str | None = None) -> str:
    pf = (pix_fmt or "").lower()
    for c in ("444", "422", "420"):
        if c in pf:
            return c
    prof = (profile or "").lower()
    if "4:4:4" in prof or "444" in prof:
        return "444"
    if "4:2:2" in prof or "422" in prof:
        return "422"
    return "420"
