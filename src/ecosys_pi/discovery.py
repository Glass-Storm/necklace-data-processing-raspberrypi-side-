"""Resolve the phone-manager hub address for the Pi client.

The hub advertises DNS-SD service ``_ecosys._tcp.local.`` only while its
foreground service is running (see the hub's ``HubBringUp.advertise`` and
``NsdDiscoveryAdapter.DEFAULT_SERVICE_TYPE``). This module turns that into a
single ``HOST:PORT`` the client can dial, with a documented precedence:

1. an explicit ``override`` argument (the ``--hub`` flag) wins outright;
2. otherwise ``PI_ECOSYS_HUB`` from the environment;
3. otherwise a bounded mDNS browse of ``_ecosys._tcp.local.`` resolved to the
   first hub address that answers.

The override path is **primary for automation**: the E2E harness and any unit
test can point the client at a fake hub without touching multicast. mDNS is
best-effort - the hub is silent unless it is running, and the hub's documented
gateway fallback is deliberately UNSET in production DI
(``AndroidAdapterModule.kt``: ``gatewayFallback = null``), so this client must
NEVER invent a subnet or a default IP. When nothing answers, the caller gets a
:class:`HubNotFoundError` whose message names ``PI_ECOSYS_HUB``.

The zeroconf dependency is imported **lazily** inside :func:`browse_mdns` so a
plain ``import ecosys_pi.discovery`` stays light and hermetic; tests inject a
fake Zeroconf/ServiceBrowser and never open a socket.

``resolve_hub`` also accepts an injectable ``resolve_mdns`` callable
``(service_type, timeout_s) -> str | None`` so callers and tests can swap the
whole browse step for a deterministic stub.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from typing import Any

__all__ = [
    "ENV_HUB",
    "SERVICE_TYPE",
    "DEFAULT_TIMEOUT_S",
    "HubNotFoundError",
    "HubServiceListener",
    "address_from_service_info",
    "browse_mdns",
    "resolve_hub",
]

#: Environment variable that overrides discovery with a literal ``HOST:PORT``.
ENV_HUB = "PI_ECOSYS_HUB"

#: The DNS-SD service type every ecosys hub advertises (mirrors the hub's
#: ``NsdDiscoveryAdapter.DEFAULT_SERVICE_TYPE``).
SERVICE_TYPE = "_ecosys._tcp.local."

#: How long a plain mDNS browse waits for a hub before giving up.
DEFAULT_TIMEOUT_S = 5.0

#: Per-service SRV/A resolution budget. Kept small and separate from the overall
#: browse timeout so ``browser.cancel()`` cannot block on a stalled lookup.
_SERVICE_INFO_TIMEOUT_MS = 2000


class HubNotFoundError(RuntimeError):
    """No hub address was supplied and mDNS did not find one.

    The message always names :data:`ENV_HUB` so the operator knows the escape
    hatch for a filtered or unavailable mDNS network.
    """


def _clean(value: str | None) -> str | None:
    """Return ``value`` stripped, or ``None`` when blank/unset."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def address_from_service_info(info: Any) -> str | None:
    """Render a resolved zeroconf ``ServiceInfo`` as ``HOST:PORT``.

    Returns ``None`` when the record is unusable (no parsed address or a
    non-positive port). IPv6 hosts are bracketed so the result stays a valid
    gRPC target (``[fe80::1]:50051``).
    """
    if info is None:
        return None
    try:
        addresses = info.parsed_addresses()
    except Exception:  # noqa: BLE001 - a malformed record must never crash a browse
        return None
    if not addresses:
        return None
    port = getattr(info, "port", 0) or 0
    if port <= 0:
        return None
    host = addresses[0]
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    return f"{host}:{port}"


