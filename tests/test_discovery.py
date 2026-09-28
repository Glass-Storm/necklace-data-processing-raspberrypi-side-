"""Tests for hub discovery: override -> env -> bounded mDNS, all hermetic.

No test opens a socket or touches a real hub: the zeroconf browser is injected
with fakes, and the CLI seam is driven in-process with a stubbed resolver. The
real :class:`HubServiceListener` logic is exercised against a fake Zeroconf, so
the record-parsing path is covered without multicast.
"""

from __future__ import annotations

import io

import pytest

from ecosys_pi import discovery
from ecosys_pi.discovery import (
    ENV_HUB,
    SERVICE_TYPE,
    HubNotFoundError,
    HubServiceListener,
    address_from_service_info,
    browse_mdns,
    resolve_hub,
)

TARGET = "10.0.0.5:50051"


class FakeServiceInfo:
    """A minimal stand-in for a resolved ``zeroconf.ServiceInfo``."""

    def __init__(self, addresses: list[str] | None, port: int) -> None:
        self._addresses = addresses
        self.port = port

    def parsed_addresses(self) -> list[str]:
        return list(self._addresses or [])


class FakeZeroconf:
    """Records ``get_service_info`` calls and returns a scripted record."""

    def __init__(self, info: object = None) -> None:
        self._info = info
        self.calls: list[tuple[str, str, int]] = []
        self.closed = False

    def get_service_info(self, type_: str, name: str, timeout: int = 3000):
        self.calls.append((type_, name, timeout))
        return self._info

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    """Fires one ``add_service`` synchronously, then is cancellable."""

    def __init__(self, zc, type_, listener, name: str = f"hub.{SERVICE_TYPE}") -> None:
        self.cancelled = False
        listener.add_service(zc, type_, name)

    def cancel(self) -> None:
        self.cancelled = True


def test_override_wins_without_touching_mdns() -> None:
    """The explicit override is PRIMARY for automation and skips mDNS entirely."""
    called: list[tuple[str, float]] = []

    def explode(service_type: str, timeout: float) -> str | None:
        called.append((service_type, timeout))
        return "mdns-should-not-run:1"

    result = resolve_hub(
        "127.0.0.1:1234",
        environ={ENV_HUB: TARGET},
        resolve_mdns=explode,
    )
    assert result == "127.0.0.1:1234"
    assert called == [], "mDNS must not be browsed when an override is given"


def test_env_hub_wins_over_mdns() -> None:
    """``PI_ECOSYS_HUB`` is the second precedence level and skips mDNS."""
    called = []

    def explode(service_type: str, timeout: float) -> str | None:
        called.append(service_type)
        return "mdns:1"

    assert resolve_hub(environ={ENV_HUB: TARGET}, resolve_mdns=explode) == TARGET
    assert called == []


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_override_and_env_are_treated_as_unset(blank: str) -> None:
    """A blank override/env value falls through to mDNS (never returned as-is)."""
    seen: list[str] = []

    def stub(service_type: str, timeout: float) -> str | None:
        seen.append(service_type)
        return TARGET

    assert resolve_hub(blank, environ={ENV_HUB: blank}, resolve_mdns=stub) == TARGET
    assert seen == [SERVICE_TYPE]


def test_mdns_result_is_returned_when_no_override() -> None:
    """With no override/env, the injected mDNS result is returned verbatim."""
    seen: list[tuple[str, float]] = []

    def stub(service_type: str, timeout: float) -> str | None:
        seen.append((service_type, timeout))
        return TARGET

    assert resolve_hub(environ={}, resolve_mdns=stub, timeout=1.5) == TARGET
    assert seen == [(SERVICE_TYPE, 1.5)]


def test_no_service_timeout_raises_error_naming_env_hub() -> None:
    """An mDNS miss raises a typed error whose message names ``PI_ECOSYS_HUB``."""
    with pytest.raises(HubNotFoundError) as excinfo:
        resolve_hub(environ={}, resolve_mdns=lambda *_: None, timeout=0.01)
    message = str(excinfo.value)
    assert ENV_HUB in message
    assert SERVICE_TYPE in message


def test_fake_zeroconf_browse_yields_expected_host_port() -> None:
    """A fake browse resolves the first hub record to ``host:port``."""
    zc = FakeZeroconf(FakeServiceInfo(["10.0.0.5"], 50051))
    result = browse_mdns(
        SERVICE_TYPE,
        0.1,
        zeroconf_factory=lambda: zc,
        browser_factory=FakeBrowser,
    )
    assert result == TARGET
    assert zc.calls and zc.calls[0][0] == SERVICE_TYPE
    assert zc.closed is True, "the Zeroconf instance must always be closed"


