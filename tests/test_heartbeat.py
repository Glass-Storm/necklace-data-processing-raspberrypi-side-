"""Tests for the periodic ``PairingService/Heartbeat`` client.

Every test uses a REAL in-process ``grpc.aio`` hub on an ephemeral loopback
port -- the same posture as ``tests/test_stream.py`` -- so the client exercises
actual unary calls and metadata over HTTP/2, never a mocked stub.

The behaviours proven here:

* the ``Bearer`` token actually reaches the wire as ``authorization`` metadata
  and the request carries the paired ``device_id``;
* a hub answering ``UNAUTHENTICATED`` raises the ONE shared re-pair signal
  (:class:`~ecosys_pi.stream.UnauthenticatedError`) and is NEVER retried;
* transient failures RETRY a bounded number of times, then log and CONTINUE
  (they never escape :meth:`HeartbeatClient.run`);
* the cadence is fully injectable (clock + sleep) and asserted exactly;
* a failing heartbeat does NOT block the media stream (independent tasks).
"""

from __future__ import annotations

import asyncio

import grpc
import pytest
from grpc import aio

from ecosys.v1 import ecosys_pb2, ecosys_pb2_grpc
from ecosys_pi.heartbeat import (
    DEFAULT_INTERVAL_S,
    HeartbeatClient,
    UnauthenticatedError,
)
from ecosys_pi.stream import StreamClient

TOKEN = "test-token-abc"
DEVICE_ID = "pi-device-42"
TIMEOUT = 30.0
NOOP_BACKOFF = 0.0


