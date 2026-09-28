# ecosys-pi

The Raspberry Pi peer for the `ecosys.v1` device ecosystem. It **pairs with the
`phone-manager` hub over gRPC**, streams camera (H.264) and microphone (PCM16)
media, receives transcripts on the same bidi stream, and speaks them back through
a language-selected local TTS engine.

This is the client that replaced the old raw TCP/JSON node. `python -m ecosys_pi`
is the single entry point; the hub-driven end-to-end harness in `phone-manager`
drives exactly this CLI.

> **Migration note.** The legacy scripts — the old TCP/JSON server, its session
> and camera modules, the old node config, the old audio-output module, and the
> BLE provisioning helper — have been moved to [`legacy/`](legacy/README.md).
> They are **superseded, kept for reference only, and are NOT imported by the
> `ecosys_pi` client**. See [`legacy/README.md`](legacy/README.md) for the exact
> file list.

## How it talks to the hub

The hub is a gRPC server (`ecosys.v1.PairingService` + `ecosys.v1.StreamService`)
and is the **access point**; the Pi is a **DHCP client** that joins the hotspot.

1. **Pair** — one single-use 6-digit PIN (120 s TTL, 5 attempts) exchanged for a
   bearer token. The token is cached at `0600` outside the repo
   (`$XDG_CONFIG_HOME/ecosys-pi/credentials.json`, default `~/.config/...`).
2. **Heartbeat** — a background `Heartbeat` keeps the paired session alive and
   independently detects a revoked token (`UNAUTHENTICATED` → re-pair).
3. **Stream** — one authenticated bidirectional `OpenStream`: the Pi pushes
   `audio_pcm16_16k` (640-byte, 20 ms frames) and/or `video_h264_nal` frames; the
   hub pushes `transcript` frames back, which the Pi speaks.

The wire contract is **frozen**: [`proto/ecosys/v1/ecosys.proto`](proto/ecosys/v1/ecosys.proto)
is a byte-identical copy of the hub's proto. See [`docs/PROTOCOL_DECISIONS.md`](docs/PROTOCOL_DECISIONS.md)
and the vendoring provenance in [`proto/PROTO_SOURCE`](proto/PROTO_SOURCE).

## Layout

```
pyproject.toml          uv-managed project metadata + dependencies
uv.lock                 the resolved, reproducible lock file
src/ecosys_pi/          the hand-written client package
src/ecosys/             the generated protobuf/gRPC stubs (see scripts/gen_proto.sh)
proto/                  the vendored frozen .proto + PROTO_SOURCE provenance
scripts/gen_proto.sh    regenerate the Python stubs from the vendored proto
tests/                  pytest suite (fake-hub + real-CLI subprocess tests)
legacy/                 the superseded TCP/JSON + BLE scripts (reference only)
docs/                   protocol decisions
```

## Prerequisites

