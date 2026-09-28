"""Tests for the pairing client.

The fake hub is a REAL in-process gRPC server on an ephemeral loopback port
(never a mock object), so the client exercises the actual generated
``PairingServiceStub`` over HTTP/2. The console is injected through the
``read_pin`` seam, so no test touches ``input()``.

Coverage:

* ``ok=true`` persists the token through :class:`TokenStore`;
* EVERY ``reject_reason`` drives its exact branch (retry / new-window / raise);
* a transport timeout does NOT re-pair the same PIN;
* a blank ``device_name`` can never be sent;
* 5x ``pin-invalid`` stops locally BEFORE the hub would answer ``pin-locked``;
* the 120 s TTL / single-use warning reaches the operator.
"""

from __future__ import annotations

import threading
import time
from concurrent import futures

import grpc
import pytest

from ecosys.v1 import ecosys_pb2, ecosys_pb2_grpc
from ecosys_pi.config import Config
from ecosys_pi.pairing import (
    MAX_PIN_ATTEMPTS,
    REASON_NAME_MISSING,
    REASON_NO_WINDOW,
    REASON_PIN_CONSUMED,
    REASON_PIN_EXPIRED,
    REASON_PIN_INVALID,
    REASON_PIN_LOCKED,
    REASON_PIN_MISSING,
    WINDOW_TTL_S,
    PairProgrammingError,
    PairProtocolError,
    PairRejected,
    PairTransportError,
    classify_reason,
    pair,
)
from ecosys_pi.token_store import Credentials, TokenStore

TOKEN = "issued-token-xyz"
DEVICE_ID = "device-42"


class FakeHub(ecosys_pb2_grpc.PairingServiceServicer):
    """Scriptable in-process hub: pops a queued response per ``Pair`` call."""

    def __init__(
        self,
        responses: list[ecosys_pb2.PairResponse],
        *,
        default: ecosys_pb2.PairResponse | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self._responses = list(responses)
        self._default = default
        self._delay_s = delay_s
        self.requests: list[ecosys_pb2.PairRequest] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def Pair(self, request, context):  # noqa: N802 - gRPC method name
        self.requests.append(request)
        if self._delay_s:
            time.sleep(self._delay_s)
        if self._responses:
            return self._responses.pop(0)
        if self._default is not None:
            return self._default
        return _ok_response()


def _ok_response(token: str = TOKEN, device_id: str = DEVICE_ID):
    return ecosys_pb2.PairResponse(ok=True, token=token, device_id=device_id)


def _reject(reason: str):
    return ecosys_pb2.PairResponse(ok=False, reject_reason=reason)


class _RunningHub:
    """Context manager: a fake hub served on an ephemeral loopback port."""

    def __init__(self, hub: FakeHub) -> None:
        self._hub = hub
        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        ecosys_pb2_grpc.add_PairingServiceServicer_to_server(hub, self._server)
        port = self._server.add_insecure_port("127.0.0.1:0")
        assert port != 0, "failed to bind an ephemeral port"
        self.target = f"127.0.0.1:{port}"
        self._server.start()

    def __enter__(self) -> str:
        return self.target

    def __exit__(self, *_exc: object) -> None:
        self._server.stop(None)


def _config(pin: str | None, device_name: str = "pi-bench") -> Config:
    return Config(hub=None, pin=pin, tts_lang="yue", device_name=device_name)


class _Reader:
    """Injected console: yields queued PINs and records every prompt."""

    def __init__(self, pins: list[str]) -> None:
        self._pins = list(pins)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self._pins:
            raise AssertionError("unexpected console prompt")
        return self._pins.pop(0)


def _pair(target: str, config: Config, store: TokenStore, **kwargs):
    notify: list[str] = []
    kwargs.setdefault("notify", notify.append)
    with grpc.insecure_channel(target) as channel:
        result = pair(channel, config, store, **kwargs)
    return result, notify


# --------------------------------------------------------------------------- #
# classify_reason: the full explicit table
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (REASON_PIN_INVALID, "retry-pin"),
        (REASON_NO_WINDOW, "new-window"),
        (REASON_PIN_EXPIRED, "new-window"),
        (REASON_PIN_CONSUMED, "new-window"),
        (REASON_PIN_LOCKED, "new-window"),
        (REASON_PIN_MISSING, "programming"),
        (REASON_NAME_MISSING, "programming"),
    ],
)
def test_classify_reason_is_explicit_for_every_hub_string(reason, expected):
    assert classify_reason(reason).value == expected


