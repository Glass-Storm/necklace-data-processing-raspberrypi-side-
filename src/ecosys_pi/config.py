"""Runtime configuration for the ecosys Pi client.

Every knob is read from an environment variable so the same code runs on the
bench and on a real Pi without edits. ``load_config`` is the single entry point;
it never mutates ``os.environ`` and never logs a secret.

Environment variables and documented defaults
---------------------------------------------

======================  =========================  ==========================
Variable                Default                    Notes
======================  =========================  ==========================
``PI_ECOSYS_HUB``       *(unset)*                  ``HOST:PORT`` override for
                                                   hub discovery (task 13). A
                                                   blank value is treated as
                                                   unset.
``PI_ECOSYS_PIN``       *(unset)*                  **Scripting only.** This is
                                                   NOT durable config: the PIN
                                                   is single-use with a 120 s
                                                   TTL. Interactive runs read
                                                   the PIN from the console.
``PI_ECOSYS_TTS_LANG``  ``yue``                    One of ``yue``/``zh``/``en``.
                                                   A blank or unknown value
                                                   falls back to ``yue``.
``PI_ECOSYS_DEVICE_NAME`` ``socket.gethostname()`` Non-blank label sent to the
                                                   hub. Blank values fall back
                                                   to the hostname.
======================  =========================  ==========================

The PIN and the bearer token are secrets: ``Config.__repr__`` redacts the PIN so
it can never leak through a stray ``print``/log of the config object.

systemd service-user caveat
---------------------------

This module reads the *process environment*. A systemd unit started for a
service user (``User=ecosys``) gets a **fresh, minimal environment** - it does
NOT inherit your login shell's exports. Set variables with ``Environment=`` /
``EnvironmentFile=`` in the unit, or the defaults apply. The same caveat applies
to ``XDG_CONFIG_HOME`` used by :mod:`ecosys_pi.token_store`: if the service user
has no ``XDG_CONFIG_HOME``, the cache lands in that user's ``~/.config`` and the
service account must own that directory with mode ``0700``.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import Mapping

__all__ = [
    "DEFAULT_TTS_LANG",
    "SUPPORTED_TTS_LANGS",
    "Config",
    "load_config",
]

#: Languages the local TTS layer can speak (tasks 11/12). See
#: :mod:`ecosys_pi.tts` for the per-language engine mapping.
SUPPORTED_TTS_LANGS: frozenset[str] = frozenset({"yue", "zh", "en"})

#: Documented fallback for ``PI_ECOSYS_TTS_LANG`` when it is blank or unknown.
DEFAULT_TTS_LANG: str = "yue"

#: Fallback device label when the hostname is blank (e.g. a container).
_FALLBACK_DEVICE_NAME = "ecosys-pi"


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


@dataclass(frozen=True)
class Config:
    """Immutable, validated runtime configuration.

    Build it with :func:`load_config` rather than calling the constructor so the
    environment-variable parsing and fallbacks are applied consistently.
    """

    #: ``HOST:PORT`` hub override, or ``None`` to use mDNS discovery (task 13).
    hub: str | None
    #: Scripting-only PIN override, or ``None`` to prompt on the console.
    pin: str | None
    #: One of :data:`SUPPORTED_TTS_LANGS`; always valid after :func:`load_config`.
    tts_lang: str
    #: Non-blank device label sent in ``PairRequest.device_name``.
    device_name: str

    def __repr__(self) -> str:
        # NEVER leak the PIN (or a future token) through repr/print/logging.
        pin = "<set>" if self.pin is not None else "<unset>"
        return (
            f"Config(hub={self.hub!r}, pin={pin}, "
            f"tts_lang={self.tts_lang!r}, device_name={self.device_name!r})"
        )


def load_config(environ: Mapping[str, str] | None = None) -> Config:
    """Read configuration from ``environ`` (defaults to ``os.environ``).

    A blank ``PI_ECOSYS_TTS_LANG`` - or one outside
    :data:`SUPPORTED_TTS_LANGS` - falls back to :data:`DEFAULT_TTS_LANG`
    (``yue``). A blank ``PI_ECOSYS_DEVICE_NAME`` falls back to the hostname;
    the result is always non-blank so the hub never reports ``name-missing``.
    """
    env = os.environ if environ is None else environ

    lang = _clean(env.get("PI_ECOSYS_TTS_LANG"))
    if lang not in SUPPORTED_TTS_LANGS:
        lang = DEFAULT_TTS_LANG

    device_name = _clean(env.get("PI_ECOSYS_DEVICE_NAME")) or _default_device_name()

    return Config(
        hub=_clean(env.get("PI_ECOSYS_HUB")),
        pin=_clean(env.get("PI_ECOSYS_PIN")),
        tts_lang=lang,
        device_name=device_name,
    )
