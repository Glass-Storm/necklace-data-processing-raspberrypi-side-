"""JPEG still capture for the ``ecosys.v1`` ``StreamFrame.image`` wire type.

This module is the Pi's video source. It produces **320x240 JPEG** stills at
**10 fps** for the additive ``ImageFrame`` member of the frozen contract: the hub
decodes each still to run isolated gesture recognition on the person the necklace
points at. There is **no H.264 path on the Pi** any more -- one JPEG per gesture
sample matches the per-image recogniser and keeps the constrained device light.

``picamera2`` is an OPTIONAL extra and is imported lazily, so this module MUST
import (and its tests MUST pass) on a non-Pi host with no camera. Only
:meth:`JpegVideoSource.open` touches the hardware, and it reports "no camera" by
returning ``False`` instead of raising -- the posture ported from the legacy
OpenCV ``video_stream.py``.

Frame-size budget
-----------------
The hub's default gRPC inbound limit is **4 MiB**. A 320x240 JPEG is a few tens of
KiB even at high quality, so :data:`MAX_JPEG_BYTES` (256 KiB, 1/16 of the hub
limit) is a generous ceiling that a single still can never approach; the real
source drops anything past it rather than letting one frame blow the session.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import os
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "DEFAULT_FRAMERATE",
    "DEFAULT_HEIGHT",
    "DEFAULT_WIDTH",
    "ENV_VIDEO_SOURCE",
    "FRAME_INTERVAL_S",
    "HUB_INBOUND_LIMIT_BYTES",
    "JpegVideoSource",
    "MAX_JPEG_BYTES",
    "SyntheticJpegSource",
    "VideoSource",
    "jpeg_dimensions",
    "select_video_source",
    "synthetic_jpeg_frame",
]

# --- Constants ---------------------------------------------------------------

#: Camera resolution. 320x240 is what the hub's gesture recogniser is tuned for.
DEFAULT_WIDTH = 320
DEFAULT_HEIGHT = 240

#: Still cadence reported to the camera and paced by the CLI pump: 10 frames/s.
DEFAULT_FRAMERATE = 10.0

#: Seconds between stills at :data:`DEFAULT_FRAMERATE` (the CLI pump cadence).
FRAME_INTERVAL_S = 1.0 / DEFAULT_FRAMERATE

#: Hub default gRPC inbound message limit (4 MiB). Documented, never approached.
HUB_INBOUND_LIMIT_BYTES = 4 * 1024 * 1024

#: Hard per-still cap enforced by this client: 1/16 of the hub limit (256 KiB).
MAX_JPEG_BYTES = HUB_INBOUND_LIMIT_BYTES // 16

#: The deterministic 320x240 golden JPEG used by the camera-free source. Kept as
#: wrapped base64 (a valid JPEG is not trivially hand-writable); it decodes to a
#: real still whose SOI/EOI and 320x240 SOF are asserted by the suite.
_SYNTHETIC_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDACAWGBwYFCAcGhwkIiAmMFA0MCwsMGJGSjpQdGZ6eHJm"
    "cG6AkLicgIiuim5woNqirr7EztDOfJri8uDI8LjKzsb/wAALCADwAUABAREA/8QAHwAAAQUBAQEB"
    "AQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1Fh"
    "ByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZ"
    "WmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
    "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/AEoooooooooooooooooo"
    "oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo"
    "oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo"
    "oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo"
    "oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo"
    "oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo"
    "oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo"
    "oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo"
    "oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo"
    "oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo"
    "oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo"
    "oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo"
    "oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo"
    "oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo"
    "oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo"
    "oooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooooo"
    "ooooooooooooooooooooooooooooooooooooooooor//2Q=="
)

#: Environment variable selecting the video source: ``auto`` (default),
#: ``synthetic``, ``jpeg`` / ``picamera2_jpeg`` / ``picamera2``.
ENV_VIDEO_SOURCE = "PI_ECOSYS_VIDEO_SOURCE"

_JPEG_NATIVE_SOURCE_CHOICES = frozenset({"jpeg", "jpg", "picamera2_jpeg", "picamera2"})

# The markers that introduce a Start-Of-Frame segment (SOF0..SOF15), minus the
# three codes that share the range but are NOT a frame header: DHT (C4),
# JPG (C8) and DAC (CC). Only a real SOF carries the height/width we parse.
_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}

_synthetic_jpeg_cache: bytes | None = None


# --- JPEG parsing (pure byte logic, testable without a decoder) ---------------


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return ``(width, height)`` from a JPEG's SOF marker, or ``None``.

    Walks the marker segments after the SOI and reads the frame header. This is
    pure byte logic -- no decoder, no optional dependency -- so tests can assert a
    still really is 320x240 without OpenCV or Pillow.
    """
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return None
    index = 2
    end = len(data)
    while index + 1 < end:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker == 0xFF:  # fill byte; not a new marker yet
            index += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            index += 2  # standalone marker, no length field
            continue
        if marker == 0xD9:  # EOI before any SOF
            return None
        if index + 4 > end:
            return None
        segment_length = int.from_bytes(data[index + 2 : index + 4], "big")
        if segment_length < 2:
            return None
        if marker in _SOF_MARKERS:
            if index + 9 > end:
                return None
            height = int.from_bytes(data[index + 5 : index + 7], "big")
            width = int.from_bytes(data[index + 7 : index + 9], "big")
            return (width, height)
        index += 2 + segment_length
    return None


