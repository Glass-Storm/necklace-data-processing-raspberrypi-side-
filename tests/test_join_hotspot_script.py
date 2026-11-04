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


def test_usage_accepts_an_optional_security_mode() -> None:
    """The runbook passes wpa3 explicitly; wpa2 stays the default."""
    source = _text()
    assert "$0 <ssid> <psk> [wpa2|wpa3]" in source
    assert 'MODE="${3:-wpa2}"' in source


def test_script_writes_the_wpa3_sae_block() -> None:
    """WPA3 needs the exact SAE shape that a WPA3-SAE (H2E) AP accepts."""
    source = _text()
    assert "key_mgmt=SAE" in source
    assert "sae_password=" in source
    assert "ieee80211w=2" in source


def test_wpa3_sae_pwe_is_global_not_inside_the_network_block() -> None:
    """Android H2E hotspots need sae_pwe=2, and it must precede network={."""
    source = _text()
    assert "sae_pwe=2" in source
    assert source.index("sae_pwe=2") < source.index("network={")


def test_wpa2_keeps_the_psk_block() -> None:
    """The WPA2 path must be unchanged for existing WPA2 access points."""
    source = _text()
    assert 'psk="%s"' in source
    assert "key_mgmt=WPA-PSK" in source


def test_wpa3_skips_the_netplan_dropin() -> None:
    """A stale WPA2 netplan drop-in must not be written for a WPA3 join."""
    source = _text()
    netplan_guard = source.index(
        'if [ "$MODE" = "wpa2" ] && compgen -G "/etc/netplan/*.yaml"'
    )
    netplan_write = source.index("netplan_tmp=")
    supplicant_start = source.index("systemctl enable --now")
    assert netplan_guard < netplan_write < supplicant_start
    assert 'MODE" = "wpa3"' in source


def test_script_always_starts_wpa_supplicant_and_reconfigures() -> None:
    """The old netplan branch forgot this; it must run in both modes."""
    source = _text()
    assert source.count("systemctl enable --now") == 1
    assert source.count('wpa_cli -i "$IFACE" reconfigure') == 1
    assert source.index("systemctl enable --now") > source.index("netplan_tmp=")
    assert source.index('wpa_cli -i "$IFACE" reconfigure') > source.index(
        "systemctl enable --now"
    )


def test_script_ensures_a_wlan0_dhcp_network_file() -> None:
    """Without a rendered wlan0 .network the join has no DHCP client."""
    source = _text()
    assert 'compgen -G "/etc/systemd/network/*wlan0*.network"' in source
    assert "25-${IFACE}.network" in source
    assert "DHCP=yes" in source
