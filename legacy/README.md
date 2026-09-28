# legacy/ — superseded Pi scripts (reference only)

**Nothing in this directory is imported by, packaged with, or tested against the
`ecosys_pi` client.** These are the Pi node's original scripts, kept only so the
history and the hardware-access shapes stay reachable. They are **not** part of
the `ecosys.v1` client and receive no maintenance.

## What is here

| File                | What it was                                                                                  | Why it is kept                                                                                                                        |
| ------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `pi_network.py`     | Raw TCP server that streamed OpenCV/JPEG frames as JSON lines; entry point for the old node. | Historical reference for the old wire protocol.                                                                                       |
| `client_session.py` | One accepted TCP connection: streamed JPEG out, played speech in.                            | Historical reference.                                                                                                                 |
| `video_stream.py`   | OpenCV `VideoCapture` + JPEG encoding for the old stream.                                    | **Reference for the camera open/teardown shape** reused by `src/ecosys_pi/camera.py` (open → `False` on failure, idempotent `close`). |
| `config.py`         | The OLD server's runtime config (`PI_NODE_*` env vars, port 8765, JPEG size/quality).        | Legacy settings only. The new client config is `src/ecosys_pi/config.py` (`PI_ECOSYS_*`).                                             |
| `audio_output.py`   | Base64 PCM playback over the OLD JSON protocol.                                              | Historical reference. The new client uses `src/ecosys_pi/tts.py`.                                                                     |
| `setup_network.py`  | BLE Wi-Fi provisioning helper (writes NetworkManager credentials via `nmcli`).               | **Out of scope for this branch** — see below.                                                                                         |

## Why these were retired

The `ecosys.v1` client in `src/ecosys_pi/` **replaces** the raw TCP/JSON server.
The hub is a gRPC server (`ecosys.v1.PairingService` / `StreamService`); the Pi
is a gRPC client that pairs with a PIN, caches a bearer token at `0600`, opens an
authenticated bidi stream, and sends H.264 NALs + 640-byte PCM16 frames. None of
that speaks the old newline-delimited JSON protocol, so the old modules no longer
have a caller.

## BLE Wi-Fi provisioning is OUT OF SCOPE for this branch

`setup_network.py` is **not** part of the `ecosys` client. In this topology the
**phone hub is the access point** and the Pi joins the hotspot as a **DHCP
client**; the Pi never receives Wi-Fi credentials over BLE. That helper is parked
here so it is not mistaken for client code. See the repository `README.md` for
the explicit scope statement.

## Running them (if you ever must)

They are plain scripts with implicit same-directory imports, so they only work
when invoked with this directory as the working directory / script location:

```bash
cd legacy && python pi_network.py
```

`cv2` (OpenCV) and `bleak` are **not** project dependencies and are not installed
by `uv sync`; these scripts will fail on import unless you install those
separately. Do **not** add them back to `pyproject.toml`.
