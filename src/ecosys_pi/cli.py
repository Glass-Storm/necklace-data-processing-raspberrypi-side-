"""The runnable Pi client: config -> discovery -> stream -> media/playback.

``python -m ecosys_pi`` is the single entry point task 14's JVM E2E harness
drives. Its **stdout is a frozen contract** (see :data:`STDOUT_CONTRACT`): the
harness asserts these exact lines, so informational chatter from the media
modules is redirected to stderr and our own lines are written to the captured
stdout object explicitly.

The stream is opened **unauthenticated**: pairing/PIN and bearer tokens were
removed from the runtime, so the client only discovers the hub (an explicit
``--hub`` / ``PI_ECOSYS_HUB`` / the default gateway) and dials it directly.

Flags
-----

=============================  =================================================
``--hub HOST:PORT``            Hub override (default: gateway discovery).
``--mode audio|video|both``    Which media producers run (default ``both``).
``--frames N``                 Frames per medium; ``0`` = run until interrupted.
``--lang yue|zh|en``           Override ``PI_ECOSYS_TTS_LANG``.
=============================  =================================================

Frozen stdout / exit contract
-----------------------------

===============================  ===============================================
``heartbeat-ok``                 FIRST successful heartbeat.
``transcript <text>``            One received transcript frame.
``error: <detail>``              Anything else.
===============================  ===============================================

Exit codes: ``0`` success; ``1`` any error.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
import time
from typing import IO, Mapping, Sequence

from ecosys_pi.config import load_config

__all__ = ["EXIT_OK", "EXIT_ERROR", "build_parser", "main"]

_LOG = logging.getLogger("ecosys_pi.cli")

#: Process exited cleanly (media ran, or the client was interrupted).
EXIT_OK = 0
#: Any error that is NOT a normal hub refusal.
EXIT_ERROR = 1

#: The exact, frozen stdout tokens the E2E harness asserts (documentation only).
STDOUT_CONTRACT = (
    "heartbeat-ok",
    "transcript <text>",
    "error: <detail>",
)

#: Pace for the video pump: one JPEG still per capture frame (10 fps).
_VIDEO_FRAME_INTERVAL_S = 0.1

#: How often the heartbeat announcer polls for the first accepted beat.
_BEAT_POLL_S = 0.01

#: Seconds to wait for the first accepted heartbeat to be announced.
_ANNOUNCE_GRACE_S = 2.0


def build_parser() -> argparse.ArgumentParser:
    """Build the frozen runtime argument parser."""
    parser = argparse.ArgumentParser(
        prog="ecosys-pi",
        description=(
            "Stream camera/microphone media to the phone-manager hub over an "
            "unauthenticated stream, with hub-synthesized audio playback."
        ),
    )
    parser.add_argument(
        "--hub",
        metavar="HOST:PORT",
        default=None,
        help="Hub address, overriding PI_ECOSYS_HUB and gateway discovery.",
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
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Run the client; return the process exit code.

    ``argv``/``environ``/``stdout``/``stderr`` are injectable so tests can drive
    the client without a console; defaults are the real process objects.
    """
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))

    env = os.environ if environ is None else environ
    out: IO[str] = sys.stdout if stdout is None else stdout
    err: IO[str] = sys.stderr if stderr is None else stderr

    # Informational prints from the media modules (e.g. the synthetic-source
    # fallback) must never pollute the frozen stdout contract; our own lines use
    # the captured ``out`` object explicitly, so they are unaffected.
    with contextlib.redirect_stdout(err):
        return _dispatch(args, env, out, err)


