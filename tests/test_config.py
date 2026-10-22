"""Tests for :mod:`ecosys_pi.config`: env parsing, defaults, TTS fallback."""

from __future__ import annotations

import dataclasses
import json
import os
import socket
from pathlib import Path

import pytest

from ecosys_pi import config as config_mod
from ecosys_pi.config import (
    DEFAULT_CONFIG_FILENAME,
    DEFAULT_DOTENV_FILENAME,
    DEFAULT_ENROLLMENT_SECONDS,
    DEFAULT_HUB_PORT,
    DEFAULT_TTS_LANG,
    DEFAULT_WIFI_SECURITY,
    SUPPORTED_TTS_LANGS,
    SUPPORTED_WIFI_SECURITY,
    Config,
    load_config,
)


def _write_json(path: Path, data: dict[str, object]) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_dotenv(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_defaults_when_environment_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty environment yields documented defaults, device name from host."""
    monkeypatch.setattr(socket, "gethostname", lambda: "pi-bench")

    cfg = load_config({})

    assert cfg.hub is None
    assert cfg.tts_lang == DEFAULT_TTS_LANG == "yue"
    assert cfg.device_name == "pi-bench"


def test_environment_overrides_every_value() -> None:
    cfg = load_config(
        {
            "PI_ECOSYS_HUB": "192.168.1.10:50051",
            "PI_ECOSYS_WIFI_SECURITY": "wpa3",
            "PI_ECOSYS_TTS_LANG": "en",
            "PI_ECOSYS_DEVICE_NAME": "front-door-pi",
        }
    )

    assert cfg.hub == "192.168.1.10:50051"
    assert cfg.wifi_security == "wpa3"
    assert cfg.tts_lang == "en"
    assert cfg.device_name == "front-door-pi"


def test_blank_values_are_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: "host-pi")

    cfg = load_config(
        {
            "PI_ECOSYS_HUB": "   ",
            "PI_ECOSYS_TTS_LANG": "   ",
            "PI_ECOSYS_DEVICE_NAME": "  ",
        }
    )

    # hub is absent; lang falls back; device name falls back to hostname.
    assert cfg.hub is None
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


def test_wifi_security_defaults_and_overrides() -> None:
    assert load_config({}).wifi_security == DEFAULT_WIFI_SECURITY == "wpa2"
    assert load_config({"PI_ECOSYS_WIFI_SECURITY": "wpa3"}).wifi_security == "wpa3"


def test_unknown_wifi_security_falls_back_to_wpa2() -> None:
    cfg = load_config({"PI_ECOSYS_WIFI_SECURITY": "wep"})

    assert cfg.wifi_security == DEFAULT_WIFI_SECURITY
    assert cfg.wifi_security in SUPPORTED_WIFI_SECURITY


def test_load_config_reads_os_environ_when_not_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PI_ECOSYS_HUB", "127.0.0.1:9")
    monkeypatch.setenv("PI_ECOSYS_TTS_LANG", "zh")

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


# ---------------------------------------------------------------------------
# Config-file + dotenv layer (task 13)
#
# Precedence, highest first:
#   real process env  >  .env  >  config file (JSON)  >  built-in defaults
# ---------------------------------------------------------------------------


def test_new_fields_have_documented_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no files and an empty env, the new settings fall back to defaults."""
    monkeypatch.setattr(socket, "gethostname", lambda: "pi-bench")

    cfg = load_config(
        {},
        config_path=tmp_path / "absent.json",
        dotenv_path=tmp_path / "absent.env",
    )

    assert cfg.ssid is None
    assert cfg.password is None
    assert cfg.wifi_security == DEFAULT_WIFI_SECURITY == "wpa2"
    assert cfg.enrollment_seconds == DEFAULT_ENROLLMENT_SECONDS == 8
    assert cfg.hub_port == DEFAULT_HUB_PORT == 50051


def test_config_file_values_are_loaded(tmp_path: Path) -> None:
    """A config.json supplies values when the environment is empty."""
    cfg_path = _write_json(
        tmp_path / "config.json",
        {
            "ssid": "ecosys-hotspot",
            "password": "hunter2",
            "wifi_security": "wpa3",
            "tts_lang": "zh",
            "device_name": "necklace",
            "enrollment_seconds": 12,
            "hub_port": 60061,
            "hub": "10.0.0.5:50051",
        },
    )

    cfg = load_config({}, config_path=cfg_path, dotenv_path=tmp_path / "absent.env")

    assert cfg.ssid == "ecosys-hotspot"
    assert cfg.password == "hunter2"
    assert cfg.wifi_security == "wpa3"
    assert cfg.tts_lang == "zh"
    assert cfg.device_name == "necklace"
    assert cfg.enrollment_seconds == 12
    assert cfg.hub_port == 60061
    assert cfg.hub == "10.0.0.5:50051"


def test_dotenv_overrides_config_file(tmp_path: Path) -> None:
    """A value in .env wins over the same key in the config file."""
    cfg_path = _write_json(tmp_path / "config.json", {"tts_lang": "zh", "hub_port": 1})
    dotenv_path = _write_dotenv(
        tmp_path / ".env", ["PI_ECOSYS_TTS_LANG=en", "PI_ECOSYS_HUB_PORT=2"]
    )

    cfg = load_config({}, config_path=cfg_path, dotenv_path=dotenv_path)

    assert cfg.tts_lang == "en"
    assert cfg.hub_port == 2


def test_real_env_overrides_dotenv(tmp_path: Path) -> None:
    """A real process env var wins over the same key in .env."""
    dotenv_path = _write_dotenv(tmp_path / ".env", ["PI_ECOSYS_TTS_LANG=en"])

    cfg = load_config(
        {"PI_ECOSYS_TTS_LANG": "yue"},
        config_path=tmp_path / "absent.json",
        dotenv_path=dotenv_path,
    )

    assert cfg.tts_lang == "yue"


def test_all_four_layers_resolve_independently(tmp_path: Path) -> None:
    """Each setting can come from a different precedence layer at once."""
    cfg_path = _write_json(tmp_path / "config.json", {"device_name": "from-file"})
    dotenv_path = _write_dotenv(tmp_path / ".env", ["PI_ECOSYS_SSID=from-dotenv"])

    cfg = load_config(
        {"PI_ECOSYS_TTS_LANG": "en"},
        config_path=cfg_path,
        dotenv_path=dotenv_path,
    )

    # real env
    assert cfg.tts_lang == "en"
    # .env
    assert cfg.ssid == "from-dotenv"
    # config file
    assert cfg.device_name == "from-file"
    # built-in defaults
    assert cfg.enrollment_seconds == DEFAULT_ENROLLMENT_SECONDS
    assert cfg.hub_port == DEFAULT_HUB_PORT


def test_higher_layer_wins_per_key(tmp_path: Path) -> None:
    """Real env beats .env beats file beats defaults for the SAME key."""
    cfg_path = _write_json(tmp_path / "config.json", {"pi_ecosys_hub_port": 4})
    dotenv_path = _write_dotenv(tmp_path / ".env", ["PI_ECOSYS_HUB_PORT=5"])

    from_env = load_config(
        {"PI_ECOSYS_HUB_PORT": "6"}, config_path=cfg_path, dotenv_path=dotenv_path
    )
    from_dotenv = load_config({}, config_path=cfg_path, dotenv_path=dotenv_path)
    from_file = load_config(
        {}, config_path=cfg_path, dotenv_path=tmp_path / "absent.env"
    )

    assert from_env.hub_port == 6
    assert from_dotenv.hub_port == 5
    assert from_file.hub_port == 4


def test_blank_higher_layer_falls_through_to_lower(tmp_path: Path) -> None:
    """A blank value is treated as unset, so the lower layer supplies the value."""
    cfg_path = _write_json(tmp_path / "config.json", {"tts_lang": "zh"})
    dotenv_path = _write_dotenv(tmp_path / ".env", ["PI_ECOSYS_TTS_LANG="])

    cfg = load_config({}, config_path=cfg_path, dotenv_path=dotenv_path)

    assert cfg.tts_lang == "zh"


def test_env_names_and_short_keys_are_both_accepted(tmp_path: Path) -> None:
    """Files may use the canonical PI_ECOSYS_* names or the short setting keys."""
    cfg_path = _write_json(tmp_path / "config.json", {"ssid": "short-key-ssid"})
    dotenv_path = _write_dotenv(
        tmp_path / ".env", ["PI_ECOSYS_HOTSPOT_PASSWORD=env-name-pw"]
    )

    cfg = load_config({}, config_path=cfg_path, dotenv_path=dotenv_path)

    assert cfg.ssid == "short-key-ssid"
    assert cfg.password == "env-name-pw"


def test_invalid_enrollment_seconds_falls_back_to_default(tmp_path: Path) -> None:
    cfg_path = _write_json(
        tmp_path / "config.json", {"enrollment_seconds": "not-a-number"}
    )

    cfg = load_config({}, config_path=cfg_path, dotenv_path=tmp_path / "absent.env")

    assert cfg.enrollment_seconds == DEFAULT_ENROLLMENT_SECONDS


def test_nonpositive_enrollment_seconds_falls_back_to_default(tmp_path: Path) -> None:
    cfg_path = _write_json(tmp_path / "config.json", {"enrollment_seconds": 0})

    cfg = load_config({}, config_path=cfg_path, dotenv_path=tmp_path / "absent.env")

    assert cfg.enrollment_seconds == DEFAULT_ENROLLMENT_SECONDS


def test_invalid_hub_port_falls_back_to_default(tmp_path: Path) -> None:
    cfg_path = _write_json(tmp_path / "config.json", {"hub_port": "abc"})

    cfg = load_config({}, config_path=cfg_path, dotenv_path=tmp_path / "absent.env")

    assert cfg.hub_port == DEFAULT_HUB_PORT


def test_out_of_range_hub_port_falls_back_to_default(tmp_path: Path) -> None:
    cfg_path = _write_json(tmp_path / "config.json", {"hub_port": 99999})

    cfg = load_config({}, config_path=cfg_path, dotenv_path=tmp_path / "absent.env")

    assert cfg.hub_port == DEFAULT_HUB_PORT


def test_missing_config_file_is_not_an_error(tmp_path: Path) -> None:
    """A missing config file falls back to defaults without raising."""
    cfg = load_config(
        {},
        config_path=tmp_path / "does-not-exist.json",
        dotenv_path=tmp_path / "does-not-exist.env",
    )

    assert isinstance(cfg, Config)
    assert cfg.enrollment_seconds == DEFAULT_ENROLLMENT_SECONDS


def test_malformed_config_file_is_not_an_error(tmp_path: Path) -> None:
    """A malformed JSON file is ignored rather than crashing the client."""
    bad = tmp_path / "config.json"
    bad.write_text("{ this is not json", encoding="utf-8")

    cfg = load_config({}, config_path=bad, dotenv_path=tmp_path / "absent.env")

    assert cfg.enrollment_seconds == DEFAULT_ENROLLMENT_SECONDS


def test_config_file_with_non_object_root_is_ignored(tmp_path: Path) -> None:
    """A JSON list/string root is ignored instead of crashing."""
    bad = tmp_path / "config.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")

    cfg = load_config({}, config_path=bad, dotenv_path=tmp_path / "absent.env")

    assert cfg.hub is None


