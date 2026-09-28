"""Tests for the runnable CLI and its FROZEN stdout/exit contract.

The fake hub is a REAL in-process gRPC server on an ephemeral loopback port
(never a mock object), served from a background thread. The CLI itself runs as
a real ``python -m ecosys_pi`` SUBPROCESS, so the tests assert exactly what the
task-14 E2E harness will observe: the process stdout and exit code.

Coverage:

* ``--help`` lists every frozen flag;
* a happy path prints ``pair-ok`` / ``heartbeat-ok`` / ``transcript ...`` and
  exits ``0``;
* a refused PIN prints ``pair-rejected reason=<r>`` and exits ``2`` in exactly
  ONE Pair attempt (non-interactive);
* ``--no-cache`` forces a fresh Pair (and a usable cache skips it);
* a non-TTY run with no PIN fails fast with ``error:`` on stdout, never hangs.
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
from ecosys_pi.token_store import Credentials, TokenStore

TOKEN = "issued-token-cli"
DEVICE_ID = "pi-device-cli"
PIN = "123456"
#: Generous timeout: a hang must FAIL the test, never block the suite.
SUBPROCESS_TIMEOUT_S = 60.0


class FakeHub(
    ecosys_pb2_grpc.PairingServiceServicer, ecosys_pb2_grpc.StreamServiceServicer
):
    """An in-process hub for Pair + Heartbeat + OpenStream.

    ``Pair`` pops a queued ``PairResponse`` (or returns a scripted default), so
    a test can script an accept or a typed rejection. ``Heartbeat`` always
    accepts. ``OpenStream`` yields one ``transcript`` per received audio frame,
    mirroring the hub's mock STT adapter shape ``mock:<len>:<hash>``.
    """

    def __init__(
        self,
        *,
        pair_responses: list[ecosys_pb2.PairResponse] | None = None,
        pair_default: ecosys_pb2.PairResponse | None = None,
        heartbeat_ok: bool = True,
        reject_token: bool = False,
    ) -> None:
        self._pair_responses = list(pair_responses or [])
        self._pair_default = pair_default
        self._heartbeat_ok = heartbeat_ok
        self._reject_token = reject_token
        self.pair_requests: list[ecosys_pb2.PairRequest] = []
        self.heartbeat_count = 0
        self.audio_frames = 0

    def _check_token(self, context: grpc.ServicerContext) -> None:
        if self._reject_token:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "token refused")

    @property
    def pair_call_count(self) -> int:
        return len(self.pair_requests)

    def Pair(self, request, context):  # noqa: N802 - gRPC method name
        self.pair_requests.append(request)
        if self._pair_responses:
            return self._pair_responses.pop(0)
        if self._pair_default is not None:
            return self._pair_default
        return ecosys_pb2.PairResponse(ok=True, token=TOKEN, device_id=DEVICE_ID)

    def Heartbeat(self, request, context):  # noqa: N802 - gRPC method name
        self._check_token(context)
        self.heartbeat_count += 1
        return ecosys_pb2.HeartbeatResponse(
            ok=self._heartbeat_ok, server_time_ms=1_700_000_000_000
        )

    def OpenStream(self, request_iterator, context):  # noqa: N802 - gRPC method name
        self._check_token(context)
        for frame in request_iterator:
            if frame.HasField("audio_pcm16_16k"):
                self.audio_frames += 1
                yield ecosys_pb2.StreamFrame(
                    transcript=f"mock:{len(frame.audio_pcm16_16k)}"
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
        ecosys_pb2_grpc.add_PairingServiceServicer_to_server(hub, self._server)
        ecosys_pb2_grpc.add_StreamServiceServicer_to_server(hub, self._server)
        port = self._server.add_insecure_port("127.0.0.1:0")
        assert port != 0, "failed to bind an ephemeral port"
        self.target = f"127.0.0.1:{port}"

    def __enter__(self) -> str:
        self._server.start()
        return self.target

    def __exit__(self, *_exc: object) -> None:
        self._server.stop(None)


def _reject(reason: str) -> ecosys_pb2.PairResponse:
    return ecosys_pb2.PairResponse(ok=False, reject_reason=reason)


def _cli_env(target: str | None, config_home: str) -> dict[str, str]:
    """A hermetic subprocess environment for one CLI run."""
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = config_home
    env["PI_ECOSYS_AUDIO_SOURCE"] = "synthetic"
    env["PI_ECOSYS_VIDEO_SOURCE"] = "synthetic"
    for key in ("PI_ECOSYS_HUB", "PI_ECOSYS_PIN", "PI_ECOSYS_TTS_LANG"):
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


def test_help_lists_every_frozen_flag() -> None:
    """``--help`` advertises every flag the harness depends on."""
    result = subprocess.run(
        [sys.executable, "-m", "ecosys_pi", "--help"],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    assert result.returncode == 0
    help_text = result.stdout
    for flag in ("--hub", "--pin", "--mode", "--frames", "--no-cache", "--lang"):
        assert flag in help_text, f"{flag} missing from --help"
    assert "HOST:PORT" in help_text
    assert "audio" in help_text and "video" in help_text and "both" in help_text
    assert "yue" in help_text and "zh" in help_text and "en" in help_text


def test_happy_path_prints_frozen_contract_and_exits_zero(tmp_path) -> None:
    """A fake hub accepts the PIN; stdout carries the exact contract lines."""
    hub = FakeHub()
    with RunningHub(hub) as target:
        env = _cli_env(target, str(tmp_path))
        result = _run_cli(
            ["--pin", PIN, "--mode", "audio", "--frames", "3"],
            env=env,
        )

    lines = result.stdout.splitlines()
    assert result.returncode == 0, result.stderr
    assert f"pair-ok device={DEVICE_ID}" in lines
    assert "heartbeat-ok" in lines
    transcripts = [line for line in lines if line.startswith("transcript ")]
    assert transcripts, f"no transcript lines in {lines!r}"
    assert all(re.fullmatch(r"transcript mock:640", line) for line in transcripts)
    # Frozen lines appear on stdout in the expected order.
    assert lines.index(f"pair-ok device={DEVICE_ID}") < lines.index("heartbeat-ok")
    assert hub.audio_frames >= 1


def test_refused_pin_prints_reason_and_exits_two_in_one_attempt(tmp_path) -> None:
    """A bad PIN is a normal outcome: one attempt, typed reason, exit 2."""
    hub = FakeHub(pair_default=_reject("pin-invalid"))
    with RunningHub(hub) as target:
        env = _cli_env(target, str(tmp_path))
        result = _run_cli(
            ["--pin", "000000", "--mode", "audio", "--frames", "1"],
            env=env,
        )

    assert result.returncode == 2, result.stderr
    assert "pair-rejected reason=pin-invalid" in result.stdout.splitlines()
    assert hub.pair_call_count == 1, "non-interactive must make exactly ONE attempt"


def test_no_cache_forces_a_fresh_pair_while_cache_skips_it(tmp_path) -> None:
    """``--no-cache`` ignores the cache; a usable cache avoids a second Pair."""
    hub = FakeHub()
    with RunningHub(hub) as target:
        env = _cli_env(target, str(tmp_path))

        first = _run_cli(["--pin", PIN, "--mode", "audio", "--frames", "1"], env=env)
        assert first.returncode == 0, first.stderr
        assert hub.pair_call_count == 1

        # A cached token (written by the first Pair) skips pairing entirely.
        cached = _run_cli(["--mode", "audio", "--frames", "1"], env=env)
        assert cached.returncode == 0, cached.stderr
        assert hub.pair_call_count == 1, "a usable cache must not Pair again"
        assert f"pair-ok device={DEVICE_ID}" not in cached.stdout.splitlines()

        # --no-cache ignores that cache and pairs fresh.
        forced = _run_cli(
            ["--no-cache", "--pin", PIN, "--mode", "audio", "--frames", "1"],
            env=env,
        )
        assert forced.returncode == 0, forced.stderr
        assert hub.pair_call_count == 2, "--no-cache must force a fresh Pair"
        assert f"pair-ok device={DEVICE_ID}" in forced.stdout.splitlines()


def test_non_tty_without_pin_or_hub_errors_instead_of_hanging(tmp_path) -> None:
    """No PIN, no hub, non-TTY stdin: a clear error and exit 1 (never a hang)."""
    env = _cli_env(target=None, config_home=str(tmp_path))
    # DEVNULL stdin is not a TTY; subprocess timeout turns a hang into a failure.
    result = _run_cli(["--mode", "audio", "--frames", "1"], env=env)
    assert result.returncode == 1, result.stderr
    assert result.stdout.splitlines()[0].startswith("error: ")


def test_non_tty_without_pin_but_with_hub_errors_locally(tmp_path) -> None:
    """With a hub but no PIN/cache and no TTY, fail fast WITHOUT calling Pair."""
    hub = FakeHub()
    with RunningHub(hub) as target:
        env = _cli_env(target, str(tmp_path))
        result = _run_cli(["--mode", "audio", "--frames", "1"], env=env)

    assert result.returncode == 1, result.stderr
    assert result.stdout.splitlines()[0].startswith("error: ")
    assert hub.pair_call_count == 0, "must refuse locally, never guess a PIN"


def test_refused_token_prints_re_pair_requested(tmp_path) -> None:
    """A stale/revoked token is a normal outcome: ``re-pair-requested``, exit 2."""
    store = TokenStore.default({"XDG_CONFIG_HOME": str(tmp_path)})
    store.save(Credentials(token=TOKEN, device_id=DEVICE_ID))
    hub = FakeHub(reject_token=True)
    with RunningHub(hub) as target:
        env = _cli_env(target, str(tmp_path))
        result = _run_cli(["--mode", "audio", "--frames", "1"], env=env)

    assert result.returncode == 2, result.stderr
    assert "re-pair-requested" in result.stdout.splitlines()
    assert hub.pair_call_count == 0


def test_cache_seeded_token_is_reused(tmp_path) -> None:
    """A 0600 cache written directly is honoured by a later run."""
    store = TokenStore.default({"XDG_CONFIG_HOME": str(tmp_path)})
    store.save(Credentials(token=TOKEN, device_id=DEVICE_ID))
    hub = FakeHub()
    with RunningHub(hub) as target:
        env = _cli_env(target, str(tmp_path))
        result = _run_cli(["--mode", "audio", "--frames", "1"], env=env)

    assert result.returncode == 0, result.stderr
    assert hub.pair_call_count == 0
    assert "pair-ok" not in result.stdout


# --- Video pump: None is "not ready", not "exhausted" (bug M2) ---------------


class _RecordingVideoClient:
    """Captures the ``StreamFrame``s ``_run_video`` sends."""

    def __init__(self) -> None:
        self.frames: list[object] = []

    async def send(self, frame: object) -> None:
        self.frames.append(frame)


class _GapThenNalsVideoSource:
    """A camera-like source: ``read_nal`` returns ``None`` for a few polls.

    Mirrors a real ``Picamera2VideoSource``, where ``None`` is the normal "no NAL
    queued yet" state. Once the gaps pass it yields its NALs, then reports
    ``done`` so a bounded pump can terminate.
    """

    def __init__(self, nals: list[bytes], gaps: int = 2) -> None:
        self._nals = list(nals)
        self._gaps = gaps
        self._index = 0

    @property
    def done(self) -> bool:
        return self._gaps <= 0 and self._index >= len(self._nals)

    def open(self) -> bool:
        return True

    def read_nal(self) -> bytes | None:
        if self._gaps > 0:
            self._gaps -= 1
            return None
        if self._index < len(self._nals):
            nal = self._nals[self._index]
            self._index += 1
            return nal
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


def test_video_pump_does_not_stop_on_an_empty_read_when_frames_unbounded() -> None:
    """Bug M2: an initial ``None`` under ``--frames 0`` must NOT end the pump."""
    nals = [camera.synthetic_video_nal(0), camera.synthetic_video_nal(1)]
    source = _GapThenNalsVideoSource(nals, gaps=3)

    sent, frames = _run_video_pump(source, max_frames=None)

    assert sent == 2, "an empty read must not abort an unbounded video pump"
    assert [frame.video_h264_nal for frame in frames] == nals


def test_video_pump_terminates_when_a_bounded_source_is_done() -> None:
    """A capped synthetic source still terminates an unbounded pump."""
    source = camera.SyntheticVideoSource(frame_count=3)

    sent, frames = _run_video_pump(source, max_frames=None)

    assert sent == 3
    assert [frame.video_h264_nal for frame in frames] == [
        camera.synthetic_video_nal(0),
        camera.synthetic_video_nal(1),
        camera.synthetic_video_nal(2),
    ]


def test_video_pump_honours_an_explicit_frame_cap() -> None:
    source = camera.SyntheticVideoSource()
    sent, _frames = _run_video_pump(source, max_frames=2)
    assert sent == 2


# --- The frozen stdout contract is referenced, not dead documentation --------


def test_stdout_contract_is_the_six_frozen_lines() -> None:
    """``STDOUT_CONTRACT`` pins exactly the six documented tokens."""
    assert cli.STDOUT_CONTRACT == (
        "pair-ok device=<id>",
        "heartbeat-ok",
        "transcript <text>",
        "pair-rejected reason=<r>",
        "re-pair-requested",
        "error: <detail>",
    )


if __name__ == "__main__":  # pragma: no cover - manual invocation
    pytest.main([__file__])