# --- The video-source contract ----------------------------------------------


@runtime_checkable
class VideoSource(Protocol):
    """A pull-based source of JPEG stills.

    ``open()`` reports availability by returning ``False`` instead of raising and
    ``close()`` is idempotent. ``read_frame()`` returns one JPEG ``bytes`` object,
    or ``None`` when no still is ready yet (or the source is exhausted). Because
    ``None`` is also the normal "not ready" state for a live camera, callers MUST
    consult :attr:`done` to tell "not ready yet" apart from "exhausted".
    """

    @property
    def done(self) -> bool:
        """True once no further still can ever be produced."""
        ...

    def open(self) -> bool:
        """Acquire the hardware/source. Returns ``False`` when unavailable."""
        ...

    def read_frame(self) -> bytes | None:
        """Return the next JPEG still, or ``None`` if none is ready."""
        ...

    def close(self) -> None:
        """Release the source. Safe to call more than once."""
        ...


# --- Synthetic source (deterministic, hermetic, no camera) -------------------


def synthetic_jpeg_frame(index: int = 0) -> bytes:
    """Return a deterministic, valid 320x240 JPEG still.

    The bytes are a pre-encoded golden still (decoded once from
    :data:`_SYNTHETIC_JPEG_B64`), so two calls are byte-identical and no encoder
    or camera is needed. ``index`` is accepted for API symmetry with a varying
    source but the payload is a single reproducible image.
    """
    if index < 0:
        raise ValueError(f"index must be non-negative, got {index}")
    global _synthetic_jpeg_cache
    if _synthetic_jpeg_cache is None:
        _synthetic_jpeg_cache = base64.b64decode("".join(_SYNTHETIC_JPEG_B64))
    return _synthetic_jpeg_cache


class SyntheticJpegSource:
    """A camera-free :class:`VideoSource` producing deterministic JPEG stills.

    Used when ``picamera2`` is unavailable (dev hosts, CI) and by tests. A pure
    function of a frame counter, so downstream assertions on frame counts can
    never be satisfied by nondeterministic garbage.
    """

    def __init__(
        self,
        frame_count: int | None = None,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
    ) -> None:
        """:param frame_count: stop after this many stills; ``None`` means infinite."""
        if frame_count is not None and frame_count < 0:
            raise ValueError(f"frame_count must be non-negative, got {frame_count}")
        self._frame_count = frame_count
        self.width = width
        self.height = height
        self._index = 0
        self._open = False

    @property
    def done(self) -> bool:
        """True once ``frame_count`` stills have been produced; never when infinite."""
        return self._frame_count is not None and self._index >= self._frame_count

    def open(self) -> bool:
        """Always succeeds; there is no hardware to fail."""
        self._open = True
        self._index = 0
        return True

    def read_frame(self) -> bytes | None:
        """Return the next deterministic still, or ``None`` once exhausted."""
        if not self._open:
            return None
        if self._frame_count is not None and self._index >= self._frame_count:
            return None
        frame = synthetic_jpeg_frame(self._index)
        self._index += 1
        return frame

    def close(self) -> None:
        """Idempotent no-op."""
        self._open = False


# --- picamera2 source (lazy import, Pi only) ---------------------------------


def _picamera2_available() -> bool:
    """True when the optional ``picamera2`` package is importable.

    Uses ``find_spec`` so the module never imports ``picamera2`` at import time.
    """
    try:
        return importlib.util.find_spec("picamera2") is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


def _select_still_encoder() -> Callable[[Any], bytes]:
    """Pick a JPEG encoder for a captured still array, lazily.

    Prefers OpenCV ``imencode('.jpg', ...)`` (commonly present alongside
    ``picamera2`` on a Pi image) and falls back to Pillow. The native picamera2
    JPEG capture path is tried FIRST by :meth:`JpegVideoSource._capture_frame_bytes`;
    this array encoder is only its fallback.
    """
    try:
        import cv2
    except ImportError:
        cv2 = None

    if cv2 is not None:

        def _encode_cv2(frame: Any) -> bytes:
            ok, buffer = cv2.imencode(".jpg", frame)
            if not ok:
                raise RuntimeError("cv2.imencode returned failure")
            return bytes(buffer.tobytes())

        return _encode_cv2

    from PIL import Image

    def _encode_pillow(frame: Any) -> bytes:
        out = io.BytesIO()
        Image.fromarray(frame).save(out, "JPEG", quality=80)
        return out.getvalue()

    return _encode_pillow


