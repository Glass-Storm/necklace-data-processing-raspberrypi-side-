"""Playback of hub-synthesized speech on the necklace speaker.

The Pi no longer synthesizes speech. The hub runs TTS and ships raw
little-endian PCM16 mono @ 16 kHz inside a ``tts_audio`` StreamFrame
(``TtsAudio.pcm16_16k``, field 7); this module only PLAYS those bytes. That is a
deliberate protocol decision: synthesis needs a model this constrained device
does not carry, so the phone is the single source of voice (see
``docs/PROTOCOL_DECISIONS.md``).

Backends
--------
Two playback backends, chosen by ``PI_ECOSYS_PLAYER``:

* ``aplay -f S16_LE -r 16000 -c 1`` (ALSA ``alsa-utils``), with the PCM fed on
  stdin. This is the default on a Raspberry Pi: it needs no Python audio stack
  and consumes the exact wire format (signed 16-bit little-endian, mono, 16 kHz).
  Playback fans out concurrently to every PCM device that advertises playback
  streams in ``/proc/asound/pcm`` (``-D plughw:<card>,<device>``) so a speaker
  and USB earphones both sound; when no device is enumerable it falls back to
  one default ``aplay`` with no ``-D``.
* ``sounddevice`` (PortAudio) as a Python fallback when ``aplay`` is absent.

``PI_ECOSYS_PLAYER=none`` selects :class:`RecordingPlayer`, which records the PCM
instead of touching hardware (dry runs and tests). Selection honours an injected
which-lookup and availability probe, so no backend is ever invoked in tests.

Failure posture
---------------
Playback is best-effort and MUST NEVER interrupt the receive stream: a missing
binary, a non-zero exit, or an ``OSError`` is logged as a warning and reported as
``False``. A blank PCM payload is a no-op.

Half-duplex (echo-loop guard)
-----------------------------
While audio plays, the Pi must not re-capture its own output. Every player flips
a shared :class:`SpeakingGate`'s ``speaking`` flag to ``True`` for the duration of
playback and back to ``False`` in a ``finally``, so it clears even when the
backend fails. The gate is the ``speaking`` flag of the audio source from
:mod:`ecosys_pi.audio`. A missing backend opens no window and leaves the gate
untouched.

Testability
-----------
No backend is ever invoked in tests: the process runner (:func:`subprocess.run`)
and the which-lookup (:func:`shutil.which`) are injected, and
:class:`RecordingPlayer` makes no sound at all.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Protocol, runtime_checkable

__all__ = [
    "APLAY_ENGINE",
    "ENV_PLAYER",
    "PCM_SAMPLE_RATE_HZ",
    "SOUNDDEVICE_ENGINE",
    "AplayPlayer",
    "LatestPcmPlayer",
    "PcmPlayer",
    "RecordingPlayer",
    "SoundDevicePlayer",
    "SpeakingGate",
    "build_player",
    "playback_devices",
    "player_choice",
]

_LOGGER = logging.getLogger(__name__)

#: The frozen wire sample rate for ``tts_audio.pcm16_16k`` (never negotiated).
PCM_SAMPLE_RATE_HZ = 16_000

#: ALSA's command-line player (``alsa-utils``); the PCM is fed on stdin.
APLAY_ENGINE = "aplay"

#: The Python/PortAudio fallback backend (no process spawn).
SOUNDDEVICE_ENGINE = "sounddevice"

#: Environment variable selecting the playback backend: ``auto`` (default),
#: ``aplay``, ``sounddevice``, or ``none``.
ENV_PLAYER = "PI_ECOSYS_PLAYER"

#: ALSA's per-host PCM inventory, one ``CC-DD: name : playback N`` per device.
ASOUND_PCM_PATH = "/proc/asound/pcm"

#: One ``/proc/asound/pcm`` line: ``card``-``device``, then feature counts.
_ASOUND_PCM_LINE = re.compile(
    r"^(?P<card>\d+)-(?P<device>\d+):.*\bplayback\s+(?P<streams>\d+)"
)


def playback_devices(
    path: str | os.PathLike[str] = ASOUND_PCM_PATH,
) -> list[tuple[int, int]]:
    """Return every ALSA PCM ``(card, device)`` that can play, in file order.

    Parses the ALSA inventory (``/proc/asound/pcm``) and keeps lines whose
    ``playback N`` count is greater than zero, so capture-only devices are
    skipped. A missing or unreadable inventory yields an empty list, which the
    :class:`AplayPlayer` turns into the legacy default-device fallback.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return []

    devices: list[tuple[int, int]] = []
    for line in lines:
        match = _ASOUND_PCM_LINE.match(line)
        if match is not None and int(match.group("streams")) > 0:
            devices.append((int(match.group("card")), int(match.group("device"))))
    return devices


