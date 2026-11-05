# ecosys-pi

The Raspberry Pi peer for the `ecosys.v1` device ecosystem. It joins the phone's
hotspot by hand, opens an **UNAUTHENTICATED** gRPC stream to the `phone-manager`
hub, streams **JPEG stills** (320×240), and plays the audio the hub synthesizes.
Microphone (PCM16) upload is **opt-in** — the necklace is a constrained device
and is video-only by default. It no longer pairs, heartbeats, or synthesizes
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
   (320×240 JPEG stills at ~10 fps) frames — and `audio_pcm16_16k` (640-byte,
   20 ms frames) only when microphone upload is explicitly opted in. The hub
   sends back `gesture` text and `tts_audio` (PCM16 @ 16 kHz), which the Pi
   plays; a `result` (transcription) frame is printed as the frozen
   `transcript <text>` line.
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
scripts/join-hotspot.sh join the phone hotspot on wlan0 (wpa_supplicant)
scripts/install-systemd.sh  install the boot-time systemd unit (run once)
scripts/ecosys-demo.sh  one-key demo restart + status + recent logs
deploy/systemd/         the unit template and the env-file example
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
# Join the hotspot from config (SSID/password) and stream video to the gateway
# hub (the default; microphone upload is opt-in).
uv run python -m ecosys_pi

# Scripted: explicit hub, microphone audio only (explicit opt-in), 100 frames.
uv run python -m ecosys_pi --hub 192.168.43.1:9000 --mode audio --enable-audio --frames 100
```

### CLI flags

| Flag                        | Default | Meaning                                                           |
| --------------------------- | ------- | ----------------------------------------------------------------- |
| `--hub HOST:PORT`           | (unset) | Hub address, overriding `PI_ECOSYS_HUB` and the gateway.          |
| `--mode audio\|video\|both` | `video` | Which media to stream. Microphone audio is video-only by default. |
| `--enable-audio`            | off     | Explicit opt-in to microphone upload (`PI_ECOSYS_ENABLE_AUDIO`).  |
| `--frames N`                | `0`     | Frames per medium; `0` runs until interrupted.                    |
| `--lang yue\|zh\|en`        | (unset) | Retained for compatibility; the hub selects the voice.            |

### stdout / exit contract (frozen)

Automation (including the hub's JVM E2E harness) keys on stdout, so these lines
are frozen. Informational chatter from the media/playback modules goes to
**stderr**.

| Line                | Meaning                                          | Exit |
| ------------------- | ------------------------------------------------ | ---- |
| `transcript <text>` | One hub `result` (transcription) frame received. | 0    |
| `error: <detail>`   | Anything else.                                   | 1    |

## Environment variables

| Variable                       | Default                | Meaning                                                                                                                                                                                                        |
| ------------------------------ | ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PI_ECOSYS_HUB`                | (unset)                | `HOST:PORT` hub override. Precedence: `--hub` > `PI_ECOSYS_HUB` > default gateway. Blank = unset.                                                                                                              |
| `PI_ECOSYS_SSID`               | (unset)                | Hotspot SSID the Pi joins with `nmcli`.                                                                                                                                                                        |
| `PI_ECOSYS_HOTSPOT_PASSWORD`   | (unset)                | Hotspot WPA2/WPA3 password. A secret: redacted in `Config.__repr__`.                                                                                                                                           |
| `PI_ECOSYS_HUB_PORT`           | `9000`                 | Hub gRPC port appended to the gateway host.                                                                                                                                                                    |
| `PI_ECOSYS_WIFI_SECURITY`      | `wpa2`                 | `nmcli` key management: `wpa2` or `wpa3` (SAE).                                                                                                                                                                |
| `PI_ECOSYS_ENABLE_AUDIO`       | (unset)                | Truthy (`1`/`true`/`yes`/`on`) opts into microphone upload; otherwise the necklace is video-only.                                                                                                              |
| `PI_ECOSYS_ENROLLMENT_SECONDS` | `8`                    | Voiceprint enrollment length (informational on the Pi).                                                                                                                                                        |
| `PI_ECOSYS_TTS_LANG`           | `yue`                  | Retained for compatibility; has no effect on the wire (the hub selects the voice).                                                                                                                             |
| `PI_ECOSYS_DEVICE_NAME`        | `socket.gethostname()` | Label sent in the `stream_open` frame; blank falls back to the hostname, then `ecosys-pi`.                                                                                                                     |
| `PI_ECOSYS_AUDIO_SOURCE`       | `auto`                 | `auto` / `synthetic` / `sounddevice` / `arecord`. `synthetic` is deterministic (used by tests).                                                                                                                |
| `PI_ECOSYS_VIDEO_SOURCE`       | `auto`                 | `auto` / `synthetic` / `uvc` / `picamera2`. `uvc` is the demo path and hard-fails when its device or ffmpeg is missing; `auto` prefers picamera2, then `/dev/video0` + ffmpeg, then a loud synthetic fallback. |
| `PI_ECOSYS_VIDEO_DEVICE`       | `/dev/video0`          | UVC/V4L2 capture node for `PI_ECOSYS_VIDEO_SOURCE=uvc` (or auto). Used by the missing-device probe.                                                                                                            |

> **systemd caveat.** The client reads the _process_ environment. A unit started
> for a service user gets a fresh, minimal environment — set variables with
> `Environment=` / `EnvironmentFile=` in the unit, or point the loader at a
> `.env` / `config.json` file, or the defaults apply.

