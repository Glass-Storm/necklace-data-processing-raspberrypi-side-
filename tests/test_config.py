"""Tests for :mod:`ecosys_pi.config`: env parsing, defaults, TTS fallback."""

from __future__ import annotations

import socket

import pytest

from ecosys_pi import config as config_mod
from ecosys_pi.config import (
    DEFAULT_TTS_LANG,
    SUPPORTED_TTS_LANGS,
    Config,
    load_config,
)


def test_defaults_when_environment_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty environment yields documented defaults, device name from host."""
    monkeypatch.setattr(socket, "gethostname", lambda: "pi-bench")

    cfg = load_config({})

    assert cfg.hub is None
    assert cfg.pin is None
    assert cfg.tts_lang == DEFAULT_TTS_LANG == "yue"
    assert cfg.device_name == "pi-bench"


def test_environment_overrides_every_value() -> None:
    cfg = load_config(
        {
            "PI_ECOSYS_HUB": "192.168.1.10:50051",
            "PI_ECOSYS_PIN": "482915",
            "PI_ECOSYS_TTS_LANG": "en",
            "PI_ECOSYS_DEVICE_NAME": "front-door-pi",
        }
    )

    assert cfg.hub == "192.168.1.10:50051"
    assert cfg.pin == "482915"
    assert cfg.tts_lang == "en"
    assert cfg.device_name == "front-door-pi"


def test_blank_values_are_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: "host-pi")

    cfg = load_config(
        {
            "PI_ECOSYS_HUB": "   ",
            "PI_ECOSYS_PIN": "",
            "PI_ECOSYS_TTS_LANG": "   ",
            "PI_ECOSYS_DEVICE_NAME": "  ",
        }
    )

    # hub/pin are absent; lang falls back; device name falls back to hostname.
    assert cfg.hub is None
    assert cfg.pin is None
    assert cfg.tts_lang == DEFAULT_TTS_LANG
    assert cfg.device_name == "host-pi"


def test_unknown_tts_lang_falls_back_to_documented_default() -> None:
    cfg = load_config({"PI_ECOSYS_TTS_LANG": "klingon"})

    assert cfg.tts_lang == DEFAULT_TTS_LANG
    assert cfg.tts_lang in SUPPORTED_TTS_LANGS


@pytest.mark.parametrize("lang", ["yue", "zh", "en"])
def test_supported_langs_are_accepted(lang: str) -> None:
    assert load_config({"PI_ECOSYS_TTS_LANG": lang}).tts_lang == lang


def test_tts_lang_is_case_sensitive_and_unknown_case_falls_back() -> None:
    # The documented set is lowercase; "EN" is not a member and falls back.
    assert load_config({"PI_ECOSYS_TTS_LANG": "EN"}).tts_lang == DEFAULT_TTS_LANG


def test_device_name_never_blank_when_hostname_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: "   ")

    cfg = load_config({})

    assert cfg.device_name.strip() != ""
    assert cfg.device_name == "ecosys-pi"


def test_pin_is_never_leaked_by_repr() -> None:
    cfg = load_config({"PI_ECOSYS_PIN": "top-secret-pin"})

    text = repr(cfg)

    assert "top-secret-pin" not in text
    assert "<set>" in text


def test_load_config_reads_os_environ_when_not_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PI_ECOSYS_HUB", "127.0.0.1:9")
    monkeypatch.setenv("PI_ECOSYS_TTS_LANG", "zh")
    monkeypatch.delenv("PI_ECOSYS_PIN", raising=False)

    cfg = load_config()

    assert cfg.hub == "127.0.0.1:9"
    assert cfg.tts_lang == "zh"


def test_config_is_frozen() -> None:
    cfg = load_config({})
    assert isinstance(cfg, Config)
    with pytest.raises(Exception):
        cfg.tts_lang = "en"  # type: ignore[misc]


def test_default_tts_lang_is_a_supported_member() -> None:
    assert DEFAULT_TTS_LANG in SUPPORTED_TTS_LANGS
    assert config_mod.DEFAULT_TTS_LANG == DEFAULT_TTS_LANG
