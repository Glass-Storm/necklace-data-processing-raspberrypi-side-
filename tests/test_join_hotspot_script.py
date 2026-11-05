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
    # save_config rewrites the file with wpa_supplicant's umask, so the mode is
    # re-tightened after the save.
    save_call = source.index("wpa_command save_config")
    assert source.index('chmod 600 "$WPA_CONF"', save_call) > save_call


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


def test_script_writes_the_wpa3_sae_fields() -> None:
    """WPA3 needs the exact SAE shape that a WPA3-SAE (H2E) AP accepts."""
    source = _text()
    assert "key_mgmt SAE" in source
    assert "sae_password" in source
    assert "ieee80211w 2" in source


def test_wpa3_sae_pwe_is_a_global_option_not_a_network_parameter() -> None:
    """Android H2E hotspots need sae_pwe=2 as a global, not per-network."""
    source = _text()
    assert "sae_pwe=2" in source
    for line in source.splitlines():
        if "sae_pwe" in line:
            assert "set_network" not in line
    # Global options are read when the supplicant starts.
    assert source.index("sae_pwe=2") < source.index("systemctl restart")


def test_wpa2_keeps_the_psk_mode() -> None:
    """The WPA2 path must stay WPA-PSK for existing WPA2 access points."""
    source = _text()
    assert 'set_network "$network_id" psk' in source
    assert "key_mgmt WPA-PSK" in source


def test_script_removes_the_netplan_dropin_and_stops_the_conflict() -> None:
    """The stale netplan drop-in and its supplicant must never survive."""
    source = _text()
    assert 'NETPLAN_DROPIN="/etc/netplan/90-ecosys-${IFACE}.yaml"' in source
    assert 'rm -f "$NETPLAN_DROPIN"' in source
    assert 'NETPLAN_UNIT="netplan-wpa-${IFACE}.service"' in source
    assert 'systemctl stop "$NETPLAN_UNIT"' in source
    # The conflicting supplicant is stopped before ours is (re)started.
    assert source.index('systemctl stop "$NETPLAN_UNIT"') < source.index(
        "systemctl restart"
    )


def test_script_never_writes_a_netplan_dropin() -> None:
    """The script must not recreate the boot-time supplicant conflict."""
    source = _text()
    assert "netplan_tmp" not in source
    assert "access-points" not in source
    assert '>"$NETPLAN_DROPIN"' not in source


def test_script_preserves_existing_networks_through_wpa_cli() -> None:
    """Multi-network memory: update in place or append, never replace the file."""
    source = _text()
    for command in (
        "list_networks",
        "add_network",
        "enable_network",
        "save_config",
        "reconfigure",
    ):
        assert command in source
    # The target SSID is matched before a new block is added.
    assert 'ENVIRON["SSID"]' in source
    assert "set_network" in source


def test_script_always_starts_wpa_supplicant_and_reconfigures() -> None:
    """The old netplan branch forgot this; it must run in both modes."""
    source = _text()
    assert "systemctl enable" in source
    assert source.count("systemctl restart") == 1
    assert source.count("wpa_command reconfigure") == 1
    assert source.index("systemctl restart") < source.index("wpa_command reconfigure")


def test_script_ensures_a_wlan0_dhcp_network_file() -> None:
    """Without a rendered wlan0 .network the join has no DHCP client."""
    source = _text()
    assert 'compgen -G "/etc/systemd/network/*wlan0*.network"' in source
    assert "25-${IFACE}.network" in source
    assert "DHCP=yes" in source