def _dispatch(
    args: argparse.Namespace,
    env: Mapping[str, str],
    out: IO[str],
    err: IO[str],
) -> int:
    """Resolve config and hub, then run the media session unauthenticated."""
    from ecosys_pi.discovery import HubNotFoundError, resolve_hub

    cfg = load_config(env)
    hub = args.hub or cfg.hub
    if not hub:
        # The hub is the phone hotspot's access point: join the hotspot when
        # credentials are configured, then dial the default gateway. With no
        # credentials we still try the gateway (the Pi may already be joined).
        if cfg.ssid:
            from ecosys_pi.network import NetworkError, join_hotspot

            try:
                join_hotspot(cfg.ssid, cfg.password)
            except NetworkError as exc:
                _emit(out, f"error: {exc}")
                return EXIT_ERROR
        try:
            hub = resolve_hub(args.hub, environ=env, port=cfg.hub_port)
        except HubNotFoundError as exc:
            _emit(out, f"error: {exc}")
            return EXIT_ERROR

    if args.frames < 0:
        _emit(out, f"error: --frames must be >= 0, got {args.frames}")
        return EXIT_ERROR

    try:
        return asyncio.run(
            _run_session(
                hub,
                device_name=cfg.device_name,
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
    *,
    device_name: str,
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
    from ecosys_pi.heartbeat import HeartbeatClient
    from ecosys_pi.stream import StreamClient, StreamTransportError
    from ecosys_pi.tts import build_player

    max_frames = frames if frames > 0 else None
    audio_enabled = mode in ("audio", "both")
    video_enabled = mode in ("video", "both")
    received = 0

    # ONE gate is shared by the capture source and the player, so capture is
    # muted for exactly the duration of hub-synthesized playback (half-duplex).
    gate = HalfDuplexGate()
    player = build_player(env, gate=gate)

    async def on_transcript(text: str) -> None:
        nonlocal received
        received += 1
        _emit(out, f"transcript {text}")

    async def on_tts_audio(pcm: bytes) -> None:
        # Blocking subprocess playback runs off-loop; the player toggles the
        # shared gate itself and always clears it, even on failure.
        await asyncio.to_thread(player.play, pcm)

    async def on_gesture(text: str) -> None:
        # A gesture frame is informational here: the hub speaks the meaning via
        # tts_audio, so surface the text without touching the frozen stdout.
        _LOG.info("gesture frame received: %s", text)
        print(f"[Pi Gesture] {text}", file=err)

    channel = aio.insecure_channel(hub)
    announce: asyncio.Task[None] | None = None
    tasks: list[asyncio.Task[object]] = []
    try:
        # No token: pairing is gone, so the stream and heartbeat open
        # unauthenticated (no bearer metadata is sent).
        client = StreamClient(
            channel,
            None,
            on_transcript,
            on_tts_audio=on_tts_audio,
            on_gesture=on_gesture,
        )
        heartbeat = HeartbeatClient(channel, None, device_name)

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


async def _gather(producers: Sequence[object]) -> list[int]:
    """Await every producer coroutine; return their delivered frame counts."""
    if not producers:
        return []
    return list(await asyncio.gather(*producers))


async def _announce_first_beat(heartbeat: object, out: IO[str]) -> None:
    """Print ``heartbeat-ok`` exactly once, when the hub accepts the first beat."""
    while heartbeat.beats_sent < 1:
        await asyncio.sleep(_BEAT_POLL_S)
    _emit(out, "heartbeat-ok")


async def _run_video(
    source: object,
    client: object,
    max_frames: int | None,
    *,
    err: IO[str],
    interval: float = _VIDEO_FRAME_INTERVAL_S,
) -> int:
    """Pump 320x240 JPEG stills from ``source`` into the stream until capped.

    ``max_frames is None`` runs until the source is exhausted or the process is
    interrupted. ``read_frame()`` returns ``None`` both when a live camera has no
    still ready yet and when a bounded source is exhausted, so this loop consults
    ``source.done`` to tell those apart: a live camera is never done and the pump
    simply keeps polling, while a bounded source stops. A real camera paces
    itself; this loop also paces the (synthetic) source at the nominal 10 fps so
    it never floods the wire.
    """
    from ecosys.v1 import ecosys_pb2

    from ecosys_pi.camera import DEFAULT_HEIGHT, DEFAULT_WIDTH

    if not source.open():
        print(
            "[Pi Video] video source unavailable; no frames will be produced", file=err
        )
        return 0

    width = getattr(source, "width", DEFAULT_WIDTH)
    height = getattr(source, "height", DEFAULT_HEIGHT)
    sent = 0
    try:
        while max_frames is None or sent < max_frames:
            jpeg = source.read_frame()
            if jpeg is None:
                if source.done:
                    break
                await asyncio.sleep(interval)
                continue
            await client.send(
                ecosys_pb2.StreamFrame(
                    image=ecosys_pb2.ImageFrame(
                        jpeg=jpeg,
                        pts_ms=time.monotonic_ns() // 1_000_000,
                        width=width,
                        height=height,
                    )
                )
            )
            sent += 1
            await asyncio.sleep(interval)
        return sent
    finally:
        source.close()


def _emit(out: IO[str], line: str) -> None:
    """Write one contract line to the frozen stdout and flush it immediately."""
    print(line, file=out, flush=True)
