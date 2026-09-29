# jftd: Jellyfin transcoding diagnostics

`jftd` is a small command-line tool that works out why Jellyfin transcodes fail,
and tells you exactly what to change.

It is read-only unless you run `jftd fix`. It needs only Python 3.10+ and the
standard library, and it works with native installs and with Docker.

## The problem

A common and confusing failure: playback of some files stops or never starts.
Jellyfin's server log shows only `FFmpeg exited with code 218`, and the FFmpeg
log says something like:

```
[hevc @ 0x...] Failed setup for format vaapi: hwaccel initialisation returned error.
[vf#0:0 @ 0x...] Error reinitializing filters!
[vf#0:0 @ 0x...] Task finished with error code: -38 (Function not implemented)
Conversion failed!
```

Exit code 218 is -38 (ENOSYS, "Function not implemented"). It usually means
Jellyfin asked the GPU to decode a codec or profile the GPU can't handle.
Jellyfin's hardware-decoding checkboxes (VP9, AV1, HEVC 10bit, HEVC RExt, ...)
are not checked against what your GPU can actually do.

Take an Intel HD 530 (Skylake) with the iHD driver as an example. It decodes
H.264, 8-bit HEVC, MPEG-2, VC-1 and VP8, but not 10-bit HEVC, VP9 or AV1. With
those boxes ticked in Jellyfin, every 10-bit HEVC transcode fails. Unticking
them makes Jellyfin decode those files on the CPU and still encode on the GPU
(QSV), which on that machine ran at about 24x realtime.

`jftd` automates that diagnosis:

| Command          | What it does                                                                                  |
| ---------------- | --------------------------------------------------------------------------------------------- |
| `jftd caps`      | Reads the GPU's decode profiles (`vainfo`) and maps them to Jellyfin's settings.              |
| `jftd drift`     | Compares Jellyfin's settings (via the API) with `caps` and prints the exact fix.              |
| `jftd scan`      | Classifies Jellyfin's FFmpeg logs and summarises failures by class, source video and file.    |
| `jftd test FILE` | Encodes a few seconds with GPU decode and with CPU decode, and gives a verdict.               |
| `jftd fix`       | Applies the `drift` fix through the API, after you confirm. Backs up first; verifies after.   |
| `jftd exit-code N` | Explains an FFmpeg exit code such as 218.                                                   |

v0.1 supports VA-API (Intel iHD/i965, AMD radeonsi), which covers Jellyfin's
`qsv` and `vaapi` modes on Linux. NVIDIA NVDEC detection is not implemented
yet, but `scan` works with any setup.

## Install

```sh
pipx install git+https://github.com/phillipstr/Jellyfin-Transcoding-Diag
# or: python3 -m pip install --user git+https://github.com/phillipstr/Jellyfin-Transcoding-Diag
jftd --version
```

## Quick start: native install (Debian/Ubuntu package)

```sh
# An admin API key: Dashboard > API Keys > +
export JELLYFIN_API_KEY=...            # or: --api-key-file ~/.config/jftd/key
export JELLYFIN_URL=http://127.0.0.1:8096

jftd caps                              # what can the GPU decode?
jftd drift                             # are Jellyfin's settings asking for more?
jftd scan --since 7d                   # what has actually been failing?
jftd test "/path/to/a/failing/file.mkv"
```

Permissions:

* `/var/log/jellyfin` is usually readable only by the `adm` or `jellyfin`
  group.
* `/dev/dri/renderD128` needs the `render` group (plus `video` on some
  distros).

If you were just added to a group, log out and in again, or run the command
with `sudo -u jellyfin`.

`jftd` prefers the `ffmpeg`, `ffprobe` and `vainfo` from jellyfin-ffmpeg in
`/usr/lib/jellyfin-ffmpeg/` when they exist, and otherwise uses the ones on
`PATH`.

## Quick start: Docker (jellyfin/jellyfin or linuxserver/jellyfin)

Run `jftd` on the host and point it at the container. It runs `vainfo`,
`ffmpeg` and `ffprobe` with `docker exec`, and reads logs from `/config/log`
inside the container:

```sh
export JELLYFIN_API_KEY=...
export JELLYFIN_URL=http://127.0.0.1:8096   # the published port
export JFTD_DOCKER=jellyfin                 # container name (or pass --docker jellyfin)

jftd caps
jftd drift
jftd scan --since 7d
jftd test "/media/movies/Some Film/Some Film.mkv"   # the path as the container sees it
```

A few notes:

* If you mounted the log directory on the host, you can scan it there instead
  of through the container: `jftd scan --log-dir /srv/jellyfin/config/log`.
* The container needs the GPU passed in, for example `--device /dev/dri` and a
  `group_add` for the render group's GID. If it isn't, `caps` says so.
* For Podman, set `JFTD_DOCKER_BIN=podman`.
* If your image has no `vainfo`, run `jftd caps` on the host, or save vainfo's
  output somewhere and pass `--vainfo-output FILE`.

## Example output

### `jftd drift`