# --- Half-duplex gate contract ----------------------------------------------


@runtime_checkable
class SpeakingGate(Protocol):
    """The half-duplex flag owned by the audio source (task 10).

    The concrete source exposes ``speaking`` as a settable ``bool``; while it is
    ``True`` the source must yield no captured frames (echo-loop guard). The
    protocol is intentionally minimal so the audio source and this module agree
    on the one thing that matters.
    """

    speaking: bool


@runtime_checkable
class PcmPlayer(Protocol):
    """Anything that can play one buffer of raw PCM16 mono @ 16 kHz.

    :meth:`play` returns ``True`` when the bytes were handed to a backend and
    ``False`` when they could not be (missing binary, non-zero exit, OSError).
    It MUST NOT raise for a runtime audio failure - the receive stream must keep
    flowing.
    """

    def play(self, pcm: bytes) -> bool:
        """Play ``pcm``; return whether playback was attempted successfully."""
        ...


# --- Shared half-duplex helpers ---------------------------------------------


def _begin(gate: SpeakingGate | None) -> None:
    """Open the half-duplex window, when a gate was supplied."""
    if gate is not None:
        gate.speaking = True


def _end(gate: SpeakingGate | None) -> None:
    """Close the half-duplex window; always runs, even on failure."""
    if gate is not None:
        gate.speaking = False


