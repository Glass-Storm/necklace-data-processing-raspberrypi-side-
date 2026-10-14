"""Tests for :mod:`ecosys_pi.network`: nmcli hotspot join + gateway detection.

Everything here is hermetic: the command runner is injected with a fake, so no
test spawns ``nmcli``/``ip`` or touches a real network. The argv builders and the
``ip route`` / ``nmcli`` parsers are exercised directly on scripted output.
"""

from __future__ import annotations

import pytest

from ecosys_pi.network import (
    CommandResult,
    HotspotConfigError,
    HotspotJoinError,
    build_connect_argv,
    detect_default_gateway,
    hub_from_gateway,
    join_hotspot,
    parse_default_gateway,
)

SSID = "ecosys-hotspot"
PASSWORD = "s3cret-pw"


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> CommandResult:
    """A scripted :class:`CommandResult` for a fake runner."""
    return CommandResult(argv=(), returncode=returncode, stdout=stdout, stderr=stderr)


class FakeRunner:
    """Records every argv and returns scripted results in order.

    ``results`` is consumed front-to-back; once exhausted ``default`` is used
    (or a bare success). This lets a test assert *which* command ran and in what
    order without a subprocess.
    """

    def __init__(
        self,
        results: list[CommandResult] | None = None,
        default: CommandResult | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self._results = list(results or [])
        self._default = default if default is not None else _result()

    def __call__(self, argv) -> CommandResult:
        self.calls.append(list(argv))
        if self._results:
            return self._results.pop(0)
        return self._default


# --- nmcli argv construction (WPA2 + WPA3 personal) --------------------------


def test_build_connect_argv_wpa2_is_the_documented_command() -> None:
    """WPA2 personal uses exactly ``nmcli device wifi connect SSID password PW``."""
    assert build_connect_argv(SSID, PASSWORD) == [
        "nmcli",
        "device",
        "wifi",
        "connect",
        SSID,
        "password",
        PASSWORD,
    ]


def test_build_connect_argv_wpa3_forces_sae_key_management() -> None:
    """WPA3 personal appends ``wifi-sec.key-mgmt sae`` to the same command."""
    argv = build_connect_argv(SSID, PASSWORD, security="wpa3")

    assert argv[:7] == [
        "nmcli",
        "device",
        "wifi",
        "connect",
        SSID,
        "password",
        PASSWORD,
    ]
    assert argv[7:] == ["wifi-sec.key-mgmt", "sae"]


def test_build_connect_argv_rejects_unknown_security() -> None:
    with pytest.raises(ValueError):
        build_connect_argv(SSID, PASSWORD, security="wep")


# --- join_hotspot ------------------------------------------------------------


def test_join_hotspot_runs_the_wpa2_command_and_returns_the_result() -> None:
    runner = FakeRunner()

    result = join_hotspot(SSID, PASSWORD, runner=runner)

    assert runner.calls == [
        ["nmcli", "device", "wifi", "connect", SSID, "password", PASSWORD]
    ]
    assert result.returncode == 0


def test_join_hotspot_wpa3_passes_the_sae_key_mgmt_on_the_wire() -> None:
    runner = FakeRunner()

    join_hotspot(SSID, PASSWORD, security="wpa3", runner=runner)

    assert runner.calls == [
        [
            "nmcli",
            "device",
            "wifi",
            "connect",
            SSID,
            "password",
            PASSWORD,
            "wifi-sec.key-mgmt",
            "sae",
        ]
    ]


@pytest.mark.parametrize(
    ("ssid", "password"),
    [
        (None, PASSWORD),
        ("", PASSWORD),
        ("   ", PASSWORD),
        (SSID, None),
        (SSID, ""),
        (SSID, "   "),
    ],
)
def test_join_hotspot_without_credentials_raises_config_error(
    ssid: str | None, password: str | None
) -> None:
    """A missing SSID or password never reaches nmcli."""
    runner = FakeRunner()

    with pytest.raises(HotspotConfigError):
        join_hotspot(ssid, password, runner=runner)

    assert runner.calls == [], "nmcli must not run without credentials"


def test_join_hotspot_failure_raises_and_redacts_the_password() -> None:
    """A non-zero nmcli exit is a typed error that never leaks the password."""
    runner = FakeRunner(
        default=_result(
            returncode=10,
            stderr=f"Error: password '{PASSWORD}' rejected",
        )
    )

    with pytest.raises(HotspotJoinError) as excinfo:
        join_hotspot(SSID, PASSWORD, runner=runner)

    message = str(excinfo.value)
    assert PASSWORD not in message, "the hotspot password must never be logged"
    assert SSID in message


# --- gateway parsing (`ip route` + nmcli) ------------------------------------


def test_parse_default_gateway_from_ip_route_output() -> None:
    output = (
        "default via 192.168.43.1 dev wlan0 proto dhcp src 192.168.43.42 "
        "metric 600\n"
        "192.168.43.0/24 dev wlan0 proto kernel scope link src 192.168.43.42\n"
    )
    assert parse_default_gateway(output) == "192.168.43.1"


def test_parse_default_gateway_from_nmcli_output() -> None:
    output = "GENERAL.DEVICE:wlan0\nIP4.GATEWAY:192.168.43.1\n"
    assert parse_default_gateway(output) == "192.168.43.1"


def test_parse_default_gateway_handles_ipv6() -> None:
    assert parse_default_gateway("default via fe80::1 dev wlan0\n") == "fe80::1"


def test_parse_default_gateway_returns_none_without_a_default_route() -> None:
    assert parse_default_gateway("") is None
    assert (
        parse_default_gateway("192.168.43.0/24 dev wlan0 proto kernel scope link\n")
        is None
    )
    assert parse_default_gateway("IP4.GATEWAY:\n") is None
    # A default route with no ``via`` (on-link) carries no gateway address.
    assert parse_default_gateway("default dev wlan0 scope link\n") is None


# --- gateway detection (command selection) -----------------------------------


def test_detect_default_gateway_prefers_ip_route() -> None:
    runner = FakeRunner(
        results=[_result(stdout="default via 192.168.43.1 dev wlan0\n")]
    )

    assert detect_default_gateway(runner=runner) == "192.168.43.1"
    assert runner.calls == [["ip", "route", "show", "default"]]


def test_detect_default_gateway_falls_back_to_nmcli() -> None:
    runner = FakeRunner(
        results=[
            _result(),  # ip route show default: nothing
            _result(),  # ip route: nothing
            _result(stdout="IP4.GATEWAY:10.42.0.1\n"),  # nmcli fallback
        ]
    )

    assert detect_default_gateway(runner=runner) == "10.42.0.1"
    assert runner.calls[-1][0] == "nmcli"


def test_detect_default_gateway_skips_commands_that_fail() -> None:
    runner = FakeRunner(
        results=[
            _result(returncode=1, stderr="/bin/sh: ip: not found"),
            _result(returncode=1, stderr="/bin/sh: ip: not found"),
            _result(stdout="IP4.GATEWAY:192.168.43.1\n"),
        ]
    )

    assert detect_default_gateway(runner=runner) == "192.168.43.1"


def test_detect_default_gateway_returns_none_when_nothing_answers() -> None:
    runner = FakeRunner(default=_result(returncode=1))

    assert detect_default_gateway(runner=runner) is None


# --- host:port formatting ----------------------------------------------------


def test_hub_from_gateway_forms_host_port() -> None:
    assert hub_from_gateway("192.168.43.1", 50051) == "192.168.43.1:50051"


def test_hub_from_gateway_brackets_ipv6() -> None:
    assert hub_from_gateway("fe80::1", 50051) == "[fe80::1]:50051"
