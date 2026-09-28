"""H.264 camera capture for the ``ecosys.v1`` video stream.

This module is the Pi's video source. It produces raw H.264 **NAL units** for the
frozen wire type ``StreamFrame.video_h264_nal`` (one NAL per frame).

Two important facts about this file:

1. **The legacy ``video_stream.py`` was OpenCV/JPEG, not picamera2/H.264.** Only its
   *hardware open/teardown shape* is reused here (an ``index``-configured capture
   object with an ``open() -> bool`` that reports "no device" instead of raising,
   and an idempotent ``close()``). The JPEG encode step was NOT ported: the wire
   requires H.264, so the encoder below is NEW code built on the proven Raspberry
   Pi camera stack (``Picamera2`` + ``H264Encoder``).

2. **``picamera2`` is an OPTIONAL extra and is imported lazily.** It is absent on
   the dev host and on any non-Pi install. This module MUST import without it; the
   real capture path can only be exercised on a Raspberry Pi. That is expected.

Frame-size budget
-----------------
The hub's default gRPC inbound message limit is **4 MiB**. The camera is capped at
``DEFAULT_WIDTH`` x ``DEFAULT_HEIGHT`` (640x480) at ``DEFAULT_BITRATE`` (2 Mbit/s),
which yields an average NAL of roughly ``2_000_000 / 8 / 30`` ~= 8.3 KiB and even
an unusually large IDR keyframe (which repeats SPS/PPS) well below the hard
``MAX_NAL_BYTES`` cap of **1 MiB** -- a 4x margin under the hub limit. This cap is
why **no hub change is needed**: the Pi keeps every individual NAL comfortably
below the server's inbound bound.
"""

from __future__ import annotations

import importlib.util
import os
import threading
from collections import deque
from collections.abc import Mapping, MutableSequence
from typing import Protocol, runtime_checkable

__all__ = [
    "DEFAULT_BITRATE",
    "DEFAULT_FRAMERATE",
    "DEFAULT_HEIGHT",
    "DEFAULT_WIDTH",
    "HUB_INBOUND_LIMIT_BYTES",
    "MAX_NAL_BYTES",
    "Picamera2VideoSource",
    "SYNTHETIC_START_CODE",
    "SyntheticVideoSource",
    "VideoSource",
    "select_video_source",
    "split_annex_b",
    "synthetic_video_nal",
]

# --- Constants ---------------------------------------------------------------

#: Camera resolution. 640x480 is a modest resolution that keeps keyframes small.
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480

#: Video bitrate in bits/second (2 Mbit/s). At 30 fps that is ~8.3 KiB/frame.
DEFAULT_BITRATE = 2_000_000

#: Nominal capture framerate reported to the encoder.
DEFAULT_FRAMERATE = 30.0

#: Hub default gRPC inbound message limit (4 MiB). Documented, never approached.
HUB_INBOUND_LIMIT_BYTES = 4 * 1024 * 1024

#: Hard per-NAL cap enforced by this client: 1 MiB = 1/4 of the hub limit.
MAX_NAL_BYTES = 1 * 1024 * 1024

#: The synthetic frame shape pinned to ``tools/mockpeer/frames.go``.
SYNTHETIC_START_CODE = b"\x00\x00\x00\x01"
_SYNTHETIC_IDR_HEADER = 0x65
_SYNTHETIC_BODY_LEN = 24

#: Environment variable selecting the video source: ``auto`` (default),
#: ``synthetic``, or ``picamera2``.
ENV_VIDEO_SOURCE = "PI_ECOSYS_VIDEO_SOURCE"


# --- Annex-B splitting (pure byte logic, testable without a camera) ----------


def split_annex_b(data: bytes) -> list[bytes]:
    """Split an H.264 Annex-B byte stream into individual NAL units.

    Each returned unit **includes its original start code** so that concatenating
    the result reproduces the input losslessly (minus discarded framing bytes).
    Including the start code matches the wire's expected NAL shape: the reference
    peer emits ``00 00 00 01 65 <body>`` (see ``tools/mockpeer/frames.go``).

    Start codes are recognised in both forms:

    * 4-byte ``00 00 00 01`` (a 3-byte code preceded by a ``zero_byte``), and
    * 3-byte ``00 00 01``.

    A 4-byte code wins when both could match at the same offset, so the extra
    ``00`` is kept with the unit it introduces.

    Rules:

    * Bytes before the first start code ("leading junk") are discarded.
    * A trailing start code with no payload produces NO unit.
    * Every emitted unit therefore has at least one payload byte after its start
      code (each emitted unit is non-empty).
    * Trailing ``zero_byte`` padding between NALs is NOT stripped; a start code is
      detected independently of what precedes it.

    :param data: raw Annex-B bytes (e.g. one ``H264Encoder`` output buffer).
    :returns: NAL units in stream order, each prefixed by its start code.
    """
    units: list[bytes] = []
    n = len(data)
    i = 0
    unit_start: int | None = None
    payload_start = 0

    while i < n:
        if (
            i + 3 < n
            and data[i] == 0x00
            and data[i + 1] == 0x00
            and data[i + 2] == 0x00
            and data[i + 3] == 0x01
        ):
            if unit_start is not None and payload_start < i:
                units.append(data[unit_start:i])
            unit_start = i
            payload_start = i + 4
            i += 4
            continue
        if (
            i + 2 < n
            and data[i] == 0x00
            and data[i + 1] == 0x00
            and data[i + 2] == 0x01
        ):
            if unit_start is not None and payload_start < i:
                units.append(data[unit_start:i])
            unit_start = i
            payload_start = i + 3
            i += 3
            continue
        i += 1

    if unit_start is not None and payload_start < n:
        units.append(data[unit_start:n])

    return units


