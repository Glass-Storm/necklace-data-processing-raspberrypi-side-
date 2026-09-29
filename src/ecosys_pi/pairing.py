"""The pairing client: redeem a single-use window PIN for a bearer token.

This is the Pi side of ``docs/protocol.md`` section 4. The hub opens a
**single-use** pairing window carrying a 6-digit PIN and a **120 s TTL**
(``PairingViewModel.WINDOW_TTL_MS``). The peer calls ``PairingService/Pair``
with that PIN, its device name, and its role, and the hub returns a bearer
token. The PIN is **burned on a successful pair** (a replay gets
``pin-consumed``), wrong PINs are counted, and after **5 failed attempts** the
current PIN is locked until a fresh window is opened (``MAX_PIN_ATTEMPTS``).

Rejections are explicit
-----------------------

A rejection is a *normal* RPC outcome (``ok=false`` + ``reject_reason``), not a
transport failure, and every reason drives a distinct branch:

==========================  ====================================================
``reject_reason``           client action
==========================  ====================================================
``pin-invalid``             re-prompt (max 5 local attempts, then stop)
``no-window``               require a NEW window/PIN; never resend
``pin-expired``             require a NEW window/PIN; never resend
``pin-consumed``            require a NEW window/PIN; never resend
``pin-locked``              require a NEW window/PIN; never resend
``pin-missing``             programming error - raise
``name-missing``            programming error - raise
==========================  ====================================================

On a **transport** error the PIN is **never resent**: the hub may already have
burned it, so replaying it would only produce a misleading ``pin-consumed``.

The 120 s TTL and the 5-attempt lock are real
---------------------------------------------

The window closes after 120 s and the attempt counter lives on the hub. This
module prints the TTL, warns that the window is single-use, and counts attempts
locally (``MAX_PIN_ATTEMPTS``) so the client can never silently burn all five
and hand the user a ``pin-locked`` dead end.

``PI_ECOSYS_PIN`` is for SCRIPTING ONLY
---------------------------------------

When ``PI_ECOSYS_PIN`` is set, :func:`pair` uses it verbatim and makes exactly
**one** attempt: a bad PIN is reported immediately instead of re-prompting. That
is a non-interactive convenience for automation, so the operator must treat the
value as **single-use with a 120 s TTL** - by the time a script retries, the PIN
is normally expired. Interactive runs leave the variable unset and read the PIN
from the console.
"""

from __future__ import annotations

from enum import Enum
from collections.abc import Callable

import grpc

from ecosys.v1 import ecosys_pb2, ecosys_pb2_grpc
from ecosys_pi.config import Config
from ecosys_pi.token_store import Credentials, TokenStore

__all__ = [
    "MAX_PIN_ATTEMPTS",
    "PAIR_CALL_TIMEOUT_S",
    "REASON_NAME_MISSING",
    "REASON_NO_WINDOW",
    "REASON_PIN_CONSUMED",
    "REASON_PIN_EXPIRED",
    "REASON_PIN_INVALID",
    "REASON_PIN_LOCKED",
    "REASON_PIN_MISSING",
    "WINDOW_TTL_S",
    "PairError",
    "PairProgrammingError",
    "PairProtocolError",
    "PairRejected",
    "PairTransportError",
    "ReasonAction",
    "classify_reason",
    "default_read_pin",
    "pair",
]

# --- Hub constants (exact strings from PairOutcome / docs/protocol.md) ---------

#: No pairing window is currently open.
REASON_NO_WINDOW = "no-window"
#: The window is open but the supplied PIN is not the window PIN.
REASON_PIN_INVALID = "pin-invalid"
#: The window's TTL has elapsed.
REASON_PIN_EXPIRED = "pin-expired"
#: The PIN was already used; recovery requires a fresh window.
REASON_PIN_CONSUMED = "pin-consumed"
#: Too many failed attempts against the current PIN.
REASON_PIN_LOCKED = "pin-locked"
#: The request carried no usable PIN.
REASON_PIN_MISSING = "pin-missing"
#: The request carried no device name.
REASON_NAME_MISSING = "name-missing"

