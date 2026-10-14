"""Tests for the runnable CLI and its FROZEN stdout/exit contract.

The fake hub is a REAL in-process gRPC server on an ephemeral loopback port
(never a mock object), served from a background thread. The CLI itself runs as
a real ``python -m ecosys_pi`` SUBPROCESS, so the tests assert exactly what the
task-14 E2E harness will observe: the process stdout and exit code.

Coverage:

* ``--help`` lists every runtime flag (and no pairing flag);
* a happy path opens the stream UNAUTHENTICATED (no bearer token, no pairing),
  and a hub emission in the REAL ``result`` shape is printed as ``transcript``;
* the necklace is VIDEO-ONLY by default: no microphone frame is uploaded unless
  the operator opts in with ``--enable-audio`` / ``PI_ECOSYS_ENABLE_AUDIO``;
* the video pump (JPEG stills, pacing, cap, bounded termination).
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import subprocess
import sys
from concurrent import futures

import grpc
import pytest

from ecosys.v1 import ecosys_pb2, ecosys_pb2_grpc
from ecosys_pi import camera, cli

#: Generous timeout: a hang must FAIL the test, never block the suite.
SUBPROCESS_TIMEOUT_S = 60.0


class FakeHub(ecosys_pb2_grpc.StreamServiceServicer):
    """An in-process hub for ``OpenStream``, with NO auth gate.

    It mirrors the REAL hub: a transcribed frame is emitted as a ``result``
    (``StreamResult``), never the legacy ``transcript`` string, so the CLI's
    ``result`` dispatch is exercised end to end. One ``result`` is yielded per
    received audio OR image frame. It records the stream's invocation metadata
    (so a test can prove the client sends no ``authorization`` header) and counts
    the audio/image frames, so a test can prove the necklace uploads no
    microphone frames by default.
    """

    def __init__(self) -> None:
        self.audio_frames = 0
        self.image_frames = 0
        self.stream_metadata: list[dict[str, str]] = []

    def OpenStream(self, request_iterator, context):  # noqa: N802 - gRPC method name
        self.stream_metadata.append(dict(context.invocation_metadata()))
        for frame in request_iterator:
            if frame.HasField("audio_pcm16_16k"):
                self.audio_frames += 1
                yield ecosys_pb2.StreamFrame(
                    result=ecosys_pb2.StreamResult(
                        text=f"mock:{len(frame.audio_pcm16_16k)}"
                    )
                )
            elif frame.HasField("image"):
                self.image_frames += 1
                yield ecosys_pb2.StreamFrame(
                    result=ecosys_pb2.StreamResult(text=f"mock:{len(frame.image.jpeg)}")
                )


class RunningHub:
    """Context manager: a fake hub served on an ephemeral loopback port.

    Uses a SYNC ``grpc.server`` (a gRPC server is protocol-compatible with both
    sync and ``grpc.aio`` clients), which keeps the stream servicer a plain
    generator and needs no event loop in the test process.
    """

    def __init__(self, hub: FakeHub) -> None:
        self.hub = hub
        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
        ecosys_pb2_grpc.add_StreamServiceServicer_to_server(hub, self._server)
        port = self._server.add_insecure_port("127.0.0.1:0")
        assert port != 0, "failed to bind an ephemeral port"
        self.target = f"127.0.0.1:{port}"

    def __enter__(self) -> str:
        self._server.start()
        return self.target

    def __exit__(self, *_exc: object) -> None:
        self._server.stop(None)


def _cli_env(target: str | None, config_home: str) -> dict[str, str]:
    """A hermetic subprocess environment for one CLI run."""
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = config_home
    env["PI_ECOSYS_AUDIO_SOURCE"] = "synthetic"
    env["PI_ECOSYS_VIDEO_SOURCE"] = "synthetic"
    for key in (
        "PI_ECOSYS_HUB",
        "PI_ECOSYS_TTS_LANG",
        "PI_ECOSYS_ENABLE_AUDIO",
    ):
        env.pop(key, None)
    if target is not None:
        env["PI_ECOSYS_HUB"] = target
    return env


def _run_cli(
    args: list[str],
    *,
    env: dict[str, str],
    stdin_tty: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m ecosys_pi`` with a DEVNULL (non-TTY) stdin by default."""
    return subprocess.run(
        [sys.executable, "-m", "ecosys_pi", *args],
        env=env,
        stdin=subprocess.DEVNULL if not stdin_tty else None,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )


