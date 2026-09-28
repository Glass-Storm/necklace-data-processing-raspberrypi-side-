# ecosys-pi

The Raspberry Pi peer for the `ecosys.v1` device ecosystem. It pairs with the
**phone-manager** hub over gRPC, streams camera and microphone media, and plays
back the audio the hub returns.

> Status: **skeleton only.** This branch bootstraps the `uv` project; pairing,
> discovery, streaming, and camera code land in later workstreams. The legacy
> scripts (`pi_network.py`, `client_session.py`, `video_stream.py`,
> `config.py`, `audio_output.py`, `setup_network.py`) are still present and are
> deleted by a later task.

## Layout

```
pyproject.toml        uv-managed project metadata + dependencies
uv.lock               the resolved, reproducible lock file
src/ecosys_pi/        the client package (placeholder for now)
tests/                pytest smoke tests
```

## Prerequisites

- Python **>= 3.11** (the dev host uses CPython 3.12 via `uv`).
- [`uv`](https://docs.astral.sh/uv/) — this project uses `uv` for **everything**
  (venv, dependencies, lock, run). Never `pip`, never `requirements.txt`.

The venv **MUST** be created with `--system-site-packages`:

```bash
uv venv --system-site-packages
```

This writes `include-system-site-packages = true` into `.venv/pyvenv.cfg`. It is
**required**: on a real Pi the Debian `python3-picamera2` package installs the
libcamera bindings into the _system_ site-packages, and they are only visible to
this venv when system site-packages are included. `tests/test_smoke.py` asserts
this flag, so a venv built without it fails the suite.

### Camera (Pi only)

`picamera2` is **not** a hard dependency. It is an optional extra because the dev
host has no camera and never will; the non-Pi install and test path must work
without it. On a real Raspberry Pi OS box, install the Debian-packaged camera
bindings first:

```bash
sudo apt install -y python3-picamera2
```

then opt into the extra when you need it:

```bash
uv sync --extra pi
```

> Building the `pi` extra on a Debian/Ubuntu host may require the libcap headers
> (`sudo apt install -y libcap-dev`) because `picamera2` pulls in
> `python-prctl`, whose build backend needs `libcap` development headers.

## Commands

```bash
uv sync                      # create/refresh the venv from uv.lock
uv run pytest                # run the test suite
uv run python -c "import ecosys_pi"   # import smoke check
```

## Adding a dependency

```bash
uv add <package>             # adds to [project].dependencies + relocks
uv add --optional pi <pkg>   # adds to the optional "pi" extra
```

Keep `grpcio` and `grpcio-tools` on the **same minor** version (`1.84.x`) so the
generated stubs match the hub's Kotlin side, which is pinned to grpc `1.84.0`.
