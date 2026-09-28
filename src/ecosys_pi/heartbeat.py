"""Periodic ``PairingService/Heartbeat`` client (bookkeeping, never gating).

The hub does **NOT** gate anything on liveness and has **no idle timeout**
(``docs/PROTOCOL_DECISIONS.md``): a heartbeat is bookkeeping that lets the hub
record when a paired peer was last seen. The consequence is a hard rule:

    A missed beat must NEVER kill the media stream.

So this client runs as its OWN dedicated asyncio task, completely independent of
:class:`ecosys_pi.stream.StreamClient`. It sends one heartbeat every
:data:`DEFAULT_INTERVAL_S` (30 s) and, on any non-authentication failure, logs
it and keeps going. It never touches the stream, so a transient hub hiccup
delays the next beat at worst and the stream is unaffected.

Authentication is the ONE exception: when the hub answers ``UNAUTHENTICATED``
the cached bearer token has been revoked and the peer MUST re-pair. That is the
typed :class:`~ecosys_pi.stream.UnauthenticatedError` signal (the SAME signal
the stream client raises -- there is deliberately one re-pair signal, not two),
which is reraised out of :meth:`HeartbeatClient.run` so the supervisor can act.

Cadence is INJECTABLE: pass ``clock`` and ``sleep`` (defaults: real monotonic
clock and :func:`asyncio.sleep`) so tests assert the schedule without waiting.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Final

import grpc
from grpc import aio

from ecosys.v1 import ecosys_pb2, ecosys_pb2_grpc

# The single shared re-pair signal lives with the stream client; re-exported
# here so callers of either subsystem import ONE type.
from ecosys_pi.stream import AUTHORIZATION_HEADER, UnauthenticatedError

__all__ = [
    "AUTHORIZATION_HEADER",
    "DEFAULT_INTERVAL_S",
    "DEFAULT_CALL_TIMEOUT_S",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_RETRY_BACKOFF_S",
    "HeartbeatClient",
    "UnauthenticatedError",
]

_LOG = logging.getLogger(__name__)

#: Seconds between heartbeats. The hub has no idle timeout, so this is purely
#: bookkeeping; 30 s matches the mockpeer's liveness cadence.
DEFAULT_INTERVAL_S: Final = 30.0
#: Per-RPC deadline. Short: a heartbeat must not pile up behind a dead hub.
DEFAULT_CALL_TIMEOUT_S: Final = 10.0
#: Transient failures retried WITHIN one beat before the beat is counted failed.
DEFAULT_MAX_RETRIES: Final = 3
#: Base seconds for the linear retry backoff (multiplied by the attempt).
DEFAULT_RETRY_BACKOFF_S: Final = 1.0

#: What a cadence scheduler returns; both are injectable for tests.
Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class HeartbeatClient:
    """Sends one heartbeat per :attr:`interval` on a dedicated task.

    Typical use::

        beat = HeartbeatClient(channel, token, device_id)
        task = asyncio.create_task(beat.run())
        try:
            await stream_client.run()
        except UnauthenticatedError:
            ...
        finally:
            beat.stop()
            await task

    :meth:`run` returns only on :meth:`stop`, on ``UNAUTHENTICATED`` (which
    raises), or after ``max_beats`` beats (used by tests). Transient failures
    never raise and never stop the loop.
    """

    def __init__(
        self,
        channel: aio.Channel,
        token: str,
        device_id: str,
        *,
        interval: float = DEFAULT_INTERVAL_S,
        timeout: float = DEFAULT_CALL_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF_S,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
        backoff_sleep: Sleeper | None = None,
        max_beats: int | None = None,
    ) -> None:
        if not token:
            raise ValueError("token must be non-empty; pair before beating")
        if not device_id:
            raise ValueError("device_id must be non-empty; it is the request field")
        if interval <= 0:
            raise ValueError("interval must be > 0")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if max_beats is not None and max_beats < 1:
            raise ValueError("max_beats must be >= 1 when set")

        self._channel = channel
        self._token = token
        self._device_id = device_id
        self._interval = float(interval)
        self._timeout = float(timeout)
        self._max_retries = max_retries
        self._retry_backoff = float(retry_backoff)
        self._clock = clock
        self._sleep = sleep
        # Retry backoff uses REAL time by default so it never distorts cadence.
        self._backoff_sleep = backoff_sleep if backoff_sleep is not None else sleep
        self._max_beats = max_beats

        self._stop = asyncio.Event()
        self._running = False
        self._beats_attempted = 0
        self._beats_sent = 0
        self._consecutive_failures = 0
        self._last_server_time_ms = 0

    # -- observability ---------------------------------------------------

    @property
    def interval(self) -> float:
        """The configured (injectable) seconds between heartbeats."""
        return self._interval

    @property
    def beats_attempted(self) -> int:
        """Loop iterations started (success or failure)."""
        return self._beats_attempted

    @property
    def beats_sent(self) -> int:
        """Heartbeats the hub accepted with ``ok=true``."""
        return self._beats_sent

    @property
    def consecutive_failures(self) -> int:
        """Failures since the last accepted heartbeat."""
        return self._consecutive_failures

    @property
    def last_server_time_ms(self) -> int:
        """``server_time_ms`` from the most recent accepted heartbeat."""
        return self._last_server_time_ms

    @property
    def stopped(self) -> bool:
        """Whether :meth:`stop` has been called."""
        return self._stop.is_set()

    # -- lifecycle -------------------------------------------------------

    def stop(self) -> None:
        """Ask :meth:`run` to return at the next interval boundary."""
        self._stop.set()

    async def run(self) -> None:
        """Beat until stopped, ``max_beats`` reached, or the token is refused.

        Raises:
            UnauthenticatedError: the hub refused the bearer token; the caller
                MUST re-pair. This is the ONLY exception that escapes.
        """
        if self._running:
            raise RuntimeError("HeartbeatClient.run() may only be called once")
        self._running = True

        next_tick = self._clock()
        while not self._stop.is_set():
            self._beats_attempted += 1
            try:
                await self.beat()
            except UnauthenticatedError:
                self._stop.set()
                raise

            if self._max_beats is not None and self._beats_attempted >= self._max_beats:
                return

            next_tick += self._interval
            delay = next_tick - self._clock()
            if delay <= 0:
                # Fell behind (e.g. a long backoff): reschedule from now rather
                # than firing a burst of catch-up beats.
                next_tick = self._clock() + self._interval
                delay = self._interval
            await self._wait_interval(delay)

    async def beat(self) -> bool:
        """Send exactly one heartbeat (with bounded transient retries).

        Returns ``True`` when the hub accepted it (``ok=true``), ``False`` when
        the beat was refused or the transient retry budget was exhausted -- both
        are logged, NEVER raised. Only ``UNAUTHENTICATED`` raises
        :class:`UnauthenticatedError`.
        """
        stub = ecosys_pb2_grpc.PairingServiceStub(self._channel)
        request = ecosys_pb2.HeartbeatRequest(device_id=self._device_id)
        metadata = ((AUTHORIZATION_HEADER, f"Bearer {self._token}"),)

        attempt = 0
        while True:
            try:
                response = await stub.Heartbeat(
                    request, metadata=metadata, timeout=self._timeout
                )
            except aio.AioRpcError as exc:
                if exc.code() == grpc.StatusCode.UNAUTHENTICATED:
                    raise UnauthenticatedError(
                        exc.details()
                        or "hub rejected the bearer token; re-pair required"
                    ) from exc
                attempt += 1
                if attempt > self._max_retries:
                    _LOG.warning(
                        "heartbeat failed after %d attempt(s): %s; continuing",
                        attempt,
                        exc.code().name,
                    )
                    self._record_failure()
                    return False
                _LOG.warning(
                    "heartbeat attempt %d/%d failed (%s); retrying",
                    attempt,
                    self._max_retries + 1,
                    exc.code().name,
                )
                await self._backoff_sleep(self._retry_backoff * attempt)
                continue

            self._last_server_time_ms = response.server_time_ms
            if not response.ok:
                _LOG.warning("hub refused the heartbeat without ok=true; continuing")
                self._record_failure()
                return False
            self._beats_sent += 1
            self._consecutive_failures = 0
            return True

    # -- internals -------------------------------------------------------

    def _record_failure(self) -> None:
        self._consecutive_failures += 1

    async def _wait_interval(self, delay: float) -> None:
        """Sleep ``delay`` seconds, waking early when :meth:`stop` is set."""
        sleeper = asyncio.ensure_future(self._sleep(delay))
        stopper = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, stopper):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleeper, stopper, return_exceptions=True)
