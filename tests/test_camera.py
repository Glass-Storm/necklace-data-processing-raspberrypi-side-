"""Tests for the JPEG still source that feeds the ``StreamFrame.image`` wire type.

The camera produces **320x240 JPEG** stills at **30 fps** for the additive
``ecosys.v1`` ``ImageFrame`` member. The real ``JpegVideoSource`` cannot start on
this host (no camera, no ``picamera2``); that is expected and is NOT a failure.
Its lazy-import and unavailable-return posture IS tested here, together with the
deterministic camera-free synthetic source and the JPEG dimension parser.

Everything here runs WITHOUT ``picamera2`` and WITHOUT OpenCV/Pillow: the
synthetic frame is a pre-encoded golden JPEG, so the suite is hermetic.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from ecosys_pi import camera

# --- Constants: 320x240 @ 30 fps and a bounded frame budget ------------------


def test_defaults_are_320x240_at_30fps() -> None:
    assert camera.DEFAULT_WIDTH == 320
    assert camera.DEFAULT_HEIGHT == 240
    assert camera.DEFAULT_FRAMERATE == 30.0
    assert camera.FRAME_INTERVAL_S == pytest.approx(1.0 / 30.0)


def test_frame_budget_has_a_margin_under_the_hub_limit() -> None:
    assert camera.MAX_JPEG_BYTES * 16 == camera.HUB_INBOUND_LIMIT_BYTES


# --- The synthetic frame is a real, reproducible 320x240 JPEG ----------------


def test_synthetic_jpeg_is_a_valid_jpeg() -> None:
    """SOI + EOI markers bracket the payload: it is a real JPEG, not a stub."""
    frame = camera.synthetic_jpeg_frame()
    assert frame[:2] == b"\xff\xd8", "missing JPEG SOI marker"
    assert frame[-2:] == b"\xff\xd9", "missing JPEG EOI marker"


def test_synthetic_jpeg_is_320x240() -> None:
    """The declared image size is exactly the wire contract's 320x240."""
    frame = camera.synthetic_jpeg_frame()
    assert camera.jpeg_dimensions(frame) == (320, 240)


def test_synthetic_jpeg_is_deterministic_and_under_budget() -> None:
    first = camera.synthetic_jpeg_frame()
    second = camera.synthetic_jpeg_frame()
    assert first == second, "the synthetic source must be byte-for-byte reproducible"
    assert len(first) < camera.MAX_JPEG_BYTES


def test_synthetic_jpeg_rejects_negative_index() -> None:
    with pytest.raises(ValueError):
        camera.synthetic_jpeg_frame(-1)


# --- The dimension parser is pure byte logic (no decoder dependency) ---------


def test_jpeg_dimensions_reads_the_golden_frame() -> None:
    assert camera.jpeg_dimensions(camera.synthetic_jpeg_frame()) == (320, 240)


@pytest.mark.parametrize("data", [b"", b"\x00\x00", b"\xff\xd8", b"not-a-jpeg"])
def test_jpeg_dimensions_returns_none_on_non_jpeg(data: bytes) -> None:
    assert camera.jpeg_dimensions(data) is None


# --- Synthetic source: bounded, deterministic, protocol-shaped ---------------


def test_synthetic_source_yields_n_deterministic_frames() -> None:
    source = camera.SyntheticJpegSource(frame_count=3)
    assert source.open() is True
    frames = [source.read_frame() for _ in range(5)]
    golden = camera.synthetic_jpeg_frame()
    assert frames == [golden, golden, golden, None, None]


def test_synthetic_source_before_open_returns_none() -> None:
    source = camera.SyntheticJpegSource()
    assert source.read_frame() is None


def test_synthetic_source_close_is_idempotent_and_stops_frames() -> None:
    source = camera.SyntheticJpegSource()
    source.open()
    source.close()
    source.close()
    assert source.read_frame() is None


def test_synthetic_source_satisfies_video_source_protocol() -> None:
    assert isinstance(camera.SyntheticJpegSource(), camera.VideoSource)


def test_synthetic_source_done_false_while_frames_remain() -> None:
    source = camera.SyntheticJpegSource(frame_count=2)
    source.open()
    assert source.done is False
    source.read_frame()
    assert source.done is False
    source.read_frame()
    assert source.done is True


def test_synthetic_source_unbounded_is_never_done() -> None:
    source = camera.SyntheticJpegSource()
    source.open()
    for _ in range(10):
        assert source.read_frame() is not None
        assert source.done is False


def test_synthetic_source_with_cap_is_done_before_open_and_after_close() -> None:
    source = camera.SyntheticJpegSource(frame_count=1)
    assert source.done is False  # cap not reached yet, even though closed
    source.open()
    source.read_frame()
    assert source.done is True
    source.close()
    assert source.done is True


