"""Tests for the H.264 camera source and its Annex-B NAL splitter.

The splitter is PURE byte logic, so it is fully testable WITHOUT a camera. The
real ``Picamera2VideoSource`` cannot start on this host (no camera, no
``picamera2``); that is expected and is NOT a failure. Its lazy-import and
unavailable-return posture IS tested here.
"""

from __future__ import annotations

import sys

import pytest

from ecosys_pi import camera

# --- A fixed Annex-B sample: two 4-byte start codes + one 3-byte start code ---
_SAMPLE = (
    b"\x00\x00\x00\x01\x67\x42\x00\x1e"  # SPS, 4-byte start code
    b"\x00\x00\x00\x01\x68\xce\x38\x80"  # PPS, 4-byte start code
    b"\x00\x00\x01\x65\x88\x84"  # IDR slice, 3-byte start code
)
_SAMPLE_EXPECTED = [
    b"\x00\x00\x00\x01\x67\x42\x00\x1e",
    b"\x00\x00\x00\x01\x68\xce\x38\x80",
    b"\x00\x00\x01\x65\x88\x84",
]


def test_split_annex_b_fixed_sample_returns_exact_nals() -> None:
    """A fixed mixed-start-code sample splits into the exact expected NAL list."""
    assert camera.split_annex_b(_SAMPLE) == _SAMPLE_EXPECTED


def test_split_annex_b_nals_are_contiguous_slices_of_input() -> None:
    """Each unit keeps its original start code; joining them is lossless here."""
    assert b"".join(camera.split_annex_b(_SAMPLE)) == _SAMPLE


def test_split_annex_b_four_byte_code_wins_over_three_byte() -> None:
    """At ``00 00 00 01`` the extra zero stays with the NAL it introduces."""
    assert camera.split_annex_b(b"\x00\x00\x00\x01\x65") == [b"\x00\x00\x00\x01\x65"]


def test_split_annex_b_three_byte_start_code_only() -> None:
    assert camera.split_annex_b(b"\x00\x00\x01\x41") == [b"\x00\x00\x01\x41"]


def test_split_annex_b_discards_leading_junk() -> None:
    assert camera.split_annex_b(b"\xff\xff\x00\x00\x00\x01\x65") == [
        b"\x00\x00\x00\x01\x65"
    ]


def test_split_annex_b_trailing_start_code_emits_no_unit() -> None:
    """A trailing start code has no payload, so it produces no NAL."""
    assert camera.split_annex_b(b"\x00\x00\x00\x01\x65\x00\x00\x00\x01") == [
        b"\x00\x00\x00\x01\x65"
    ]


@pytest.mark.parametrize("data", [b"", b"\x00", b"\x00\x00", b"\x00\x00\x01"])
def test_split_annex_b_no_payload_is_empty(data: bytes) -> None:
    assert camera.split_annex_b(data) == []


def test_split_annex_b_every_emitted_unit_is_non_empty() -> None:
    units = camera.split_annex_b(_SAMPLE)
    assert units, "the fixed sample must produce units"
    assert all(unit for unit in units), f"empty NAL emitted: {units!r}"


# --- Synthetic source: deterministic, mockpeer-shaped, under budget ----------


def test_synthetic_video_nal_is_deterministic() -> None:
    first = camera.synthetic_video_nal(7)
    second = camera.synthetic_video_nal(7)
    assert first == second
    assert first != camera.synthetic_video_nal(8)


def test_synthetic_video_nal_matches_mockpeer_formula() -> None:
    """Shape per phone-manager ``tools/mockpeer/frames.go::SyntheticVideoNAL``."""
    nal = camera.synthetic_video_nal(3)
    assert nal[:5] == b"\x00\x00\x00\x01\x65"
    body = nal[5:]
    assert len(body) == 24
    assert body == bytes((3 * 17 + i) % 251 for i in range(24))


def test_synthetic_video_nal_rejects_negative_index() -> None:
    with pytest.raises(ValueError):
        camera.synthetic_video_nal(-1)


def test_synthetic_nal_is_one_unit_and_under_budget() -> None:
    """A synthetic NAL is a single unit and well under the documented cap."""
    nal = camera.synthetic_video_nal(11)
    assert camera.split_annex_b(nal) == [nal]
    assert len(nal) < camera.MAX_NAL_BYTES


def test_frame_size_budget_has_a_four_x_margin_under_the_hub_limit() -> None:
    assert camera.MAX_NAL_BYTES * 4 == camera.HUB_INBOUND_LIMIT_BYTES
    assert camera.DEFAULT_WIDTH == 640
    assert camera.DEFAULT_HEIGHT == 480


def test_synthetic_source_yields_n_deterministic_frames() -> None:
    source = camera.SyntheticVideoSource(frame_count=3)
    assert source.open() is True
    frames = [source.read_nal() for _ in range(5)]
    assert frames == [
        camera.synthetic_video_nal(0),
        camera.synthetic_video_nal(1),
        camera.synthetic_video_nal(2),
        None,
        None,
    ]


def test_synthetic_source_before_open_returns_none() -> None:
    source = camera.SyntheticVideoSource()
    assert source.read_nal() is None


def test_synthetic_source_close_is_idempotent_and_stops_frames() -> None:
    source = camera.SyntheticVideoSource()
    source.open()
    source.close()
    source.close()
    assert source.read_nal() is None


def test_synthetic_source_satisfies_video_source_protocol() -> None:
    assert isinstance(camera.SyntheticVideoSource(), camera.VideoSource)