class JpegVideoSource:
    """Real 320x240 @ 10 fps JPEG capture on a Raspberry Pi CSI camera.

    ``picamera2`` is imported lazily inside :meth:`open`, so importing this module
    on a host without the optional extra never fails. When the import or the
    hardware pipeline fails, :meth:`open` returns ``False`` (the camera-unavailable
    posture ported from the legacy OpenCV source) and the caller keeps running.

    Each still is encoded by picamera2's own JPEG path when available (a one-shot
    ``capture_file`` into memory), else a captured array is encoded with OpenCV
    ``imencode`` (Pillow as a last resort). A still larger than
    :data:`MAX_JPEG_BYTES` is dropped so one frame can never approach the hub's
    inbound limit.
    """

    def __init__(
        self,
        width: int = DEFAULT_WIDTH,
        height: int = DEFAULT_HEIGHT,
        framerate: float = DEFAULT_FRAMERATE,
        encode: Callable[[Any], bytes] | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self._framerate = framerate
        self._encode = encode
        self._picam2: object | None = None

    @property
    def done(self) -> bool:
        """Always ``False``: a live camera never exhausts its stream."""
        return False

    def open(self) -> bool:
        """Start the camera. ``False`` when unavailable (never raises)."""
        try:
            from picamera2 import Picamera2
        except ImportError:
            print("[Pi Camera] picamera2 is not installed; camera unavailable")
            return False

        try:
            picam2 = Picamera2()
            # Assign IMMEDIATELY: if configure/start fails below, the except path
            # calls close(), which must see ``self._picam2`` to release the
            # V4L2/ISP handles. Leaving it unassigned would leak the camera.
            self._picam2 = picam2
            self._start_pipeline(picam2)
        except Exception as exc:  # noqa: BLE001 - hardware failure => keep running
            print(f"[Pi Camera] could not start capture: {exc}")
            self.close()
            return False

        print(f"[Pi Camera] opened {self.width}x{self.height} @ {self._framerate} fps")
        return True

    def _start_pipeline(self, picam2: object) -> None:
        """Configure and start the still-capture pipeline (injectable for tests)."""
        config = picam2.create_still_configuration(  # type: ignore[attr-defined]
            main={"size": (self.width, self.height)}
        )
        picam2.configure(config)  # type: ignore[attr-defined]
        picam2.start()  # type: ignore[attr-defined]

    def _capture_frame_bytes(self) -> bytes:
        """Capture and JPEG-encode one still (injectable for tests)."""
        picam2 = self._picam2
        try:
            buffer = io.BytesIO()
            picam2.capture_file(buffer, format="jpeg")  # type: ignore[attr-defined]
            data = buffer.getvalue()
            if data[:2] == b"\xff\xd8":
                return data
        except Exception as exc:  # noqa: BLE001 - fall back to array encoding
            print(f"[Pi Camera] native JPEG capture unavailable: {exc}")

        encoder = self._encode if self._encode is not None else _select_still_encoder()
        return encoder(picam2.capture_array("main"))  # type: ignore[attr-defined]

    def read_frame(self) -> bytes | None:
        """Return one JPEG still, or ``None`` when none is ready."""
        if self._picam2 is None:
            return None
        try:
            jpeg = self._capture_frame_bytes()
        except Exception as exc:  # noqa: BLE001 - a dropped still must not kill the session
            print(f"[Pi Camera] capture failed: {exc}")
            return None
        if not jpeg or len(jpeg) > MAX_JPEG_BYTES:
            print(
                f"[Pi Camera] dropping oversize/empty still "
                f"({len(jpeg)} B, cap {MAX_JPEG_BYTES} B)"
            )
            return None
        return jpeg

    def close(self) -> None:
        """Stop capture and release the camera; safe to call more than once."""
        picam2 = self._picam2
        self._picam2 = None
        if picam2 is not None:
            try:
                picam2.stop()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - teardown must never raise
                print(f"[Pi Camera] error while stopping capture: {exc}")
            try:
                picam2.close()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                print(f"[Pi Camera] error while closing camera: {exc}")


# --- Selection by availability + env ----------------------------------------


def select_video_source(
    env: Mapping[str, str] | None = None,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    framerate: float = DEFAULT_FRAMERATE,
) -> VideoSource:
    """Choose a :class:`VideoSource` from availability and ``PI_ECOSYS_VIDEO_SOURCE``.

    Values (case-insensitive):

    * ``synthetic`` -- always the deterministic camera-free source.
    * ``jpeg`` / ``jpg`` / ``picamera2_jpeg`` / ``picamera2`` -- the real JPEG
      source when ``picamera2`` is importable; otherwise it falls back to
      synthetic so the client still runs.
    * ``auto`` (default) or anything else -- JPEG when available, else synthetic.

    :param env: environment mapping; defaults to ``os.environ``.
    """
    source_env = os.environ if env is None else env
    choice = source_env.get(ENV_VIDEO_SOURCE, "auto").strip().lower()

    if choice == "synthetic":
        return SyntheticJpegSource(width=width, height=height)

    if _picamera2_available():
        return JpegVideoSource(width=width, height=height, framerate=framerate)

    if choice in _JPEG_NATIVE_SOURCE_CHOICES:
        print(
            f"[Pi Camera] PI_ECOSYS_VIDEO_SOURCE={choice} but picamera2 is absent; "
            "falling back to the synthetic source"
        )
    return SyntheticJpegSource(width=width, height=height)
