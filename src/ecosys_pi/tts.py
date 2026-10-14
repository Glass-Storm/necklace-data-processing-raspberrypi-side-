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
import shutil
import subprocess
from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable

__all__ = [
    "APLAY_ENGINE",
    "ENV_PLAYER",
    "PCM_SAMPLE_RATE_HZ",
    "SOUNDDEVICE_ENGINE",
    "AplayPlayer",
    "PcmPlayer",
    "RecordingPlayer",
    "SoundDevicePlayer",
    "SpeakingGate",
    "build_player",
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

    The process runner and the which-lookup are **injected** so tests can assert
    the exact argv and simulate a missing binary without ever invoking audio.

    :param runner: called as ``runner(argv, input=pcm, check=False,
        capture_output=True)``; defaults to :func:`subprocess.run`.
    :param which: executable lookup; defaults to :func:`shutil.which`.
    :param gate: half-duplex flag flipped ``True``/``False`` around playback.
    :param logger: logger for the "aplay missing"/failure warnings.
    :param sample_rate: PCM sample rate for ``-r`` (the frozen 16 kHz).
    """

    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
        which: Callable[[str], str | None] | None = None,
        gate: SpeakingGate | None = None,
        logger: logging.Logger | None = None,
        sample_rate: int = PCM_SAMPLE_RATE_HZ,
    ) -> None:
        self._runner = runner if runner is not None else subprocess.run
        self._which = which if which is not None else shutil.which
        self._gate = gate
        self._logger = logger if logger is not None else _LOGGER
        self._sample_rate = sample_rate

    def _argv(self) -> list[str]:
        """The exact ``aplay`` argv for the frozen mono/PCM16/16 kHz format."""
        return [
            APLAY_ENGINE,
            "-f",
            "S16_LE",
            "-r",
            str(self._sample_rate),
            "-c",
            "1",
        ]

    def play(self, pcm: bytes) -> bool:
        """Play ``pcm`` through ``aplay``; warn and return ``False`` on failure."""
        if not pcm:
            return True

        if self._which(APLAY_ENGINE) is None:
            self._logger.warning(
                "audio player %r not found; install alsa-utils "
                "(e.g. sudo apt install alsa-utils)",
                APLAY_ENGINE,
            )
            return False

        # The gate toggles exactly once around the playback window.
        _begin(self._gate)
        try:
            completed = self._runner(
                self._argv(), input=pcm, check=False, capture_output=True
            )
            if completed.returncode != 0:
                self._logger.warning(
                    "%s exited %s: %s",
                    APLAY_ENGINE,
                    completed.returncode,
                    _stderr_text(completed.stderr).strip(),
                )
                return False
            return True
        except OSError as exc:
            self._logger.warning(
                "%s could not start: %s (install alsa-utils)", APLAY_ENGINE, exc
            )
            return False
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
