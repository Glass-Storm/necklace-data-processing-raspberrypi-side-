"""Tests for :mod:`ecosys_pi.tts`: PLAYBACK of hub-synthesized speech.

The Pi no longer synthesizes speech: the hub sends raw PCM16 mono @ 16 kHz
``tts_audio`` frames and the necklace only PLAYS them. No audio hardware is
touched here - the process runner and the which-lookup are injected, so the
tests assert the exact ``aplay`` argv, the PCM fed on stdin, the backend
selection, and the half-duplex gate, all without producing any sound.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable

import pytest

from ecosys_pi import tts as tts_mod
from ecosys_pi.tts import (
    APLAY_ENGINE,
    ENV_PLAYER,
    PCM_SAMPLE_RATE_HZ,
    AplayPlayer,
    PcmPlayer,
    RecordingPlayer,
    SoundDevicePlayer,
    SpeakingGate,
    build_player,
    player_choice,
)

PCM = b"\x01\x02\x03\x04"

# --- fakes -------------------------------------------------------------------


class FakeGate:
    """A stand-in for the task-10 audio source's half-duplex flag."""

    def __init__(self) -> None:
        self.speaking = False


class RecordingRunner:
    """A ``subprocess.run`` double: records argv AND the stdin bytes fed to it."""

    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.calls: list[list[str]] = []
        self.inputs: list[bytes | None] = []
        self._returncode = returncode
        self._stderr = stderr

    def __call__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(list(argv))
        self.inputs.append(kwargs.get("input"))
        return subprocess.CompletedProcess(
            argv, self._returncode, stdout=b"", stderr=self._stderr
        )


def _which_returning(path: str | None) -> Callable[[str], str | None]:
    return lambda _name: path


# --- the aplay backend: exact argv + PCM on stdin ----------------------------


def test_aplay_sample_rate_constant_is_16k() -> None:
    assert PCM_SAMPLE_RATE_HZ == 16_000


def test_aplay_player_uses_exact_argv_and_feeds_pcm_on_stdin() -> None:
    runner = RecordingRunner()
    player = AplayPlayer(runner=runner, which=_which_returning("/usr/bin/aplay"))

    assert player.play(PCM) is True
    assert runner.calls == [[APLAY_ENGINE, "-f", "S16_LE", "-r", "16000", "-c", "1"]]
    assert runner.inputs == [PCM]


def test_blank_pcm_is_a_noop() -> None:
    runner = RecordingRunner()
    player = AplayPlayer(runner=runner, which=_which_returning("/usr/bin/aplay"))

    assert player.play(b"") is True
    assert runner.calls == []


# --- missing backend -> warn, never crash the stream -------------------------


