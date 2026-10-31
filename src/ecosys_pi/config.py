"""Runtime configuration for the ecosys Pi client.

``load_config`` is the single entry point. It merges four layers, highest
precedence first:

1. **Real process environment** (``os.environ``) — only the ``PI_ECOSYS_*``
   names are honoured here, so an unrelated OS variable named ``password`` or
   ``hub`` can never leak into the config.
2. **A ``.env`` file** (parsed with :func:`python_dotenv.dotenv_values`; the
   process environment is **never** mutated).
3. **A ``config.json`` file** — the same settings, expressed as JSON.
4. **Built-in defaults.**

For a single setting the highest layer that supplies a **non-blank** value wins;
a blank value is treated as unset and falls through to the next layer down.

Files and discovery
-------------------

By default the ``.env`` and ``config.json`` files are discovered in the current
working directory. When ``environ`` is passed explicitly (the injectable,
unit-test path) file discovery is **disabled** unless ``dotenv_path`` /
``config_path`` are given — so an injected run is hermetic and never depends on
whatever happens to sit in the working tree.

Settings
--------

========================  ============================  ==========================
Setting                   Canonical env var             Default
========================  ============================  ==========================
``hub``                   ``PI_ECOSYS_HUB``             *(unset)* → gateway discover
``wifi_security``         ``PI_ECOSYS_WIFI_SECURITY``   ``wpa2``
``tts_lang``              ``PI_ECOSYS_TTS_LANG``        ``yue``
``device_name``           ``PI_ECOSYS_DEVICE_NAME``     ``socket.gethostname()``
``ssid``                  ``PI_ECOSYS_SSID``            *(unset)*
``password``              ``PI_ECOSYS_HOTSPOT_PASSWORD``*(unset)*
``enrollment_seconds``    ``PI_ECOSYS_ENROLLMENT_SECONDS``      ``8``
``hub_port``              ``PI_ECOSYS_HUB_PORT``        ``9000``
========================  ============================  ==========================

Inside a ``.env`` or ``config.json`` file a setting may be spelled either with
its canonical ``PI_ECOSYS_*`` name or with its short key (e.g. ``ssid``); the
match is case-insensitive.

The hotspot password is a secret: :meth:`Config.__repr__` redacts it so it can
never leak through a stray ``print``/log of the config object.

systemd service-user caveat
---------------------------

This module reads the *process environment*. A systemd unit started for a
service user (``User=ecosys``) gets a **fresh, minimal environment** - it does
NOT inherit your login shell's exports. Set variables with ``Environment=`` /
``EnvironmentFile=`` in the unit, or point ``PI_ECOSYS_*`` at a ``.env`` /
``config.json`` file, or the defaults apply.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

__all__ = [
    "DEFAULT_CONFIG_FILENAME",
    "DEFAULT_DOTENV_FILENAME",
    "DEFAULT_ENROLLMENT_SECONDS",
    "DEFAULT_HUB_PORT",
    "DEFAULT_TTS_LANG",
    "DEFAULT_WIFI_SECURITY",
    "SUPPORTED_TTS_LANGS",
    "SUPPORTED_WIFI_SECURITY",
    "Config",
    "load_config",
]

#: Languages the local TTS layer can speak. See :mod:`ecosys_pi.tts` for the
#: per-language engine mapping.
SUPPORTED_TTS_LANGS: frozenset[str] = frozenset({"yue", "zh", "en"})

#: Documented fallback for ``PI_ECOSYS_TTS_LANG`` when it is blank or unknown.
DEFAULT_TTS_LANG: str = "yue"

#: Default voice-enrolment length in seconds (plan task 13).
DEFAULT_ENROLLMENT_SECONDS: int = 8

#: Default hub gRPC port, used when only the gateway host is known.
DEFAULT_HUB_PORT: int = 9000

#: Wi-Fi security modes ``nmcli`` may join. Mirrors
#: :data:`ecosys_pi.network.SUPPORTED_SECURITY`.
SUPPORTED_WIFI_SECURITY: frozenset[str] = frozenset({"wpa2", "wpa3"})

#: Security used when ``wifi_security`` is blank or unknown.
DEFAULT_WIFI_SECURITY: str = "wpa2"

#: File discovered in the working directory for the dotenv layer.
DEFAULT_DOTENV_FILENAME: str = ".env"

#: File discovered in the working directory for the JSON config layer.
DEFAULT_CONFIG_FILENAME: str = "config.json"

#: Fallback device label when the hostname is blank (e.g. a container).
_FALLBACK_DEVICE_NAME = "ecosys-pi"

#: The largest valid TCP/UDP port number.
_MAX_PORT = 65535

#: Per-setting accepted spellings, most-canonical first. Each tuple lists the
#: canonical ``PI_ECOSYS_*`` env var name (and any aliases) before the short
#: key(s). Matching is case-insensitive for the file layers; only names starting
#: ``PI_ECOSYS_`` are ever read from the real process environment.
_SETTING_ALIASES: Mapping[str, tuple[str, ...]] = {
    "hub": ("PI_ECOSYS_HUB", "hub"),
    "wifi_security": ("PI_ECOSYS_WIFI_SECURITY", "wifi_security"),
    "tts_lang": ("PI_ECOSYS_TTS_LANG", "tts_lang"),
    "device_name": ("PI_ECOSYS_DEVICE_NAME", "device_name"),
    "ssid": ("PI_ECOSYS_SSID", "ssid"),
    "password": (
        "PI_ECOSYS_HOTSPOT_PASSWORD",
        "PI_ECOSYS_PASSWORD",
        "hotspot_password",
        "password",
    ),
    "enrollment_seconds": ("PI_ECOSYS_ENROLLMENT_SECONDS", "enrollment_seconds"),
    "hub_port": ("PI_ECOSYS_HUB_PORT", "hub_port"),
}

#: Prefix that marks a name as a real-process-environment variable.
_ENV_PREFIX = "PI_ECOSYS_"


def _clean(value: str | None) -> str | None:
    """Return ``value`` stripped, or ``None`` when blank/unset."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _default_device_name() -> str:
    """Hostname, or a fixed label when the hostname is blank/unavailable."""
    try:
        hostname = socket.gethostname().strip()
    except OSError:  # pragma: no cover - hostname is essentially always available
        hostname = ""
    return hostname or _FALLBACK_DEVICE_NAME