def test_dotenv_values_do_not_mutate_os_environ(tmp_path: Path) -> None:
    """load_config must never mutate the real process environment."""
    sentinel = "PI_ECOSYS_SSID_LEAK_SENTINEL"
    dotenv_path = _write_dotenv(tmp_path / ".env", [f"{sentinel}=leaked"])

    load_config({}, config_path=tmp_path / "absent.json", dotenv_path=dotenv_path)

    assert sentinel not in os.environ


def test_discovery_is_gated_on_environ_for_hermetic_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit environ + no paths loads no files (hermetic unit runs)."""
    monkeypatch.chdir(tmp_path)
    _write_json(tmp_path / DEFAULT_CONFIG_FILENAME, {"tts_lang": "zh"})

    cfg = load_config({})

    # The on-disk config file is ignored because environ was injected.
    assert cfg.tts_lang == DEFAULT_TTS_LANG


def test_default_paths_discovered_from_cwd_when_environ_not_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real run (environ=None) discovers .env and config.json from cwd."""
    monkeypatch.chdir(tmp_path)
    _write_json(tmp_path / DEFAULT_CONFIG_FILENAME, {"tts_lang": "zh"})
    _write_dotenv(tmp_path / DEFAULT_DOTENV_FILENAME, ["PI_ECOSYS_DEVICE_NAME=cwd-pi"])

    clean_env = {k: v for k, v in os.environ.items() if not k.startswith("PI_ECOSYS_")}
    monkeypatch.setattr(os, "environ", clean_env)

    cfg = load_config()

    assert cfg.tts_lang == "zh"
    assert cfg.device_name == "cwd-pi"