def test_new_source_starts_closed_and_reopens() -> None:
    source = camera.SyntheticJpegSource(frame_count=1)
    assert source.read_frame() is None
    assert source.open() is True
    assert source.read_frame() == camera.synthetic_jpeg_frame()
    assert source.open() is True  # reopen resets the counter
    assert source.read_frame() == camera.synthetic_jpeg_frame()


def test_synthetic_source_exposes_wire_dimensions() -> None:
    source = camera.SyntheticJpegSource()
    assert source.width == 320
    assert source.height == 240


# --- Selection by availability + env ----------------------------------------


def test_select_synthetic_when_picamera2_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(camera, "_picamera2_available", lambda: False)
    monkeypatch.setattr(camera, "_uvc_device_present", lambda _path: False)
    assert isinstance(camera.select_video_source(env={}), camera.SyntheticJpegSource)


def test_select_synthetic_when_env_forces_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(camera, "_picamera2_available", lambda: True)
    source = camera.select_video_source(env={"PI_ECOSYS_VIDEO_SOURCE": "synthetic"})
    assert isinstance(source, camera.SyntheticJpegSource)


@pytest.mark.parametrize("choice", ["jpg", "jpeg", "picamera2", "picamera2_jpeg"])
def test_select_jpeg_when_available(
    monkeypatch: pytest.MonkeyPatch, choice: str
) -> None:
    monkeypatch.setattr(camera, "_picamera2_available", lambda: True)
    source = camera.select_video_source(env={"PI_ECOSYS_VIDEO_SOURCE": choice})
    assert isinstance(source, camera.JpegVideoSource)


def test_select_defaults_to_jpeg_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(camera, "_picamera2_available", lambda: True)
    assert isinstance(camera.select_video_source(env={}), camera.JpegVideoSource)


@pytest.mark.parametrize("choice", ["jpeg", "picamera2_jpeg"])
def test_select_jpeg_falls_back_to_synthetic_when_absent(
    monkeypatch: pytest.MonkeyPatch, choice: str
) -> None:
    monkeypatch.setattr(camera, "_picamera2_available", lambda: False)
    source = camera.select_video_source(env={"PI_ECOSYS_VIDEO_SOURCE": choice})
    assert isinstance(source, camera.SyntheticJpegSource)


# --- picamera2 is optional and imported lazily -------------------------------


def test_module_imports_without_picamera2() -> None:
    """Importing the camera module must not import the optional extra eagerly."""
    assert "picamera2" not in sys.modules, "picamera2 imported at module import time"


def test_jpeg_source_is_never_done_and_reports_wire_dimensions() -> None:
    source = camera.JpegVideoSource()
    assert source.done is False
    assert source.width == 320
    assert source.height == 240


def test_jpeg_source_open_returns_false_without_package() -> None:
    """With picamera2 absent, open() reports unavailable instead of raising."""
    if camera._picamera2_available():  # pragma: no cover - Pi-only host
        pytest.skip("picamera2 present on this host; absence path not exercisable")
    source = camera.JpegVideoSource()
    assert source.open() is False
    assert source.read_frame() is None


def test_jpeg_source_close_is_idempotent_without_open() -> None:
    source = camera.JpegVideoSource()
    source.close()
    source.close()


class _FakePicamera2:
    """Stands in for ``picamera2.Picamera2``; records teardown calls."""

    instances: list["_FakePicamera2"] = []

    def __init__(self) -> None:
        self.started = 0
        self.closed = 0
        self.stopped = 0
        _FakePicamera2.instances.append(self)

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def close(self) -> None:
        self.closed += 1

    def capture_array(self, _stream: str = "main") -> object:  # pragma: no cover
        return object()


def _install_fake_picamera2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``from picamera2 import Picamera2`` resolve without hardware."""
    import types

    _FakePicamera2.instances = []
    picamera2 = types.ModuleType("picamera2")
    picamera2.Picamera2 = _FakePicamera2
    monkeypatch.setitem(sys.modules, "picamera2", picamera2)


def test_jpeg_source_start_failure_closes_the_constructed_camera(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pipeline-start failure must tear down the already-constructed camera.

    The camera is assigned immediately after construction, so a failing
    ``configure``/``start`` still releases the V4L2/ISP handles on the Pi.
    """
    _install_fake_picamera2(monkeypatch)
    source = camera.JpegVideoSource()

    def _boom(_picam2: object) -> None:
        raise RuntimeError("simulated ISP failure")

    monkeypatch.setattr(source, "_start_pipeline", _boom)

    assert source.open() is False
    assert len(_FakePicamera2.instances) == 1, "Picamera2 must have been constructed"
    assert _FakePicamera2.instances[0].closed == 1, "camera handle leaked on failure"
    assert source.read_frame() is None