def test_missing_aplay_warns_and_returns_false(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = RecordingRunner()
    gate = FakeGate()
    player = AplayPlayer(runner=runner, which=_which_returning(None), gate=gate)

    with caplog.at_level(logging.WARNING):
        assert player.play(PCM) is False

    assert runner.calls == []  # never spawned
    assert "aplay" in caplog.text
    assert "alsa-utils" in caplog.text
    # No audio was attempted, so no half-duplex window was opened.
    assert gate.speaking is False


def test_nonzero_exit_warns_and_returns_false(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = RecordingRunner(returncode=1, stderr=b"audio device busy")
    player = AplayPlayer(runner=runner, which=_which_returning("/usr/bin/aplay"))

    with caplog.at_level(logging.WARNING):
        assert player.play(PCM) is False

    assert "audio device busy" in caplog.text


def test_oserror_does_not_crash_the_stream(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def exploding_runner(argv, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("no such binary")

    player = AplayPlayer(
        runner=exploding_runner, which=_which_returning("/usr/bin/aplay")
    )

    with caplog.at_level(logging.WARNING):
        assert player.play(PCM) is False

    assert "could not start" in caplog.text


# --- half-duplex gate: toggles exactly once around one play ------------------


def test_gate_is_true_during_playback_and_false_after() -> None:
    gate = FakeGate()
    observed: list[bool] = []

    def observing_runner(argv, **kwargs):  # type: ignore[no-untyped-def]
        observed.append(gate.speaking)  # must be True while playing
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    player = AplayPlayer(
        runner=observing_runner,
        which=_which_returning("/usr/bin/aplay"),
        gate=gate,
    )

    assert gate.speaking is False
    player.play(PCM)
    assert observed == [True]
    assert gate.speaking is False  # cleared after


def test_gate_clears_even_when_the_runner_raises() -> None:
    gate = FakeGate()

    def exploding_runner(argv, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("boom")

    player = AplayPlayer(
        runner=exploding_runner,
        which=_which_returning("/usr/bin/aplay"),
        gate=gate,
    )

    player.play(PCM)

    assert gate.speaking is False  # try/finally cleared it


def test_gate_toggles_exactly_once_per_playback() -> None:
    gate = FakeGate()
    transitions: list[bool] = []

    class TracingGate:
        @property
        def speaking(self) -> bool:
            return gate.speaking

        @speaking.setter
        def speaking(self, value: bool) -> None:
            transitions.append(value)
            gate.speaking = value

    player = AplayPlayer(
        runner=RecordingRunner(),
        which=_which_returning("/usr/bin/aplay"),
        gate=TracingGate(),
    )

    player.play(PCM)

    assert transitions == [True, False]


# --- the recording player (silent, for tests and dry runs) -------------------


def test_recording_player_records_bytes_and_satisfies_pcm_player() -> None:
    player = RecordingPlayer()

    assert isinstance(player, PcmPlayer)
    assert player.play(b"ab") is True
    assert player.play(b"") is True
    assert player.played == [b"ab"]


# --- backend selection -------------------------------------------------------


def test_player_choice_defaults_to_auto() -> None:
    assert player_choice({}) == "auto"
    assert player_choice({ENV_PLAYER: "  NONE  "}) == "none"


def test_build_player_honours_none_without_touching_hardware() -> None:
    player = build_player({ENV_PLAYER: "none"})

    assert isinstance(player, RecordingPlayer)
    assert player.play(PCM) is True
    assert player.played == [PCM]


def test_build_player_prefers_aplay_when_available() -> None:
    player = build_player(
        {}, which=_which_returning("/usr/bin/aplay"), sounddevice_available=lambda: True
    )

    assert isinstance(player, AplayPlayer)


def test_build_player_falls_back_to_sounddevice_when_aplay_missing() -> None:
    player = build_player(
        {}, which=_which_returning(None), sounddevice_available=lambda: True
    )

    assert isinstance(player, SoundDevicePlayer)


def test_build_player_auto_with_no_backend_warns_on_play(
    caplog: pytest.LogCaptureFixture,
) -> None:
    player = build_player(
        {}, which=_which_returning(None), sounddevice_available=lambda: False
    )

    assert isinstance(player, AplayPlayer)  # aplay path warns at play time
    with caplog.at_level(logging.WARNING):
        assert player.play(PCM) is False
    assert "aplay" in caplog.text


def test_player_choice_blank_falls_back_to_auto() -> None:
    assert player_choice({ENV_PLAYER: "   "}) == "auto"


def test_build_player_explicit_aplay_choice_is_honoured() -> None:
    player = build_player(
        {ENV_PLAYER: "aplay"},
        which=_which_returning("/usr/bin/aplay"),
        sounddevice_available=lambda: True,
    )

    assert isinstance(player, AplayPlayer)


def test_build_player_explicit_sounddevice_choice_is_honoured() -> None:
    player = build_player(
        {ENV_PLAYER: "sounddevice"},
        which=_which_returning("/usr/bin/aplay"),
        sounddevice_available=lambda: True,
    )

    assert isinstance(player, SoundDevicePlayer)


def test_build_player_unknown_choice_falls_back_to_auto() -> None:
    player = build_player(
        {ENV_PLAYER: "bogus"},
        which=_which_returning("/usr/bin/aplay"),
        sounddevice_available=lambda: True,
    )

    assert isinstance(player, AplayPlayer)


def test_sounddevice_blank_pcm_is_a_noop() -> None:
    played: list[bytes] = []
    player = SoundDevicePlayer(available=lambda: True, playback=played.append)

    assert player.play(b"") is True
    assert played == []


# --- sounddevice backend -----------------------------------------------------


def test_sounddevice_player_reports_unavailable_and_returns_false(
    caplog: pytest.LogCaptureFixture,
) -> None:
    player = SoundDevicePlayer(available=lambda: False)

    with caplog.at_level(logging.WARNING):
        assert player.play(PCM) is False

    assert "sounddevice" in caplog.text


def test_sounddevice_player_plays_and_drives_the_gate() -> None:
    gate = FakeGate()
    played: list[bytes] = []
    player = SoundDevicePlayer(
        gate=gate,
        available=lambda: True,
        playback=played.append,
    )

    assert player.play(PCM) is True
    assert played == [PCM]
    assert gate.speaking is False


# --- integration with task 10's half-duplex gate -----------------------------


def test_task10_half_duplex_gate_satisfies_speaking_gate() -> None:
    from ecosys_pi.audio import HalfDuplexGate

    gate = HalfDuplexGate()

    assert isinstance(gate, SpeakingGate)
    assert gate.speaking is False


def test_aplay_player_drives_task10_gate() -> None:
    from ecosys_pi.audio import HalfDuplexGate

    gate = HalfDuplexGate()
    player = AplayPlayer(
        runner=RecordingRunner(),
        which=_which_returning("/usr/bin/aplay"),
        gate=gate,
    )

    player.play(PCM)

    assert gate.speaking is False


# --- no local synthesis remains (the hub synthesizes) ------------------------


def test_no_local_synthesis_engine_remains() -> None:
    """The receive path must not synthesize speech itself."""
    for retired in ("CommandSpeaker", "build_speaker", "build_argv", "plan_for_lang"):
        assert not hasattr(tts_mod, retired), f"{retired} was not retired"