#: The hub's per-window TTL (``PairingViewModel.WINDOW_TTL_MS``).
WINDOW_TTL_S = 120
#: The hub's failed-attempt cap (``PairingService.MAX_PIN_ATTEMPTS``). The client
#: mirrors it locally so it stops BEFORE the hub would answer ``pin-locked``.
MAX_PIN_ATTEMPTS = 5
#: Default deadline for a single ``Pair`` call.
PAIR_CALL_TIMEOUT_S = 15.0

_PROMPT = "Enter pairing PIN"


class ReasonAction(Enum):
    """What the client must do for a given ``reject_reason``."""

    #: Re-prompt for a different PIN (the wrong PIN was not fatal).
    RETRY_PIN = "retry-pin"
    #: A fresh window/PIN is required; the presented PIN must NOT be resent.
    NEW_WINDOW = "new-window"
    #: The hub rejected something the client should never have sent: raise.
    PROGRAMMING = "programming"


#: Rejection reasons that are recoverable only by opening a NEW window.
_NEW_WINDOW_REASONS: frozenset[str] = frozenset(
    {REASON_NO_WINDOW, REASON_PIN_EXPIRED, REASON_PIN_CONSUMED, REASON_PIN_LOCKED}
)
#: Rejection reasons that indicate a client bug (bad request), never a user fix.
_PROGRAMMING_REASONS: frozenset[str] = frozenset(
    {REASON_PIN_MISSING, REASON_NAME_MISSING}
)


class PairError(Exception):
    """Base class for every pairing failure."""