def _stderr_text(value: object) -> str:
    """Best-effort decode of a completed process's stderr (bytes or str)."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


# --- aplay backend -----------------------------------------------------------


class AplayPlayer:
    """Play PCM16 @ 16 kHz via ``aplay`` with the bytes on stdin.

    Playback fans out to every device returned by :attr:`devices`, one
    ``aplay -D plughw:<card>,<device>`` per target, concurrently; a device that
    fails is logged and does not abort the others. An empty device list falls
    back to a single default ``aplay`` with no ``-D``.

    The process runner and the which-lookup are **injected** so tests can assert
    the exact argv and simulate a missing binary without ever invoking audio.

    :param runner: called as ``runner(argv, input=pcm, check=False,
        capture_output=True)``; defaults to :func:`subprocess.run`.
    :param which: executable lookup; defaults to :func:`shutil.which`.
    :param gate: half-duplex flag flipped ``True``/``False`` around playback.
    :param logger: logger for the "aplay missing"/failure warnings.
    :param sample_rate: PCM sample rate for ``-r`` (the frozen 16 kHz).
    :param devices: enumerates the playback ``(card, device)`` targets; defaults
        to :func:`playback_devices` (never called in tests).
    """

    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
        which: Callable[[str], str | None] | None = None,
        gate: SpeakingGate | None = None,
        logger: logging.Logger | None = None,
        sample_rate: int = PCM_SAMPLE_RATE_HZ,
        devices: Callable[[], Sequence[tuple[int, int]]] | None = None,
    ) -> None:
        self._runner = runner if runner is not None else subprocess.run
        self._which = which if which is not None else shutil.which
        self._gate = gate
        self._logger = logger if logger is not None else _LOGGER
        self._sample_rate = sample_rate
        self._devices = devices if devices is not None else playback_devices

    def _argv(self, device: tuple[int, int] | None = None) -> list[str]:
        """The exact ``aplay`` argv for the frozen mono/PCM16/16 kHz format."""
        argv = [APLAY_ENGINE]
        if device is not None:
            argv.extend(["-D", f"plughw:{device[0]},{device[1]}"])
        argv.extend(["-f", "S16_LE", "-r", str(self._sample_rate), "-c", "1"])
        return argv

    def _play_on(self, pcm: bytes, device: tuple[int, int] | None) -> bool:
        """Run one ``aplay``; return whether it accepted the whole buffer."""
        target = "default" if device is None else f"plughw:{device[0]},{device[1]}"
        try:
            completed = self._runner(
                self._argv(device), input=pcm, check=False, capture_output=True
            )
        except OSError as exc:
            self._logger.warning(
                "%s could not start on %s: %s (install alsa-utils)",
                APLAY_ENGINE,
                target,
                exc,
            )
            return False
        except Exception as exc:  # noqa: BLE001 - playback must not kill the stream
            self._logger.warning("%s failed on %s: %s", APLAY_ENGINE, target, exc)
            return False
        if completed.returncode != 0:
            self._logger.warning(
                "%s exited %s on %s: %s",
                APLAY_ENGINE,
                completed.returncode,
                target,
                _stderr_text(completed.stderr).strip(),
            )
            return False
        return True

    def play(self, pcm: bytes) -> bool:
        """Play ``pcm`` on every device; warn and return ``False`` if all fail."""
        if not pcm:
            return True

        if self._which(APLAY_ENGINE) is None:
            self._logger.warning(
                "audio player %r not found; install alsa-utils "
                "(e.g. sudo apt install alsa-utils)",
                APLAY_ENGINE,
            )
            return False

        targets: list[tuple[int, int] | None] = list(self._devices()) or [None]

        # The gate toggles exactly once around the whole playback window.
        _begin(self._gate)
        try:
            if len(targets) == 1:
                return self._play_on(pcm, targets[0])
            results: list[bool] = []
            with ThreadPoolExecutor(max_workers=len(targets)) as pool:
                futures = [pool.submit(self._play_on, pcm, d) for d in targets]
                for future in as_completed(futures):
                    results.append(future.result())
            return any(results)
        finally:
            _end(self._gate)


# --- sounddevice backend -----------------------------------------------------


def _sounddevice_playback(pcm: bytes) -> None:
    """Play raw PCM16 @ 16 kHz through PortAudio (lazy import, never at import)."""
    import numpy as np  # local import: keep this module importable without numpy
    import sounddevice as sd

    samples = np.frombuffer(pcm, dtype="<i2")
    sd.play(samples, samplerate=PCM_SAMPLE_RATE_HZ)
    sd.wait()


def _sounddevice_available() -> bool:
    """Probe PortAudio without importing it eagerly at module load."""
    from ecosys_pi.audio import sounddevice_available

    return sounddevice_available()


class SoundDevicePlayer:
    """Play PCM16 @ 16 kHz via ``sounddevice``/PortAudio.

    :param gate: half-duplex flag flipped around playback.
    :param available: probe returning whether PortAudio is usable; defaults to
        :func:`ecosys_pi.audio.sounddevice_available`.
    :param playback: callable taking the PCM bytes; defaults to PortAudio.
    :param logger: logger for the "unavailable"/failure warnings.
    """

    def __init__(
        self,
        *,
        gate: SpeakingGate | None = None,
        available: Callable[[], bool] | None = None,
        playback: Callable[[bytes], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._gate = gate
        self._available = available if available is not None else _sounddevice_available
        self._playback = playback if playback is not None else _sounddevice_playback
        self._logger = logger if logger is not None else _LOGGER

    def play(self, pcm: bytes) -> bool:
        """Play ``pcm`` through PortAudio; warn and return ``False`` on failure."""
        if not pcm:
            return True

        if not self._available():
            self._logger.warning(
                "sounddevice/PortAudio unavailable; install libportaudio2 "
                "(or install alsa-utils for the aplay backend)"
            )
            return False

        _begin(self._gate)
        try:
            self._playback(pcm)
            return True
        except Exception as exc:  # noqa: BLE001 - playback must not kill the stream
            self._logger.warning("sounddevice playback failed: %s", exc)
            return False
        finally:
            _end(self._gate)


# --- silent recorder (tests and dry runs) ------------------------------------


class RecordingPlayer:
    """A silent :class:`PcmPlayer` that records every non-empty buffer.

    Records each buffer in :attr:`played` so a test can assert a ``tts_audio``
    frame reached the player **exactly once**, without any audio. A blank buffer
    is a no-op and is not recorded, mirroring the real backends.
    """

    def __init__(self) -> None:
        self.played: list[bytes] = []

    def play(self, pcm: bytes) -> bool:
        if pcm:
            self.played.append(pcm)
        return True


# --- Latest-wins playback (the Pi must not queue stale words) ----------------


class LatestPcmPlayer:
    """A one-slot, latest-wins wrapper around another :class:`PcmPlayer`.

    Why this exists
    ---------------
    The hub synthesizes a fresh word roughly every 0.5 s, while ``aplay`` blocks
    the caller for the whole duration of that word. If the gRPC reader invokes
    the backend directly for every ``tts_audio`` frame, the frames queue in the
    Pi and the speaker plays words the user saw seconds ago. This wrapper keeps
    at most ONE *pending* frame: a newer :meth:`submit` replaces it, and the
    reader returns immediately. Only the word already in flight is allowed to
    finish, so the next word heard is always the newest one.

    The inner player still owns the :class:`SpeakingGate` toggling and the
    all-device fan-out; this class only reorders/coalesces frames and moves the
    blocking call onto a single daemon worker thread. A backend exception is
    logged and swallowed so it can never kill the worker or the receive stream.

    :param inner: the real backend whose ``play`` blocks for one word.
    :param logger: logger for failures (warning) and dropped frames (debug).
    :param join_timeout: best-effort seconds to wait for the worker in
        :meth:`close`; the in-flight word may still be finishing.
    """

    def __init__(
        self,
        inner: PcmPlayer,
        *,
        logger: logging.Logger | None = None,
        join_timeout: float = 2.0,
    ) -> None:
        self._inner = inner
        self._logger = logger if logger is not None else _LOGGER
        self._join_timeout = join_timeout
        # One pending frame only; ``None`` is the worker's shutdown sentinel.
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=1)
        self._closed = threading.Event()
        self._close_lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._run,
            name="ecosys-tts-latest-player",
            daemon=True,
        )
        self._worker.start()

    def submit(self, pcm: bytes) -> None:
        """Queue ``pcm`` as the newest word without ever blocking the caller.

        An empty payload is ignored. When a frame is already pending, it is
        dropped (at DEBUG) and replaced, so the queue never grows beyond one and
        the oldest, never-heard word loses. The word currently playing is not
        affected: the worker already owns it.
        """
        if not pcm or self._closed.is_set():
            return

        try:
            self._queue.put_nowait(pcm)
            return
        except queue.Full:
            pass

        try:
            self._queue.get_nowait()
        except queue.Empty:  # the worker consumed it between Full and here
            pass
        else:
            self._logger.debug("dropped a pending TTS frame; playing the newest")

        try:
            self._queue.put_nowait(pcm)
        except queue.Full:
            # Another producer won the race for the slot; its frame is newer.
            self._logger.debug("a fresher TTS frame is already pending")

    def _run(self) -> None:
        """Drain the slot on one daemon thread until the sentinel arrives."""
        while True:
            pcm = self._queue.get()
            if pcm is None or self._closed.is_set():
                return
            try:
                self._inner.play(pcm)
            except Exception as exc:  # noqa: BLE001 - playback must not kill the stream
                self._logger.warning("TTS playback failed: %s", exc)

    def close(self) -> None:
        """Stop the worker and join it best-effort; safe to call repeatedly.

        A pending (not-yet-playing) frame is discarded so shutdown does not
        wait for a stale word; the word already playing is allowed to finish
        within ``join_timeout``.
        """
        with self._close_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(None)
            except queue.Full:  # pragma: no cover - sentinel slot is now free
                pass
            if self._worker is not threading.current_thread():
                self._worker.join(timeout=self._join_timeout)


# --- Backend selection (availability + env) ---------------------------------


def player_choice(environ: Mapping[str, str] | None = None) -> str:
    """Resolve ``PI_ECOSYS_PLAYER`` to a lower-case backend name.

    Blank/unset defaults to ``auto``; surrounding whitespace is ignored.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(ENV_PLAYER) or "").strip().lower()
    return raw or "auto"


