"""Resolve the phone-manager hub address for the Pi client.

The hub **is** the phone's Wi-Fi hotspot access point, so from the Pi the hub is
the **default gateway**. This module turns that into a single ``HOST:PORT`` the
client can dial, with a documented precedence:

1. an explicit ``override`` argument (the ``--hub`` flag) wins outright;
2. otherwise ``PI_ECOSYS_HUB`` from the environment;
3. otherwise the default gateway
   (:func:`ecosys_pi.network.detect_default_gateway`) combined with the
   configured ``hub_port`` (``PI_ECOSYS_HUB_PORT``, default ``50051``).

There is **no mDNS**: discovery is gateway-only. Steps 1-2 are the escape hatch
for tests and hosts where the hub is not the gateway - notably this repo's E2E
harness, which dials loopback via ``--hub``/``PI_ECOSYS_HUB``. The gateway
detector is injected through ``detect_gateway`` so unit tests never run
``ip``/``nmcli``.

When nothing resolves, the caller gets a :class:`HubNotFoundError` whose message
names ``PI_ECOSYS_HUB`` so the operator knows the escape hatch.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping

from ecosys_pi.config import DEFAULT_HUB_PORT
from ecosys_pi.network import detect_default_gateway, hub_from_gateway

__all__ = [
    "ENV_HUB",
    "HubNotFoundError",
    "resolve_hub",
]

#: Environment variable that overrides discovery with a literal ``HOST:PORT``.
ENV_HUB = "PI_ECOSYS_HUB"


class HubNotFoundError(RuntimeError):
    """No hub address was supplied and the default gateway was not found.

    The message always names :data:`ENV_HUB` and ``--hub`` so the operator knows
    the escape hatch for a host where the hub is not the gateway.
    """


def _clean(value: str | None) -> str | None:
    """Return ``value`` stripped, or ``None`` when blank/unset."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def resolve_hub(
    override: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    port: int = DEFAULT_HUB_PORT,
    detect_gateway: Callable[[], str | None] = detect_default_gateway,
) -> str:
    """Resolve the hub address by override -> env -> default gateway.

    ``override`` (the ``--hub`` flag) wins over ``PI_ECOSYS_HUB``, which wins
    over the detected default gateway. A blank override or env value is treated
    as unset. ``port`` is the gRPC port appended to a gateway host. Raises
    :class:`HubNotFoundError` - naming ``PI_ECOSYS_HUB`` and ``--hub`` - when no
    address can be determined.
    """
    env = os.environ if environ is None else environ

    chosen = _clean(override) or _clean(env.get(ENV_HUB))
    if chosen is not None:
        return chosen

    gateway = detect_gateway()
    if gateway is None:
        raise HubNotFoundError(
            "no hub address found: no default gateway on this host; "
            f"set {ENV_HUB}=HOST:PORT or pass --hub HOST:PORT"
        )
    return hub_from_gateway(gateway, port)