- Python **>= 3.11** (the dev host uses CPython 3.12 via `uv`).
- [`uv`](https://docs.astral.sh/uv/) — this project uses `uv` for **everything**
  (venv, dependencies, lock, run). Never `pip`, never `requirements.txt`.

The venv **MUST** be created with `--system-site-packages`:

```bash
uv venv --system-site-packages
uv sync
```

This writes `include-system-site-packages = true` into `.venv/pyvenv.cfg`. It is
**required**: on a real Pi the Debian `python3-picamera2` package installs the
libcamera bindings into the _system_ site-packages, and they are only visible to
this venv when system site-packages are included. `tests/test_smoke.py` asserts
this flag, so a venv built without it fails the suite.

### Hardware prerequisites (real Pi only)

```bash
# Camera (picamera2) — Debian-packaged libcamera bindings.
sudo apt install -y python3-picamera2

# Cantonese TTS (yue) + Piper phonemisation data.
sudo apt install -y espeak-ng espeak-ng-data

# Optional: ALSA capture fallback + Piper playback.
sudo apt install -y alsa-utils libportaudio2
```

`picamera2` is **not** a hard dependency and is imported lazily; it is an
optional extra because the dev host has no camera and never will. Opt into it on
a Pi:

```bash
uv sync --extra pi
```

> Building the `pi` extra on a Debian/Ubuntu host may require the libcap headers
> (`sudo apt install -y libcap-dev`) because `picamera2` pulls in
> `python-prctl`, whose build backend needs `libcap` development headers.

### Piper voices (zh / en)

`piper-tts` is a normal `uv` dependency (`uv add piper-tts`, already in the
lockfile). Its voice models are **not** bundled and `piper` is archived, so the
exact model files are pinned by URL in `src/ecosys_pi/tts.py`
(`PIPER_VOICE_URLS`). Download both the `.onnx` and the `.onnx.json` for each
language you need into `$PI_ECOSYS_PIPER_DATA_DIR`
(default `~/.local/share/piper-voices`):

- `zh` (Mandarin, `zh_CN-huayan-medium`):
  - https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx
  - https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx.json
- `en` (English, `en_US-lessac-medium`):
  - https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx
  - https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json

`yue` (Cantonese) uses `espeak-ng` instead — Piper has no Cantonese voice. When
an engine or voice is missing the client **warns and keeps streaming**; it never
crashes the session.

## Commands

```bash
uv sync                                   # create/refresh the venv from uv.lock
uv run pytest -q                          # run the test suite
uv run python -c "import ecosys_pi"       # import smoke check
uv run python -m ecosys_pi --help         # the CLI
```

### Running the client

```bash
# Pair interactively (the PIN is read from the console) and stream both media.
uv run python -m ecosys_pi

# Scripted: explicit hub + PIN, audio only, 100 frames, English TTS.
PI_ECOSYS_TTS_LANG=en uv run python -m ecosys_pi \
    --hub 192.168.43.1:50051 --pin 482915 --mode audio --frames 100

# Find the hub by mDNS `_ecosys._tcp` (no --hub, no PI_ECOSYS_HUB).
uv run python -m ecosys_pi
```

### CLI flags

| Flag                        | Default | Meaning                                                               |
| --------------------------- | ------- | --------------------------------------------------------------------- |
| `--hub HOST:PORT`           | (unset) | Hub address, overriding `PI_ECOSYS_HUB` and mDNS discovery.           |
| `--pin PIN`                 | (unset) | Pairing PIN, overriding the console. Forces ONE non-interactive Pair. |
| `--mode audio\|video\|both` | `both`  | Which media to stream.                                                |
| `--frames N`                | `0`     | Frames per medium; `0` runs until interrupted.                        |
| `--no-cache`                | `false` | Ignore the cached token and force a fresh Pair.                       |
| `--lang yue\|zh\|en`        | (unset) | TTS language, overriding `PI_ECOSYS_TTS_LANG`.                        |

### stdout / exit contract (frozen)

Automation (including the hub's JVM E2E harness) keys on stdout, so these lines
are frozen. Informational chatter from the media/TTS modules goes to **stderr**.

| Line                       | Meaning                                          | Exit |
| -------------------------- | ------------------------------------------------ | ---- |
| `pair-ok device=<id>`      | Pair succeeded.                                  | 0    |
| `heartbeat-ok`             | The first heartbeat was accepted.                | 0    |
| `transcript <text>`        | One transcript frame received from the hub.      | 0    |
| `pair-rejected reason=<r>` | The hub refused the PIN (typed reason verbatim). | 2    |
| `re-pair-requested`        | The token was refused (`UNAUTHENTICATED`).       | 2    |
| `error: <detail>`          | Anything else.                                   | 1    |

## Environment variables

| Variable                   | Default                       | Meaning                                                                                                    |
| -------------------------- | ----------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `PI_ECOSYS_HUB`            | (unset)                       | `HOST:PORT` hub override. Precedence: `--hub` > `PI_ECOSYS_HUB` > mDNS. A blank value is treated as unset. |
| `PI_ECOSYS_PIN`            | (unset)                       | **Scripting only.** Single-use PIN with a 120 s TTL — not durable config.                                  |
| `PI_ECOSYS_TTS_LANG`       | `yue`                         | One of `yue` / `zh` / `en`. Blank or unknown falls back to `yue`.                                          |
| `PI_ECOSYS_DEVICE_NAME`    | `socket.gethostname()`        | Non-blank label sent to the hub; blank falls back to the hostname, then `ecosys-pi`.                       |
| `PI_ECOSYS_PIPER_DATA_DIR` | `~/.local/share/piper-voices` | Where Piper looks for voice models.                                                                        |
| `PI_ECOSYS_AUDIO_SOURCE`   | `auto`                        | `auto` / `synthetic` / `sounddevice` / `arecord`. `synthetic` is deterministic (used by tests).            |
| `PI_ECOSYS_VIDEO_SOURCE`   | `auto`                        | `auto` / `synthetic` / `picamera2`. `picamera2` when absent falls back to `synthetic`.                     |

> **systemd caveat.** The client reads the _process_ environment. A unit started
> for a service user gets a fresh, minimal environment — set variables with
> `Environment=` / `EnvironmentFile=` in the unit, and give the user an
> `XDG_CONFIG_HOME` it owns at mode `0700` so the token cache lands somewhere
> writable.

## Scope: what this branch does NOT do

- **BLE Wi-Fi provisioning is OUT OF SCOPE for this branch.** The BLE Wi-Fi
  helper (a script that pushed Wi-Fi credentials via `nmcli`) is **not** part of
  the `ecosys` client and has been parked under [`legacy/`](legacy/README.md).
  In this topology the **hub is the access point** and the Pi is a **DHCP
  client**; the Pi never receives Wi-Fi credentials over BLE. Do not wire BLE
  back into the client.
- **Glass display (glass↔hub fan-out) is OUT OF SCOPE.** <!-- TODO(glass-display): the hub currently DISCARDS all video (`DiscardingFrameSink`); a glasses app that consumes the H.264 stream is future work and is intentionally not built here. -->
- Sign-language recognition, speaker diarization, and real (non-mock)
  transcription are out of scope; the hub's default engine emits a deterministic
  mock transcript.

## Adding a dependency

```bash
uv add <package>             # adds to [project].dependencies + relocks
uv add --optional pi <pkg>   # adds to the optional "pi" extra
```

Keep `grpcio` and `grpcio-tools` on the **same minor** version (`1.84.x`) so the
generated stubs match the hub's Kotlin side, which is pinned to grpc `1.84.0`.

## Regenerating the wire stubs

Only after re-vendoring the frozen proto from the hub:

```bash
./scripts/gen_proto.sh
```

The generated `src/ecosys/v1/*_pb2*.py` are committed and **must not** be
hand-edited.
