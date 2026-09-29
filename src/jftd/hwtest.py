"""Encode a few seconds of a file two ways and compare.

  hw: GPU decode + GPU encode   (what Jellyfin does with hardware decoding on)
  sw: CPU decode + GPU encode   (what Jellyfin does with hardware decoding off)

If hw fails and sw works, the GPU can't decode this file and the fix is to
turn off hardware decoding for its codec/profile. If both fail, the problem is
the encoder, the device, or the file itself.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass

from . import logs, profiles
from .runner import Runner

MODES = ("qsv", "vaapi")

HW_OK = "HW_OK"
HW_DECODE_FAILS = "HW_DECODE_FAILS"
BOTH_FAIL = "BOTH_FAIL"
NO_DEVICE = "NO_DEVICE"
SW_FAILS = "SW_FAILS"

VERDICT_TEXT = {
    HW_OK: "GPU decode and encode both work for this file. If Jellyfin still fails on it, "
    "run `jftd scan` and look at the class of the failing runs.",
    HW_DECODE_FAILS: "The GPU cannot decode this file, but CPU decode + GPU encode works. "
    "Turn off hardware decoding for this codec/profile: run `jftd drift` for the exact settings.",
    BOTH_FAIL: "Both paths fail, so decode is not the (only) problem: suspect the GPU encoder, "
    "the driver, or the file. Check the sw error above and run `jftd caps`.",
    NO_DEVICE: "The GPU could not be opened at all, so nothing was tested. Check the --device path, "
    "the 'render' group (or --device /dev/dri for Docker), and run `jftd caps`.",
    SW_FAILS: "GPU decode works but CPU decode + upload fails. Unusual; please open an issue "
    "with the output of `jftd test --json`.",
}

_SPEED = re.compile(r"speed=\s*([\d.]+)x")
_TIME = re.compile(r"time=\s*(\d+):(\d+):([\d.]+)")


@dataclass
class Probe:
    codec: str | None = None
    profile: str | None = None
    pix_fmt: str | None = None
    width: int | None = None
    height: int | None = None

    def describe(self) -> str:
        if not self.codec:
            return "unknown (ffprobe failed)"
        s = self.codec
        if self.profile:
            s += f" {self.profile}"
        if self.pix_fmt:
            s += f", {self.pix_fmt} ({profiles.bit_depth(self.pix_fmt, self.profile)}-bit)"
        if self.width and self.height:
            s += f", {self.width}x{self.height}"
        return s


@dataclass
class Run:
    name: str
    description: str
    ok: bool
    returncode: int
    wall: float
    media_seconds: float | None
    speed: float | None
    cls: str | None
    evidence: str | None
    argv: list


def probe(runner: Runner, ffprobe: str, path: str) -> Probe:
    r = runner.run(
        [
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,profile,pix_fmt,width,height",
            "-of", "json", path,
        ],
        timeout=60,
    )
    if not r.ok:
        return Probe()
    try:
        streams = json.loads(r.stdout).get("streams") or []
    except ValueError:
        return Probe()
    if not streams:
        return Probe()
    s = streams[0]
    return Probe(s.get("codec_name"), s.get("profile"), s.get("pix_fmt"), s.get("width"), s.get("height"))


def build_commands(ffmpeg: str, path: str, seconds: int, device: str, mode: str) -> dict[str, list[str]]:
    base = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-stats"]
    if mode == "qsv":
        dev = ["-init_hw_device", f"vaapi=va:{device}", "-init_hw_device", "qsv=qs@va", "-filter_hw_device", "qs"]
        hw_vf = "scale_vaapi=format=nv12,hwmap=derive_device=qsv,format=qsv"
        sw_vf = "format=nv12,hwupload=extra_hw_frames=64,format=qsv"
        enc = "h264_qsv"
    elif mode == "vaapi":
        dev = ["-init_hw_device", f"vaapi=va:{device}", "-filter_hw_device", "va"]
        hw_vf = "scale_vaapi=format=nv12"
        sw_vf = "format=nv12,hwupload"
        enc = "h264_vaapi"
    else:
        raise ValueError(f"unknown mode {mode}")
    hwdec = ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi", "-hwaccel_device", "va"]
    tail = ["-map", "0:v:0", "-an", "-sn", "-dn", "-t", str(seconds)]
    out = ["-c:v", enc, "-f", "null", "-"]
    return {
        "hw": base + dev + hwdec + ["-i", path] + tail + ["-vf", hw_vf] + out,
        "sw": base + dev + ["-i", path] + tail + ["-vf", sw_vf] + out,
    }


def run_one(runner: Runner, name: str, description: str, argv: list[str], timeout: float) -> Run:
    t0 = time.monotonic()
    r = runner.run(argv, timeout=timeout)
    wall = time.monotonic() - t0
    lines = r.stderr.splitlines()
    c = logs.classify_output(lines, " ".join(argv))
    speeds = _SPEED.findall(r.stderr)
    times = _TIME.findall(r.stderr)
    media = None
    if times:
        h, m, s = times[-1]
        media = int(h) * 3600 + int(m) * 60 + float(s)
    ok = r.ok and c.status != logs.FAILED
    evidence = c.evidence
    if not ok and not evidence:
        tail = [ln.strip() for ln in lines if ln.strip()]
        evidence = tail[-1] if tail else f"exit {r.returncode}"
    return Run(
        name=name,
        description=description,
        ok=ok,
        returncode=r.returncode,
        wall=round(wall, 2),
        media_seconds=media,
        speed=float(speeds[-1]) if speeds else None,
        cls=(c.cls or "other") if not ok else None,
        evidence=evidence if not ok else None,
        argv=r.argv,
    )


def verdict(hw: Run, sw: Run) -> str:
    if hw.ok and sw.ok:
        return HW_OK
    if not hw.ok and sw.ok:
        return HW_DECODE_FAILS
    if not hw.ok and not sw.ok:
        return NO_DEVICE if hw.cls == sw.cls == "hw_device" else BOTH_FAIL
    return SW_FAILS


def run_test(runner: Runner, ffmpeg: str, ffprobe: str, path: str, seconds: int, device: str, mode: str) -> dict:
    info = probe(runner, ffprobe, path)
    cmds = build_commands(ffmpeg, path, seconds, device, mode)
    enc = "h264_qsv" if mode == "qsv" else "h264_vaapi"
    timeout = max(120, seconds * 20)
    hw = run_one(runner, "hw", f"GPU decode + {enc}", cmds["hw"], timeout)
    sw = run_one(runner, "sw", f"CPU decode + {enc}", cmds["sw"], timeout)
    v = verdict(hw, sw)
    return {
        "file": path,
        "video": asdict(info),
        "video_description": info.describe(),
        "needs": profiles.required_features(info.codec, info.pix_fmt, info.profile),
        "mode": mode,
        "seconds": seconds,
        "runs": [asdict(hw), asdict(sw)],
        "verdict": v,
        "explanation": VERDICT_TEXT[v],
    }


def format_result(res: dict, caps=None) -> str:
    out = [f"File:    {res['file']}", f"Video:   {res['video_description']}"]
    needs = res["needs"]
    if needs:
        line = "Needs:   " + " + ".join(profiles.label(f) for f in needs) + " hardware decode"
        if caps is not None:
            missing = [f for f in needs if not caps.supports(f)]
            if missing:
                reqs = sorted({p for f in missing for p in caps.requires(f)})
                line += f"  -> GPU: NO (missing {' or '.join(reqs)})"
            else:
                line += "  -> GPU: yes"
        out.append(line)
    out.append("")
    for r in res["runs"]:
        if r["ok"]:
            rate = ""
            if r["speed"]:
                rate = f"  ({r['speed']:.1f}x realtime)"
            elif r["media_seconds"] and r["wall"]:
                rate = f"  ({r['media_seconds'] / r['wall']:.1f}x realtime)"
            done = f"{r['media_seconds']:.1f}s" if r["media_seconds"] is not None else "?"
            detail = f"OK    {done} in {r['wall']:.1f}s{rate}"
        else:
            detail = f"FAIL  {r['cls']}: {r['evidence']}"
        out.append(f"{r['name']}  ({r['description']})  {detail}")
    out += ["", f"Verdict: {res['verdict']}", f"  {res['explanation']}"]
    return "\n".join(out)