```
Jellyfin: hardware acceleration 'qsv'
GPU:      Intel iHD driver for Intel(R) Gen Graphics - 24.1.0 (vaapi, /dev/dri/renderD128)

  Setting              Jellyfin  GPU   Status
  H264                 on        yes   ok
  HEVC                 on        yes   ok
  MPEG2                on        yes   ok
  MPEG4                off       no    ok
  VC1                  on        yes   ok
  VP8                  on        yes   ok
  VP9                  on        no    BROKEN  transcodes of this format will fail
  AV1                  on        no    BROKEN  transcodes of this format will fail
  HEVC 10bit           on        no    BROKEN  transcodes of this format will fail
  VP9 10bit            on        no    BROKEN  transcodes of this format will fail
  HEVC RExt 8/10bit    on        no    BROKEN  transcodes of this format will fail
  HEVC RExt 12bit      on        no    BROKEN  transcodes of this format will fail

Problem: Jellyfin asks the GPU to decode 6 format(s) it can't (VP9, AV1, HEVC 10bit, ...).

Fix: Dashboard > Playback > Transcoding > 'Enable hardware decoding for':
  untick: VP9, AV1, HEVC 10bit, VP9 10bit, HEVC RExt 8/10bit, HEVC RExt 12bit
  then Save. Those formats are then decoded on the CPU and still encoded on the GPU.
Or run `jftd fix` (shows this change and asks before writing).

API change (POST /System/Configuration/encoding, merged into the current config):
  {"HardwareDecodingCodecs": ["h264", "hevc", "mpeg2video", "vc1", "vp8"], "EnableDecodingColorDepth10Hevc": false, ...}
```

### `jftd scan`

```
Scanned 212 FFmpeg logs in /var/log/jellyfin, last 7d

  ok          158
  aborted      30   stopped by Jellyfin (client left or seeked); not failures
  failed       24

Failures by class:
  hw_decode    24   GPU could not decode the source

Failures by source video:
  hevc Main 10 (10-bit), hw decode vaapi     24   hw_decode x24

Failures by file:
     6  Example Series - S01E02.mkv  [hw_decode]  last 2026-01-05 20:03
     ...

What to do:
  hw_decode: Usually a codec or profile the GPU can't decode (e.g. HEVC 10-bit on older Intel).
             Run `jftd drift` and turn off the unsupported hardware-decoding settings.

FFmpeg exit codes in the Jellyfin server log:
     24 x 218 = -38 = ENOSYS (Function not implemented): typically hardware decode of a
          codec/profile the GPU does not support
```

Use `--redact` to replace media paths with short hashes before you paste output
into an issue or forum post. Use `--json` for scripts, and `-v` to list every
failed run.

### `jftd test FILE`

```
File:    /media/tv/Example Series/Season 01/Example Series - S01E02.mkv
Video:   hevc Main 10, yuv420p10le (10-bit), 3840x2160
Needs:   HEVC + HEVC 10bit hardware decode  -> GPU: NO (missing VAProfileHEVCMain10)

hw  (GPU decode + h264_qsv)  FAIL  hw_decode: [hevc @ 0x...] Failed setup for format vaapi: hwaccel initialisation returned error.
sw  (CPU decode + h264_qsv)  OK    30.0s in 1.3s  (23.9x realtime)

Verdict: HW_DECODE_FAILS
  The GPU cannot decode this file, but CPU decode + GPU encode works. Turn off hardware
  decoding for this codec/profile: run `jftd drift` for the exact settings.
```

## Reading the verdicts

### `jftd test`

The test encodes N seconds (`--seconds`, default 30) twice, discarding the
output:

* **hw**: GPU decode + GPU encode. This is what Jellyfin does with hardware
  decoding on.
* **sw**: CPU decode + GPU encode. This is what Jellyfin does for a format whose
  hardware-decoding box is unticked.

Both runs use the same encoder, `h264_qsv` by default or `h264_vaapi` with
`--mode vaapi`. The only difference between them is who decodes.

| Verdict           | Meaning                                                                                              | Next step                                                         |
| ----------------- | ---------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| `HW_OK`           | Both work. GPU decode is fine for this file.                                                         | If Jellyfin still fails, `jftd scan -v` shows the class.          |
| `HW_DECODE_FAILS` | GPU decode fails and CPU decode works. The GPU can't decode this codec/profile.                      | `jftd drift`, then untick that format (or `jftd fix`).            |
| `BOTH_FAIL`       | Both fail, so the encoder, the driver or the file is the problem.                                    | Read the sw error; `jftd caps`; check the encoder settings.       |
| `NO_DEVICE`       | The GPU could not be opened, so nothing was tested.                                                  | Check `--device`, the `render` group and Docker `--device`.       |
| `SW_FAILS`        | GPU decode works but CPU decode + upload fails. This is unusual.                                     | Please open an issue with `jftd test --json` output.              |

### `jftd drift`

| Status   | Meaning                                                                                          |
| -------- | ------------------------------------------------------------------------------------------------ |
| `BROKEN` | Enabled in Jellyfin, the GPU can't do it, and it is in use. Transcodes of this format fail.       |
| `latent` | Enabled flag the GPU can't do, but its codec is off. Harmless now; it breaks if the codec is turned on. |
| `unused` | The GPU could decode this, but it is off, so the CPU does it. That works, just slower. `jftd fix --also-enable` turns these on. |
| `ok`     | Settings and GPU agree.                                                                          |

