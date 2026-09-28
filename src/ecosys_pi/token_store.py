"""Secure, on-disk cache of the hub-issued bearer token.

The hub mints a **single-use-issued bearer token** when a Pi device pairs; the
Pi caches it so it can authenticate ``Heartbeat`` and ``OpenStream`` without
re-pairing (tasks 7, 8, 12). That token is as sensitive as a password, so this
cache is hardened:

* it lives **outside the repo**, under ``$XDG_CONFIG_HOME`` (default
  ``~/.config``) at ``ecosys-pi/credentials.json``;
* the **file is mode 0600** and the **directory is mode 0700** - never
  world-readable;
* every write is **atomic**: the JSON is written to a uniquely-named temporary
  file in the same directory, ``fsync``-ed, then ``os.replace``-d over the real
  path. A crash mid-write therefore leaves the previous file **intact** rather
  than truncated;
* ``load`` treats a missing, unreadable, or corrupt file as **absent** (returns
  ``None``) so a half-written or hand-edited file triggers a clean re-pair
  instead of a crash;
* the token is **never logged** - neither this module nor :mod:`ecosys_pi.config`
  emits it.

systemd service-user caveat
---------------------------

``$XDG_CONFIG_HOME`` is honoured, but a systemd unit started for a service user
(``User=ecosys``) gets a minimal environment and usually has no
``XDG_CONFIG_HOME`` set. In that case the cache lands in that user's
``~/.config/ecosys-pi`` - which MUST be owned by the service user with mode
``0700``. Set ``Environment=XDG_CONFIG_HOME=/var/lib/ecosys`` in the unit if you
want the cache elsewhere; this module creates the ``ecosys-pi`` subdirectory
0700 either way.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

__all__ = [
    "APP_DIRNAME",
    "CREDENTIALS_FILENAME",
    "Credentials",
    "TokenStore",
    "config_home",
    "credentials_path",
]

#: Subdirectory under the XDG config home that owns the credentials file.
APP_DIRNAME = "ecosys-pi"

#: File name of the JSON credential cache.
CREDENTIALS_FILENAME = "credentials.json"

#: Mode for the credentials file: owner read/write only.
_FILE_MODE = 0o600

#: Mode for the credentials directory: owner only.
_DIR_MODE = 0o700


def config_home(environ: Mapping[str, str] | None = None) -> Path:
    """Return the XDG config home.

    Honours ``XDG_CONFIG_HOME`` when it is set and non-blank; otherwise falls
    back to ``~/.config`` as the XDG Base Directory spec prescribes. The value
    is returned verbatim (not expanded/validated) so callers and tests control
    the exact path.
    """
    env = os.environ if environ is None else environ
    xdg = (env.get("XDG_CONFIG_HOME") or "").strip()
    if xdg:
        return Path(xdg)
    return Path.home() / ".config"


def credentials_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the full ``.../ecosys-pi/credentials.json`` path."""
    return config_home(environ) / APP_DIRNAME / CREDENTIALS_FILENAME


@dataclass(frozen=True)
class Credentials:
    """The cached pairing result: the bearer ``token`` and the hub ``device_id``.

    ``device_id`` is retained so the client can report which device it is (and
    the E2E harness can assert it) without re-deriving it from the token.
    """

    token: str
    device_id: str

    def __repr__(self) -> str:
        # NEVER leak the token through repr/print/logging.
        return f"Credentials(token=<redacted>, device_id={self.device_id!r})"


class TokenStore:
    """Reads and writes :class:`Credentials` at a fixed path.

    Use :meth:`at` to bind a store to an explicit path (tests use ``tmp_path``)
    or :meth:`default` to bind it to the XDG-derived location.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @classmethod
    def default(cls, environ: Mapping[str, str] | None = None) -> "TokenStore":
        """A store at the XDG-derived ``ecosys-pi/credentials.json``."""
        return cls(credentials_path(environ))

    @property
    def path(self) -> Path:
        """The credentials file path this store reads and writes."""
        return self._path

    def load(self) -> Credentials | None:
        """Return the cached credentials, or ``None`` when unusable.

        A missing file, a permission error, malformed JSON, a non-object
        top-level value, or missing/blank/typed-wrong fields all yield ``None``
        (treated as absent) rather than raising - a corrupt cache must trigger a
        re-pair, not a crash.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return None
        except (OSError, ValueError):
            # ValueError also covers UnicodeDecodeError on a binary/garbage file.
            return None

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None

        if not isinstance(data, dict):
            return None

        token = data.get("token")
        device_id = data.get("device_id")
        if not isinstance(token, str) or not token.strip():
            return None
        if not isinstance(device_id, str) or not device_id.strip():
            return None
        return Credentials(token=token, device_id=device_id)

    def save(self, credentials: Credentials) -> None:
        """Atomically persist ``credentials`` with file mode 0600.

        The parent directory is created/forced to mode 0700. The payload is
        written to a temporary file in that directory (opened ``0600``),
        ``fsync``-ed, and ``os.replace``-d into place, so a concurrent reader or
        a crash mid-write never observes a partial file.
        """
        if not credentials.token.strip():
            raise ValueError("refusing to persist a blank token")
        if not credentials.device_id.strip():
            raise ValueError("refusing to persist a blank device_id")

        directory = self._path.parent
        _ensure_private_dir(directory)

        payload = json.dumps(
            {"token": credentials.token, "device_id": credentials.device_id},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        # Unique, hidden temp name in the SAME directory so os.replace is atomic
        # (rename across filesystems is not).
        tmp = directory / f".{self._path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        fd = -1
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, _FILE_MODE)
            os.fchmod(fd, _FILE_MODE)
            os.write(fd, payload)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(tmp, self._path)
            _fsync_dir(directory)
        finally:
            if fd >= 0:
                os.close(fd)
            # If os.replace did NOT run (crash/error), drop the temp file; the
            # real credentials file is untouched.
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            except OSError:  # pragma: no cover - best-effort cleanup
                pass

    def clear(self) -> None:
        """Remove the cached credentials file if present (idempotent)."""
        try:
            self._path.unlink()
        except FileNotFoundError:
            return


def _ensure_private_dir(directory: Path) -> None:
    """Create ``directory`` if needed and force it to mode 0700."""
    os.makedirs(directory, mode=_DIR_MODE, exist_ok=True)
    os.chmod(directory, _DIR_MODE)


def _fsync_dir(directory: Path) -> None:
    """Best-effort ``fsync`` of a directory so the rename is durable.

    Some filesystems disallow opening a directory for ``fsync``; that is not
    fatal to correctness, so any ``OSError`` is swallowed.
    """
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform-dependent
        return
    try:
        os.fsync(dir_fd)
    except OSError:  # pragma: no cover - platform-dependent
        pass
    finally:
        os.close(dir_fd)
