"""Tests for the 16 kHz mono PCM16 audio capture path.

No microphone is required: every test uses the deterministic
:class:`~ecosys_pi.audio.SyntheticAudioSource` plus an injected clock, so the
whole suite runs on a host with no PortAudio, no ALSA and no hardware.

The synthetic bytes are pinned to ``tools/mockpeer/frames.go`` on purpose: the
frame's exact bytes make the hub's deterministic ``mock:<len>:<fnv1a64>``
transcript computable in advance, and that is what task 14 asserts against a real
hub. A hand-computed frame and its hash are recorded below.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from ecosys_pi.audio import (
    BYTES_PER_SAMPLE,
    FRAME_INTERVAL_SECONDS,
    FRAME_MS,
    SAMPLES_PER_FRAME,
    SYNTHETIC_FRAME_BYTES,
    WIRE_SAMPLE_RATE_HZ,
    AudioProducer,
    HalfDuplexGate,
    SyntheticAudioSource,
    downmix_to_mono,
    resample_factors,
    resample_to_16k,
    synthetic_audio_frame,
)

#: The frame index 0 bytes, hand-computed from the Go formula:
#: sample[i] = (0*31 + i*7) % 32767, little-endian int16.
#: first sample = 0x0000, last sample (i=319) = (2233) = 0x08b9 -> "b908".
EXPECTED_FRAME0_FIRST_SAMPLE = 0
EXPECTED_FRAME0_LAST_SAMPLE = 2233
#: FNV-1a 64-bit of the 640-byte frame 0 = "60982a2ea6b465ed", so the hub's
#: default mock recogniser answers with this exact transcript (PROTOCOL_DECISIONS b).
EXPECTED_FRAME0_TRANSCRIPT = "mock:640:60982a2ea6b465ed"


def _fnv1a64_hex(data: bytes) -> str:
    """FNV-1a 64-bit hex, matching MockSttAdapter.fnv1a64Hex (hub Kotlin)."""
    h = 0xCBF29CE484222325
    for byte in data:
        h ^= byte
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return format(h, "016x")


class FakeClock:
    """A monotonically advancing clock plus a sleep that only advances time.

    ``AudioProducer`` schedules on absolute ticks, so this deterministic clock
    lets a test run a full second of audio instantly while still proving the
    20 ms cadence.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# --- Frame shape -------------------------------------------------------------


def test_frame_is_exactly_640_bytes_and_320_samples() -> None:
    """The wire frame size is a hard contract: 320 samples * 2 bytes."""
    frame = synthetic_audio_frame(0)
    assert len(frame) == SYNTHETIC_FRAME_BYTES == 640
    assert SAMPLES_PER_FRAME == 320
    assert BYTES_PER_SAMPLE == 2
    assert FRAME_MS == 20
    assert WIRE_SAMPLE_RATE_HZ == 16_000
    assert len(frame) // BYTES_PER_SAMPLE == SAMPLES_PER_FRAME


def test_synthetic_frame_matches_the_go_mockpeer_formula() -> None:
    """Byte-for-byte the ``SyntheticAudioFrame`` formula in frames.go."""
    frame = synthetic_audio_frame(0)
    # Hand-computed first and last samples (little-endian).
    assert frame[:2] == EXPECTED_FRAME0_FIRST_SAMPLE.to_bytes(2, "little")
    assert frame[-2:] == EXPECTED_FRAME0_LAST_SAMPLE.to_bytes(2, "little")

    # The full 320 samples equal the Go formula exactly.
    expected = b"".join(
        ((0 * 31 + i * 7) % 32767).to_bytes(2, "little") for i in range(320)
    )
    assert frame == expected


def test_synthetic_frame_is_deterministic_and_index_dependent() -> None:
    """Two calls with the same index are identical; different indices differ."""
    assert synthetic_audio_frame(7) == synthetic_audio_frame(7)
    assert synthetic_audio_frame(7) != synthetic_audio_frame(8)


def test_mock_transcript_is_computable_in_advance() -> None:
    """The hub's mock transcript for frame 0 is pinned for task 14."""
    frame = synthetic_audio_frame(0)
    assert _fnv1a64_hex(frame) == "60982a2ea6b465ed"
    assert f"mock:{len(frame)}:{_fnv1a64_hex(frame)}" == EXPECTED_FRAME0_TRANSCRIPT


def test_negative_index_is_rejected() -> None:
    with pytest.raises(ValueError):
        synthetic_audio_frame(-1)


# --- Resampling / downmix ----------------------------------------------------


def test_resample_factors_are_exact_rationals() -> None:
    assert resample_factors(48_000) == (1, 3)
    assert resample_factors(44_100) == (160, 441)
    assert resample_factors(16_000) == (1, 1)


