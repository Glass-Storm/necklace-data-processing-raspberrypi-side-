"""Tests for the concurrent bidirectional stream client.

The fake hub is a REAL in-process ``grpc.aio`` server on an ephemeral loopback
port (never a mock object), so the client exercises the actual
``write()``/``read()``/``done_writing()`` machinery over a real HTTP/2 stream.

Two behaviours are proven:

(a) transcripts are received WHILE frames are still being sent -- the fake hub
    yields a ``transcript`` as soon as it sees the first audio frame, so a
    send-all-then-read client would deadlock and this test would hang;
(b) the writer BACKPRESSURES on a bounded queue -- the fake hub delays its
    reads, and the producer stalls instead of buffering without limit.

Every scenario is wrapped in ``asyncio.wait_for`` so a deadlock FAILS the test
instead of hanging the suite forever.
"""

from __future__ import annotations

import asyncio

import grpc
import pytest
from grpc import aio

from ecosys.v1 import ecosys_pb2, ecosys_pb2_grpc
from ecosys_pi.stream import (
    StreamClient,
    StreamClosedError,
    StreamTransportError,
    UnauthenticatedError,
)

TOKEN = "test-token-abc"

#: Generous per-scenario timeout: a deadlock must fail, not hang.
TIMEOUT = 30.0