def test_password_is_never_leaked_by_repr() -> None:
    cfg = load_config({"PI_ECOSYS_HOTSPOT_PASSWORD": "top-secret-pw"})

    text = repr(cfg)

    assert "top-secret-pw" not in text
    assert "<set>" in text


def test_config_example_documents_every_setting(tmp_path: Path) -> None:
    """The tracked example names every Config field and loads without error."""
    example = Path(__file__).resolve().parents[1] / "config.example.json"
    assert example.is_file(), f"missing tracked example file: {example}"

    data = json.loads(example.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    for field in dataclasses.fields(Config):
        assert field.name in data, f"config.example.json omits {field.name!r}"

    # The example is safe to load: unknown keys such as _comment are ignored and
    # the documented keys round-trip into the parsed config.
    cfg = load_config({}, config_path=example, dotenv_path=tmp_path / "absent.env")
    assert cfg.hub is not None
    assert cfg.ssid is not None
    assert cfg.password is not None
    assert cfg.tts_lang in SUPPORTED_TTS_LANGS
    assert cfg.device_name
    assert cfg.wifi_security == DEFAULT_WIFI_SECURITY
    assert cfg.enrollment_seconds == DEFAULT_ENROLLMENT_SECONDS
    assert cfg.hub_port == DEFAULT_HUB_PORT