# --- The video-source contract ----------------------------------------------


@runtime_checkable
class VideoSource(Protocol):
    """A pull-based source of raw H.264 NAL units.

    Implementations must follow the legacy camera posture: ``open()`` reports
    availability by returning ``False`` instead of raising, and ``close()`` is
    idempotent. ``read_nal()`` returns one start-code-prefixed NAL, or ``None``
    when no frame is ready yet (or the source is done). Because ``None`` is also
    the normal "no NAL queued yet" state for a live camera, callers MUST consult
    :attr:`done` to tell "not ready yet" apart from "exhausted": a live camera is
    never done, a bounded synthetic source is done once its cap is reached.
    """

    @property
    def done(self) -> bool:
        """True once no further NAL can ever be produced."""
        ...

    def open(self) -> bool:
        """Acquire the hardware/source. Returns ``False`` when unavailable."""
        ...

    def read_nal(self) -> bytes | None:
        """Return the next start-code-prefixed NAL, or ``None`` if not ready."""
        ...

    def close(self) -> None:
        """Release the source. Safe to call more than once."""
        ...


# --- Synthetic source (deterministic, mirrors tools/mockpeer/frames.go) ------


def synthetic_video_nal(index: int) -> bytes:
    """Return one deterministic H.264-NAL-shaped blob for ``index``.

    Byte-for-byte the same formula as ``SyntheticVideoNAL`` in the Go mockpeer:
    a 4-byte start code, an IDR-like header byte ``0x65``, then a 24-byte body
    where ``body[i] = (index * 17 + i) % 251``. Two calls with the same index are
    byte-identical, so tests can assert an exact payload without a camera.
    """
    if index < 0:
        raise ValueError(f"index must be non-negative, got {index}")
    body = bytes((index * 17 + i) % 251 for i in range(_SYNTHETIC_BODY_LEN))
    return SYNTHETIC_START_CODE + bytes((_SYNTHETIC_IDR_HEADER,)) + body


class SyntheticVideoSource:
    """A camera-free :class:`VideoSource` producing deterministic NALs.

    Used when ``picamera2`` is unavailable (dev hosts, CI) and by tests. It is a
    pure function of a frame counter, so downstream E2E assertions on frame
    counts can never be satisfied by nondeterministic garbage.
    """

    def __init__(self, frame_count: int | None = None) -> None:
        """:param frame_count: stop after this many NALs; ``None`` means infinite."""
        if frame_count is not None and frame_count < 0:
            raise ValueError(f"frame_count must be non-negative, got {frame_count}")
        self._frame_count = frame_count
        self._index = 0
        self._open = False

    @property
    def done(self) -> bool:
        """True once ``frame_count`` NALs have been produced; never when infinite."""
        return self._frame_count is not None and self._index >= self._frame_count

    def open(self) -> bool:
        """Always succeeds; there is no hardware to fail."""
        self._open = True
        self._index = 0
        return True

    def read_nal(self) -> bytes | None:
        """Return the next deterministic NAL, or ``None`` once exhausted."""
        if not self._open:
            return None
        if self._frame_count is not None and self._index >= self._frame_count:
            return None
        nal = synthetic_video_nal(self._index)
        self._index += 1
        return nal

    def close(self) -> None:
        """Idempotent no-op."""
        self._open = False


# --- picamera2 source (NEW H.264 code; lazy import, Pi only) -----------------


def _picamera2_available() -> bool:
    """True when the optional ``picamera2`` package is importable.

    Uses ``find_spec`` so the module never imports ``picamera2`` at import time.
    """
    try:
        return importlib.util.find_spec("picamera2") is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


def _make_annex_b_output(sink: MutableSequence[bytes], lock: threading.Lock) -> object:
    """Build a ``picamera2.outputs.Output`` that splits Annex-B into NALs.

    Defined in a factory so the ``picamera2`` import (and the ``Output`` base
    class) is only required when a real encoder is actually being started. The
    returned object's ``outputframe`` appends each start-code-prefixed NAL to
    ``sink`` under ``lock``; NALs over ``MAX_NAL_BYTES`` are dropped so a single
    frame can never approach the hub's inbound limit.
    """
    from picamera2.outputs import Output

    class _AnnexBOutput(Output):  # type: ignore[misc, valid-type]
        def outputframe(  # noqa: D102 - inherited signature
            self,
            frame: bytes,
            keyframe: bool = True,
            timestamp: int | None = None,
            packet: object = None,
            audio: bool = False,
        ) -> None:
            if not frame:
                return
            for nal in split_annex_b(bytes(frame)):
                if len(nal) > MAX_NAL_BYTES:
                    print(
                        f"[Pi Camera] dropping oversize NAL {len(nal)} B "
                        f"(cap {MAX_NAL_BYTES} B)"
                    )
                    continue
                with lock:
                    sink.append(nal)

    return _AnnexBOutput()


