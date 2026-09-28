"""Smoke tests: the package imports and the venv is configured as required."""

from __future__ import annotations

import sys
from pathlib import Path

import ecosys_pi


def _venv_flags(cfg_path: Path) -> dict[str, str]:
    """Parse ``pyvenv.cfg`` (plain ``key = value`` lines, not INI sections)."""
    flags: dict[str, str] = {}
    for line in cfg_path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            flags[key.strip()] = value.strip()
    return flags


def test_import() -> None:
    assert ecosys_pi.__version__ == "0.1.0"


def test_venv_has_system_site_packages() -> None:
    """The venv MUST be created with --system-site-packages.

    On a real Pi the Debian ``python3-picamera2`` package installs the
    libcamera bindings into the system site-packages; they are only visible to
    this venv when ``include-system-site-packages = true`` is set in
    ``pyvenv.cfg``.
    """
    cfg_path = Path(sys.prefix) / "pyvenv.cfg"
    assert cfg_path.is_file(), f"not running inside a venv (no {cfg_path})"

    flags = _venv_flags(cfg_path)
    value = flags.get("include-system-site-packages", "false")
    assert value.lower() == "true", (
        f"expected include-system-site-packages=true in {cfg_path}, got {value!r}"
    )