## Run on boot (systemd) / one-key demo

The client ships as a systemd unit, so it starts on boot and can be restarted
with one command during a live demo. This is the concrete answer to the
[systemd caveat](#environment-variables): the unit loads
`/etc/ecosys/ecosys-client.env` with `EnvironmentFile=`, so `PI_ECOSYS_HUB` and
the camera settings reach the process even though a service gets a minimal
environment. The service runs **only** the client — it does not manage Wi-Fi,
`usb0`, `eth0`, or the phone hotspot.

| Path                                                                                   | Purpose                                                    |
| -------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| [`deploy/systemd/ecosys-client.service`](deploy/systemd/ecosys-client.service)         | Unit **template** with `@REPO@` / `@PYTHON@` placeholders. |
| [`deploy/systemd/ecosys-client.env.example`](deploy/systemd/ecosys-client.env.example) | Env-file example; contains no secrets.                     |
| `scripts/install-systemd.sh`                                                           | Renders and installs the unit, then enables/restarts it.   |
| `scripts/ecosys-demo.sh`                                                               | One-key restart + status + recent journal lines.           |

### Install once (on the Pi)

The deployed checkout is `/root/necklace` and its venv is
`/root/necklace/.venv`; run the installer from there as root:

```bash
cd /root/necklace
sudo ./scripts/install-systemd.sh
```

The installer renders the template with this checkout's absolute repo and venv
paths, installs `/etc/systemd/system/ecosys-client.service` (mode 0644), creates
`/etc/ecosys/ecosys-client.env` **only if it is absent**, then runs
`systemctl daemon-reload`, `systemctl enable ecosys-client.service`, and
`systemctl restart ecosys-client.service`. It is idempotent and never overwrites
an existing env file.

Point the client at the phone hotspot gateway for the demo:

```bash
sudoedit /etc/ecosys/ecosys-client.env
```

```ini
PI_ECOSYS_HUB=10.240.85.29:9000   # edit to the phone hotspot gateway for the demo
PI_ECOSYS_VIDEO_SOURCE=uvc
PI_ECOSYS_VIDEO_DEVICE=/dev/video0
PI_ECOSYS_DEVICE_NAME=ecosys-pi
```

`ExecStart` deliberately has **no `--hub`**: the CLI falls back to
`PI_ECOSYS_HUB`, so the env file — not the unit — controls the hub address. (Note
that systemd does not expand `${VAR}` in `ExecStart`; the CLI's own env fallback
is what makes this work.)

### Start / restart for the demo

```bash
# One key: restart the service, wait briefly, print status + the last 20 log lines.
sudo ./scripts/ecosys-demo.sh

# The same restart on its own.
sudo systemctl restart ecosys-client
```

`ecosys-demo.sh` exits `0` only when `ecosys-client.service` is running.

### Logs

```bash
journalctl -u ecosys-client -f              # follow
journalctl -u ecosys-client -n 100 --no-pager
```

The client's frozen stdout contract (`transcript <text>` / `error: <detail>`)
and its informational stderr chatter both land in the journal.

### Troubleshooting

- **Camera (`/dev/video0`).** `ExecStartPre` waits up to 60 s for the USB Y360 to
  enumerate. If the camera never appears, the client starts anyway and reports
  the real error; check the unit with `systemctl status ecosys-client`, confirm
  the node with `ls -l /dev/video0`, and replug the Y360. With
  `PI_ECOSYS_VIDEO_SOURCE=uvc` a missing device or `ffmpeg` is a hard failure, by
  design; use `auto` only if a CSI `picamera2` camera is the intended source.
- **Hub address.** The phone is the hotspot access point, so the hub is normally
  the Pi's default gateway — confirm it with `ip route` on the Pi. If the phone
  re-assigned a different address, edit `PI_ECOSYS_HUB` in
  `/etc/ecosys/ecosys-client.env` and run `sudo systemctl restart ecosys-client`.
- **Wi-Fi / hotspot.** The unit does not join the hotspot. Join it once with
  [`scripts/join-hotspot.sh`](scripts/join-hotspot.sh); the unit only waits for
  `network-online.target` and `wpa_supplicant@wlan0.service`.
- **Restart loop.** The unit sets `StartLimitIntervalSec=0`, so a missing camera
  or unreachable hub never exhausts the start limit. Read
  `journalctl -u ecosys-client -n 50` for the actual cause.
- **Repo moved or venv recreated.** The rendered unit embeds absolute paths;
  rerun `sudo ./scripts/install-systemd.sh` after moving the checkout or
  recreating `.venv`.

## Scope: what this branch does NOT do

- **No BLE and no programmatic Wi-Fi.** The BLE provisioning helper is parked
  under [`legacy/`](legacy/README.md) and is NOT imported. The Pi joins the
  hotspot from config with `nmcli` and finds the hub at the **default gateway**;
  there is no mDNS and no BLE.
- **The Pi does not synthesize speech.** The hub runs TTS and ships PCM16 @
  16 kHz; the Pi only plays it.
- **The Pi does not run gesture recognition or diarization.** It sends JPEG
  stills — and mic audio only when explicitly opted in — while the hub decodes
  the stills, recognizes the gesture, runs the speech/diarization engine, and
  fans the results out to the glasses and the necklace.

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