def test_jpeg_source_success_path_starts_and_tears_down_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The success path starts the pipeline and close() tears down exactly once."""
    _install_fake_picamera2(monkeypatch)
    source = camera.JpegVideoSource()
    started: list[object] = []
    monkeypatch.setattr(
        source, "_start_pipeline", lambda picam2: started.append(picam2)
    )
    monkeypatch.setattr(
        source, "_capture_frame_bytes", lambda: camera.synthetic_jpeg_frame()
    )

    assert source.open() is True
    assert started == _FakePicamera2.instances
    assert source.read_frame() == camera.synthetic_jpeg_frame()
    assert _FakePicamera2.instances[0].closed == 0

    source.close()
    assert _FakePicamera2.instances[0].stopped == 1
    assert _FakePicamera2.instances[0].closed == 1


def test_jpeg_source_drops_an_oversize_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """A frame past the documented budget is dropped rather than sent upstream."""
    _install_fake_picamera2(monkeypatch)
    source = camera.JpegVideoSource()
    monkeypatch.setattr(source, "_start_pipeline", lambda _picam2: None)
    monkeypatch.setattr(
        source,
        "_capture_frame_bytes",
        lambda: b"\xff\xd8" + b"x" * camera.MAX_JPEG_BYTES,
    )

    assert source.open() is True
    assert source.read_frame() is None


if __name__ == "__main__":  # pragma: no cover - manual invocation
    pytest.main([__file__])


# --- UVC / V4L2 MJPEG source -------------------------------------------------

#: A tiny fixture MJPEG stream: three SOI/EOI-delimited "JPEGs", with junk
#: before the first frame, between frames, and after the last one (ffmpeg writes
#: a raw byte stream, so the parser must resynchronise on SOI).
_MJPEG_FRAME_ONE = b"\xff\xd8" + b"frame-one" * 4 + b"\xff\xd9"
_MJPEG_FRAME_TWO = b"\xff\xd8" + b"frame-two" * 4 + b"\xff\xd9"
_MJPEG_FRAME_THREE = b"\xff\xd8" + b"frame-three" * 4 + b"\xff\xd9"
_MJPEG_FIXTURE = (
    b"\x00junk"
    + _MJPEG_FRAME_ONE
    + b"gap"
    + _MJPEG_FRAME_TWO
    + _MJPEG_FRAME_THREE
    + b"trailer"
)


def test_mjpeg_parser_splits_concatenated_jpegs() -> None:
    parser = camera.MjpegFrameParser()
    frames = parser.feed(_MJPEG_FIXTURE)
    assert len(frames) == 3, "one frame per SOI/EOI pair"
    assert frames == [_MJPEG_FRAME_ONE, _MJPEG_FRAME_TWO, _MJPEG_FRAME_THREE]
    for frame in frames:
        assert frame[:2] == b"\xff\xd8", "frame must start with the JPEG SOI"
        assert frame[-2:] == b"\xff\xd9", "frame must end with the JPEG EOI"


def test_mjpeg_parser_tolerates_a_split_at_every_byte() -> None:
    parser = camera.MjpegFrameParser()
    frames: list[bytes] = []
    for index in range(len(_MJPEG_FIXTURE)):
        frames.extend(parser.feed(_MJPEG_FIXTURE[index : index + 1]))
    assert frames == [_MJPEG_FRAME_ONE, _MJPEG_FRAME_TWO, _MJPEG_FRAME_THREE]


def test_mjpeg_parser_never_emits_a_truncated_frame() -> None:
    parser = camera.MjpegFrameParser()
    assert parser.feed(_MJPEG_FRAME_ONE[:8]) == [], "partial frame must be buffered"
    assert parser.feed(_MJPEG_FRAME_ONE[8:]) == [_MJPEG_FRAME_ONE]


def test_mjpeg_parser_drops_an_oversize_frame_and_resyncs() -> None:
    parser = camera.MjpegFrameParser(max_frame_bytes=len(_MJPEG_FRAME_TWO) + 4)
    oversize = b"\xff\xd8" + b"x" * 64 + b"\xff\xd9"
    assert parser.feed(oversize + _MJPEG_FRAME_TWO[:8]) == []
    assert parser.feed(_MJPEG_FRAME_TWO[8:]) == [_MJPEG_FRAME_TWO]
    assert parser.dropped == 1


class _FakeFfmpegProcess:
    """Stands in for the persistent ffmpeg subprocess (no hardware needed)."""

    def __init__(self, payload: bytes) -> None:
        self.stdout = io.BytesIO(payload)
        self.stderr = io.BytesIO(b"")
        self.returncode: int | None = None
        self.terminated = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated += 1
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def _install_fake_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> tuple[_FakeFfmpegProcess, list[list[str]], object]:
    """Make ``ffmpeg`` resolve without spawning a real process."""
    process = _FakeFfmpegProcess(payload)
    commands: list[list[str]] = []

    def _popen(command: list[str], **_kwargs: object) -> _FakeFfmpegProcess:
        commands.append(command)
        return process

    monkeypatch.setattr(camera, "_ffmpeg_available", lambda _name: "/usr/bin/ffmpeg")
    return process, commands, _popen


def test_uvc_source_reads_frames_from_the_ffmpeg_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    device = tmp_path / "video0"
    device.write_bytes(b"")
    process, commands, popen = _install_fake_ffmpeg(monkeypatch, _MJPEG_FIXTURE)
    source = camera.UvcVideoSource(device=str(device), popen=popen)

    assert source.open() is True
    command = commands[0]
    assert command[0] == "/usr/bin/ffmpeg"
    assert command[command.index("-f") + 1] == "v4l2"
    assert command[command.index("-i") + 1] == str(device)
    assert "image2pipe" in command
    assert command[command.index("-fflags") + 1] == "nobuffer"
    assert command[command.index("-flags") + 1] == "low_delay"
    assert command[command.index("-framerate") + 1] == "30"
    assert command[command.index("-flush_packets") + 1] == "1"
    assert "mjpeg" in command

    frames = [source.read_frame() for _ in range(3)]
    assert frames == [_MJPEG_FRAME_ONE, _MJPEG_FRAME_TWO, _MJPEG_FRAME_THREE]
    assert all(frame is not None and frame[-2:] == b"\xff\xd9" for frame in frames)

    with pytest.raises(camera.CameraSourceError):
        source.read_frame()  # ffmpeg EOF/device loss is a named, visible error
    assert source.done is True
    source.close()
    assert process.terminated == 1


def test_uvc_source_missing_device_is_a_named_error(tmp_path: Path) -> None:
    missing = tmp_path / "video9"
    source = camera.UvcVideoSource(device=str(missing))
    with pytest.raises(camera.CameraSourceError, match="video9"):
        source.open()
    assert source.read_frame() is None


def test_uvc_source_missing_ffmpeg_is_a_named_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    device = tmp_path / "video0"
    device.write_bytes(b"")
    monkeypatch.setattr(camera, "_ffmpeg_available", lambda _name: None)
    source = camera.UvcVideoSource(device=str(device))
    with pytest.raises(camera.CameraSourceError, match="ffmpeg"):
        source.open()


def test_uvc_source_close_is_idempotent_and_terminates_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    device = tmp_path / "video0"
    device.write_bytes(b"")
    process, _commands, popen = _install_fake_ffmpeg(monkeypatch, _MJPEG_FRAME_ONE)
    source = camera.UvcVideoSource(device=str(device), popen=popen)

    source.close()
    assert source.open() is True
    source.close()
    source.close()
    assert process.terminated == 1
    assert source.read_frame() is None


def test_select_uvc_when_env_forces_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(camera, "_ffmpeg_available", lambda _name: "/usr/bin/ffmpeg")
    source = camera.select_video_source(
        env={
            camera.ENV_VIDEO_SOURCE: "uvc",
            camera.ENV_VIDEO_DEVICE: "/dev/video7",
        }
    )
    assert isinstance(source, camera.UvcVideoSource)
    assert source.device == "/dev/video7"


def test_select_uvc_logs_the_selected_source(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(camera, "_ffmpeg_available", lambda _name: "/usr/bin/ffmpeg")
    camera.select_video_source(env={camera.ENV_VIDEO_SOURCE: "uvc"})
    assert "[Pi Camera] source=uvc" in capsys.readouterr().out


def test_auto_prefers_uvc_over_synthetic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(camera, "_picamera2_available", lambda: False)
    monkeypatch.setattr(camera, "_uvc_device_present", lambda _path: True)
    monkeypatch.setattr(camera, "_ffmpeg_available", lambda _name: "/usr/bin/ffmpeg")
    assert isinstance(camera.select_video_source(env={}), camera.UvcVideoSource)


def test_auto_still_prefers_picamera2_when_it_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(camera, "_picamera2_available", lambda: True)
    monkeypatch.setattr(camera, "_uvc_device_present", lambda _path: True)
    assert isinstance(camera.select_video_source(env={}), camera.JpegVideoSource)


def test_auto_falls_back_to_synthetic_without_a_camera(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(camera, "_picamera2_available", lambda: False)
    monkeypatch.setattr(camera, "_uvc_device_present", lambda _path: False)
    assert isinstance(camera.select_video_source(env={}), camera.SyntheticJpegSource)
