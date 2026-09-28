"""The runnable Pi client: config -> pair -> heartbeat -> stream -> media/TTS.

``python -m ecosys_pi`` is the single entry point task 14's JVM E2E harness
drives. Its **stdout is a frozen contract** (see :data:`STDOUT_CONTRACT`): the
harness asserts these exact lines, so informational chatter from the media
modules is redirected to stderr and our own lines are written to the captured
stdout object explicitly.

Flags (the harness uses ONLY these six)
---------------------------------------

=============================  =================================================
``--hub HOST:PORT``            Hub override (mDNS discovery is task 13).
``--pin PIN``                  PIN override; forces ONE non-interactive Pair.
``--mode audio|video|both``    Which media producers run (default ``both``).
``--frames N``                 Frames per medium; ``0`` = run until interrupted.
``--no-cache``                 Skip the token cache and force a fresh Pair.
``--lang yue|zh|en``           Override ``PI_ECOSYS_TTS_LANG``.
=============================  =================================================

Frozen stdout / exit contract
-----------------------------

===============================  ===============================================
``pair-ok device=<id>``          Pair succeeded (``<id>`` from the credentials).
``heartbeat-ok``                 FIRST successful heartbeat.
``transcript <text>``            One received transcript frame.
``pair-rejected reason=<r>``     Hub refused the PIN (typed reason verbatim).
``re-pair-requested``            Token refused (``UNAUTHENTICATED``).
``error: <detail>``              Anything else.
===============================  ===============================================

Exit codes: ``0`` success; ``2`` a normal hub refusal (bad/expired/locked PIN,
or a refused token); ``1`` any other error.

Non-interactive mode
--------------------

When ``--pin`` is supplied OR stdin is not a TTY the client never blocks on
``input()``. With ``--pin`` it makes exactly ONE Pair attempt; a rejection is
printed and the process exits ``2``. A non-interactive run with no PIN and no
cached token fails fast with an ``error:`` line instead of hanging.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from typing import IO, Any, Mapping, Sequence

from ecosys_pi.config import Config, load_config
from ecosys_pi.token_store import Credentials, TokenStore

__all__ = ["EXIT_OK", "EXIT_ERROR", "EXIT_REFUSED", "build_parser", "main"]

_LOG = logging.getLogger("ecosys_pi.cli")

#: Process exited cleanly (media ran, or the client was interrupted).
EXIT_OK = 0
#: Any error that is NOT a normal hub refusal.
EXIT_ERROR = 1
#: A hub refusal that is a normal outcome: bad/expired/locked PIN, refused token.
EXIT_REFUSED = 2

#: The exact, frozen stdout tokens the E2E harness asserts (documentation only).
STDOUT_CONTRACT = (
    "pair-ok device=<id>",
    "heartbeat-ok",
    "transcript <text>",
    "pair-rejected reason=<r>",
    "re-pair-requested",
    "error: <detail>",
)

#: Pace for the video pump: one NAL per nominal capture frame (30 fps).
_VIDEO_FRAME_INTERVAL_S = 1.0 / 30.0

#: How often the heartbeat announcer polls for the first accepted beat.
_BEAT_POLL_S = 0.01

#: Seconds to wait for the first accepted heartbeat to be announced.
_ANNOUNCE_GRACE_S = 2.0


def build_parser() -> argparse.ArgumentParser:
    """Build the frozen six-flag argument parser."""
    parser = argparse.ArgumentParser(
        prog="ecosys-pi",
        description=(
            "Pair with the phone-manager hub and stream camera/microphone media "
            "with local TTS playback."
        ),
    )
    parser.add_argument(
        "--hub",
        metavar="HOST:PORT",
        default=None,
        help="Hub address, overriding PI_ECOSYS_HUB and mDNS discovery.",
    )
    parser.add_argument(
        "--pin",
        metavar="PIN",
        default=None,
        help="Pairing PIN, overriding the console. Forces ONE non-interactive Pair.",
    )
    parser.add_argument(
        "--mode",
        choices=("audio", "video", "both"),
        default="both",
        help="Which media to stream (default: both).",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=0,
        metavar="N",
        help="Frames per medium; 0 (default) runs until interrupted.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore the cached token and force a fresh Pair.",
    )
    parser.add_argument(
        "--lang",
        choices=("yue", "zh", "en"),
        default=None,
        help="TTS language, overriding PI_ECOSYS_TTS_LANG.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdin: Any = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Run the client; return the process exit code.

    ``argv``/``environ``/``stdin``/``stdout``/``stderr`` are injectable so tests
    can drive the client without a console; defaults are the real process
    objects.
    """
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))

    env = os.environ if environ is None else environ
    in_stream = sys.stdin if stdin is None else stdin
    out: IO[str] = sys.stdout if stdout is None else stdout
    err: IO[str] = sys.stderr if stderr is None else stderr

    # Informational prints from the media modules (e.g. the synthetic-source
    # fallback) must never pollute the frozen stdout contract; our own lines use
    # the captured ``out`` object explicitly, so they are unaffected.
    with contextlib.redirect_stdout(err):
        return _dispatch(args, env, in_stream, out, err)