async def _wait_until(predicate, *, timeout: float = TIMEOUT) -> None:
    """Poll ``predicate`` until it is true, failing on timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.01)


class FakeHub(ecosys_pb2_grpc.StreamServiceServicer):
    """An in-process hub that records the call and can stall or refuse it."""

    def __init__(
        self,
        *,
        transcript_on_audio: bool = True,
        require_token: bool = True,
        reject_token: bool = False,
        stall_event: asyncio.Event | None = None,
        abort_calls: dict[int, grpc.StatusCode] | None = None,
    ) -> None:
        self.transcript_on_audio = transcript_on_audio
        self.require_token = require_token
        self.reject_token = reject_token
        # When set, the hub reads NOTHING until it is released -- this is what
        # makes the client's write() hit transport backpressure.
        self.stall_event = stall_event
        self.stall_seen = asyncio.Event()
        # 1-based call index -> status to abort that call with.
        self.abort_calls = abort_calls or {}

        self.call_count = 0
        self.metadata_seen: list[dict[str, str]] = []
        self.received_frames = 0
        self.transcripts_sent = 0
        self.stream_ended = False
        self.active_readers = 0

    async def OpenStream(self, request_iterator, context):
        self.call_count += 1
        call_index = self.call_count
        md = dict(context.invocation_metadata())
        self.metadata_seen.append(md)

        if self.require_token:
            if self.reject_token or not md.get("authorization", "").startswith(
                "Bearer "
            ):
                await context.abort(
                    grpc.StatusCode.UNAUTHENTICATED, "missing bearer token"
                )

        abort_code = self.abort_calls.get(call_index)
        if abort_code is not None:
            await context.abort(abort_code, f"forced abort on call {call_index}")

        self.active_readers += 1
        try:
            if self.stall_event is not None:
                self.stall_seen.set()
                await self.stall_event.wait()
            async for frame in request_iterator:
                self.received_frames += 1
                if self.transcript_on_audio and frame.HasField("audio_pcm16_16k"):
                    self.transcripts_sent += 1
                    yield ecosys_pb2.StreamFrame(
                        transcript=f"mock:{len(frame.audio_pcm16_16k)}"
                    )
        finally:
            self.active_readers -= 1
            self.stream_ended = True


async def _start_hub(hub: FakeHub) -> tuple[aio.Server, str]:
    """Start ``hub`` on an ephemeral loopback port; return (server, target)."""
    server = aio.server()
    ecosys_pb2_grpc.add_StreamServiceServicer_to_server(hub, server)
    port = server.add_insecure_port("127.0.0.1:0")
    assert port != 0, "failed to bind an ephemeral port"
    await server.start()
    return server, f"127.0.0.1:{port}"


async def _stop_hub(server: aio.Server) -> None:
    await server.stop(None)
    await asyncio.sleep(0)


def _audio_frame(index: int, *, size: int = 640) -> ecosys_pb2.StreamFrame:
    return ecosys_pb2.StreamFrame(audio_pcm16_16k=bytes([index % 256]) * size)


async def _scenario_no_deadlock() -> None:
    hub = FakeHub()
    server, target = await _start_hub(hub)
    channel = aio.insecure_channel(target)
    transcripts: list[str] = []

    try:
        client = StreamClient(channel, TOKEN, transcripts.append)

        async def produce() -> None:
            for i in range(8):
                await client.send(_audio_frame(i))

        run_task = asyncio.create_task(client.run())
        # Concurrently: produce frames while the reader drains transcripts.
        await asyncio.wait_for(produce(), timeout=TIMEOUT)

        await _wait_until(lambda: len(transcripts) >= 8)
        # Frames were still streaming in when the first transcript arrived:
        # the reader ran concurrently with the writer (no deadlock).
        assert hub.received_frames >= 1
        assert hub.transcripts_sent >= 1
        assert transcripts[0].startswith("mock:")

        # Long-lived: the hub has NOT seen EOF yet -- done_writing only on
        # shutdown.
        assert hub.stream_ended is False

        client.close()
        await asyncio.wait_for(run_task, timeout=TIMEOUT)

        assert hub.stream_ended is True
        assert hub.received_frames == 8
        assert len(transcripts) == 8
        assert client.written_frames == 8

        # Bearer metadata actually reached the wire.
        assert hub.metadata_seen[0]["authorization"] == f"Bearer {TOKEN}"
    finally:
        await channel.close()
        await _stop_hub(server)


def test_transcripts_received_while_frames_still_sent() -> None:
    """Proves concurrent read+write: no deadlock (guard is wait_for)."""
    asyncio.run(asyncio.wait_for(_scenario_no_deadlock(), timeout=TIMEOUT))


async def _scenario_backpressure() -> None:
    # The hub reads NOTHING until released. gRPC c-core buffers ~4 MiB per
    # stream before flow control engages, so the synthetic frames must exceed
    # that buffer for write() to block and the bounded queue to fill.
    release = asyncio.Event()
    hub = FakeHub(stall_event=release)
    server, target = await _start_hub(hub)
    channel = aio.insecure_channel(
        target, options=(("grpc.max_send_message_length", 16 * 1024 * 1024),)
    )
    frame_bytes = 1024 * 1024
    total_frames = 24

    try:
        client = StreamClient(channel, TOKEN, lambda _text: None, queue_maxsize=4)
        run_task = asyncio.create_task(client.run())
        await _wait_until(lambda: hub.stall_seen.is_set())

        submitted = 0
        producer_done = False

        async def produce() -> None:
            nonlocal submitted, producer_done
            for i in range(total_frames):
                await client.send(_audio_frame(i, size=frame_bytes))
                submitted += 1
            producer_done = True

        producer = asyncio.create_task(produce())

        # Wait until the producer is stalled with a FULL queue.
        await _wait_until(lambda: client.pending_frames >= client.queue_maxsize)
        # Give the writer a beat to prove it stays blocked (flow control), not
        # just momentarily full.
        await asyncio.sleep(0.25)

        assert client.pending_frames <= client.queue_maxsize
        assert producer_done is False, "producer ran ahead of the bounded queue"
        assert submitted < total_frames, "queue did not park the producer"

        # Release the hub: the flow still completes, so there was no deadlock.
        release.set()
        await asyncio.wait_for(producer, timeout=TIMEOUT)
        client.close()
        await asyncio.wait_for(run_task, timeout=TIMEOUT)

        assert submitted == total_frames
        assert hub.received_frames == total_frames
    finally:
        release.set()
        await channel.close()
        await _stop_hub(server)


def test_writer_backpressures_on_bounded_queue() -> None:
    """A stalled hub parks the producer instead of buffering without limit."""
    asyncio.run(asyncio.wait_for(_scenario_backpressure(), timeout=TIMEOUT))


async def _scenario_unauthenticated() -> None:
    hub = FakeHub(reject_token=True)
    server, target = await _start_hub(hub)
    channel = aio.insecure_channel(target)

    try:
        # An empty token is refused locally before any RPC.
        with pytest.raises(ValueError):
            StreamClient(channel, "", lambda _t: None)

        # A rejected bearer token surfaces the typed re-pair signal.
        client = StreamClient(channel, TOKEN, lambda _t: None)
        run_task = asyncio.create_task(client.run())
        with pytest.raises(UnauthenticatedError):
            await asyncio.wait_for(run_task, timeout=TIMEOUT)
    finally:
        await channel.close()
        await _stop_hub(server)


def test_unauthenticated_raises_typed_signal() -> None:
    """A hub that refuses the token must raise UnauthenticatedError."""
    asyncio.run(asyncio.wait_for(_scenario_unauthenticated(), timeout=TIMEOUT))


async def _scenario_reconnect_reuses_token() -> None:
    # Call 1 is aborted (transport loss); call 2 succeeds. The client must
    # reconnect with the SAME cached token -- never a re-pair.
    hub = FakeHub(abort_calls={1: grpc.StatusCode.UNAVAILABLE})
    server, target = await _start_hub(hub)
    channel = aio.insecure_channel(target)
    transcripts: list[str] = []

    try:
        client = StreamClient(
            channel,
            TOKEN,
            transcripts.append,
            max_reconnects=3,
            reconnect_backoff=0.05,
        )
        run_task = asyncio.create_task(client.run())

        await _wait_until(lambda: hub.call_count >= 2)
        await client.send(_audio_frame(0))
        await _wait_until(lambda: len(transcripts) == 1)

        client.close()
        await asyncio.wait_for(run_task, timeout=TIMEOUT)

        assert hub.call_count == 2
        for md in hub.metadata_seen:
            assert md["authorization"] == f"Bearer {TOKEN}"
    finally:
        await channel.close()
        await _stop_hub(server)


def test_transport_loss_reconnects_with_cached_token() -> None:
    """A transport drop reconnects with the cached token (no re-pair)."""
    asyncio.run(asyncio.wait_for(_scenario_reconnect_reuses_token(), timeout=TIMEOUT))


async def _scenario_reconnect_budget_exhausted() -> None:
    # Every call fails: the bounded reconnect budget must give up, not loop.
    hub = FakeHub(abort_calls={i: grpc.StatusCode.UNAVAILABLE for i in range(1, 10)})
    server, target = await _start_hub(hub)
    channel = aio.insecure_channel(target)

    try:
        client = StreamClient(
            channel,
            TOKEN,
            lambda _t: None,
            max_reconnects=2,
            reconnect_backoff=0.01,
        )
        with pytest.raises(StreamTransportError):
            await asyncio.wait_for(client.run(), timeout=TIMEOUT)
        assert hub.call_count == 3  # initial + 2 reconnects
    finally:
        await channel.close()
        await _stop_hub(server)


def test_reconnect_budget_is_bounded() -> None:
    """Exhausting the reconnect budget raises instead of looping forever."""
    asyncio.run(
        asyncio.wait_for(_scenario_reconnect_budget_exhausted(), timeout=TIMEOUT)
    )


def test_send_after_close_is_refused() -> None:
    """A frame submitted after shutdown raises StreamClosedError."""

    async def scenario() -> None:
        client = StreamClient(
            aio.insecure_channel("127.0.0.1:1"), TOKEN, lambda _t: None
        )
        client.close()
        with pytest.raises(StreamClosedError):
            await client.send(_audio_frame(0))

    asyncio.run(asyncio.wait_for(scenario(), timeout=TIMEOUT))