class PairRejected(PairError):
    """The hub answered ``ok=false`` with a known ``reject_reason``.

    This is an EXPECTED outcome (bad/expired/replayed PIN), not a bug, so it
    carries the machine-checkable :attr:`reason` the CLI prints verbatim.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        message = f"pair-rejected reason={reason}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class PairTransportError(PairError):
    """A ``Pair`` RPC failed at the transport layer (including a timeout).

    The PIN is not resent after this: the hub may already have consumed it.
    """


class PairProgrammingError(PairError):
    """The client sent (or was about to send) a request that cannot succeed."""


class PairProtocolError(PairProgrammingError):
    """The hub replied in a way the frozen contract forbids (unknown reason,
    or ``ok=true`` with no token). A ``PairProgrammingError`` subclass so a
    single ``except PairProgrammingError`` catches every malformed exchange.
    """


def classify_reason(reason: str) -> ReasonAction:
    """Map a hub ``reject_reason`` to the action the client must take.

    The mapping is explicit and total: an unknown or blank reason is a contract
    violation and raises :class:`PairProtocolError` rather than being silently
    treated as retryable.
    """
    if reason == REASON_PIN_INVALID:
        return ReasonAction.RETRY_PIN
    if reason in _NEW_WINDOW_REASONS:
        return ReasonAction.NEW_WINDOW
    if reason in _PROGRAMMING_REASONS:
        return ReasonAction.PROGRAMMING
    raise PairProtocolError(
        f"hub returned an unknown reject_reason {reason!r}; refusing to guess"
    )


def default_read_pin(prompt: str) -> str:
    """Read one line from the console (the injectable default for ``read_pin``)."""
    return input(prompt)


def pair(
    channel: grpc.Channel,
    config: Config,
    store: TokenStore | None = None,
    *,
    read_pin: Callable[[str], str] | None = None,
    notify: Callable[[str], None] | None = None,
    timeout: float = PAIR_CALL_TIMEOUT_S,
) -> Credentials:
    """Pair against ``channel`` and persist the issued token.

    ``read_pin`` (default :func:`input`) and ``notify`` (default: stderr) are
    injected so tests need no console. ``config.pin`` is the scripting override:
    when set it is used verbatim for exactly one attempt; otherwise the PIN is
    re-prompted on ``pin-invalid`` up to :data:`MAX_PIN_ATTEMPTS` times.

    Returns the :class:`Credentials` that were saved on success. Raises
    :class:`PairRejected`, :class:`PairTransportError`, or
    :class:`PairProgrammingError` on every other path.
    """
    device_name = config.device_name.strip()
    if not device_name:
        raise PairProgrammingError(
            "device_name is blank; the hub would reject it with 'name-missing'"
        )

    reader = read_pin if read_pin is not None else default_read_pin
    say = notify if notify is not None else _stderr_notify

    scripting_pin = config.pin
    interactive = scripting_pin is None

    say(
        f"The pairing window is SINGLE-USE and expires after {WINDOW_TTL_S} s. "
        f"After {MAX_PIN_ATTEMPTS} wrong PINs the current PIN is locked and a "
        "NEW window is required."
    )
    if not interactive:
        say(
            "PI_ECOSYS_PIN is set: this is the SCRIPTING path. The PIN is used "
            "once and is subject to the same single-use 120 s TTL."
        )

    stub = ecosys_pb2_grpc.PairingServiceStub(channel)
    pin = scripting_pin if not interactive else _prompt_pin(reader, 1)
    attempts = 0

    while True:
        attempts += 1
        response = _call_pair(stub, pin, device_name, timeout)

        if response.ok:
            credentials = _credentials_from(response)
            (store if store is not None else TokenStore.default()).save(credentials)
            return credentials

        action = classify_reason(response.reject_reason)

        if action is ReasonAction.PROGRAMMING:
            raise PairProgrammingError(
                f"hub rejected the request with {response.reject_reason!r}; "
                "this is a client bug, not a fixable PIN"
            )
        if action is ReasonAction.NEW_WINDOW:
            raise PairRejected(
                response.reject_reason,
                "open a NEW pairing window and use its PIN; the presented PIN "
                "was not resent",
            )
        if not interactive:
            raise PairRejected(
                response.reject_reason,
                "non-interactive PIN rejected; open a new window and update "
                "PI_ECOSYS_PIN",
            )
        if attempts >= MAX_PIN_ATTEMPTS:
            raise PairRejected(
                response.reject_reason,
                f"hit the {MAX_PIN_ATTEMPTS}-attempt limit; a NEW pairing "
                "window is required",
            )
        pin = _prompt_pin(reader, attempts + 1)


def _prompt_pin(reader: Callable[[str], str], attempt: int) -> str:
    """Prompt until a non-blank PIN is entered (local validation, not a hub try)."""
    while True:
        entered = reader(f"{_PROMPT} (attempt {attempt}/{MAX_PIN_ATTEMPTS}): ").strip()
        if entered:
            return entered


def _call_pair(
    stub: ecosys_pb2_grpc.PairingServiceStub,
    pin: str,
    device_name: str,
    timeout: float,
) -> ecosys_pb2.PairResponse:
    """Issue ONE ``Pair`` call. Any RPC failure becomes a ``PairTransportError``."""
    request = ecosys_pb2.PairRequest(
        pin=pin,
        device_name=device_name,
        role=ecosys_pb2.DEVICE_ROLE_DAEMON,
    )
    try:
        return stub.Pair(request, timeout=timeout)
    except grpc.RpcError as exc:
        code = exc.code().name if exc.code() is not None else "UNKNOWN"
        detail = exc.details() or str(exc)
        raise PairTransportError(
            f"Pair RPC failed ({code}): {detail}; not resending the PIN because "
            "the hub may already have burned it"
        ) from exc


def _credentials_from(response: ecosys_pb2.PairResponse) -> Credentials:
    """Validate an accepted response and build the :class:`Credentials`."""
    token = response.token.strip()
    device_id = response.device_id.strip()
    if not token:
        raise PairProtocolError("hub reported ok=true but issued an empty token")
    if not device_id:
        raise PairProtocolError("hub reported ok=true but issued an empty device_id")
    return Credentials(token=token, device_id=device_id)


def _stderr_notify(message: str) -> None:
    """Default ``notify``: informational output goes to stderr, never stdout."""
    import sys

    print(message, file=sys.stderr)
