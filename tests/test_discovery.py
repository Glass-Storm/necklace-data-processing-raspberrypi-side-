"""Tests for hub discovery: override -> env -> default gateway, all hermetic.

The hub is the phone hotspot's access point, so the hub address is the Pi's
**default gateway**. No test runs ``nmcli``/``ip``: the gateway detector is
injected with a deterministic stub, and the CLI seam is driven in-process.
"""

from __future__ import annotations

import io

import pytest

from ecosys_pi import discovery
from ecosys_pi.discovery import ENV_HUB, HubNotFoundError, resolve_hub

TARGET = "192.168.43.1:50051"
GATEWAY = "192.168.43.1"


def test_override_wins_without_touching_gateway() -> None:
    """The explicit override is PRIMARY for automation and skips gateway probing."""
    called: list[bool] = []

    def explode() -> str | None:
        called.append(True)
        return "gateway-should-not-run"

    result = resolve_hub(
        "127.0.0.1:1234",
        environ={ENV_HUB: TARGET},
        detect_gateway=explode,
    )

    assert result == "127.0.0.1:1234"
    assert called == [], "the gateway must not be probed when an override is given"


def test_env_hub_wins_over_gateway() -> None:
    """``PI_ECOSYS_HUB`` is the second precedence level and skips the gateway."""
    called: list[bool] = []

    def explode() -> str | None:
        called.append(True)
        return GATEWAY

    assert resolve_hub(environ={ENV_HUB: TARGET}, detect_gateway=explode) == TARGET
    assert called == []


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_override_and_env_are_treated_as_unset(blank: str) -> None:
    """A blank override/env value falls through to the gateway (never returned)."""
    result = resolve_hub(
        blank,
        environ={ENV_HUB: blank},
        detect_gateway=lambda: GATEWAY,
    )

    assert result == TARGET


def test_gateway_host_with_default_port() -> None:
    """A detected default gateway yields ``gateway:50051`` by default."""
    assert resolve_hub(environ={}, detect_gateway=lambda: GATEWAY) == TARGET


def test_gateway_host_honours_a_configured_port() -> None:
    """``hub_port`` (PI_ECOSYS_HUB_PORT) chooses the port on the gateway host."""
    assert (
        resolve_hub(environ={}, port=60061, detect_gateway=lambda: GATEWAY)
        == "192.168.43.1:60061"
    )


def test_no_default_route_raises_error_naming_env_hub() -> None:
    """With no override/env and no default route, the typed error plus escape hatch."""
    with pytest.raises(HubNotFoundError) as excinfo:
        resolve_hub(environ={}, detect_gateway=lambda: None)

    message = str(excinfo.value)
    assert ENV_HUB in message
    assert "--hub" in message


# --- CLI seam: the frozen stdout/exit contract still holds --------------------


def test_cli_seam_reports_discovery_failure_and_exits_one(monkeypatch) -> None:
    """The CLI prints the discovery detail on the ``error:`` line and exits 1."""
    from ecosys_pi import cli

    def fail(*args, **kwargs):
        raise HubNotFoundError(f"no hub; set {ENV_HUB}=HOST:PORT")

    monkeypatch.setattr(discovery, "resolve_hub", fail)
    out = io.StringIO()
    err = io.StringIO()
    code = cli.main(
        ["--mode", "audio", "--frames", "1"],
        environ={},
        stdout=out,
        stderr=err,
    )
    assert code == cli.EXIT_ERROR
    line = out.getvalue().splitlines()[0]
    assert line.startswith("error: ")
    assert ENV_HUB in line


def test_cli_seam_uses_the_discovered_hub_without_pairing(
    monkeypatch, tmp_path
) -> None:
    """With no ``--hub``/env, the CLI calls ``resolve_hub`` and dials its result.

    Discovery is stubbed to succeed and the session runner is replaced with a
    sentinel, so this proves the CLI moved PAST discovery and used its address -
    with no pairing step in between (pairing was removed from the runtime).
    """
    from ecosys_pi import cli

    calls: list[str | None] = []

    def resolve(override, *, environ, port=50051):
        calls.append(override)
        return "127.0.0.1:1"

    used: dict[str, object] = {}

    async def fake_session(hub, *, device_name, mode, frames, env, out, err):
        used["hub"] = hub
        return cli.EXIT_OK

    monkeypatch.setattr(discovery, "resolve_hub", resolve)
    monkeypatch.setattr(cli, "_run_session", fake_session)

    out = io.StringIO()
    code = cli.main(
        ["--mode", "audio", "--frames", "1"],
        environ={"XDG_CONFIG_HOME": str(tmp_path)},
        stdout=out,
        stderr=io.StringIO(),
    )
    assert calls == [None], "discovery must be consulted when no hub is configured"
    assert used["hub"] == "127.0.0.1:1", "the CLI must dial the discovered hub"
    assert code == cli.EXIT_OK