class HubServiceListener:
    """A zeroconf ``ServiceListener`` that captures the FIRST hub address.

    ``add_service`` runs on zeroconf's browser thread; it resolves the SRV/A
    records with a bounded per-service timeout and latches the first usable
    ``HOST:PORT``. The main thread waits on :meth:`wait`, so the whole browse is
    bounded regardless of how many services appear.
    """

    def __init__(self, info_timeout_ms: int = _SERVICE_INFO_TIMEOUT_MS) -> None:
        self._info_timeout_ms = info_timeout_ms
        self._event = threading.Event()
        self._address: str | None = None

    @property
    def address(self) -> str | None:
        """The first resolved ``HOST:PORT``, or ``None`` until one arrives."""
        return self._address

    def wait(self, timeout: float) -> bool:
        """Block up to ``timeout`` seconds for the first address."""
        return self._event.wait(timeout)

    def add_service(self, zc: Any, type_: str, name: str) -> None:
        """Resolve the newly advertised service and latch it if usable."""
        if self._address is not None:
            return
        try:
            info = zc.get_service_info(type_, name, timeout=self._info_timeout_ms)
        except Exception:  # noqa: BLE001 - a flaky lookup must not kill the browse
            return
        address = address_from_service_info(info)
        if address is None:
            return
        self._address = address
        self._event.set()

    def update_service(self, zc: Any, type_: str, name: str) -> None:
        """Treat an updated record like a fresh advertisement."""
        self.add_service(zc, type_, name)

    def remove_service(self, zc: Any, type_: str, name: str) -> None:
        """A withdrawn service is ignored: we only need the first answer."""


def browse_mdns(
    service_type: str = SERVICE_TYPE,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    *,
    zeroconf_factory: Callable[[], Any] | None = None,
    browser_factory: Callable[..., Any] | None = None,
) -> str | None:
    """Browse ``service_type`` for up to ``timeout_s``; return ``HOST:PORT``.

    Returns ``None`` on a miss. This is deliberately best-effort: a host with no
    multicast (or no zeroconf) simply yields ``None`` and the caller reports a
    clear error. ``zeroconf_factory``/``browser_factory`` are injectable so
    tests exercise the real listener logic against fakes, with no socket.

    ``service_type`` and ``timeout_s`` may also be passed positionally by an
    injected ``resolve_mdns`` callable (see :func:`resolve_hub`).
    """
    if zeroconf_factory is None or browser_factory is None:
        from zeroconf import ServiceBrowser, Zeroconf

        if zeroconf_factory is None:
            zeroconf_factory = Zeroconf
        if browser_factory is None:
            browser_factory = ServiceBrowser

    listener = HubServiceListener()
    try:
        zc = zeroconf_factory()
    except Exception:  # noqa: BLE001 - no multicast stack => a clean miss
        return None

    browser = None
    try:
        browser = browser_factory(zc, service_type, listener)
        listener.wait(timeout_s)
        return listener.address
    except Exception:  # noqa: BLE001 - a broken browse is a miss, never a crash
        return None
    finally:
        if browser is not None:
            try:
                browser.cancel()
            except Exception:  # noqa: BLE001
                pass
        try:
            zc.close()
        except Exception:  # noqa: BLE001
            pass


def resolve_hub(
    override: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    resolve_mdns: Callable[[str, float], str | None] | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> str:
    """Resolve the hub address by override -> env -> mDNS.

    ``override`` (the ``--hub`` flag) wins over ``PI_ECOSYS_HUB``, which wins
    over a bounded mDNS browse. A blank override or env value is treated as
    unset. Raises :class:`HubNotFoundError` - naming ``PI_ECOSYS_HUB`` - when
    no address can be determined.
    """
    env = os.environ if environ is None else environ

    chosen = _clean(override) or _clean(env.get(ENV_HUB))
    if chosen is not None:
        return chosen

    resolver = browse_mdns if resolve_mdns is None else resolve_mdns
    discovered = resolver(SERVICE_TYPE, timeout)
    if discovered is None:
        raise HubNotFoundError(
            "no hub address found: mDNS found no "
            f"{SERVICE_TYPE!r} service within {timeout}s; set {ENV_HUB}=HOST:PORT "
            "or pass --hub HOST:PORT"
        )
    return discovered