def test_classify_reason_rejects_unknown_reason():
    with pytest.raises(PairProtocolError):
        classify_reason("some-new-hub-reason")


# --------------------------------------------------------------------------- #
# Success
# --------------------------------------------------------------------------- #


def test_ok_persists_the_token(tmp_path):
    hub = FakeHub([_ok_response()])
    reader = _Reader(["123456"])
    with _RunningHub(hub) as target:
        creds, _ = _pair(
            target, _config(None), TokenStore(tmp_path / "c.json"), read_pin=reader
        )

    assert creds == Credentials(token=TOKEN, device_id=DEVICE_ID)
    assert TokenStore(tmp_path / "c.json").load() == Credentials(TOKEN, DEVICE_ID)
    assert hub.call_count == 1
    assert hub.requests[0].pin == "123456"


def test_pair_uses_daemon_role_and_nonblank_device_name(tmp_path):
    hub = FakeHub([_ok_response()])
    with _RunningHub(hub) as target:
        _pair(
            target,
            _config("123456", "my-pi"),
            TokenStore(tmp_path / "c.json"),
        )

    request = hub.requests[0]
    assert request.role == ecosys_pb2.DEVICE_ROLE_DAEMON
    assert request.role == 2
    assert request.device_name == "my-pi"
    assert request.device_name.strip() != ""


def test_blank_device_name_is_impossible_and_never_calls_hub(tmp_path):
    hub = FakeHub([_ok_response()])
    reader = _Reader(["123456"])
    with _RunningHub(hub) as target:
        with pytest.raises(PairProgrammingError, match="device_name is blank"):
            _pair(
                target,
                _config("123456", "   "),
                TokenStore(tmp_path / "c.json"),
                read_pin=reader,
            )

    assert hub.call_count == 0
    assert reader.prompts == []


def test_blank_pin_prompt_retries_locally_without_an_rpc(tmp_path):
    hub = FakeHub([_ok_response()])
    reader = _Reader(["", "   ", "123456"])
    with _RunningHub(hub) as target:
        _pair(target, _config(None), TokenStore(tmp_path / "c.json"), read_pin=reader)

    assert hub.call_count == 1
    assert len(reader.prompts) == 3


# --------------------------------------------------------------------------- #
# Each rejection reason drives its branch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "reason",
    [REASON_NO_WINDOW, REASON_PIN_EXPIRED, REASON_PIN_CONSUMED, REASON_PIN_LOCKED],
)
def test_new_window_reasons_require_a_fresh_window_and_do_not_resend(tmp_path, reason):
    hub = FakeHub([_reject(reason)])
    reader = _Reader(["123456"])
    with _RunningHub(hub) as target:
        with pytest.raises(PairRejected) as excinfo:
            _pair(
                target, _config(None), TokenStore(tmp_path / "c.json"), read_pin=reader
            )

    assert excinfo.value.reason == reason
    assert "not resent" in excinfo.value.detail
    assert hub.call_count == 1
    assert len(reader.prompts) == 1  # no re-prompt: a NEW window is required


@pytest.mark.parametrize("reason", [REASON_PIN_MISSING, REASON_NAME_MISSING])
def test_programming_reasons_raise(tmp_path, reason):
    hub = FakeHub([_reject(reason)])
    with _RunningHub(hub) as target:
        with pytest.raises(PairProgrammingError, match="client bug"):
            # A scripting PIN reaches the wire; the fake hub answers the
            # programming reason even though the client sent a good request.
            _pair(
                target,
                _config("123456"),
                TokenStore(tmp_path / "c.json"),
                read_pin=_Reader([]),
            )

    assert hub.call_count == 1


def test_pin_invalid_reprompts_and_can_succeed(tmp_path):
    hub = FakeHub([_reject(REASON_PIN_INVALID), _ok_response()])
    reader = _Reader(["111111", "654321"])
    with _RunningHub(hub) as target:
        creds, _ = _pair(
            target, _config(None), TokenStore(tmp_path / "c.json"), read_pin=reader
        )

    assert creds.token == TOKEN
    assert [r.pin for r in hub.requests] == ["111111", "654321"]
    assert hub.call_count == 2