def _dispatch(
    args: argparse.Namespace,
    env: Mapping[str, str],
    stdin: Any,
    out: IO[str],
    err: IO[str],
) -> int:
    """Resolve config, acquire credentials, then run the media session."""
    import grpc

    from ecosys_pi.pairing import (
        PairProgrammingError,
        PairRejected,
        PairTransportError,
        pair,
    )

    from ecosys_pi.discovery import HubNotFoundError, resolve_hub

    cfg = load_config(env)
    hub = args.hub or cfg.hub
    if not hub:
        # No explicit override or PI_ECOSYS_HUB: fall back to a bounded mDNS
        # browse. Discovery failures are normal (the hub only advertises while
        # its service runs), so they land on the frozen `error:` line + exit 1.
        try:
            hub = resolve_hub(args.hub, environ=env)
        except HubNotFoundError as exc:
            _emit(out, f"error: {exc}")
            return EXIT_ERROR

    if args.frames < 0:
        _emit(out, f"error: --frames must be >= 0, got {args.frames}")
        return EXIT_ERROR

    lang = args.lang or cfg.tts_lang
    pin = args.pin if args.pin is not None else cfg.pin
    store = TokenStore.default(env)

    # --pin is an explicit override and --no-cache always ignores the cache;
    # both force a fresh Pair. Otherwise a usable cached token skips pairing.
    credentials: Credentials | None = (
        None if (args.no_cache or args.pin is not None) else store.load()
    )

    if credentials is None:
        if pin is None and not _stdin_is_tty(stdin):
            _emit(
                out,
                "error: no cached token and no PIN supplied while stdin is not a "
                "TTY; pass --pin PIN or run interactively",
            )
            return EXIT_ERROR

        pair_config = Config(
            hub=hub, pin=pin, tts_lang=lang, device_name=cfg.device_name
        )
        channel = grpc.insecure_channel(hub)
        try:
            credentials = pair(channel, pair_config, store=store)
        except PairRejected as exc:
            _emit(out, f"pair-rejected reason={exc.reason}")
            return EXIT_REFUSED
        except PairTransportError as exc:
            _emit(out, f"error: {exc}")
            return EXIT_ERROR
        except PairProgrammingError as exc:
            _emit(out, f"error: {exc}")
            return EXIT_ERROR
        finally:
            channel.close()
        _emit(out, f"pair-ok device={credentials.device_id}")

    session_config = Config(
        hub=hub, pin=pin, tts_lang=lang, device_name=cfg.device_name
    )
    try:
        return asyncio.run(
            _run_session(
                hub,
                credentials,
                session_config,
                mode=args.mode,
                frames=args.frames,
                env=env,
                out=out,
                err=err,
            )
        )
    except KeyboardInterrupt:
        _emit(out, "error: interrupted before a clean shutdown")
        return EXIT_ERROR