class FakeClock:
    """Deterministic monotonic clock + sleep that only advances time.

    The heartbeat loop schedules on absolute ticks, so this lets a test run
    several intervals instantly while still proving the 30 s cadence.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakePairingHub(ecosys_pb2_grpc.PairingServiceServicer):
    """Records every heartbeat call; can abort by index, refuse, or reject."""

    def __init__(
        self,
        *,
        abort_codes: dict[int, grpc.StatusCode] | None = None,
        reject_token: bool = False,
        ok: bool = True,
        server_time_ms: int = 1_700_000_000_000,
    ) -> None:
        self.abort_codes = abort_codes or {}
        self.reject_token = reject_token
        self.ok = ok
        self.server_time_ms = server_time_ms

        self.call_count = 0
        self.metadata_seen: list[dict[str, str]] = []
        self.requests: list[ecosys_pb2.HeartbeatRequest] = []

    async def Heartbeat(self, request, context):  # noqa: N802 (grpc naming)
        self.call_count += 1
        self.metadata_seen.append(dict(context.invocation_metadata()))
        self.requests.append(request)

        if self.reject_token:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "token revoked")
        abort_code = self.abort_codes.get(self.call_count)
        if abort_code is not None:
            await context.abort(abort_code, f"forced abort on call {self.call_count}")
        return ecosys_pb2.HeartbeatResponse(
            ok=self.ok, server_time_ms=self.server_time_ms
        )


async def _start_hub(*servicer_hubs) -> tuple[aio.Server, str]:
    """Register 1+ ``(servicer, add_to_server)`` pairs on one ephemeral server."""
    server = aio.server()
    for servicer, registrar in servicer_hubs:
        registrar(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    assert port != 0, "failed to bind an ephemeral port"
    await server.start()
    return server, f"127.0.0.1:{port}"


async def _stop_hub(server: aio.Server) -> None:
    await server.stop(None)
    await asyncio.sleep(0)


async def _pairing_hub(**kwargs) -> tuple[FakePairingHub, aio.Server, str]:
    hub = FakePairingHub(**kwargs)
    server, target = await _start_hub(
        (hub, ecosys_pb2_grpc.add_PairingServiceServicer_to_server)
    )
    return hub, server, target


# --- Constants / construction -------------------------------------------------


def test_default_interval_is_30_seconds() -> None:
    """The documented default cadence is 30 s."""
    assert DEFAULT_INTERVAL_S == 30.0


def test_missing_credentials_are_refused_locally() -> None:
    """An empty token or device_id cannot produce a valid heartbeat."""
    channel = aio.insecure_channel("127.0.0.1:1")
    try:
        with pytest.raises(ValueError):
            HeartbeatClient(channel, "", DEVICE_ID)
        with pytest.raises(ValueError):
            HeartbeatClient(channel, TOKEN, "")
        with pytest.raises(ValueError):
            HeartbeatClient(channel, TOKEN, DEVICE_ID, interval=0)
    finally:
        asyncio.run(channel.close())


# --- Wire shape ---------------------------------------------------------------


async def _scenario_bearer_header() -> None:
    hub, server, target = await _pairing_hub()
    channel = aio.insecure_channel(target)
    try:
        client = HeartbeatClient(channel, TOKEN, DEVICE_ID, max_beats=1)
        await asyncio.wait_for(client.run(), timeout=TIMEOUT)

        assert hub.call_count == 1
        # The bearer token is on the wire, not just in the client.
        assert hub.metadata_seen[0]["authorization"] == f"Bearer {TOKEN}"
        # The request carries the paired device_id (the contract field).
        assert hub.requests[0].device_id == DEVICE_ID
        assert client.beats_sent == 1
        assert client.last_server_time_ms == hub.server_time_ms
    finally:
        await channel.close()
        await _stop_hub(server)


def test_bearer_header_and_device_id_reach_the_wire() -> None:
    asyncio.run(asyncio.wait_for(_scenario_bearer_header(), timeout=TIMEOUT))


# --- Authentication -----------------------------------------------------------


async def _scenario_unauthenticated() -> None:
    hub, server, target = await _pairing_hub(reject_token=True)
    channel = aio.insecure_channel(target)
    try:
        client = HeartbeatClient(channel, TOKEN, DEVICE_ID, max_beats=3)
        # The re-pair signal escapes run() -- it is not swallowed like a
        # transient failure.
        with pytest.raises(UnauthenticatedError):
            await asyncio.wait_for(client.run(), timeout=TIMEOUT)
        # ...and it is NEVER retried.
        assert hub.call_count == 1
        assert client.stopped is True
        assert client.beats_sent == 0
    finally:
        await channel.close()
        await _stop_hub(server)


def test_unauthenticated_raises_the_repair_signal() -> None:
    asyncio.run(asyncio.wait_for(_scenario_unauthenticated(), timeout=TIMEOUT))


# --- Transient failures: bounded retry, then log-and-continue -----------------


async def _scenario_retry_then_succeed() -> None:
    hub, server, target = await _pairing_hub(
        abort_codes={1: grpc.StatusCode.UNAVAILABLE}
    )
    channel = aio.insecure_channel(target)
    try:
        client = HeartbeatClient(
            channel,
            TOKEN,
            DEVICE_ID,
            max_retries=2,
            backoff_sleep=_noop_sleep,
            max_beats=1,
        )
        await asyncio.wait_for(client.run(), timeout=TIMEOUT)
        # One transient failure retried WITHIN the same beat, then success.
        assert hub.call_count == 2
        assert client.beats_sent == 1
        assert client.beats_attempted == 1
        assert client.consecutive_failures == 0
    finally:
        await channel.close()
        await _stop_hub(server)


def test_transient_error_retries_then_succeeds_within_one_beat() -> None:
    asyncio.run(asyncio.wait_for(_scenario_retry_then_succeed(), timeout=TIMEOUT))


async def _scenario_bounded_retry_exhausted() -> None:
    # Every attempt fails: the beat gives up after max_retries+1 calls and
    # run() CONTINUES (returns normally on max_beats) instead of raising.
    hub, server, target = await _pairing_hub(
        abort_codes={i: grpc.StatusCode.UNAVAILABLE for i in range(1, 20)}
    )
    channel = aio.insecure_channel(target)
    clock = FakeClock()
    try:
        client = HeartbeatClient(
            channel,
            TOKEN,
            DEVICE_ID,
            interval=30.0,
            clock=clock.clock,
            sleep=clock.sleep,
            max_retries=2,
            backoff_sleep=_noop_sleep,
            max_beats=2,
        )
        await asyncio.wait_for(client.run(), timeout=TIMEOUT)

        # Bounded: 3 attempts per beat (max_retries + 1), 2 beats -> 6 calls.
        assert hub.call_count == 6
        assert client.beats_attempted == 2
        assert client.beats_sent == 0
        assert client.consecutive_failures == 2
    finally:
        await channel.close()
        await _stop_hub(server)


def test_bounded_retry_exhaustion_logs_and_continues() -> None:
    asyncio.run(asyncio.wait_for(_scenario_bounded_retry_exhausted(), timeout=TIMEOUT))


async def _scenario_hub_refuses_ok_false() -> None:
    # A transport-successful but ok=false reply is a refused beat: no raise.
    hub, server, target = await _pairing_hub(ok=False)
    channel = aio.insecure_channel(target)
    try:
        client = HeartbeatClient(channel, TOKEN, DEVICE_ID, max_beats=1)
        await asyncio.wait_for(client.run(), timeout=TIMEOUT)
        assert hub.call_count == 1
        assert client.beats_sent == 0
        assert client.consecutive_failures == 1
    finally:
        await channel.close()
        await _stop_hub(server)


def test_ok_false_is_logged_not_raised() -> None:
    asyncio.run(asyncio.wait_for(_scenario_hub_refuses_ok_false(), timeout=TIMEOUT))


# --- Cadence ------------------------------------------------------------------


async def _scenario_cadence_is_injectable() -> None:
    hub, server, target = await _pairing_hub()
    channel = aio.insecure_channel(target)
    clock = FakeClock()
    try:
        client = HeartbeatClient(
            channel,
            TOKEN,
            DEVICE_ID,
            interval=30.0,
            clock=clock.clock,
            sleep=clock.sleep,
            max_beats=3,
        )
        assert client.interval == 30.0
        await asyncio.wait_for(client.run(), timeout=TIMEOUT)

        # Three beats, two inter-beat sleeps, each exactly the interval, and
        # the (fake) clock advanced by 60 s -- no real waiting happened.
        assert hub.call_count == 3
        assert clock.sleeps == [30.0, 30.0]
        assert clock.now == 60.0
        assert client.beats_attempted == 3
        assert client.beats_sent == 3
    finally:
        await channel.close()
        await _stop_hub(server)


def test_cadence_is_injectable_and_asserted() -> None:
    asyncio.run(asyncio.wait_for(_scenario_cadence_is_injectable(), timeout=TIMEOUT))


async def _scenario_stop_ends_the_loop() -> None:
    hub, server, target = await _pairing_hub()
    channel = aio.insecure_channel(target)
    clock = FakeClock()
    release = asyncio.Event()

    async def gated_sleep(seconds: float) -> None:
        # Simulates a long interval: the loop parks here until stop() wakes it.
        clock.sleeps.append(seconds)
        await release.wait()

    try:
        client = HeartbeatClient(
            channel,
            TOKEN,
            DEVICE_ID,
            interval=30.0,
            clock=clock.clock,
            sleep=gated_sleep,
        )
        run_task = asyncio.create_task(client.run())
        # First beat, then the loop parks in the injected interval.
        for _ in range(100):
            if hub.call_count >= 1:
                break
            await asyncio.sleep(0.01)
        client.stop()
        release.set()
        await asyncio.wait_for(run_task, timeout=TIMEOUT)

        assert client.stopped is True
        assert hub.call_count == 1
    finally:
        await channel.close()
        await _stop_hub(server)


def test_stop_ends_the_loop_at_the_next_boundary() -> None:
    asyncio.run(asyncio.wait_for(_scenario_stop_ends_the_loop(), timeout=TIMEOUT))


# --- Independence from the media stream --------------------------------------


class FakeStreamHub(ecosys_pb2_grpc.StreamServiceServicer):
    """Minimal hub that yields one transcript per audio frame."""

    def __init__(self) -> None:
        self.received_frames = 0
        self.end_seen = False

    async def OpenStream(self, request_iterator, context):  # noqa: N802
        try:
            async for frame in request_iterator:
                self.received_frames += 1
                if frame.HasField("audio_pcm16_16k"):
                    yield ecosys_pb2.StreamFrame(transcript="mock:ok")
        finally:
            self.end_seen = True


async def _scenario_heartbeat_does_not_block_stream() -> None:
    # Heartbeats are REFUSED (ok=false) while a media stream runs on the SAME
    # channel. The stream must be wholly unaffected: a missed beat never kills
    # the stream (the hub gates nothing on liveness).
    pairing = FakePairingHub(ok=False)
    stream_hub = FakeStreamHub()
    server, target = await _start_hub(
        (pairing, ecosys_pb2_grpc.add_PairingServiceServicer_to_server),
        (stream_hub, ecosys_pb2_grpc.add_StreamServiceServicer_to_server),
    )
    channel = aio.insecure_channel(target)
    transcripts: list[str] = []

    try:
        stream = StreamClient(channel, TOKEN, transcripts.append, queue_maxsize=8)
        beat = HeartbeatClient(
            channel,
            TOKEN,
            DEVICE_ID,
            interval=0.01,
            max_retries=0,
            max_beats=100,
        )
        stream_task = asyncio.create_task(stream.run())
        beat_task = asyncio.create_task(beat.run())

        try:
            for i in range(8):
                await stream.send(
                    ecosys_pb2.StreamFrame(audio_pcm16_16k=bytes([i]) * 640)
                )
            for _ in range(200):
                if len(transcripts) >= 8:
                    break
                await asyncio.sleep(0.01)

            # The stream is alive despite every heartbeat being refused.
            assert transcripts[:3] == ["mock:ok"] * 3
            assert beat.beats_sent == 0
            assert stream.closed is False
        finally:
            beat.stop()
            stream.close()
            await asyncio.wait_for(beat_task, timeout=TIMEOUT)
            await asyncio.wait_for(stream_task, timeout=TIMEOUT)

        assert len(transcripts) == 8
        assert stream_hub.received_frames == 8
        assert stream_hub.end_seen is True
    finally:
        await channel.close()
        await _stop_hub(server)


def test_heartbeat_failures_do_not_block_the_stream() -> None:
    asyncio.run(
        asyncio.wait_for(_scenario_heartbeat_does_not_block_stream(), timeout=TIMEOUT)
    )


async def _noop_sleep(_seconds: float) -> None:
    """Instant backoff for tests that do not assert cadence."""
    return None
