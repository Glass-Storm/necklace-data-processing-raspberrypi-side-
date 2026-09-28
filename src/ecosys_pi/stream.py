"""Concurrent bidirectional stream client for ``StreamService/OpenStream``.

The hub's stream is LONG-LIVED (see ``docs/PROTOCOL_DECISIONS.md``): the Pi keeps
sending audio/video frames and must keep reading hub frames -- exactly one
``transcript`` per 20 ms audio chunk -- at the same time. The two directions are
driven by two concurrent asyncio tasks over a single ``grpc.aio``
stream-stream call:

* a WRITER drains a BOUNDED ``asyncio.Queue`` and ``write()``s each frame. The
  bound is the backpressure valve: when the hub stops reading (its own 64-deep
  audio queue PARKS), the writer stalls on ``write()``, the queue fills, and
  :meth:`StreamClient.send` blocks the media producer instead of buffering
  without limit.
* a READER ``read()``s ``StreamFrame``s and dispatches every ``transcript`` to
  the caller's callback.

Hard rules (frozen protocol):

* the ``__aiter__`` iterator API is NEVER mixed with ``write()`` -- mixing them
  on one call is unsupported;
* ``done_writing()`` is called ONLY on shutdown, never after the first frame;
* a transport drop reconnects with a BOUNDED number of attempts REUSING the
  cached bearer token -- it never re-pairs;
* ``UNAUTHENTICATED`` raises :class:`UnauthenticatedError` (the typed re-pair
  signal) and is never retried.

The caller owns the channel; this client never closes it.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Final

import grpc
from grpc import aio

from ecosys.v1 import ecosys_pb2, ecosys_pb2_grpc

_LOG = logging.getLogger(__name__)

#: Bound on the outbound frame queue. Sized to one hub audio-queue depth (64)
#: so a parked hub backpressures the producer instead of growing RAM.
DEFAULT_QUEUE_MAXSIZE: Final = 64
#: Maximum reconnect attempts after a transport drop before giving up.
DEFAULT_MAX_RECONNECTS: Final = 5
#: Base seconds for the linear reconnect backoff (multiplied by the attempt).
DEFAULT_RECONNECT_BACKOFF: Final = 0.25
#: Seconds to keep draining hub frames after the writer half-closes.
DEFAULT_SHUTDOWN_GRACE: Final = 5.0

AUTHORIZATION_HEADER: Final = "authorization"

#: Wakes a writer parked in ``queue.get()`` at shutdown; never a media frame.
_SHUTDOWN: Final = object()

TranscriptCallback = Callable[[str], Awaitable[None] | None]


class StreamError(Exception):
    """Base class for typed stream failures."""


class UnauthenticatedError(StreamError):
    """The hub refused the bearer token: the caller MUST re-pair."""


class StreamTransportError(StreamError):
    """The stream was lost and could not be re-established (no re-pair)."""


class StreamClosedError(StreamError):
    """A frame was submitted after :meth:`StreamClient.close`."""


class StreamClient:
    """Runs one long-lived ``OpenStream`` call with concurrent read and write.

    Typical use::

        client = StreamClient(channel, token, on_transcript)
        run_task = asyncio.create_task(client.run())
        await client.send(audio_frame)   # backpressures when the hub stalls
        ...
        client.close()                   # half-closes (done_writing) on shutdown
        await run_task
    """

    def __init__(
        self,
        channel: aio.Channel,
        token: str,
        on_transcript: TranscriptCallback,
        *,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        max_reconnects: int = DEFAULT_MAX_RECONNECTS,
        reconnect_backoff: float = DEFAULT_RECONNECT_BACKOFF,
        shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE,
    ) -> None:
        if not token:
            raise ValueError("token must be non-empty; pair before opening the stream")
        if queue_maxsize < 1:
            raise ValueError("queue_maxsize must be >= 1")

        self._channel = channel
        # The token is cached for the whole client lifetime. A transport drop
        # reconnects with THIS token; only UNAUTHENTICATED asks for a re-pair.
        self._token = token
        self._on_transcript = on_transcript
        self._queue: asyncio.Queue[object] = asyncio.Queue(maxsize=queue_maxsize)
        self._shutdown = asyncio.Event()
        self._max_reconnects = max_reconnects
        self._reconnect_backoff = reconnect_backoff
        self._shutdown_grace = shutdown_grace
        self._closed = False
        self._running = False
        self._call: aio.StreamStreamCall | None = None
        self._written_frames = 0

    # -- observability ---------------------------------------------------

    @property
    def queue_maxsize(self) -> int:
        """The bound on the outbound frame queue."""
        return self._queue.maxsize

    @property
    def pending_frames(self) -> int:
        """Frames currently waiting to be written."""
        return self._queue.qsize()

    @property
    def written_frames(self) -> int:
        """Frames successfully handed to ``write()``."""
        return self._written_frames

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has been called."""
        return self._closed

    # -- producer surface ------------------------------------------------

    async def send(self, frame: ecosys_pb2.StreamFrame) -> None:
        """Queue one frame for the writer.

        This is the BACKPRESSURE POINT: when the bounded queue is full (the hub
        has stopped reading), this awaits until the writer drains a slot.
        """
        if self._closed:
            raise StreamClosedError("cannot send after close()")
        await self._queue.put(frame)

    def close(self) -> None:
        """Request a graceful shutdown: the writer calls ``done_writing()``.

        No queued frame is ever dropped: the writer keeps draining until the
        queue is empty, then half-closes the call.
        """
        if self._closed:
            return
        self._closed = True
        self._shutdown.set()
        # Wake a writer parked in ``queue.get()``. If the queue is full the
        # writer is draining and will observe the shutdown event once empty.
        try:
            self._queue.put_nowait(_SHUTDOWN)
        except asyncio.QueueFull:
            pass

    # -- lifecycle -------------------------------------------------------

    async def run(self) -> None:
        """Drive the stream until :meth:`close` or an unrecoverable failure.

        Raises:
            UnauthenticatedError: the hub refused the token (re-pair required).
            StreamTransportError: the stream was lost and the bounded reconnect
                budget was exhausted.
        """
        if self._running:
            raise RuntimeError("StreamClient.run() may only be called once")
        self._running = True

        attempt = 0
        while not self._closed:
            try:
                await self._run_connection()
                if self._closed:
                    return
                failure: StreamError = StreamTransportError(
                    "hub closed the stream unexpectedly"
                )
            except UnauthenticatedError:
                if self._closed:
                    return
                raise
            except StreamTransportError as exc:
                if self._closed:
                    return
                failure = exc

            attempt += 1
            if attempt > self._max_reconnects:
                raise StreamTransportError(
                    "stream lost and not restored after "
                    f"{self._max_reconnects} reconnect(s)"
                ) from failure
            _LOG.warning(
                "stream lost (%s); reconnect %d/%d",
                failure,
                attempt,
                self._max_reconnects,
            )
            await asyncio.sleep(self._reconnect_backoff * attempt)

    def _open_call(self) -> aio.StreamStreamCall:
        """Open one ``OpenStream`` call with the cached bearer token."""
        stub = ecosys_pb2_grpc.StreamServiceStub(self._channel)
        return stub.OpenStream(
            metadata=((AUTHORIZATION_HEADER, f"Bearer {self._token}"),)
        )

    async def _run_connection(self) -> None:
        """Run one connection's writer+reader until one side stops."""
        call = self._open_call()
        self._call = call
        writer = asyncio.create_task(
            self._writer_loop(call), name="ecosys-stream-writer"
        )
        reader = asyncio.create_task(
            self._reader_loop(call), name="ecosys-stream-reader"
        )
        try:
            done, _ = await asyncio.wait(
                {writer, reader}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc

            if reader in done:
                # The hub ended the stream. Stop the writer; queued frames are
                # preserved and re-sent after a reconnect.
                writer.cancel()
                await asyncio.gather(writer, return_exceptions=True)
                return

            # The writer only finishes normally on shutdown (done_writing), so
            # drain any remaining hub frames until EOF, bounded by the grace.
            try:
                await asyncio.wait_for(reader, timeout=self._shutdown_grace)
            except asyncio.TimeoutError:
                _LOG.warning("hub did not close after done_writing(); cancelling")
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
        finally:
            for task in (writer, reader):
                if not task.done():
                    task.cancel()
            await asyncio.gather(writer, reader, return_exceptions=True)
            self._call = None

    async def _writer_loop(self, call: aio.StreamStreamCall) -> None:
        while True:
            try:
                frame = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                if self._shutdown.is_set():
                    await call.done_writing()
                    return
                frame = await self._queue.get()
            if frame is _SHUTDOWN:
                await call.done_writing()
                return
            try:
                await call.write(frame)
            except aio.AioRpcError as exc:
                raise _classify(exc) from exc
            self._written_frames += 1

    async def _reader_loop(self, call: aio.StreamStreamCall) -> None:
        while True:
            try:
                response = await call.read()
            except aio.AioRpcError as exc:
                raise _classify(exc) from exc
            if response is aio.EOF:
                return
            text = response.transcript
            if text:
                await _dispatch(self._on_transcript, text)


def _classify(exc: aio.AioRpcError) -> StreamError:
    """Map a gRPC failure to the typed stream error."""
    if exc.code() == grpc.StatusCode.UNAUTHENTICATED:
        return UnauthenticatedError(
            exc.details() or "hub rejected the bearer token; re-pair required"
        )
    return StreamTransportError(f"{exc.code().name}: {exc.details()}")


async def _dispatch(callback: TranscriptCallback, text: str) -> None:
    """Invoke the transcript callback, awaiting it when it is a coroutine."""
    result = callback(text)
    if inspect.isawaitable(result):
        await result
