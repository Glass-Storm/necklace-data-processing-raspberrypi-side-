"""Language-selected local text-to-speech for received transcripts.

The Pi receives ``transcript`` frames from the hub and speaks them on its own
speaker. Which engine speaks is selected by ``PI_ECOSYS_TTS_LANG`` (see
:mod:`ecosys_pi.config`); the value never changes what the hub *recognises*, only
which local voice plays (the frozen contract has no language field).

What the Pi actually says
-------------------------

With the hub's **default mock STT** the "transcript" is a deterministic test
string of the form ``mock:<len>:<hash>``. Speaking it proves the **plumbing** -
pair -> stream -> transcript -> local audio - and is **NOT speech recognition**.
Do not mistake a spoken ``mock:...`` for a real transcript.

Two stacks, two failure modes
-----------------------------

``yue`` (Cantonese) uses ``espeak-ng``, a formant synthesiser whose voice data
ships as a separate package:

* command: ``espeak-ng -v yue -- <text>``
* install: ``sudo apt install espeak-ng espeak-ng-data``
* failure mode: the **binary** or the **voice data** may be absent. Either way
  the process fails and this module logs a warning naming that install command.

``zh`` (Mandarin) and ``en`` (English) use **Piper**, a neural TTS whose voices
are separate model files:

* ``zh`` -> voice ``zh_CN-huayan-medium``
* ``en`` -> voice ``en_US-lessac-medium``
* command: ``piper -m <voice> --data-dir <dir> -- <text>``
* install: ``uv add piper-tts`` - **NEVER pip** (this is a ``uv`` project).
* failure mode: the ``piper`` binary may be absent **or the voice model may not
  have been downloaded**; either way a warning names the exact install/download
  command. Piper is **ARCHIVED** (development moved to ``OHF-Voice/piper1-gpl``),
  so the voice files are pinned by URL in :data:`PIPER_VOICE_URLS` rather than
  fetched by a version range.

Piper has **no Cantonese voice**, which is exactly why ``yue`` falls back to the
robotic ``espeak-ng``.

Half-duplex (echo-loop guard)
-----------------------------

While an utterance plays, the Pi must not re-capture its own output. A
:class:`Speaker` therefore flips a half-duplex gate's ``speaking`` flag to
``True`` for the utterance's whole duration and back to ``False`` afterwards, in
a ``try/finally`` so it clears **even if the engine fails**. The gate is the
``speaking`` flag of the audio source from :mod:`ecosys_pi.audio` (task 10); the
minimal contract is the :class:`SpeakingGate` protocol (a settable ``speaking``
attribute). Alternatively pass ``on_speak_start`` / ``on_speak_end`` callbacks.

Testability
-----------

No engine is ever invoked in tests: the process runner (``subprocess.run``) and
the which-lookup (``shutil.which``) are injected, and :class:`NullSpeaker` makes
no sound at all. Tests assert the **exact argv** per language without audio.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ecosys_pi.config import SUPPORTED_TTS_LANGS

__all__ = [
    "DEFAULT_PIPER_DATA_DIR",
    "ENV_PIPER_DATA_DIR",
    "ESPEAK_ENGINE",
    "ESPEAK_YUE_VOICE",
    "PIPER_ENGINE",
    "PIPER_VOICES",
    "PIPER_VOICE_URLS",
    "CommandSpeaker",
    "NullSpeaker",
    "Speaker",
    "SpeakingGate",
    "TtsPlan",
    "UnsupportedLanguageError",
    "build_argv",
    "build_speaker",
    "piper_data_dir",
    "plan_for_lang",
]

_LOGGER = logging.getLogger(__name__)

# --- Engines and voices ------------------------------------------------------

#: Cantonese + phonemisation engine (apt ``espeak-ng``; voice data separate).
ESPEAK_ENGINE = "espeak-ng"

#: The ``espeak-ng`` voice name for Cantonese.
ESPEAK_YUE_VOICE = "yue"

#: Neural TTS engine. Installed with ``uv add piper-tts`` - NEVER pip.
PIPER_ENGINE = "piper"

#: Language -> Piper voice model name. Piper has NO Cantonese voice.
PIPER_VOICES: Mapping[str, str] = {
    "zh": "zh_CN-huayan-medium",
    "en": "en_US-lessac-medium",
}

#: Voice files are pinned by URL because Piper is ARCHIVED (Oct 2025). Both the
#: ``.onnx`` model and its ``.onnx.json`` config are required by Piper.
_PIPER_VOICE_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
PIPER_VOICE_URLS: Mapping[str, tuple[str, str]] = {
    voice: (
        f"{_PIPER_VOICE_BASE}/{path}/{voice}.onnx",
        f"{_PIPER_VOICE_BASE}/{path}/{voice}.onnx.json",
    )
    for voice, path in (
        ("zh_CN-huayan-medium", "zh/zh_CN/huayan/medium"),
        ("en_US-lessac-medium", "en/en_US/lessac/medium"),
    )
}

#: Environment variable overriding where Piper looks for voice models.
ENV_PIPER_DATA_DIR = "PI_ECOSYS_PIPER_DATA_DIR"

#: Default voice directory (``$PI_ECOSYS_PIPER_DATA_DIR`` overrides).
DEFAULT_PIPER_DATA_DIR = Path.home() / ".local" / "share" / "piper-voices"

#: Install hints surfaced in warnings (never crash the transcript stream).
_ESPEAK_INSTALL_HINT = "sudo apt install espeak-ng espeak-ng-data"
_PIPER_INSTALL_HINT = "uv add piper-tts (never pip)"


class UnsupportedLanguageError(ValueError):
    """Raised for a TTS language outside :data:`SUPPORTED_TTS_LANGS`.

    Subclasses :class:`ValueError` so callers may catch either. Raised *before*
    any synthesis is attempted: an unknown language fails fast.
    """


# --- Half-duplex gate contract ----------------------------------------------


@runtime_checkable
class SpeakingGate(Protocol):
    """The half-duplex flag owned by the audio source (task 10).

    The concrete source exposes ``speaking`` as a settable ``bool``; while it is
    ``True`` the source must yield no captured frames (echo-loop guard). The
    protocol is intentionally minimal so an in-flight task 10 and this module
    agree on the one thing that matters.
    """

    speaking: bool


# --- Language -> plan --------------------------------------------------------


@dataclass(frozen=True)
class TtsPlan:
    """Everything needed to speak one language, resolved but not yet run."""

    #: Language code, one of :data:`SUPPORTED_TTS_LANGS`.
    lang: str
    #: Executable name (``espeak-ng`` or ``piper``).
    engine: str
    #: espeak-ng voice name, or Piper voice model name.
    voice: str
    #: Piper-only voice directory; ``None`` for espeak-ng.
    data_dir: Path | None
    #: Command that installs the engine when it is missing.
    install_hint: str
    #: Command that fetches the voice/model when it is missing.
    download_hint: str


def piper_data_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Resolve the Piper voice directory.

    ``PI_ECOSYS_PIPER_DATA_DIR`` wins when it is set and non-blank; otherwise
    :data:`DEFAULT_PIPER_DATA_DIR`.
    """
    env = os.environ if environ is None else environ
    override = (env.get(ENV_PIPER_DATA_DIR) or "").strip()
    return Path(override) if override else DEFAULT_PIPER_DATA_DIR