class Picamera2VideoSource:
    """Real H.264 capture on a Raspberry Pi CSI camera.

    Built on ``Picamera2`` + ``H264Encoder``: the camera is opened, configured
    for ``main={"size": (width, height)}``, and started via
    ``start_recording(encoder, output)``. The custom output splits each encoder
    buffer's Annex-B bytes into individual, start-code-prefixed NAL units.

    ``picamera2`` is imported lazily inside :meth:`open`, so importing this module
    on a host without the optional extra never fails. When the import or the
    hardware pipeline fails, :meth:`open` returns ``False`` (the camera-unavailable
    posture ported from the legacy OpenCV source) and the caller keeps running.
    """

    def __init__(
        self,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        bitrate: int = DEFAULT_BITRATE,
        framerate: float = DEFAULT_FRAMERATE,
    ) -> None:
        self._width = width
        self._height = height
        self._bitrate = bitrate
        self._framerate = framerate
        self._picam2: object | None = None
        self._encoder: object | None = None
        self._pending: deque[bytes] = deque()
        self._lock = threading.Lock()

    @property
    def done(self) -> bool:
        """Always ``False``: a live camera never exhausts its stream."""
        return False

    def open(self) -> bool:
        """Start the camera + encoder. ``False`` when unavailable (never raises)."""
        try:
            from picamera2 import Picamera2
            from picamera2.encoders import H264Encoder
        except ImportError:
            print("[Pi Camera] picamera2 is not installed; camera unavailable")
            return False

        try:
            picam2 = Picamera2()
            # Assign IMMEDIATELY: if configure/start fails below, the except path
            # calls close(), which must see ``self._picam2`` to release the V4L2/ISP
            # handles. Leaving it unassigned would leak the camera on the Pi.
            self._picam2 = picam2
            self._start_pipeline(picam2, H264Encoder)
        except Exception as exc:  # noqa: BLE001 - hardware failure => keep running
            print(f"[Pi Camera] could not start capture: {exc}")
            self.close()
            return False

        print(
            f"[Pi Camera] opened {self._width}x{self._height} @ {self._bitrate} bit/s"
        )
        return True

    def _start_pipeline(self, picam2: object, encoder_cls: object) -> None:
        """Configure and start the recording pipeline (injectable for tests)."""
        config = picam2.create_video_configuration(  # type: ignore[attr-defined]
            main={"size": (self._width, self._height)}
        )
        picam2.configure(config)  # type: ignore[attr-defined]
        encoder = encoder_cls(  # type: ignore[operator]
            bitrate=self._bitrate, framerate=self._framerate
        )
        output = _make_annex_b_output(self._pending, self._lock)
        picam2.start_recording(encoder, output)  # type: ignore[attr-defined]
        self._encoder = encoder

    def read_nal(self) -> bytes | None:
        """Drain one queued NAL, or ``None`` when none has arrived yet."""
        with self._lock:
            if self._pending:
                return self._pending.popleft()
        return None

    def close(self) -> None:
        """Stop recording and release the camera; safe to call more than once."""
        picam2 = self._picam2
        self._picam2 = None
        self._encoder = None
        if picam2 is not None:
            try:
                picam2.stop_recording()
            except Exception as exc:  # noqa: BLE001 - teardown must never raise
                print(f"[Pi Camera] error while stopping recording: {exc}")
            try:
                picam2.close()
            except Exception as exc:  # noqa: BLE001
                print(f"[Pi Camera] error while closing camera: {exc}")


# --- Selection by availability + env ----------------------------------------


def select_video_source(
    env: Mapping[str, str] | None = None,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    bitrate: int = DEFAULT_BITRATE,
    framerate: float = DEFAULT_FRAMERATE,
) -> VideoSource:
    """Choose a :class:`VideoSource` from availability and ``PI_ECOSYS_VIDEO_SOURCE``.

    Values (case-insensitive):

    * ``synthetic`` -- always the deterministic camera-free source.
    * ``picamera2`` -- the real source when ``picamera2`` is importable; otherwise
      it falls back to synthetic so the client still runs.
    * ``auto`` (default) or anything else -- picamera2 when available, else
      synthetic.

    :param env: environment mapping; defaults to ``os.environ``.
    """
    source_env = os.environ if env is None else env
    choice = source_env.get(ENV_VIDEO_SOURCE, "auto").strip().lower()

    if choice == "synthetic":
        return SyntheticVideoSource()

    if _picamera2_available():
        return Picamera2VideoSource(
            width=width, height=height, bitrate=bitrate, framerate=framerate
        )

    if choice == "picamera2":
        print(
            "[Pi Camera] PI_ECOSYS_VIDEO_SOURCE=picamera2 but picamera2 is absent; "
            "falling back to the synthetic source"
        )
    return SyntheticVideoSource()