@pytest.mark.parametrize("rate", [8_000, 16_000, 44_100, 48_000])
def test_resample_output_length_is_the_expected_16k_length(rate: int) -> None:
    """A 1-second block resamples to ~16 000 samples for any plausible mic rate."""
    samples = np.zeros(rate, dtype=np.int16)
    out = resample_to_16k(samples, rate)
    expected = round(len(samples) * WIRE_SAMPLE_RATE_HZ / rate)
    assert len(out) == expected
    assert out.dtype == np.int16


def test_resample_at_wire_rate_is_identity() -> None:
    samples = np.arange(320, dtype=np.int16)
    assert np.array_equal(resample_to_16k(samples, 16_000), samples)


def test_downmix_averages_stereo_into_mono() -> None:
    stereo = np.array([100, 300, -100, 100], dtype=np.int16).reshape(-1, 2)
    mono = downmix_to_mono(stereo, 2)
    assert mono.dtype == np.int16
    assert list(mono) == [200, 0]


def test_downmix_single_channel_is_unchanged() -> None:
    mono_in = np.arange(10, dtype=np.int16)
    assert np.array_equal(downmix_to_mono(mono_in, 1), mono_in)


# --- Half-duplex gate --------------------------------------------------------


def test_gate_defaults_to_not_speaking() -> None:
    gate = HalfDuplexGate()
    assert gate.speaking is False
    gate.speaking = True
    assert gate.speaking is True
    gate.speaking = False
    assert gate.speaking is False


def test_source_yields_nothing_while_speaking() -> None:
    """The echo-loop guard: a speak window captures ZERO frames."""
    gate = HalfDuplexGate()
    source = SyntheticAudioSource(gate=gate)
    assert source.speaking is False
    assert source.open() is True

    gate.speaking = True
    assert source.read_frame() is None
    assert source.read_frame() is None
    assert source.emitted == 0  # speech consumed no frame index either

    gate.speaking = False
    first = source.read_frame()
    assert first == synthetic_audio_frame(0)  # the window was fully skipped
    assert source.emitted == 1


def test_source_owns_a_gate_when_none_is_injected() -> None:
    source = SyntheticAudioSource()
    assert isinstance(source.gate, HalfDuplexGate)
    source.speaking = True
    assert source.gate.speaking is True


# --- Producer cadence + backpressure ----------------------------------------


def test_producer_emits_exactly_the_configured_number_of_frames() -> None:
    """50 frames == 1 s of audio, all exactly 640 bytes."""
    clock = FakeClock()
    frames: list[bytes] = []

    async def collect(frame: bytes) -> None:
        frames.append(frame)

    async def scenario() -> int:
        source = SyntheticAudioSource(frame_count=50)
        producer = AudioProducer(source, collect, clock=clock.clock, sleep=clock.sleep)
        return await producer.run()

    delivered = asyncio.run(scenario())
    assert delivered == 50
    assert len(frames) == 50
    assert all(len(f) == SYNTHETIC_FRAME_BYTES for f in frames)
    # 50 * 20 ms = 1 s of audio.
    assert len(frames) * FRAME_MS == 1000


def test_producer_paces_on_the_20ms_cadence() -> None:
    """The injected clock advances one 20 ms tick per delivered frame."""
    clock = FakeClock()

    async def sink(_frame: bytes) -> None:
        return None

    async def scenario() -> int:
        source = SyntheticAudioSource(frame_count=10)
        producer = AudioProducer(source, sink, clock=clock.clock, sleep=clock.sleep)
        return await producer.run()

    asyncio.run(scenario())
    assert clock.now == pytest.approx(10 * FRAME_INTERVAL_SECONDS)
    assert all(s == pytest.approx(FRAME_INTERVAL_SECONDS) for s in clock.sleeps)


def test_producer_cadence_does_not_drift_across_a_speak_window() -> None:
    """A muted window keeps the schedule: no frames, but the ticks still pass."""
    frame_count = 5
    mute_ticks = 3
    source = SyntheticAudioSource(frame_count=frame_count)
    clock = FakeClock()
    frames: list[bytes] = []
    ticks = 0

    async def sink(frame: bytes) -> None:
        frames.append(frame)

    async def sleep(seconds: float) -> None:
        nonlocal ticks
        clock.sleeps.append(seconds)
        clock.now += seconds
        ticks += 1
        source.speaking = ticks < mute_ticks

    async def scenario() -> int:
        source.speaking = True
        producer = AudioProducer(source, sink, clock=clock.clock, sleep=sleep)
        return await producer.run()

    delivered = asyncio.run(scenario())
    assert delivered == frame_count
    assert len(frames) == frame_count
    assert ticks == mute_ticks + frame_count
    assert clock.now == pytest.approx(
        (mute_ticks + frame_count) * FRAME_INTERVAL_SECONDS
    )


