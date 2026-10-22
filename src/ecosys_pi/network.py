"""Join the phone hotspot and find the hub at the default gateway.

The hub IS the access point: the Pi joins the phone's Wi-Fi hotspot with
``nmcli`` and then dials the **default gateway** (the phone), not a DNS-SD
advertisement. There is no mDNS and no BLE anywhere in this path.

Two concerns live here, both pure stdlib and both unit-testable without a
network:

* :func:`join_hotspot` runs ``nmcli device wifi connect <ssid> password <pw>``
  (WPA2 personal, or WPA3 personal with ``wifi-sec.key-mgmt sae``);
* :func:`detect_default_gateway` reads ``ip route`` (falling back to
  ``nmcli``) and parses the gateway address.

Every actual process is launched through an **injectable** ``runner`` so tests
assert the exact argv and the parsed output without spawning ``nmcli``/``ip``.
:func:`run_command` is the default runner.
"""

from __future__ import annotations

import ipaddress
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "SUPPORTED_SECURITY",
    "NetworkError",
    "HotspotConfigError",
    "HotspotJoinError",
    "CommandResult",
    "CommandRunner",
    "run_command",
    "build_connect_argv",
    "join_hotspot",
    "parse_default_gateway",
    "detect_default_gateway",
    "hub_from_gateway",
]

#: How long a single ``nmcli``/``ip`` invocation may run before it is aborted.
DEFAULT_TIMEOUT_S: float = 15.0

#: Wi-Fi security modes this module knows how to join (personal, pre-shared key).
SUPPORTED_SECURITY: tuple[str, ...] = ("wpa2", "wpa3")

#: Rewritten into error text in place of a real password, so it never leaks.
_REDACTED = "***"


class NetworkError(RuntimeError):
    """A hotspot join or gateway lookup went wrong."""


class HotspotConfigError(NetworkError):
    """The hotspot SSID or password is missing from the configuration."""


class HotspotJoinError(NetworkError):
    """``nmcli`` refused or failed to join the hotspot.

    The message names the SSID and the redacted failure detail; it NEVER carries
    the plaintext password.
    """


@dataclass(frozen=True)
class CommandResult:
    """The captured outcome of one command invocation."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


#: A process launcher: takes an argv and returns its captured result. Injectable
#: so tests never spawn a real process.
CommandRunner = Callable[[Sequence[str]], CommandResult]


def run_command(argv: Sequence[str], *, timeout_s: float = DEFAULT_TIMEOUT_S):
    """Run ``argv`` and capture it; a missing binary or timeout is a result.

    Never raises for a non-zero exit: the caller inspects ``returncode``. A
    missing executable (``FileNotFoundError``) maps to ``127`` and a timeout to
    ``124`` - the conventional shell codes - so callers can treat "tool absent"
    as an ordinary miss.
    """
    argv_list = list(argv)
    try:
        proc = subprocess.run(
            argv_list,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        return CommandResult(tuple(argv_list), 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        return CommandResult(tuple(argv_list), 124, "", f"timed out: {exc}")
    return CommandResult(
        tuple(argv_list),
        proc.returncode,
        proc.stdout or "",
        proc.stderr or "",
    )


def _clean(value: str | None) -> str | None:
    """Return ``value`` stripped, or ``None`` when blank/unset."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def build_connect_argv(
    ssid: str, password: str, *, security: str = "wpa2"
) -> list[str]:
    """Build the ``nmcli`` argv that joins ``ssid`` with ``password``.

    ``security`` selects the key-management scheme: ``"wpa2"`` (the default, the
    plain command nmcli auto-negotiates) or ``"wpa3"``, which appends
    ``wifi-sec.key-mgmt sae`` so the station uses SAE. Any other value raises
    :class:`ValueError` rather than silently emitting a broken command.
    """
    if security not in SUPPORTED_SECURITY:
        raise ValueError(
            f"unsupported wifi security {security!r}; "
            f"expected one of {SUPPORTED_SECURITY}"
        )
    argv = ["nmcli", "device", "wifi", "connect", ssid, "password", password]
    if security == "wpa3":
        argv += ["wifi-sec.key-mgmt", "sae"]
    return argv