def test_browse_miss_returns_none_instead_of_raising() -> None:
    """A record with no addresses is a miss (``None``), never an exception."""
    zc = FakeZeroconf(FakeServiceInfo([], 50051))
    assert (
        browse_mdns(
            SERVICE_TYPE,
            0.1,
            zeroconf_factory=lambda: zc,
            browser_factory=FakeBrowser,
        )
        is None
    )


def test_broken_zeroconf_factory_is_a_miss() -> None:
    """A host without a working multicast stack yields ``None``, not a crash."""

    def boom():
        raise OSError("no multicast here")

    assert browse_mdns(SERVICE_TYPE, 0.1, zeroconf_factory=boom) is None


def test_address_from_service_info_forms() -> None:
    """IPv4 stays bare; IPv6 is bracketed; unusable records are ``None``."""
    assert address_from_service_info(FakeServiceInfo(["192.168.43.1"], 50051)) == (
        "192.168.43.1:50051"
    )
    assert address_from_service_info(FakeServiceInfo(["fe80::1"], 50051)) == (
        "[fe80::1]:50051"
    )
    assert address_from_service_info(FakeServiceInfo([], 50051)) is None
    assert address_from_service_info(FakeServiceInfo(["10.0.0.5"], 0)) is None
    assert address_from_service_info(None) is None


def test_listener_latches_only_the_first_address() -> None:
    """Once latched, a later advertisement cannot overwrite the first answer."""
    listener = HubServiceListener()
    zc_one = FakeZeroconf(FakeServiceInfo(["10.0.0.5"], 50051))
    zc_two = FakeZeroconf(FakeServiceInfo(["10.0.0.9"], 50052))

    listener.add_service(zc_one, SERVICE_TYPE, "one")
    listener.add_service(zc_two, SERVICE_TYPE, "two")

    assert listener.address == TARGET
    assert listener.wait(0.0) is True
    assert zc_two.calls == [], "the second service must not be resolved"


def test_listener_skips_unusable_record_and_keeps_waiting() -> None:
    """An unusable record does not latch; a later good one does."""
    listener = HubServiceListener()
    listener.add_service(FakeZeroconf(FakeServiceInfo([], 1)), SERVICE_TYPE, "bad")
    assert listener.address is None
    assert listener.wait(0.0) is False

    listener.add_service(
        FakeZeroconf(FakeServiceInfo(["10.0.0.5"], 50051)), SERVICE_TYPE, "good"
    )
    assert listener.address == TARGET


def test_listener_swallows_a_flaky_lookup() -> None:
    """A ``get_service_info`` exception is a miss, not a crash."""

    class RaisingZc:
        def get_service_info(self, *args, **kwargs):
            raise RuntimeError("record vanished")

    listener = HubServiceListener()
    listener.add_service(RaisingZc(), SERVICE_TYPE, "flaky")
    assert listener.address is None


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
        stdin=io.StringIO(),
        stdout=out,
        stderr=err,
    )
    assert code == cli.EXIT_ERROR
    line = out.getvalue().splitlines()[0]
    assert line.startswith("error: ")
    assert ENV_HUB in line


def test_cli_seam_consults_discovery_when_no_hub_configured(
    monkeypatch, tmp_path
) -> None:
    """With no ``--hub``/env, the CLI calls ``resolve_hub`` and moves past it.

    Discovery is stubbed to succeed; the run then stops at the local "no cached
    token / no PIN" guard (non-TTY stdin), so no socket is dialed. Reaching that
    guard - rather than the hub-missing error - proves the CLI consulted
    discovery and used its result.
    """
    from ecosys_pi import cli

    calls: list[str | None] = []

    def resolve(override, *, environ, timeout=discovery.DEFAULT_TIMEOUT_S):
        calls.append(override)
        return "127.0.0.1:1"

    monkeypatch.setattr(discovery, "resolve_hub", resolve)

    out = io.StringIO()
    code = cli.main(
        ["--mode", "audio", "--frames", "1"],
        environ={"XDG_CONFIG_HOME": str(tmp_path)},
        stdin=io.StringIO(),
        stdout=out,
        stderr=io.StringIO(),
    )
    lines = out.getvalue().splitlines()
    assert calls == [None], "discovery must be consulted when no hub is configured"
    assert code == cli.EXIT_ERROR
    assert lines[0].startswith("error: ")
    assert "no cached token" in lines[0], "must have moved past the hub check"