def plan_for_lang(
    lang: str,
    *,
    environ: Mapping[str, str] | None = None,
    data_dir: Path | None = None,
) -> TtsPlan:
    """Map a language code to its engine/voice.

    ``yue`` -> ``espeak-ng -v yue``; ``zh`` -> Piper ``zh_CN-huayan-medium``;
    ``en`` -> Piper ``en_US-lessac-medium``.

    :raises UnsupportedLanguageError: for any language outside
        :data:`SUPPORTED_TTS_LANGS` (fail fast, before any process is spawned).
    """
    if lang not in SUPPORTED_TTS_LANGS:
        raise UnsupportedLanguageError(
            f"unsupported TTS language {lang!r}; "
            f"expected one of {sorted(SUPPORTED_TTS_LANGS)}"
        )

    if lang == "yue":
        return TtsPlan(
            lang=lang,
            engine=ESPEAK_ENGINE,
            voice=ESPEAK_YUE_VOICE,
            data_dir=None,
            install_hint=_ESPEAK_INSTALL_HINT,
            download_hint=_ESPEAK_INSTALL_HINT,
        )

    voice = PIPER_VOICES[lang]
    resolved_dir = data_dir if data_dir is not None else piper_data_dir(environ)
    return TtsPlan(
        lang=lang,
        engine=PIPER_ENGINE,
        voice=voice,
        data_dir=resolved_dir,
        install_hint=_PIPER_INSTALL_HINT,
        download_hint=(
            f"uv run python -m piper.download_voices {voice} --data-dir {resolved_dir}"
        ),
    )


# --- Exact argv per engine ---------------------------------------------------


def build_argv(plan: TtsPlan, text: str) -> list[str]:
    """Build the exact argv for one utterance.

    Text is passed **after ``--``** so a transcript that begins with ``-`` is
    never mistaken for an option (transcripts are attacker-influenced input).

    * espeak-ng: ``espeak-ng -v yue -- <text>``
    * Piper: ``piper -m <voice> --data-dir <dir> -- <text>``
    """
    if plan.engine == ESPEAK_ENGINE:
        return [ESPEAK_ENGINE, "-v", plan.voice, "--", text]

    if plan.engine == PIPER_ENGINE:
        if plan.data_dir is None:
            raise UnsupportedLanguageError("a Piper plan must carry a voice directory")
        return [
            PIPER_ENGINE,
            "-m",
            plan.voice,
            "--data-dir",
            str(plan.data_dir),
            "--",
            text,
        ]

    # Unreachable via plan_for_lang, but keep the failure explicit.
    raise UnsupportedLanguageError(f"no argv builder for engine {plan.engine!r}")


# --- Speaker interface -------------------------------------------------------