def test_five_pin_invalid_stops_before_the_lock(tmp_path):
    # The hub mirrors its real semantics: pin-invalid for the first five, then
    # pin-locked. The client must stop at five and NEVER see pin-locked.
    hub = FakeHub(
        [_reject(REASON_PIN_INVALID)] * MAX_PIN_ATTEMPTS + [_reject(REASON_PIN_LOCKED)]
    )
    reader = _Reader([f"00000{i}" for i in range(MAX_PIN_ATTEMPTS + 1)])
    with _RunningHub(hub) as target:
        with pytest.raises(PairRejected) as excinfo:
            _pair(
                target, _config(None), TokenStore(tmp_path / "c.json"), read_pin=reader
            )

    assert excinfo.value.reason == REASON_PIN_INVALID
    assert str(MAX_PIN_ATTEMPTS) in excinfo.value.detail
    assert hub.call_count == MAX_PIN_ATTEMPTS
    assert len(reader.prompts) == MAX_PIN_ATTEMPTS
    assert hub.requests[-1].pin == "000004"
    assert hub.requests[-1].pin != "000005"  # the 6th PIN was never prompted/sent


def test_scripting_pin_is_single_attempt(tmp_path):
    hub = FakeHub([_reject(REASON_PIN_INVALID)])

    def _never(_prompt: str) -> str:  # pragma: no cover - must not be called
        raise AssertionError("scripting path must not prompt")

    with _RunningHub(hub) as target:
        with pytest.raises(PairRejected) as excinfo:
            _pair(
                target,
                _config("123456"),
                TokenStore(tmp_path / "c.json"),
                read_pin=_never,
            )

    assert excinfo.value.reason == REASON_PIN_INVALID
    assert "non-interactive" in excinfo.value.detail
    assert hub.call_count == 1


# --------------------------------------------------------------------------- #
# Transport failures never resend the PIN
# --------------------------------------------------------------------------- #


def test_transport_timeout_does_not_repair_the_same_pin(tmp_path):
    hub = FakeHub([], default=_ok_response(), delay_s=0.5)
    reader = _Reader(["123456", "999999"])
    with _RunningHub(hub) as target:
        with pytest.raises(PairTransportError):
            _pair(
                target,
                _config(None),
                TokenStore(tmp_path / "c.json"),
                read_pin=reader,
                timeout=0.05,
            )

    # Exactly ONE attempt: the PIN may already be burned, so it is not resent.
    assert hub.call_count == 1
    assert len(reader.prompts) == 1


# --------------------------------------------------------------------------- #
# Operator guidance: TTL and single-use warning
# --------------------------------------------------------------------------- #


def test_ttl_and_single_use_are_announced(tmp_path):
    hub = FakeHub([_ok_response()])
    with _RunningHub(hub) as target:
        _, notify = _pair(
            target,
            _config(None),
            TokenStore(tmp_path / "c.json"),
            read_pin=_Reader(["123456"]),
        )

    joined = " ".join(notify)
    assert str(WINDOW_TTL_S) in joined
    assert "SINGLE-USE" in joined
    assert "NEW window" in joined


def test_scripting_override_is_announced(tmp_path):
    hub = FakeHub([_ok_response()])
    with _RunningHub(hub) as target:
        _, notify = _pair(
            target,
            _config("123456"),
            TokenStore(tmp_path / "c.json"),
            read_pin=_Reader([]),
        )

    assert any("SCRIPTING" in message for message in notify)


# --------------------------------------------------------------------------- #
# Malformed hub responses are contract violations
# --------------------------------------------------------------------------- #


def test_ok_with_empty_token_is_a_protocol_error(tmp_path):
    hub = FakeHub([_ok_response(token="")])
    with _RunningHub(hub) as target:
        with pytest.raises(PairProtocolError, match="empty token"):
            _pair(
                target,
                _config("123456"),
                TokenStore(tmp_path / "c.json"),
                read_pin=_Reader([]),
            )


def test_unknown_reject_reason_is_a_protocol_error(tmp_path):
    hub = FakeHub([_reject("brand-new-reason")])
    with _RunningHub(hub) as target:
        with pytest.raises(PairProtocolError, match="unknown reject_reason"):
            _pair(
                target,
                _config("123456"),
                TokenStore(tmp_path / "c.json"),
                read_pin=_Reader([]),
            )


def test_reader_is_called_from_the_caller_thread(tmp_path):
    """The injected reader runs on the caller's thread, never a gRPC worker."""
    calls: list[int] = []

    def _reader(_prompt: str) -> str:
        calls.append(threading.get_ident())
        return "123456"

    hub = FakeHub([_reject(REASON_PIN_INVALID), _ok_response()])
    with _RunningHub(hub) as target:
        main = threading.get_ident()
        with grpc.insecure_channel(target) as channel:
            pair(
                channel,
                _config(None),
                TokenStore(tmp_path / "c.json"),
                read_pin=_reader,
            )

    assert calls == [main, main]