def test_producer_parks_when_the_bounded_sink_is_full() -> None:
    """Backpressure: an awaited full queue stops the producer (no unbounded buffer)."""
    clock = FakeClock()
    queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)
    submitted = 0

    async def scenario() -> None:
        nonlocal submitted

        async def sink(frame: bytes) -> None:
            nonlocal submitted
            await queue.put(frame)
            submitted += 1

        source = SyntheticAudioSource()
        producer = AudioProducer(source, sink, clock=clock.clock, sleep=clock.sleep)
        runner = asyncio.create_task(producer.run())

        for _ in range(20):
            await asyncio.sleep(0)

        assert queue.full()
        assert queue.qsize() <= queue.maxsize
        assert submitted == queue.maxsize
        assert producer.delivered == queue.maxsize

        queue.get_nowait()
        for _ in range(10):
            await asyncio.sleep(0)
        assert submitted == queue.maxsize + 1

        producer.stop()
        while not runner.done():
            if not queue.empty():
                queue.get_nowait()
            await asyncio.sleep(0)
        await asyncio.wait_for(runner, timeout=5.0)

    asyncio.run(scenario())


def test_producer_stops_at_source_exhaustion() -> None:
    async def scenario() -> int:
        source = SyntheticAudioSource(frame_count=3)
        frames: list[bytes] = []

        async def sink(frame: bytes) -> None:
            frames.append(frame)

        producer = AudioProducer(source, sink)
        return await producer.run()

    assert asyncio.run(scenario()) == 3


# --- Integration with task 8's StreamClient ----------------------------------


class RecordingStreamClient:
    """Minimal stand-in for task 8's StreamClient.send."""

    def __init__(self) -> None:
        self.sent = []

    async def send(self, frame: object) -> None:
        self.sent.append(frame)


def test_pb_frame_sink_wraps_bytes_into_the_frozen_wire_message() -> None:
    """The sink emits ``StreamFrame(audio_pcm16_16k=<640 bytes>)``."""
    from ecosys_pi.audio import pb_frame_sink

    client = RecordingStreamClient()
    sink = pb_frame_sink(client)  # type: ignore[arg-type]
    frame = synthetic_audio_frame(0)

    asyncio.run(sink(frame))  # type: ignore[arg-type]

    assert len(client.sent) == 1
    sent = client.sent[0]
    assert sent.HasField("audio_pcm16_16k")
    assert sent.audio_pcm16_16k == frame
    assert len(sent.audio_pcm16_16k) == SYNTHETIC_FRAME_BYTES


# --- Selection / absent-hardware posture -------------------------------------


def test_select_audio_source_env_synthetic() -> None:
    from ecosys_pi.audio import select_audio_source

    assert isinstance(
        select_audio_source({"PI_ECOSYS_AUDIO_SOURCE": "synthetic"}),
        SyntheticAudioSource,
    )


def test_select_audio_source_falls_back_without_hardware() -> None:
    from ecosys_pi.audio import select_audio_source

    source = select_audio_source({})
    assert isinstance(source, SyntheticAudioSource)
    assert source.open() is True
    assert source.read_frame() == synthetic_audio_frame(0)


def test_sounddevice_source_open_returns_false_without_portaudio() -> None:
    """The real source reports unavailability by returning False, never raising."""
    from ecosys_pi.audio import SoundDeviceAudioSource

    source = SoundDeviceAudioSource()
    assert isinstance(source.open(), bool)
    source.close()


class _FailingStartStream:
    """A stand-in ``InputStream`` whose ``start()`` raises, recording close()."""

    instances: list["_FailingStartStream"] = []

    def __init__(self, **_kwargs: object) -> None:
        self.started = 0
        self.closed = 0
        _FailingStartStream.instances.append(self)

    def start(self) -> None:
        self.started += 1
        raise RuntimeError("simulated PortAudio start failure")

    def stop(self) -> None:
        pass

    def close(self) -> None:
        self.closed += 1


def test_sounddevice_open_closes_stream_when_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ``stream.start()`` must close the constructed stream, not leak it.

    Reproduces the leak: ``InputStream(...)`` succeeded but ``start()`` raised, so
    ``open()`` returned False while leaving the PortAudio stream open. The created
    stream's ``close()`` must be called exactly once.
    """
    import sys
    import types

    from ecosys_pi.audio import SoundDeviceAudioSource

    _FailingStartStream.instances = []
    fake = types.ModuleType("sounddevice")
    fake.InputStream = _FailingStartStream
    monkeypatch.setitem(sys.modules, "sounddevice", fake)

    source = SoundDeviceAudioSource()
    assert source.open() is False
    assert len(_FailingStartStream.instances) == 1
    stream = _FailingStartStream.instances[0]
    assert stream.started == 1
    assert stream.closed == 1, "partially-started stream leaked on failure"
    assert source._stream is None


def test_module_imports_without_sounddevice() -> None:
    """audio.py must import on a host with no PortAudio (lazy import)."""
    import ecosys_pi.audio as audio

    assert audio.WIRE_SAMPLE_RATE_HZ == 16_000
    assert audio.SYNTHETIC_FRAME_BYTES == 640
    assert audio.synthetic_audio_frame(0)[:2] == b"\x00\x00"