async def _run_session(
    hub: str,
    credentials: Credentials,
    cfg: Config,
    *,
    mode: str,
    frames: int,
    env: Mapping[str, str],
    out: IO[str],
    err: IO[str],
) -> int:
    """Run heartbeat + stream + media producers until the frame cap is reached."""
    from grpc import aio

    from ecosys_pi.audio import (
        AudioProducer,
        HalfDuplexGate,
        pb_frame_sink,
        select_audio_source,
    )
    from ecosys_pi.camera import select_video_source
    from ecosys_pi.heartbeat import HeartbeatClient, UnauthenticatedError
    from ecosys_pi.stream import StreamClient, StreamTransportError
    from ecosys_pi.tts import build_speaker

    max_frames = frames if frames > 0 else None
    audio_enabled = mode in ("audio", "both")
    video_enabled = mode in ("video", "both")
    received = 0

    # ONE gate is shared by the capture source and the TTS speaker, so audio is
    # muted for exactly the duration of an utterance (half-duplex).
    gate = HalfDuplexGate()
    speaker = build_speaker(cfg.tts_lang, environ=env, gate=gate)

    async def on_transcript(text: str) -> None:
        nonlocal received
        received += 1
        _emit(out, f"transcript {text}")
        # Blocking subprocess playback runs off-loop; the speaker toggles the
        # shared gate itself and always clears it, even on failure.
        await asyncio.to_thread(speaker.speak, text)

    channel = aio.insecure_channel(hub)
    announce: asyncio.Task[None] | None = None
    tasks: list[asyncio.Task[Any]] = []
    try:
        client = StreamClient(channel, credentials.token, on_transcript)
        heartbeat = HeartbeatClient(channel, credentials.token, credentials.device_id)

        stream_task = asyncio.create_task(client.run(), name="ecosys-cli-stream")
        beat_task = asyncio.create_task(heartbeat.run(), name="ecosys-cli-heartbeat")
        announce = asyncio.create_task(
            _announce_first_beat(heartbeat, out), name="ecosys-cli-announce"
        )

        producers = []
        if audio_enabled:
            source = select_audio_source(env, gate=gate)
            producers.append(
                AudioProducer(source, pb_frame_sink(client)).run(max_frames)
            )
        if video_enabled:
            video_source = select_video_source(env)
            producers.append(_run_video(video_source, client, max_frames, err=err))

        outcome = asyncio.create_task(_gather(producers), name="ecosys-cli-producers")
        tasks = [stream_task, beat_task, announce, outcome]

        done, _pending = await asyncio.wait(
            {outcome, stream_task, beat_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc

        # Normal completion: every producer hit its cap. Stop beating, then
        # half-close the stream and let it drain the hub's remaining frames.
        heartbeat.stop()
        client.close()
        await stream_task
        await beat_task
        # A slow first heartbeat must not lose the frozen line to a short session.
        try:
            await asyncio.wait_for(announce, timeout=_ANNOUNCE_GRACE_S)
        except asyncio.TimeoutError:
            pass

        _LOG.info(
            "session finished: audio/video capped at %s frame(s) per medium, "
            "%d transcript(s) received",
            frames,
            received,
        )
        return EXIT_OK
    except UnauthenticatedError:
        _emit(out, "re-pair-requested")
        return EXIT_REFUSED
    except StreamTransportError as exc:
        _emit(out, f"error: {exc}")
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - the contract reports any other error
        _emit(out, f"error: {exc}")
        return EXIT_ERROR
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await channel.close()


async def _gather(producers: Sequence[Any]) -> list[int]:
    """Await every producer coroutine; return their delivered frame counts."""
    if not producers:
        return []
    return list(await asyncio.gather(*producers))


async def _announce_first_beat(heartbeat: Any, out: IO[str]) -> None:
    """Print ``heartbeat-ok`` exactly once, when the hub accepts the first beat."""
    while heartbeat.beats_sent < 1:
        await asyncio.sleep(_BEAT_POLL_S)
    _emit(out, "heartbeat-ok")


async def _run_video(
    source: Any,
    client: Any,
    max_frames: int | None,
    *,
    err: IO[str],
    interval: float = _VIDEO_FRAME_INTERVAL_S,
) -> int:
    """Pump H.264 NALs from ``source`` into the stream until the cap is reached.

    ``max_frames is None`` runs until the source is exhausted or the process is
    interrupted. ``read_nal()`` returns ``None`` both when a live camera has no
    NAL queued yet and when a bounded source is exhausted, so this loop consults
    ``source.done`` to tell those apart: a live camera is never done and the pump
    simply keeps polling, while a bounded source stops. A real camera paces
    itself; this loop also paces the (synthetic) source at the nominal 30 fps so
    it never floods the bounded queue.
    """
    from ecosys.v1 import ecosys_pb2

    if not source.open():
        print(
            "[Pi Video] video source unavailable; no frames will be produced", file=err
        )
        return 0

    sent = 0
    try:
        while max_frames is None or sent < max_frames:
            nal = source.read_nal()
            if nal is None:
                if source.done:
                    break
                await asyncio.sleep(interval)
                continue
            await client.send(ecosys_pb2.StreamFrame(video_h264_nal=nal))
            sent += 1
            await asyncio.sleep(interval)
        return sent
    finally:
        source.close()


def _stdin_is_tty(stdin: Any) -> bool:
    """Whether ``stdin`` is an interactive terminal (never raises)."""
    try:
        return bool(stdin.isatty())
    except (AttributeError, ValueError):
        return False


def _emit(out: IO[str], line: str) -> None:
    """Write one contract line to the frozen stdout and flush it immediately."""
    print(line, file=out, flush=True)