def build_player(
    environ: Mapping[str, str] | None = None,
    *,
    which: Callable[[str], str | None] | None = None,
    sounddevice_available: Callable[[], bool] | None = None,
    gate: SpeakingGate | None = None,
) -> PcmPlayer:
    """Build the playback backend from availability and ``PI_ECOSYS_PLAYER``.

    Values (case-insensitive):

    * ``none`` -- :class:`RecordingPlayer` (no hardware touched).
    * ``aplay`` -- the ALSA backend (warns at play time if missing).
    * ``sounddevice`` -- the PortAudio backend.
    * ``auto`` (default) or anything else -- ``aplay`` when present, else
      ``sounddevice`` when available, else ``aplay`` so a play attempt warns
      rather than silently dropping the audio.
    """
    resolve = which if which is not None else shutil.which
    probe = (
        sounddevice_available
        if sounddevice_available is not None
        else _sounddevice_available
    )
    choice = player_choice(environ)

    if choice == "none":
        return RecordingPlayer()
    if choice == APLAY_ENGINE:
        return AplayPlayer(which=resolve, gate=gate)
    if choice == SOUNDDEVICE_ENGINE:
        return SoundDevicePlayer(available=probe, gate=gate)

    # "auto" (and any unknown value): aplay, then sounddevice, then aplay.
    if resolve(APLAY_ENGINE):
        return AplayPlayer(which=resolve, gate=gate)
    if probe():
        return SoundDevicePlayer(available=probe, gate=gate)
    return AplayPlayer(which=resolve, gate=gate)