@runtime_checkable
class Speaker(Protocol):
    """Anything that can speak a transcript.

    :meth:`speak` returns ``True`` when audio was played and ``False`` when it
    could not be (missing engine/voice). It MUST NOT raise for a runtime audio
    failure - the transcript stream must keep flowing.
    """

    def speak(self, text: str) -> bool:
        """Speak ``text``; return whether audio was actually played."""
        ...


@dataclass
class NullSpeaker:
    """A silent :class:`Speaker` for tests and dry runs.

    It records every utterance in :attr:`spoken` so a test can assert a
    transcript reached the speaker **exactly once**, without any audio.
    """

    spoken: list[str] = field(default_factory=list)

    def speak(self, text: str) -> bool:
        self.spoken.append(text)
        return True


class CommandSpeaker:
    """A :class:`Speaker` that shells out to espeak-ng or piper.

    The process runner and the which-lookup are **injected** so tests can assert
    the exact argv and simulate a missing engine without ever invoking audio.

    :param plan: resolved engine/voice (see :func:`plan_for_lang`).
    :param runner: called as ``runner(argv, check=False, capture_output=True,
        text=True)``; defaults to :func:`subprocess.run`.
    :param which: executable lookup; defaults to :func:`shutil.which`.
    :param gate: half-duplex flag flipped ``True``/``False`` around the
        utterance (see :class:`SpeakingGate`).
    :param on_speak_start: alternative to ``gate``; called before playback.
    :param on_speak_end: called after playback in a ``finally``.
    :param logger: logger for the "engine/voice missing" warnings.
    """

    def __init__(
        self,
        plan: TtsPlan,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        which: Callable[[str], str | None] | None = None,
        gate: SpeakingGate | None = None,
        on_speak_start: Callable[[], None] | None = None,
        on_speak_end: Callable[[], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._plan = plan
        self._runner = runner if runner is not None else subprocess.run
        self._which = which if which is not None else shutil.which
        self._gate = gate
        self._on_speak_start = on_speak_start
        self._on_speak_end = on_speak_end
        self._logger = logger if logger is not None else _LOGGER

    @property
    def plan(self) -> TtsPlan:
        """The resolved :class:`TtsPlan` this speaker was built from."""
        return self._plan

    # -- half-duplex ----------------------------------------------------------

    def _begin(self) -> None:
        if self._gate is not None:
            self._gate.speaking = True
        if self._on_speak_start is not None:
            self._on_speak_start()

    def _end(self) -> None:
        if self._gate is not None:
            self._gate.speaking = False
        if self._on_speak_end is not None:
            self._on_speak_end()

    # -- availability ---------------------------------------------------------

    def _engine_available(self) -> bool:
        """Check engine (and Piper voice) presence, warning instead of raising."""
        plan = self._plan

        if self._which(plan.engine) is None:
            self._logger.warning(
                "TTS engine %r not found; install with: %s",
                plan.engine,
                plan.install_hint,
            )
            return False

        if plan.engine == PIPER_ENGINE:
            if plan.data_dir is None:
                return False
            model = plan.data_dir / f"{plan.voice}.onnx"
            if not model.exists():
                self._logger.warning(
                    "Piper voice %r missing at %s; fetch it with: %s",
                    plan.voice,
                    model,
                    plan.download_hint,
                )
                return False

        return True

    # -- speak ----------------------------------------------------------------

    def speak(self, text: str) -> bool:
        """Speak ``text`` via the selected engine.

        Blank text is a no-op. When the engine/voice is missing a warning naming
        the install command is logged and ``False`` is returned - the stream is
        never interrupted. ``speaking`` is flipped exactly once per utterance
        and always cleared, even on failure.
        """
        if not text or not text.strip():
            return True

        if not self._engine_available():
            return False

        plan = self._plan
        argv = build_argv(plan, text)

        # The gate toggles exactly once around the utterance.
        self._begin()
        try:
            completed = self._runner(argv, check=False, capture_output=True, text=True)
            if completed.returncode != 0:
                self._logger.warning(
                    "TTS %s exited %s: %s",
                    plan.engine,
                    completed.returncode,
                    (completed.stderr or "").strip(),
                )
                return False
            return True
        except OSError as exc:
            self._logger.warning(
                "TTS %s could not start: %s (install: %s)",
                plan.engine,
                exc,
                plan.install_hint,
            )
            return False
        finally:
            self._end()


def build_speaker(
    lang: str,
    *,
    environ: Mapping[str, str] | None = None,
    data_dir: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    which: Callable[[str], str | None] | None = None,
    gate: SpeakingGate | None = None,
    on_speak_start: Callable[[], None] | None = None,
    on_speak_end: Callable[[], None] | None = None,
) -> CommandSpeaker:
    """Build the language-selected :class:`CommandSpeaker`.

    :raises UnsupportedLanguageError: for an unknown language (fail fast).
    """
    plan = plan_for_lang(lang, environ=environ, data_dir=data_dir)
    return CommandSpeaker(
        plan,
        runner=runner,
        which=which,
        gate=gate,
        on_speak_start=on_speak_start,
        on_speak_end=on_speak_end,
    )