def test_help_lists_the_runtime_flags() -> None:
    """``--help`` advertises every runtime flag and no pairing flag."""
    result = subprocess.run(
        [sys.executable, "-m", "ecosys_pi", "--help"],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    assert result.returncode == 0
    help_text = result.stdout
    for flag in ("--hub", "--mode", "--frames", "--lang", "--enable-audio"):
        assert flag in help_text, f"{flag} missing from --help"
    # Pairing/PIN were removed from the runtime, so their flags are gone.
    assert "--pin" not in help_text
    assert "--no-cache" not in help_text
    assert "HOST:PORT" in help_text
    assert "audio" in help_text and "video" in help_text and "both" in help_text
    assert "yue" in help_text and "zh" in help_text and "en" in help_text


def test_session_opens_unauthenticated_without_a_pin_and_exits_zero(tmp_path) -> None:
    """A hub with no auth gate: the stream opens with no PIN and no token.

    The fake hub emits the REAL ``result`` frame, so a ``transcript`` line here
    proves the reader consumes that frame (the legacy ``transcript`` field the
    hub no longer sends).
    """
    hub = FakeHub()
    with RunningHub(hub) as target:
        env = _cli_env(target, str(tmp_path))
        # No --pin, no cached token: the session must simply run.
        result = _run_cli(["--mode", "video", "--frames", "3"], env=env)

    lines = result.stdout.splitlines()
    assert result.returncode == 0, result.stderr
    assert not any(line.startswith("pair-") for line in lines), "no pairing line"
    transcripts = [line for line in lines if line.startswith("transcript ")]
    assert transcripts, f"no transcript lines in {lines!r}"
    assert all(re.fullmatch(r"transcript mock:\d+", line) for line in transcripts)
    assert hub.image_frames >= 1
    # Video-only: no microphone frame left the Pi.
    assert hub.audio_frames == 0
    # Pairing was removed: the stream carries no bearer token.
    assert all("authorization" not in md for md in hub.stream_metadata)


# --- The necklace is video-only unless audio upload is explicitly opted in ----


def test_default_mode_is_video_only_and_sends_no_audio(tmp_path) -> None:
    """No ``--mode``: the default session sends JPEG stills and NO mic audio."""
    hub = FakeHub()
    with RunningHub(hub) as target:
        env = _cli_env(target, str(tmp_path))
        result = _run_cli(["--frames", "2"], env=env)

    assert result.returncode == 0, result.stderr
    assert hub.image_frames >= 1, "the default must still stream images"
    assert hub.audio_frames == 0, "the default must NOT upload microphone audio"


def test_audio_mode_without_the_opt_in_sends_no_audio(tmp_path) -> None:
    """``--mode audio`` alone is not enough: mic upload needs an explicit opt-in."""
    hub = FakeHub()
    with RunningHub(hub) as target:
        env = _cli_env(target, str(tmp_path))
        result = _run_cli(["--mode", "audio", "--frames", "2"], env=env)

    assert result.returncode == 0, result.stderr
    assert hub.audio_frames == 0
    assert "opt-in" in result.stderr


def test_enable_audio_flag_opts_into_microphone_upload(tmp_path) -> None:
    """``--enable-audio`` (the explicit opt-in) restores the audio producer."""
    hub = FakeHub()
    with RunningHub(hub) as target:
        env = _cli_env(target, str(tmp_path))
        result = _run_cli(
            ["--mode", "audio", "--enable-audio", "--frames", "3"], env=env
        )

    assert result.returncode == 0, result.stderr
    assert hub.audio_frames >= 1
    assert hub.image_frames == 0


# --- Video pump: JPEG stills, None is "not ready", not "exhausted" (bug M2) --


class _RecordingVideoClient:
    """Captures the ``StreamFrame``s ``_run_video`` sends."""

    def __init__(self) -> None:
        self.frames: list[object] = []

    async def send(self, frame: object) -> None:
        self.frames.append(frame)


class _GapThenFramesVideoSource:
    """A camera-like source: ``read_frame`` returns ``None`` for a few polls.

    Mirrors a real ``JpegVideoSource``, where ``None`` is the normal "no still
    ready yet" state. Once the gaps pass it yields its JPEGs, then reports
    ``done`` so a bounded pump can terminate.
    """

    width = 320
    height = 240

    def __init__(self, frames: list[bytes], gaps: int = 2) -> None:
        self._frames = list(frames)
        self._gaps = gaps
        self._index = 0

    @property
    def done(self) -> bool:
        return self._gaps <= 0 and self._index >= len(self._frames)

    def open(self) -> bool:
        return True

    def read_frame(self) -> bytes | None:
        if self._gaps > 0:
            self._gaps -= 1
            return None
        if self._index < len(self._frames):
            frame = self._frames[self._index]
            self._index += 1
            return frame
        return None

    def close(self) -> None:
        pass


def _run_video_pump(source: object, max_frames: int | None) -> tuple[int, list[object]]:
    client = _RecordingVideoClient()
    err = io.StringIO()
    sent = asyncio.run(
        cli._run_video(source, client, max_frames, err=err, interval=0.0)
    )
    return sent, client.frames


def test_video_frame_interval_is_the_10fps_cadence() -> None:
    """The pump paces at exactly 10 frames per second (0.1 s)."""
    assert cli._VIDEO_FRAME_INTERVAL_S == 0.1


def test_video_pump_sends_image_frames_for_the_cap() -> None:
    """Bug M2 + the JPEG contract: N ``--frames N`` sends N ``image`` frames."""
    jpeg = camera.synthetic_jpeg_frame()
    source = _GapThenFramesVideoSource([jpeg, jpeg], gaps=3)

    sent, frames = _run_video_pump(source, max_frames=None)

    assert sent == 2, "an empty read must not abort an unbounded video pump"
    assert [frame.WhichOneof("payload") for frame in frames] == ["image", "image"]
    assert [frame.image.jpeg for frame in frames] == [jpeg, jpeg]


def test_video_pump_sends_320x240_dimensions_on_every_frame() -> None:
    source = camera.SyntheticJpegSource(frame_count=2)

    sent, frames = _run_video_pump(source, max_frames=None)

    assert sent == 2
    assert [(frame.image.width, frame.image.height) for frame in frames] == [
        (320, 240),
        (320, 240),
    ]


def test_video_pump_terminates_when_a_bounded_source_is_done() -> None:
    """A capped synthetic source still terminates an unbounded pump."""
    source = camera.SyntheticJpegSource(frame_count=3)

    sent, frames = _run_video_pump(source, max_frames=None)

    assert sent == 3
    assert all(frame.WhichOneof("payload") == "image" for frame in frames)


def test_video_pump_honours_an_explicit_frame_cap() -> None:
    source = camera.SyntheticJpegSource()
    sent, frames = _run_video_pump(source, max_frames=2)
    assert sent == 2
    assert len(frames) == 2


# --- The frozen stdout contract is referenced, not dead documentation --------


def test_stdout_contract_is_the_frozen_lines() -> None:
    """``STDOUT_CONTRACT`` pins exactly the lines still emitted after retirement."""
    assert cli.STDOUT_CONTRACT == (
        "transcript <text>",
        "error: <detail>",
    )


if __name__ == "__main__":  # pragma: no cover - manual invocation
    pytest.main([__file__])
