# ecosys-pi

The Raspberry Pi peer for the `ecosys.v1` device ecosystem. It joins the phone's
hotspot by hand, opens an **UNAUTHENTICATED** gRPC stream to the `phone-manager`
hub, streams **JPEG stills** (320×240) and microphone (PCM16) media, and plays
the audio the hub synthesizes. It no longer pairs, heartbeats, or synthesizes
speech locally.

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

The hub is a gRPC server (`ecosys.v1.StreamService`) and is the **access point**;
the Pi is a **DHCP client** that joins the hotspot.

1. **Join + discover** — the Pi joins the hotspot from its config with `nmcli`
   (SSID + WPA2/WPA3 password), then resolves the hub as the **default gateway**
   (`ip route`, falling back to `nmcli`). There is no PIN, no token, no heartbeat
   and no mDNS; `--hub` / `PI_ECOSYS_HUB` override the gateway.
2. **Stream** — one UNAUTHENTICATED `OpenStream`: the Pi opens it with a
   `stream_open` frame that binds its device id/role, then pushes `image`
   (320×240 JPEG stills at ~10 fps) and/or `audio_pcm16_16k` (640-byte, 20 ms
   frames) frames. The hub sends back `gesture` text and `tts_audio` (PCM16 @
   16 kHz), which the Pi plays; a legacy `transcript` frame is logged to stdout.
3. **No heartbeat** — liveness is implied by the open stream; a transport loss
   simply reconnects.

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

# ALSA playback (aplay) for hub-synthesized audio; libportaudio2 is the fallback.
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

### Audio playback

The Pi does **not** synthesize speech: the hub runs text-to-speech and ships raw
PCM16 mono @ 16 kHz. The Pi plays those bytes with
`aplay -f S16_LE -r 16000 -c 1` (`alsa-utils`), falling back to
`sounddevice`/PortAudio (`libportaudio2`). When neither backend is present the
client **warns and keeps streaming**; it never crashes the session.

## Commands

```bash
uv sync                                   # create/refresh the venv from uv.lock
uv run pytest -q                          # run the test suite
uv run python -c "import ecosys_pi"       # import smoke check
uv run python -m ecosys_pi --help         # the CLI
```

### Running the client

```bash
# Join the hotspot from config (SSID/password) and stream both media to the
# gateway hub.
uv run python -m ecosys_pi

# Scripted: explicit hub, audio only, 100 frames.
uv run python -m ecosys_pi --hub 192.168.43.1:50051 --mode audio --frames 100
```

### CLI flags

| Flag                        | Default | Meaning                                                  |
| --------------------------- | ------- | -------------------------------------------------------- |
| `--hub HOST:PORT`           | (unset) | Hub address, overriding `PI_ECOSYS_HUB` and the gateway. |
| `--mode audio\|video\|both` | `both`  | Which media to stream.                                   |
| `--frames N`                | `0`     | Frames per medium; `0` runs until interrupted.           |
| `--lang yue\|zh\|en`        | (unset) | Retained for compatibility; the hub selects the voice.   |

### stdout / exit contract (frozen)

Automation (including the hub's JVM E2E harness) keys on stdout, so these lines
are frozen. Informational chatter from the media/playback modules goes to
**stderr**.

| Line                | Meaning                               | Exit |
| ------------------- | ------------------------------------- | ---- |
| `transcript <text>` | One legacy transcript frame received. | 0    |
| `error: <detail>`   | Anything else.                        | 1    |

## Environment variables

| Variable                       | Default                | Meaning                                                                                           |
| ------------------------------ | ---------------------- | ------------------------------------------------------------------------------------------------- |
| `PI_ECOSYS_HUB`                | (unset)                | `HOST:PORT` hub override. Precedence: `--hub` > `PI_ECOSYS_HUB` > default gateway. Blank = unset. |
| `PI_ECOSYS_SSID`               | (unset)                | Hotspot SSID the Pi joins with `nmcli`.                                                           |
| `PI_ECOSYS_HOTSPOT_PASSWORD`   | (unset)                | Hotspot WPA2/WPA3 password. A secret: redacted in `Config.__repr__`.                              |
| `PI_ECOSYS_HUB_PORT`           | `50051`                | Hub gRPC port appended to the gateway host.                                                       |
| `PI_ECOSYS_ENROLLMENT_SECONDS` | `8`                    | Voiceprint enrollment length (informational on the Pi).                                           |
| `PI_ECOSYS_TTS_LANG`           | `yue`                  | Retained for compatibility; has no effect on the wire (the hub selects the voice).                |
| `PI_ECOSYS_DEVICE_NAME`        | `socket.gethostname()` | Label sent in the `stream_open` frame; blank falls back to the hostname, then `ecosys-pi`.        |
| `PI_ECOSYS_AUDIO_SOURCE`       | `auto`                 | `auto` / `synthetic` / `sounddevice` / `arecord`. `synthetic` is deterministic (used by tests).   |
| `PI_ECOSYS_VIDEO_SOURCE`       | `auto`                 | `auto` / `synthetic` / `picamera2`. `picamera2` when absent falls back to `synthetic`.            |

> **systemd caveat.** The client reads the _process_ environment. A unit started
> for a service user gets a fresh, minimal environment — set variables with
> `Environment=` / `EnvironmentFile=` in the unit, or point the loader at a
> `.env` / `config.json` file, or the defaults apply.

## Scope: what this branch does NOT do

- **No BLE and no programmatic Wi-Fi.** The BLE provisioning helper is parked
  under [`legacy/`](legacy/README.md) and is NOT imported. The Pi joins the
  hotspot from config with `nmcli` and finds the hub at the **default gateway**;
  there is no mDNS and no BLE.
- **The Pi does not synthesize speech.** The hub runs TTS and ships PCM16 @
  16 kHz; the Pi only plays it.
- **The Pi does not run gesture recognition or diarization.** It sends JPEG
  stills and mic audio; the hub decodes the stills, recognizes the gesture, runs
  the speech/diarization engine, and fans the results out to the glasses and the
  necklace.

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
