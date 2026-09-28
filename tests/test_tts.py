"""Tests for :mod:`ecosys_pi.tts`: engine selection, fail-fast, injectable speaker.

No engine is invoked. The process runner and the which-lookup are injected so we
assert the **exact argv** per language, simulate a missing engine/voice, and
toggle the half-duplex gate - all without producing any audio.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from ecosys_pi import tts as tts_mod
from ecosys_pi.config import DEFAULT_TTS_LANG, SUPPORTED_TTS_LANGS
from ecosys_pi.tts import (
    ESPEAK_ENGINE,
    PIPER_ENGINE,
    PIPER_VOICES,
    CommandSpeaker,
    NullSpeaker,
    UnsupportedLanguageError,
    build_argv,
    build_speaker,
    plan_for_lang,
    piper_data_dir,
)

# --- fakes -------------------------------------------------------------------


class FakeGate:
    """A stand-in for the task-10 audio source's half-duplex flag."""

    def __init__(self) -> None:
        self.speaking = False


class RecordingRunner:
    """A ``subprocess.run`` double: records argv, returns a fixed result."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self._returncode = returncode
        self._stderr = stderr

    def __call__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv, self._returncode, stdout="", stderr=self._stderr
        )


def _which_returning(path: str | None) -> Callable[[str], str | None]:
    return lambda _name: path


def _voice_dir(tmp_path: Path, voice: str) -> Path:
    """A Piper data dir that already holds ``voice``.onnx."""
    (tmp_path / f"{voice}.onnx").write_bytes(b"onnx")
    (tmp_path / f"{voice}.onnx.json").write_text("{}")
    return tmp_path


# --- plan / argv per language ------------------------------------------------


def test_all_supported_languages_have_a_plan() -> None:
    for lang in SUPPORTED_TTS_LANGS:
        plan = plan_for_lang(lang)
        assert plan.lang == lang


def test_yue_uses_espeak_ng_yue_voice() -> None:
    plan = plan_for_lang("yue")

    assert plan.engine == ESPEAK_ENGINE
    assert plan.voice == "yue"
    assert plan.data_dir is None
    assert build_argv(plan, "你好") == ["espeak-ng", "-v", "yue", "--", "你好"]


def test_zh_uses_piper_huayan_medium() -> None:
    plan = plan_for_lang("zh", data_dir=Path("/voices"))

    assert plan.engine == PIPER_ENGINE
    assert plan.voice == "zh_CN-huayan-medium"
    assert build_argv(plan, "你好") == [
        "piper",
        "-m",
        "zh_CN-huayan-medium",
        "--data-dir",
        "/voices",
        "--",
        "你好",
    ]


def test_en_uses_piper_lessac_medium() -> None:
    plan = plan_for_lang("en", data_dir=Path("/voices"))

    assert plan.engine == PIPER_ENGINE
    assert plan.voice == "en_US-lessac-medium"
    assert build_argv(plan, "hello") == [
        "piper",
        "-m",
        "en_US-lessac-medium",
        "--data-dir",
        "/voices",
        "--",
        "hello",
    ]


def test_espeak_and_piper_are_distinct_stacks() -> None:
    yue = plan_for_lang("yue")
    zh = plan_for_lang("zh")

    assert yue.engine != zh.engine
    # Different install commands: apt for espeak, uv for piper (never pip).
    assert "apt install" in yue.install_hint
    assert "uv add piper-tts" in zh.install_hint
    assert "pip " not in zh.install_hint


def test_transcript_text_is_always_placed_after_double_dash() -> None:
    """A transcript beginning with '-' must not be parsed as an option."""
    for lang in SUPPORTED_TTS_LANGS:
        plan = plan_for_lang(lang)
        argv = build_argv(plan, "--rm -rf /")

        assert argv[-2:] == ["--", "--rm -rf /"]


# --- unknown language fails fast ---------------------------------------------


def test_unknown_language_fails_fast_in_plan() -> None:
    with pytest.raises(UnsupportedLanguageError):
        plan_for_lang("fr")


def test_unknown_language_fails_fast_in_build_speaker() -> None:
    with pytest.raises(UnsupportedLanguageError):
        build_speaker("klingon")


def test_unsupported_language_error_is_a_value_error() -> None:
    with pytest.raises(ValueError):
        plan_for_lang("de")


def test_default_lang_plans_cleanly() -> None:
    # config guarantees tts_lang is always supported; the default must plan.
    assert plan_for_lang(DEFAULT_TTS_LANG).lang == DEFAULT_TTS_LANG


# --- piper data dir resolution ----------------------------------------------


def test_piper_data_dir_honours_override(tmp_path: Path) -> None:
    assert piper_data_dir({"PI_ECOSYS_PIPER_DATA_DIR": str(tmp_path)}) == tmp_path


def test_piper_data_dir_blank_falls_back_to_default() -> None:
    assert (
        piper_data_dir({"PI_ECOSYS_PIPER_DATA_DIR": "  "})
        == tts_mod.DEFAULT_PIPER_DATA_DIR
    )
    assert piper_data_dir({}) == tts_mod.DEFAULT_PIPER_DATA_DIR


# --- injectable speaker: correct command, no audio ---------------------------


def test_speaker_invokes_exact_argv_for_lang(tmp_path: Path) -> None:
    runner = RecordingRunner()
    speaker = build_speaker(
        "en",
        data_dir=_voice_dir(tmp_path, "en_US-lessac-medium"),
        runner=runner,
        which=_which_returning("/usr/bin/piper"),
    )

    assert speaker.speak("hello world") is True
    assert runner.calls == [
        [
            "piper",
            "-m",
            "en_US-lessac-medium",
            "--data-dir",
            str(tmp_path),
            "--",
            "hello world",
        ]
    ]


def test_speaker_espeak_exact_argv(tmp_path: Path) -> None:
    runner = RecordingRunner()
    speaker = build_speaker(
        "yue",
        runner=runner,
        which=_which_returning("/usr/bin/espeak-ng"),
    )

    assert speaker.speak("早晨") is True
    assert runner.calls == [["espeak-ng", "-v", "yue", "--", "早晨"]]


def test_null_speaker_records_utterances() -> None:
    speaker = NullSpeaker()

    assert speaker.speak("one") is True
    assert speaker.speak("two") is True
    assert speaker.spoken == ["one", "two"]


def test_transcript_reaches_speaker_exactly_once(tmp_path: Path) -> None:
    runner = RecordingRunner()
    speaker = build_speaker(
        "en",
        data_dir=_voice_dir(tmp_path, "en_US-lessac-medium"),
        runner=runner,
        which=_which_returning("/usr/bin/piper"),
    )

    speaker.speak("mock:17:deadbeef")

    assert len(runner.calls) == 1


# --- missing engine / voice -> warning, no crash -----------------------------


def test_missing_engine_warns_with_install_command(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = RecordingRunner()
    speaker = build_speaker("yue", runner=runner, which=_which_returning(None))

    with caplog.at_level(logging.WARNING):
        assert speaker.speak("hi") is False

    assert runner.calls == []  # never spawned
    assert "espeak-ng" in caplog.text
    assert "apt install espeak-ng" in caplog.text


def test_missing_piper_voice_warns_with_download_command(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runner = RecordingRunner()
    # Engine present, but the voice model was never downloaded.
    speaker = build_speaker(
        "zh",
        data_dir=tmp_path,
        runner=runner,
        which=_which_returning("/usr/bin/piper"),
    )

    with caplog.at_level(logging.WARNING):
        assert speaker.speak("你好") is False

    assert runner.calls == []
    assert "zh_CN-huayan-medium" in caplog.text
    assert "download_voices" in caplog.text


def test_nonzero_exit_warns_and_returns_false(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runner = RecordingRunner(returncode=1, stderr="voice load failed")
    speaker = build_speaker(
        "en",
        data_dir=_voice_dir(tmp_path, "en_US-lessac-medium"),
        runner=runner,
        which=_which_returning("/usr/bin/piper"),
    )

    with caplog.at_level(logging.WARNING):
        assert speaker.speak("hi") is False

    assert "voice load failed" in caplog.text


def test_oserror_does_not_crash_the_stream(caplog: pytest.LogCaptureFixture) -> None:
    def exploding_runner(argv, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("no such binary")

    speaker = build_speaker(
        "yue", runner=exploding_runner, which=_which_returning("/usr/bin/espeak-ng")
    )

    with caplog.at_level(logging.WARNING):
        assert speaker.speak("hi") is False

    assert "could not start" in caplog.text


def test_blank_text_is_a_noop(tmp_path: Path) -> None:
    runner = RecordingRunner()
    speaker = build_speaker(
        "en",
        data_dir=_voice_dir(tmp_path, "en_US-lessac-medium"),
        runner=runner,
        which=_which_returning("/usr/bin/piper"),
    )

    assert speaker.speak("   ") is True
    assert runner.calls == []


# --- half-duplex gate: toggles exactly once around one utterance --------------


def test_speaking_flag_is_true_during_and_false_after(tmp_path: Path) -> None:
    gate = FakeGate()
    observed: list[bool] = []

    def observing_runner(argv, **kwargs):  # type: ignore[no-untyped-def]
        observed.append(gate.speaking)  # must be True while speaking
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    speaker = build_speaker(
        "en",
        data_dir=_voice_dir(tmp_path, "en_US-lessac-medium"),
        runner=observing_runner,
        which=_which_returning("/usr/bin/piper"),
        gate=gate,
    )

    assert gate.speaking is False
    speaker.speak("hello")
    assert observed == [True]
    assert gate.speaking is False  # cleared after


def test_speaking_flag_clears_even_when_engine_raises() -> None:
    gate = FakeGate()

    def exploding_runner(argv, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("boom")

    speaker = build_speaker(
        "yue",
        runner=exploding_runner,
        which=_which_returning("/usr/bin/espeak-ng"),
        gate=gate,
    )

    speaker.speak("hi")

    assert gate.speaking is False  # try/finally cleared it


def test_speaking_flag_toggles_exactly_once_per_utterance(tmp_path: Path) -> None:
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

    speaker = build_speaker(
        "en",
        data_dir=_voice_dir(tmp_path, "en_US-lessac-medium"),
        runner=RecordingRunner(),
        which=_which_returning("/usr/bin/piper"),
        gate=TracingGate(),
    )

    speaker.speak("one two three")

    assert transitions == [True, False]


def test_speaking_flag_untouched_when_engine_missing() -> None:
    gate = FakeGate()
    speaker = build_speaker(
        "yue", runner=RecordingRunner(), which=_which_returning(None), gate=gate
    )

    speaker.speak("hi")

    # No audio attempted -> no half-duplex window opened.
    assert gate.speaking is False


def test_callbacks_alternative_to_gate(tmp_path: Path) -> None:
    events: list[str] = []
    speaker = build_speaker(
        "en",
        data_dir=_voice_dir(tmp_path, "en_US-lessac-medium"),
        runner=RecordingRunner(),
        which=_which_returning("/usr/bin/piper"),
        on_speak_start=lambda: events.append("start"),
        on_speak_end=lambda: events.append("end"),
    )

    speaker.speak("hello")

    assert events == ["start", "end"]


# --- integration with task 10's audio gate -----------------------------------


def test_task10_half_duplex_gate_satisfies_speaking_gate() -> None:
    from ecosys_pi.audio import HalfDuplexGate

    gate = HalfDuplexGate()

    assert isinstance(gate, tts_mod.SpeakingGate)
    assert gate.speaking is False


def test_command_speaker_drives_task10_gate(tmp_path: Path) -> None:
    from ecosys_pi.audio import HalfDuplexGate

    gate = HalfDuplexGate()
    speaker = build_speaker(
        "en",
        data_dir=_voice_dir(tmp_path, "en_US-lessac-medium"),
        runner=RecordingRunner(),
        which=_which_returning("/usr/bin/piper"),
        gate=gate,
    )

    speaker.speak("hello")

    assert gate.speaking is False