def test_synthetic_source_done_false_while_frames_remain() -> None:
    source = camera.SyntheticVideoSource(frame_count=2)
    source.open()
    assert source.done is False
    source.read_nal()
    assert source.done is False
    source.read_nal()
    assert source.done is True


def test_synthetic_source_unbounded_is_never_done() -> None:
    source = camera.SyntheticVideoSource()
    source.open()
    for _ in range(10):
        assert source.read_nal() is not None
        assert source.done is False


def test_synthetic_source_with_cap_is_done_before_open_and_after_close() -> None:
    source = camera.SyntheticVideoSource(frame_count=1)
    assert source.done is False  # cap not reached yet, even though closed
    source.open()
    source.read_nal()
    assert source.done is True
    source.close()
    assert source.done is True


def test_picamera2_source_is_never_done() -> None:
    assert camera.Picamera2VideoSource().done is False


def test_new_source_starts_closed_and_reopens() -> None:
    source = camera.SyntheticVideoSource(frame_count=1)
    assert source.read_nal() is None
    assert source.open() is True
    assert source.read_nal() == camera.synthetic_video_nal(0)
    assert source.open() is True  # reopen resets the counter
    assert source.read_nal() == camera.synthetic_video_nal(0)


# --- Selection by availability + env ----------------------------------------


def test_select_synthetic_when_picamera2_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(camera, "_picamera2_available", lambda: False)
    assert isinstance(camera.select_video_source(env={}), camera.SyntheticVideoSource)


def test_select_synthetic_when_env_forces_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(camera, "_picamera2_available", lambda: True)
    source = camera.select_video_source(env={"PI_ECOSYS_VIDEO_SOURCE": "synthetic"})
    assert isinstance(source, camera.SyntheticVideoSource)


def test_select_picamera2_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(camera, "_picamera2_available", lambda: True)
    source = camera.select_video_source(env={})
    assert isinstance(source, camera.Picamera2VideoSource)


def test_select_picamera2_falls_back_to_synthetic_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(camera, "_picamera2_available", lambda: False)
    source = camera.select_video_source(env={"PI_ECOSYS_VIDEO_SOURCE": "picamera2"})
    assert isinstance(source, camera.SyntheticVideoSource)


# --- picamera2 is optional and imported lazily -------------------------------


def test_module_imports_without_picamera2() -> None:
    """Importing the camera module must not import the optional extra eagerly."""
    assert "picamera2" not in sys.modules, "picamera2 imported at module import time"


def test_picamera2_source_open_returns_false_without_package() -> None:
    """With picamera2 absent, open() reports unavailable instead of raising."""
    if camera._picamera2_available():  # pragma: no cover - Pi-only host
        pytest.skip("picamera2 present on this host; absence path not exercisable")
    source = camera.Picamera2VideoSource()
    assert source.open() is False
    assert source.read_nal() is None


def test_picamera2_source_close_is_idempotent_without_open() -> None:
    source = camera.Picamera2VideoSource()
    source.close()
    source.close()


class _FakePicamera2:
    """Stands in for ``picamera2.Picamera2``; records teardown calls."""

    instances: list["_FakePicamera2"] = []

    def __init__(self) -> None:
        self.closed = 0
        self.stop_recording_calls = 0
        _FakePicamera2.instances.append(self)

    def close(self) -> None:
        self.closed += 1

    def stop_recording(self) -> None:
        self.stop_recording_calls += 1


def _install_fake_picamera2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``from picamera2 import Picamera2`` resolve without hardware."""
    import types

    _FakePicamera2.instances = []
    picamera2 = types.ModuleType("picamera2")
    picamera2.Picamera2 = _FakePicamera2
    encoders = types.ModuleType("picamera2.encoders")
    encoders.H264Encoder = object
    picamera2.encoders = encoders
    monkeypatch.setitem(sys.modules, "picamera2", picamera2)
    monkeypatch.setitem(sys.modules, "picamera2.encoders", encoders)


def test_picamera2_start_failure_closes_the_constructed_camera(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pipeline-start failure must tear down the already-constructed camera.

    Reproduces bug M1 WITHOUT hardware: ``Picamera2()`` succeeds, then the
    pipeline start raises, so ``open()`` must return ``False`` AND close the
    camera it created (previously the handle leaked because ``_picam2`` was
    assigned only after success).
    """
    _install_fake_picamera2(monkeypatch)
    source = camera.Picamera2VideoSource()

    def _boom(_picam2: object, _encoder_cls: object) -> None:
        raise RuntimeError("simulated ISP failure")

    monkeypatch.setattr(source, "_start_pipeline", _boom)

    assert source.open() is False
    assert len(_FakePicamera2.instances) == 1, "Picamera2 must have been constructed"
    assert _FakePicamera2.instances[0].closed == 1, "camera handle leaked on failure"
    assert source.read_nal() is None


def test_picamera2_success_path_keeps_the_camera_open_and_starts_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The success path is unchanged: open() True and close() tears down once."""
    _install_fake_picamera2(monkeypatch)
    source = camera.Picamera2VideoSource()
    started: list[object] = []
    monkeypatch.setattr(
        source, "_start_pipeline", lambda picam2, _cls: started.append(picam2)
    )

    assert source.open() is True
    assert started == _FakePicamera2.instances
    assert _FakePicamera2.instances[0].closed == 0

    source.close()
    assert _FakePicamera2.instances[0].stop_recording_calls == 1
    assert _FakePicamera2.instances[0].closed == 1
