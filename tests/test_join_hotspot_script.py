"""Hermetic checks for ``scripts/join-hotspot.sh``.

The script is only ever parsed and inspected here. It is never executed: a real
run writes ``/etc/wpa_supplicant``, starts ``wpa_supplicant@wlan0``, and touches
the Pi's only network path, so the live join is an operator step, not a unit
test.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "join-hotspot.sh"


def _text() -> str:
    """Return the script source as UTF-8 text."""
    return SCRIPT.read_text(encoding="utf-8")


def test_script_exists_and_is_executable() -> None:
    """The runbook invokes the script directly, so it must be committed +x."""
    assert SCRIPT.is_file(), f"missing script: {SCRIPT}"
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, f"{SCRIPT} is not user-executable: {oct(mode)}"
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable by this user"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_script_passes_bash_syntax_check() -> None:
    """``bash -n`` parses the script without running any command."""
    completed = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        f"bash -n failed for {SCRIPT}:\n{completed.stderr}"
    )


def test_script_refuses_to_touch_the_management_links() -> None:
    """usb0/eth0 are the SSH links and must be named as untouchable."""
    source = _text()
    assert "usb0" in source
    assert "eth0" in source
    assert "MANAGEMENT_IFACES" in source
    assert "refusing to touch management interface" in source
    # The management links are checked before and after, never reconfigured.
    assert "management_links_with_ipv4" in source


def test_script_writes_the_wpa_supplicant_config_mode_600() -> None:
    """It renders wpa_supplicant-wlan0.conf with the required keys at 0600."""
    source = _text()
    assert 'IFACE="wlan0"' in source
    assert "/etc/wpa_supplicant/wpa_supplicant-${IFACE}.conf" in source
    assert "ctrl_interface=" in source
    assert "country=" in source
    assert "chmod 600" in source


def test_script_never_restarts_systemd_networkd_wholesale() -> None:
    """A restart could drop the management link; only a reload is allowed."""
    source = _text()
    assert "networkctl reload" in source
    assert "systemctl restart systemd-networkd" not in source


def test_script_verifies_association_and_ipv4() -> None:
    """Success requires wpa_state=COMPLETED and an IPv4 on wlan0."""
    source = _text()
    assert "wpa_state=COMPLETED" in source
    assert "ip -4 addr show dev" in source


def test_script_is_strict_mode() -> None:
    """Unset variables, failing commands, and pipeline failures are fatal."""
    assert "set -euo pipefail" in _text()