def join_hotspot(
    ssid: str | None,
    password: str | None,
    *,
    security: str = "wpa2",
    runner: CommandRunner = run_command,
) -> CommandResult:
    """Join the ``ssid`` hotspot with ``password`` via ``nmcli``.

    Raises :class:`HotspotConfigError` when either credential is blank (never
    invoking the runner), and :class:`HotspotJoinError` when ``nmcli`` exits
    non-zero. The success :class:`CommandResult` is returned.
    """
    clean_ssid = _clean(ssid)
    clean_password = _clean(password)
    if clean_ssid is None:
        raise HotspotConfigError(
            "no hotspot SSID configured; set PI_ECOSYS_SSID or the config file's `ssid`"
        )
    if clean_password is None:
        raise HotspotConfigError(
            f"no hotspot password configured for {clean_ssid!r}; set "
            "PI_ECOSYS_HOTSPOT_PASSWORD or the config file's `password`"
        )

    argv = build_connect_argv(clean_ssid, clean_password, security=security)
    result = runner(argv)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").replace(
            clean_password, _REDACTED
        )
        detail = detail.strip()
        raise HotspotJoinError(
            f"nmcli failed to join hotspot {clean_ssid!r} "
            f"(exit {result.returncode}): {detail or 'no detail'}"
        )
    return result


def _valid_address(candidate: str) -> str | None:
    """Return ``candidate`` if it is a valid IP literal, else ``None``."""
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def parse_default_gateway(output: str) -> str | None:
    """Extract the default-gateway address from ``ip route`` or ``nmcli`` text.

    Recognises both ``ip route`` (``default via 192.168.43.1 dev wlan0 ...``)
    and ``nmcli --terse --fields IP4.GATEWAY ...`` (``IP4.GATEWAY:192.168.43.1``).
    Returns ``None`` when there is no default route or no usable address; a
    ``default ... scope link`` route with no ``via`` is a miss.
    """
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("default"):
            tokens = line.split()
            if "via" in tokens:
                index = tokens.index("via")
                if index + 1 < len(tokens):
                    found = _valid_address(tokens[index + 1])
                    if found is not None:
                        return found
            continue
        upper = line.upper()
        for prefix in ("IP4.GATEWAY:", "IP6.GATEWAY:"):
            if upper.startswith(prefix):
                found = _valid_address(line[len(prefix) :].strip())
                if found is not None:
                    return found
    return None


#: Commands tried in order by :func:`detect_default_gateway`. ``ip route`` is
#: preferred; ``nmcli`` is the fallback when ``ip`` is absent or route-less.
_GATEWAY_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("ip", "route", "show", "default"),
    ("ip", "route"),
    ("nmcli", "--terse", "--fields", "IP4.GATEWAY", "device", "show"),
)


def detect_default_gateway(*, runner: CommandRunner = run_command) -> str | None:
    """Return the default gateway address, or ``None`` when there is none.

    Tries each command in :data:`_GATEWAY_COMMANDS` until one succeeds and its
    output names a usable gateway. A failed or empty command is skipped, so a
    host without ``ip`` still resolves through ``nmcli`` and vice versa.
    """
    for argv in _GATEWAY_COMMANDS:
        result = runner(argv)
        if result.returncode != 0:
            continue
        gateway = parse_default_gateway(result.stdout)
        if gateway is not None:
            return gateway
    return None


def hub_from_gateway(gateway: str, port: int) -> str:
    """Render ``gateway`` + ``port`` as a gRPC target ``HOST:PORT``.

    An IPv6 gateway is bracketed (``[fe80::1]:50051``) so the result stays a
    valid dial string.
    """
    host = f"[{gateway}]" if ":" in gateway else gateway
    return f"{host}:{port}"