### `jftd scan` classes

`scan` classifies only FFmpeg's own output. It skips the JSON header and the
command line at the top of each log, because those contain words like
`subtitles=` and `hwaccel` even on runs that worked.

A run counts as failed only if FFmpeg printed a real failure marker:

* `Conversion failed!`
* `Task finished with error code`
* `Error reinitializing filters`
* `Terminating thread with return code -N`
* `Error while opening encoder`
* `Error opening input/output file`

The exceptions are `disk_full` and `hw_device`, whose messages are
unambiguous on their own. Runs that end with `Exiting normally, received
signal 15` were stopped by Jellyfin because the client left or seeked. Those
are counted as `aborted`, not as failures.

| Class       | Typical message                                                     |
| ----------- | ------------------------------------------------------------------- |
| `disk_full` | `No space left on device`                                           |
| `hw_device` | `No VA display found`, `Failed to initialise VAAPI connection`, `Device creation failed` |
| `hw_decode` | `Failed setup for format vaapi: hwaccel initialisation returned error`; or ENOSYS (-38) with `-hwaccel` in use |
| `hw_encode` | `[h264_qsv @ ...] Error initializing the encoder`, `Error while opening encoder` |
| `subtitle`  | `[Parsed_subtitles_0 @ ...] Unable to open ...`                     |
| `filter`    | `Impossible to convert between the formats supported by the filter` |
| `input`     | `moov atom not found`, `Invalid data found when processing input`   |
| `other`     | A failure marker with nothing more specific. Please report it (with `--redact`). |

`scan` also counts `FFmpeg exited with code N` in Jellyfin's server logs and
decodes the codes (`jftd exit-code 218`).

## Changing settings safely: `jftd fix`

`fix` changes nothing without your say-so:

1. It prints each setting it would change, as old value -> new value.
2. It asks you to type `yes`. When it isn't run from a terminal, it refuses
   unless you pass `--yes`.
3. It saves the full current encoding config to
   `~/.local/state/jftd/encoding-<time>.json` (mode 600).
4. It POSTs the full config with only those fields changed, checks each value
   has the type the server already uses, and reads the config back to verify.
5. It prints `jftd fix --restore <backup>` so you can undo the change.

By default `fix` only turns off formats the GPU can't decode. Add
`--also-enable` to also turn on formats the GPU supports but Jellyfin has off.

## Configuration

Every option can be passed as a flag or set in the environment:

| Flag              | Environment                 | Default                                                      |
| ----------------- | --------------------------- | ------------------------------------------------------------ |
| `--url`           | `JELLYFIN_URL`              | `http://127.0.0.1:8096`                                      |
| (none)            | `JELLYFIN_API_KEY`          | none; needed for `drift` and `fix`                           |
| `--api-key-file`  | `JELLYFIN_API_KEY_FILE`     | none                                                         |
| `--docker`        | `JFTD_DOCKER`               | none (native)                                                |
| `--docker-bin`    | `JFTD_DOCKER_BIN`           | `docker`                                                     |
| `--log-dir`       | `JFTD_LOG_DIR`              | `/var/log/jellyfin`, or `/config/log` with `--docker`        |
| `--ffmpeg` / `--ffprobe` / `--vainfo` | `JFTD_FFMPEG` / `JFTD_FFPROBE` / `JFTD_VAINFO` | jellyfin-ffmpeg's copy, else `PATH` |
| `--device`        | `JFTD_DEVICE`               | Jellyfin's configured device, else `/dev/dri/renderD128`     |
| `--backend`       | `JFTD_BACKEND`              | from Jellyfin's acceleration type, else `vaapi`              |

There is deliberately no `--api-key` flag, because it would show up in `ps`
output and shell history.

To work offline, or to reproduce a bug report, point `jftd` at saved files:

* `--vainfo-output FILE`: saved `vainfo` output.
* `--encoding-json FILE`: saved JSON from `GET /System/Configuration/encoding`.

`scan --state FILE` remembers the newest log it has seen, so you can run it
from a timer and see only new failures.

Exit status: `0` means fine, `1` means a problem was found (drift, failures,
or a failing test verdict), and `2` means `jftd` could not run.

## Contributing

* Tests: `python -m unittest discover -s tests -t .`. They use only synthetic
  fixtures, in `tests/fixtures/`.
* New failure patterns: add a scrubbed or synthetic log to
  `tests/fixtures/logs/` and a pattern in `src/jftd/logs.py`. Never commit real
  logs: they contain your media paths, and sometimes hostnames and addresses.
  `tests/test_hygiene.py` rejects private IP addresses, email addresses and
  API tokens.
* New GPU backends (NVDEC, other VA-API quirks): implement `probe()` and a
  feature map in `src/jftd/caps.py`. `profiles.py` is backend-neutral.

## License

Apache-2.0. See [LICENSE](LICENSE).
