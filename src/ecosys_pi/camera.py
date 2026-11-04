"""JPEG still capture for the ``ecosys.v1`` ``StreamFrame.image`` wire type.

This module is the Pi's video source. It produces **320x240 JPEG** stills at
**10 fps** for the additive ``ImageFrame`` member of the frozen contract: the hub
decodes each still to run isolated gesture recognition on the person the necklace
points at. There is **no H.264 path on the Pi** any more -- one JPEG per gesture
sample matches the per-image recogniser and keeps the constrained device light.

Two hardware backends are provided: the CSI path (:class:`JpegVideoSource` via
``picamera2``, an OPTIONAL extra imported lazily) and the USB webcam path
(:class:`UvcVideoSource`, a persistent ``ffmpeg`` V4L2 -> MJPEG pump). Both parse
the same ``ImageFrame`` contract. Importing this module MUST succeed on a
non-Pi host with no camera: only ``open()`` touches hardware.

The CSI source reports "no camera" by returning ``False`` (the posture ported
from the legacy OpenCV ``video_stream.py``). The UVC source, when selected
explicitly with ``PI_ECOSYS_VIDEO_SOURCE=uvc`` for the demo, instead raises the
named :class:`CameraSourceError` for a missing device/ffmpeg or a mid-stream
device loss, so the client exits non-zero rather than silently degrading to
synthetic video.

Frame-size budget
-----------------
The hub's default gRPC inbound limit is **4 MiB**. A 320x240 (or the USB webcam's
native 640x480) JPEG is a few tens of KiB even at high quality, so
:data:`MAX_JPEG_BYTES` (256 KiB, 1/16 of the hub limit) is a generous ceiling that
a single still can never approach; the real sources drop anything past it rather
than letting one frame blow the session.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import os
import shutil
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "CameraSourceError",
    "DEFAULT_FRAMERATE",
    "DEFAULT_HEIGHT",
    "DEFAULT_UVC_HEIGHT",
    "DEFAULT_UVC_QUALITY",
    "DEFAULT_UVC_WIDTH",
    "DEFAULT_VIDEO_DEVICE",
    "DEFAULT_WIDTH",
    "ENV_VIDEO_DEVICE",
    "ENV_VIDEO_SOURCE",
    "FRAME_INTERVAL_S",
    "HUB_INBOUND_LIMIT_BYTES",
    "JpegVideoSource",
    "MAX_JPEG_BYTES",
    "MjpegFrameParser",
    "SyntheticJpegSource",
    "UvcVideoSource",
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

#: Default UVC/V4L2 capture node. The necklace webcam is a USB UVC device.
DEFAULT_VIDEO_DEVICE = "/dev/video0"

#: Environment variable overriding the UVC capture node (documented probe seam).
ENV_VIDEO_DEVICE = "PI_ECOSYS_VIDEO_DEVICE"

#: The necklace webcam advertises MJPG at 640x480 (no 320x240 mode), so the UVC
#: source captures at that native size; the hub's recogniser resizes as needed.
DEFAULT_UVC_WIDTH = 640
DEFAULT_UVC_HEIGHT = 480

#: ffmpeg MJPEG quality (1 = best, 31 = worst) for the V4L2 -> image2pipe pump.
DEFAULT_UVC_QUALITY = 5

#: ffmpeg executable looked up on PATH for the UVC source.
DEFAULT_FFMPEG = "ffmpeg"

#: Environment variable selecting the video source: ``auto`` (default),
#: ``synthetic``, ``uvc``, ``jpeg`` / ``picamera2_jpeg`` / ``picamera2``.
ENV_VIDEO_SOURCE = "PI_ECOSYS_VIDEO_SOURCE"

_JPEG_NATIVE_SOURCE_CHOICES = frozenset({"jpeg", "jpg", "picamera2_jpeg", "picamera2"})

#: JPEG frame delimiters used by the MJPEG parser.
_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"

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


# --- MJPEG frame parsing (pure byte logic, testable without a device) --------


class MjpegFrameParser:
    """Split an MJPEG byte stream into complete JPEG frames on SOI/EOI markers.

    ffmpeg writes a raw concatenation of JPEGs to ``image2pipe``; reads arrive in
    arbitrary chunks, so a frame header/footer can be split across two reads. The
    parser buffers partial frames and only ever returns bytes that start with
    ``FF D8`` and end with ``FF D9`` -- a truncated frame is never emitted. Junk
    between frames (or a resync after device loss) is skipped by scanning for the
    next SOI, and frames larger than ``max_frame_bytes`` are dropped so a runaway
    stream can never grow the buffer without bound.

    The parser is deliberately pure (no subprocess, no camera) so the framing
    rules are unit-testable with a fixture byte stream.
    """

    def __init__(self, max_frame_bytes: int = MAX_JPEG_BYTES) -> None:
        self._max_frame_bytes = max_frame_bytes
        self._buffer = bytearray()
        self.dropped = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        """Append ``chunk`` and return every complete JPEG it completed."""
        if chunk:
            self._buffer.extend(chunk)
        frames: list[bytes] = []
        while True:
            buffer = self._buffer
            start = buffer.find(_JPEG_SOI)
            if start < 0:
                # No SOI yet: keep only a trailing 0xFF, which may be the first
                # byte of an SOI split across the next chunk.
                if buffer and buffer[-1] == 0xFF:
                    del buffer[: len(buffer) - 1]
                else:
                    buffer.clear()
                break
            if start:
                del buffer[:start]
            end = buffer.find(_JPEG_EOI, 2)
            if end < 0:
                if len(buffer) > self._max_frame_bytes + 2:
                    self.dropped += 1
                    next_start = buffer.find(_JPEG_SOI, 1)
                    if next_start < 0:
                        if buffer and buffer[-1] == 0xFF:
                            del buffer[: len(buffer) - 1]
                        else:
                            buffer.clear()
                        break
                    del buffer[:next_start]
                    continue
                break
            frame = bytes(buffer[: end + 2])
            del buffer[: end + 2]
            if len(frame) > self._max_frame_bytes:
                self.dropped += 1
                continue
            frames.append(frame)
        return frames


# --- The video-source contract ----------------------------------------------


class CameraSourceError(RuntimeError):
    """A camera that was explicitly selected could not start, or died mid-stream.

    Unlike :class:`JpegVideoSource` (which reports "no camera" by returning
    ``False``), an explicit ``PI_ECOSYS_VIDEO_SOURCE=uvc`` request must never
    silently degrade to synthetic video: the client surfaces this error and exits
    non-zero.
    """


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


# --- UVC/V4L2 source (persistent ffmpeg, USB webcam) ------------------------


def _uvc_device_present(path: str) -> bool:
    """True when the V4L2 capture node exists (a seam for hermetic tests)."""
    return os.path.exists(path)


def _ffmpeg_available(name: str) -> str | None:
    """Resolve the ffmpeg executable to an absolute path, or ``None``."""
    return shutil.which(name)


class UvcVideoSource:
    """Persistent UVC/V4L2 MJPEG source driven by a single ffmpeg process.

    ffmpeg captures the USB webcam (MJPG) and writes a raw concatenation of JPEGs
    to stdout; :class:`MjpegFrameParser` turns that byte stream into complete
    stills. The process is started once in :meth:`open` and stopped in
    :meth:`close`, so no per-frame renegotiation happens on the constrained Pi.

    Availability posture differs from :class:`JpegVideoSource`: when this source
    was *explicitly* selected, a missing device or a missing ffmpeg is a hard,
    named :class:`CameraSourceError` (the caller exits non-zero) rather than a
    silent fallback to synthetic video. ``stderr`` from ffmpeg is drained on a
    daemon thread so a chatty process can never block on a full pipe.
    """

    def __init__(
        self,
        device: str = DEFAULT_VIDEO_DEVICE,
        width: int = DEFAULT_UVC_WIDTH,
        height: int = DEFAULT_UVC_HEIGHT,
        quality: int = DEFAULT_UVC_QUALITY,
        ffmpeg: str = DEFAULT_FFMPEG,
        popen: Callable[..., Any] | None = None,
    ) -> None:
        self.device = device
        self.width = width
        self.height = height
        self._quality = quality
        self._ffmpeg = ffmpeg
        self._popen = popen if popen is not None else subprocess.Popen
        self._process: Any | None = None
        self._parser = MjpegFrameParser()
        self._pending: list[bytes] = []
        self._stderr_lines: deque[str] = deque(maxlen=20)
        self._exhausted = False

    @property
    def done(self) -> bool:
        """True once the ffmpeg stream ended and no parsed frame is pending."""
        if self._process is None:
            return self._exhausted
        return self._exhausted and not self._pending

    def ffmpeg_command(self, executable: str | None = None) -> list[str]:
        """The exact persistent ffmpeg pipeline used for the webcam."""
        binary = executable if executable is not None else self._ffmpeg
        return [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "v4l2",
            "-video_size",
            f"{self.width}x{self.height}",
            "-i",
            self.device,
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-q:v",
            str(self._quality),
            "-",
        ]

    def open(self) -> bool:
        """Start ffmpeg. Raises :class:`CameraSourceError` when unavailable."""
        self._release_process()
        self._parser = MjpegFrameParser()
        self._pending = []
        self._stderr_lines.clear()
        self._exhausted = False

        if not _uvc_device_present(self.device):
            raise CameraSourceError(
                f"[Pi Camera] UVC device {self.device} is missing; "
                f"set {ENV_VIDEO_DEVICE} or plug the webcam in"
            )
        executable = _ffmpeg_available(self._ffmpeg)
        if executable is None:
            raise CameraSourceError(
                f"[Pi Camera] ffmpeg executable {self._ffmpeg!r} was not found on "
                "PATH; the UVC source cannot start"
            )
        try:
            process = self._popen(
                self.ffmpeg_command(executable),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:  # pragma: no cover - spawn failure is host-specific
            raise CameraSourceError(
                f"[Pi Camera] could not start ffmpeg for {self.device}: {exc}"
            ) from exc
        self._process = process
        stderr = getattr(process, "stderr", None)
        if stderr is not None:
            threading.Thread(
                target=self._drain_stderr,
                args=(stderr,),
                name="ecosys-ffmpeg-stderr",
                daemon=True,
            ).start()
        print(
            f"[Pi Camera] source=uvc device={self.device} "
            f"{self.width}x{self.height} q={self._quality}"
        )
        return True

    def read_frame(self) -> bytes | None:
        """Return the next complete JPEG, or raise when ffmpeg/device dies."""
        if self._process is None:
            return None
        while True:
            if self._pending:
                frame = self._pending.pop(0)
                if len(frame) > MAX_JPEG_BYTES:
                    print(
                        f"[Pi Camera] dropping oversize UVC still "
                        f"({len(frame)} B, cap {MAX_JPEG_BYTES} B)"
                    )
                    continue
                return frame
            chunk = self._read_chunk()
            if not chunk:
                self._exhausted = True
                raise CameraSourceError(self._eof_message())
            self._pending.extend(self._parser.feed(chunk))

    def close(self) -> None:
        """Terminate ffmpeg and release its pipes; safe to call repeatedly."""
        self._release_process()
        self._exhausted = True

    def _read_chunk(self) -> bytes:
        process = self._process
        if process is None:
            return b""
        stream = getattr(process, "stdout", None)
        if stream is None:
            return b""
        read1 = getattr(stream, "read1", None)
        try:
            chunk = read1(65536) if read1 is not None else stream.read(65536)
        except (OSError, ValueError):
            return b""
        return b"" if chunk is None else chunk

    def _eof_message(self) -> str:
        process = self._process
        code = process.poll() if process is not None else None
        diagnostics = "; ".join(self._stderr_lines) or "no ffmpeg diagnostics"
        return (
            f"[Pi Camera] UVC capture from {self.device} ended unexpectedly "
            f"(ffmpeg exit code {code!r}): {diagnostics}"
        )

    def _drain_stderr(self, stream: Any) -> None:
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._stderr_lines.append(line)
        except (OSError, ValueError):  # pragma: no cover - pipe torn down
            return

    def _release_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=2)
        except Exception:  # noqa: BLE001 - best-effort teardown
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:  # noqa: BLE001 - teardown must never raise
                pass
        for attribute in ("stdout", "stderr"):
            stream = getattr(process, attribute, None)
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass


# --- Selection by availability + env ----------------------------------------


def select_video_source(
    env: Mapping[str, str] | None = None,
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    framerate: float = DEFAULT_FRAMERATE,
    uvc_width: int = DEFAULT_UVC_WIDTH,
    uvc_height: int = DEFAULT_UVC_HEIGHT,
) -> VideoSource:
    """Choose a :class:`VideoSource` from availability and ``PI_ECOSYS_VIDEO_SOURCE``.

    Values (case-insensitive):

    * ``synthetic`` -- always the deterministic camera-free source.
    * ``uvc`` -- the UVC/V4L2 source on ``PI_ECOSYS_VIDEO_DEVICE`` (default
      ``/dev/video0``). This is the DEMO path and never falls back: a missing
      device or ffmpeg raises :class:`CameraSourceError` at :meth:`open`.
    * ``jpeg`` / ``jpg`` / ``picamera2_jpeg`` / ``picamera2`` -- the real JPEG
      source when ``picamera2`` is importable; otherwise it falls back to
      synthetic so the client still runs.
    * ``auto`` (default) or anything else -- ``picamera2`` when available, else
      UVC when ``/dev/video0`` and ffmpeg are present, else a loud synthetic
      fallback for camera-free hosts and CI.

    :param env: environment mapping; defaults to ``os.environ``.
    """
    source_env = os.environ if env is None else env
    choice = source_env.get(ENV_VIDEO_SOURCE, "auto").strip().lower()
    device = (
        source_env.get(ENV_VIDEO_DEVICE, DEFAULT_VIDEO_DEVICE).strip()
        or DEFAULT_VIDEO_DEVICE
    )

    if choice == "synthetic":
        return SyntheticJpegSource(width=width, height=height)

    if choice == "uvc":
        print(f"[Pi Camera] source=uvc device={device}")
        return UvcVideoSource(
            device=device, width=uvc_width, height=uvc_height
        )

    if _picamera2_available():
        return JpegVideoSource(width=width, height=height, framerate=framerate)

    if choice in _JPEG_NATIVE_SOURCE_CHOICES:
        print(
            f"[Pi Camera] PI_ECOSYS_VIDEO_SOURCE={choice} but picamera2 is absent; "
            "falling back to the synthetic source"
        )
        return SyntheticJpegSource(width=width, height=height)

    if _uvc_device_present(device) and _ffmpeg_available(DEFAULT_FFMPEG) is not None:
        print(f"[Pi Camera] source=uvc device={device}")
        return UvcVideoSource(
            device=device, width=uvc_width, height=uvc_height
        )

    print(
        "[Pi Camera] source=synthetic: no camera found "
        f"({device} absent or ffmpeg missing) and picamera2 is absent"
    )
    return SyntheticJpegSource(width=width, height=height)
