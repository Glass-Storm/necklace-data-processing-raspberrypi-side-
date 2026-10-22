"""Tests for the JPEG still source that feeds the ``StreamFrame.image`` wire type.

The camera produces **320x240 JPEG** stills at **10 fps** for the additive
``ecosys.v1`` ``ImageFrame`` member. The real ``JpegVideoSource`` cannot start on
this host (no camera, no ``picamera2``); that is expected and is NOT a failure.
Its lazy-import and unavailable-return posture IS tested here, together with the
deterministic camera-free synthetic source and the JPEG dimension parser.

Everything here runs WITHOUT ``picamera2`` and WITHOUT OpenCV/Pillow: the
synthetic frame is a pre-encoded golden JPEG, so the suite is hermetic.
"""

from __future__ import annotations

import sys

import pytest

from ecosys_pi import camera

# --- Constants: 320x240 @ 10 fps and a bounded frame budget ------------------


def test_defaults_are_320x240_at_10fps() -> None:
    assert camera.DEFAULT_WIDTH == 320
    assert camera.DEFAULT_HEIGHT == 240
    assert camera.DEFAULT_FRAMERATE == 10.0
    assert camera.FRAME_INTERVAL_S == pytest.approx(0.1)


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