def _normalise(source: Mapping[str, Any]) -> dict[str, Any]:
    """Return a case-insensitive view of ``source`` keyed by ``str(key).lower()``."""
    return {str(key).strip().lower(): value for key, value in source.items()}


def _lookup(
    source: Mapping[str, Any],
    aliases: tuple[str, ...],
    *,
    env_only: bool,
) -> str | None:
    """First non-blank value among ``aliases`` in a normalised ``source``.

    ``env_only`` restricts the search to ``PI_ECOSYS_*`` names so the real
    process environment cannot contribute a generic ``password``/``hub`` key.
    """
    for alias in aliases:
        key = alias.lower()
        if env_only and not key.startswith(_ENV_PREFIX.lower()):
            continue
        value = source.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _read_config_file(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Parse a JSON ``config.json``; a missing/malformed/non-object file is ``{}``."""
    if path is None:
        return {}
    resolved = Path(path)
    if not resolved.is_file():
        return {}
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_dotenv(path: str | os.PathLike[str] | None) -> dict[str, str]:
    """Parse a ``.env`` without mutating ``os.environ``; missing file is ``{}``."""
    if path is None:
        return {}
    resolved = Path(path)
    if not resolved.is_file():
        return {}
    try:
        values = dotenv_values(resolved)
    except OSError:  # pragma: no cover - is_file() already filtered the common case
        return {}
    return {key: value for key, value in values.items() if value is not None}


def _merge(
    *,
    config_file: Mapping[str, Any],
    dotenv: Mapping[str, Any],
    environ: Mapping[str, Any],
) -> dict[str, str]:
    """Resolve every setting with precedence env > .env > file > (defaults)."""
    layers = (
        (_normalise(config_file), False),
        (_normalise(dotenv), False),
        (_normalise(environ), True),
    )
    resolved: dict[str, str] = {}
    for setting, aliases in _SETTING_ALIASES.items():
        for source, env_only in layers:
            value = _lookup(source, aliases, env_only=env_only)
            if value is not None:
                resolved[setting] = value
    return resolved


def _bounded_int(
    raw: str | None, default: int, *, minimum: int, maximum: int | None = None
) -> int:
    """Parse ``raw`` as an int within ``[minimum, maximum]``, else ``default``."""
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


@dataclass(frozen=True)
class Config:
    """Immutable, validated runtime configuration.

    Build it with :func:`load_config` rather than calling the constructor so the
    environment/file parsing and fallbacks are applied consistently. The three
    original fields are positional for backward compatibility; every later field
    (``ssid``/``password``/``wifi_security``/``enrollment_seconds``/``hub_port``)
    carries a default so existing keyword constructions keep working unchanged.
    """

    #: ``HOST:PORT`` hub override, or ``None`` to use default-gateway discovery.
    hub: str | None
    #: One of :data:`SUPPORTED_TTS_LANGS`; always valid after :func:`load_config`.
    tts_lang: str
    #: Non-blank device label sent in the ``stream_open`` frame.
    device_name: str
    #: Hotspot SSID the Pi joins (task 12), or ``None`` when unset.
    ssid: str | None = None
    #: Hotspot WPA2/WPA3 password (task 12), or ``None`` when unset. A secret.
    password: str | None = None
    #: One of :data:`SUPPORTED_WIFI_SECURITY`; the ``nmcli`` key-management scheme.
    wifi_security: str = DEFAULT_WIFI_SECURITY
    #: Voice-enrolment capture length in seconds (task 13).
    enrollment_seconds: int = DEFAULT_ENROLLMENT_SECONDS
    #: Hub gRPC port, used when only the gateway host is known (task 12).
    hub_port: int = DEFAULT_HUB_PORT

    def __repr__(self) -> str:
        # NEVER leak the hotspot password through repr/print/logging.
        password = "<set>" if self.password is not None else "<unset>"
        return (
            f"Config(hub={self.hub!r}, tts_lang={self.tts_lang!r}, "
            f"device_name={self.device_name!r}, ssid={self.ssid!r}, "
            f"password={password}, wifi_security={self.wifi_security!r}, "
            f"enrollment_seconds={self.enrollment_seconds!r}, "
            f"hub_port={self.hub_port!r})"
        )


def load_config(
    environ: Mapping[str, str] | None = None,
    *,
    dotenv_path: str | os.PathLike[str] | None = None,
    config_path: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> Config:
    """Resolve configuration from env files and the process environment.

    Precedence, highest first: real process env > ``.env`` > ``config.json`` >
    built-in defaults. A blank value is treated as unset and falls through.

    ``environ`` defaults to ``os.environ``. Passing it explicitly marks an
    injected (unit-test) run: file discovery is then **off** unless
    ``dotenv_path``/``config_path`` are supplied, keeping the run hermetic.
    When ``environ`` is ``None`` the two files are discovered in ``cwd``
    (default: the current working directory). A blank
    ``PI_ECOSYS_TTS_LANG`` - or one outside :data:`SUPPORTED_TTS_LANGS` - falls
    back to :data:`DEFAULT_TTS_LANG` (``yue``). A blank device name falls back to
    the hostname; the result is always non-blank. A blank or unknown
    ``PI_ECOSYS_WIFI_SECURITY`` falls back to :data:`DEFAULT_WIFI_SECURITY`.

    This function never mutates ``os.environ``: the ``.env`` file is parsed with
    :func:`python_dotenv.dotenv_values`, not ``load_dotenv``.
    """
    injected = environ is not None
    env = os.environ if environ is None else environ
    base = Path(cwd) if cwd is not None else Path.cwd()

    if config_path is None and not injected:
        config_path = base / DEFAULT_CONFIG_FILENAME
    if dotenv_path is None and not injected:
        dotenv_path = base / DEFAULT_DOTENV_FILENAME

    merged = _merge(
        config_file=_read_config_file(config_path),
        dotenv=_read_dotenv(dotenv_path),
        environ=env,
    )

    lang = merged.get("tts_lang")
    if lang not in SUPPORTED_TTS_LANGS:
        lang = DEFAULT_TTS_LANG

    security = merged.get("wifi_security")
    if security not in SUPPORTED_WIFI_SECURITY:
        security = DEFAULT_WIFI_SECURITY

    return Config(
        hub=merged.get("hub"),
        tts_lang=lang,
        device_name=merged.get("device_name") or _default_device_name(),
        ssid=merged.get("ssid"),
        password=merged.get("password"),
        wifi_security=security,
        enrollment_seconds=_bounded_int(
            merged.get("enrollment_seconds"),
            DEFAULT_ENROLLMENT_SECONDS,
            minimum=1,
        ),
        hub_port=_bounded_int(
            merged.get("hub_port"),
            DEFAULT_HUB_PORT,
            minimum=1,
            maximum=_MAX_PORT,
        ),
    )
