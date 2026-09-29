"""Microphone capture for the ``ecosys.v1`` audio stream.

This module is the Pi's audio source. It captures the mic, resamples to the
frozen wire rate, downmixes to mono, and emits **exactly 640-byte (320-sample)**
little-endian PCM16 frames at a **20 ms cadence**, then feeds task 8's
:class:`~ecosys_pi.stream.StreamClient` writer.

Wire contract (``docs/PROTOCOL_DECISIONS.md``, ``docs/PROTOCOL.md`` section 6)
--------------------------------------------------------------------------------
``StreamFrame.audio_pcm16_16k`` is raw little-endian PCM16, **mono, 16 kHz**.
The canonical chunk is 20 ms = 320 samples = 640 bytes. The hub never decodes the
audio; it forwards each chunk to the configured STT engine, which answers with
exactly one ``transcript`` per chunk (``mock:<len>:<fnv1a64>`` for the default
mock engine). The frame size is therefore a hard contract, not a tuning knob.

Concrete stack
--------------
* **Capture**: ``sounddevice`` (PortAudio) when the PortAudio library is present;
  otherwise the ``arecord`` subprocess fallback. Both are OPTIONAL: the module
  imports with neither (``sounddevice`` is imported **lazily**, and importing it
  raises ``OSError`` when PortAudio is missing), and tests use the deterministic
  :class:`SyntheticAudioSource`.
* **Resample**: ``scipy.signal.resample_poly`` with explicit, named up/down
  factors (never an implicit resampler).
* **Downmix**: average the interleaved channels into mono int16.

Mic-rate assumption
-------------------
The mic is assumed to run at ``DEFAULT_MIC_RATE_HZ`` (48 kHz) unless configured
otherwise. 48 kHz is the usual ALSA/USB-mic rate on a Raspberry Pi and downsamples
to 16 kHz by an exact factor of 3 (``resample_factors(48000, 16000) == (1, 3)``,
no fractional resampling). Arbitrary rates (e.g. 44.1 kHz -> (160, 441)) are
supported and always reduced by ``gcd``, so the resampler is never left implicit.

Half-duplex interface (task 10 <-> task 11)
-------------------------------------------
While task 11's TTS is speaking, the Pi MUST NOT re-capture its own output and
send it back (an echo loop). The guard is a shared, testable flag:

* :class:`HalfDuplexGate` is the single seam both sides share. ``speaking`` is a
  plain boolean property.
* The audio source withholds frames while ``gate.speaking`` is true, and it also
  **discards** blocks captured during speech, so nothing spoken is ever queued.
* Task 11 toggles the gate around an utterance::

      gate = source.gate          # or pass your own gate at construction
      gate.speaking = True
      try:
          ... speak the transcript ...
      finally:
          gate.speaking = False

Pass the SAME :class:`HalfDuplexGate` to the source and the TTS layer; a source
built with ``gate=None`` creates its own, which is fine only when nothing else
needs to toggle it.

Backpressure
------------
The producer **awaits** the sink for every frame, so it cannot outrun the
consumer. Feeding task 8's ``StreamClient.send`` (an ``await`` on a bounded
``asyncio.Queue(64)``) parks the producer when the hub stops reading; the bounded
queue is the real backpressure valve (gRPC c-core buffers ~4 MiB before flow
control, so small 640-byte frames never block ``write()`` by themselves).
"""

from __future__ import annotations

import asyncio
import inspect
import math
import os
import shutil
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np
from scipy import signal

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from ecosys_pi.stream import StreamClient

__all__ = [
    "BYTES_PER_SAMPLE",
    "DEFAULT_MIC_RATE_HZ",
    "ENV_AUDIO_SOURCE",
    "FRAME_INTERVAL_SECONDS",
    "FRAME_MS",
    "MAX_BUFFERED_FRAMES",
    "SAMPLES_PER_FRAME",
    "SYNTHETIC_FRAME_BYTES",
    "WIRE_SAMPLE_RATE_HZ",
    "ArecordAudioSource",
    "AudioProducer",
    "AudioSource",
    "HalfDuplexGate",
    "SoundDeviceAudioSource",
    "downmix_to_mono",
    "pb_frame_sink",
    "resample_factors",
    "resample_to_16k",
    "select_audio_source",
    "synthetic_audio_frame",
]

# --- Constants ---------------------------------------------------------------

#: Frozen wire rate for ``audio_pcm16_16k``.
WIRE_SAMPLE_RATE_HZ = 16_000
#: Canonical streaming chunk length.
FRAME_MS = 20
#: 20 ms of audio at :data:`WIRE_SAMPLE_RATE_HZ`.
SAMPLES_PER_FRAME = WIRE_SAMPLE_RATE_HZ * FRAME_MS // 1000  # 320
#: PCM16 => two bytes per sample.
BYTES_PER_SAMPLE = 2
#: The exact frame size on the wire: 320 samples * 2 bytes = 640 bytes.
SYNTHETIC_FRAME_BYTES = SAMPLES_PER_FRAME * BYTES_PER_SAMPLE  # 640
#: Seconds between frames; the 20 ms cadence.
FRAME_INTERVAL_SECONDS = FRAME_MS / 1000.0  # 0.02

#: Assumed hardware mic rate for the real capture paths (ALSA/USB default).
DEFAULT_MIC_RATE_HZ = 48_000
#: Capture block size handed to the callback/reader, in samples.
CAPTURE_BLOCK_SAMPLES = 1024
#: Upper bound on frames buffered by a real source. The producer drains at the
#: same 20 ms cadence, so this only absorbs jitter; excess is dropped, never
#: allowed to grow RAM without bound.
MAX_BUFFERED_FRAMES = 32

#: Environment variable selecting the audio source: ``auto`` (default),
#: ``synthetic``, ``sounddevice``, or ``arecord``.
ENV_AUDIO_SOURCE = "PI_ECOSYS_AUDIO_SOURCE"


# --- Frame generation (pinned to tools/mockpeer/frames.go) -------------------


def synthetic_audio_frame(index: int) -> bytes:
    """Return one deterministic 640-byte PCM16 frame for ``index``.

    Byte-for-byte the same formula as ``SyntheticAudioFrame`` in the Go mockpeer
    (``tools/mockpeer/frames.go``)::

        sample = (index*31 + i*7) % 32767        # i in [0, 320)
        little-endian int16

    Every sample is in ``[0, 32766]``, below the int16 sign bit, so the value is
    identical whether written signed or unsigned. Two calls with the same index
    are byte-identical, which is what lets task 14 compute the expected
    ``mock:<len>:<fnv1a64>`` transcript in advance.
    """
    if index < 0:
        raise ValueError(f"index must be non-negative, got {index}")
    i = np.arange(SAMPLES_PER_FRAME, dtype=np.int64)
    samples = ((index * 31 + i * 7) % 32767).astype("<i2")
    return samples.tobytes()


# --- Resampling / downmix ----------------------------------------------------


def resample_factors(
    src_rate: int, dst_rate: int = WIRE_SAMPLE_RATE_HZ
) -> tuple[int, int]:
    """Return the exact ``(up, down)`` factors for ``src_rate`` -> ``dst_rate``.

    ``resample_poly`` takes ``up``/``down`` explicitly, so a rate pair is always
    written as an exact rational rather than an opaque "resample" call. Common
    Pi mics reduce cleanly::

        resample_factors(48_000) == (1, 3)     # exact /3 downsample
        resample_factors(44_100) == (160, 441) # 16 kHz / 44.1 kHz
        resample_factors(16_000) == (1, 1)     # already wire rate

    :raises ValueError: if either rate is not a positive integer.
    """
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError(f"rates must be positive, got {src_rate} -> {dst_rate}")
    divisor = math.gcd(src_rate, dst_rate)
    return dst_rate // divisor, src_rate // divisor


def downmix_to_mono(samples: np.ndarray, channels: int) -> np.ndarray:
    """Average interleaved ``channels``-channel int16 samples into mono int16.

    A single channel is returned unchanged. Averaging is done in float to avoid
    int16 overflow, then rounded and clipped back to int16.
    """
    if channels < 1:
        raise ValueError(f"channels must be >= 1, got {channels}")
    if channels == 1:
        return samples.astype(np.int16, copy=False)
    frames = samples.reshape(-1, channels)
    mono = np.round(frames.astype(np.float64).mean(axis=1))
    return np.clip(mono, -32768, 32767).astype(np.int16)


def resample_to_16k(samples: np.ndarray, src_rate: int) -> np.ndarray:
    """Resample int16 ``samples`` from ``src_rate`` to 16 kHz as int16.

    Uses ``scipy.signal.resample_poly`` with :func:`resample_factors`. When the
    source is already 16 kHz the input is returned as int16 unchanged (factor
    1/1). Output length is ``round(len(samples) * 16000 / src_rate)``.
    """
    up, down = resample_factors(src_rate, WIRE_SAMPLE_RATE_HZ)
    samples = np.asarray(samples, dtype=np.int16)
    if up == down:
        return samples
    poly = signal.resample_poly(samples.astype(np.float64), up, down)
    return np.clip(np.round(poly), -32768, 32767).astype(np.int16)


# --- Half-duplex gate --------------------------------------------------------


class HalfDuplexGate:
    """The shared ``speaking`` flag that mutes capture during TTS playback.

    Task 11 sets ``speaking = True`` for the duration of an utterance and back to
    ``False`` afterwards; the audio source reads it and withholds frames while it
    is true. Thread-safe because the capture callback runs on PortAudio's thread
    while the TTS layer toggles from the event loop.
    """

    __slots__ = ("_speaking",)

    def __init__(self) -> None:
        self._speaking = False

    @property
    def speaking(self) -> bool:
        """True while the TTS layer is playing audio (capture is muted)."""
        return self._speaking

    @speaking.setter
    def speaking(self, value: bool) -> None:
        self._speaking = bool(value)

    def __repr__(self) -> str:
        return f"HalfDuplexGate(speaking={self._speaking})"


# --- The audio-source contract ----------------------------------------------


@runtime_checkable
class AudioSource(Protocol):
    """A pull-based source of 640-byte PCM16 16 kHz mono frames.

    Mirrors the camera source posture: ``open()`` reports availability by
    returning ``False`` instead of raising, and ``close()`` is idempotent.
    ``read_frame()`` returns exactly one frame or ``None`` when none is ready --
    including while :attr:`speaking` is true (the half-duplex guard).
    """

    @property
    def gate(self) -> HalfDuplexGate:
        """The shared half-duplex gate (task 11 toggles this)."""
        ...

    @property
    def speaking(self) -> bool:
        """Convenience view of ``gate.speaking``; assignable for the TTS layer."""
        ...

    @property
    def done(self) -> bool:
        """True once no further frame can ever be produced."""
        ...

    def open(self) -> bool:
        """Acquire the hardware/source. Returns ``False`` when unavailable."""
        ...

    def read_frame(self) -> bytes | None:
        """Return the next 640-byte PCM16 frame, or ``None`` if not ready."""
        ...

    def close(self) -> None:
        """Release the source. Safe to call more than once."""
        ...


class _PcmFrameBuffer:
    """A thread-safe byte buffer that hands out fixed 640-byte frames.

    The capture callback (PortAudio thread / arecord reader thread) calls
    :meth:`extend`; the producer calls :meth:`take`. The buffer is capped at
    :data:`MAX_BUFFERED_FRAMES` frames: if a stalled consumer lets it fill, the
    OLDEST bytes are dropped and counted, so RAM can never grow without bound.
    """

    __slots__ = ("_lock", "_buf", "_max_bytes", "_dropped_frames")

    def __init__(self, max_frames: int = MAX_BUFFERED_FRAMES) -> None:
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._max_bytes = max_frames * SYNTHETIC_FRAME_BYTES
        self._dropped_frames = 0

    def extend(self, pcm: bytes) -> None:
        """Append captured PCM bytes, dropping the oldest whole frames if full."""
        if not pcm:
            return
        with self._lock:
            self._buf.extend(pcm)
            excess = len(self._buf) - self._max_bytes
            if excess >= SYNTHETIC_FRAME_BYTES:
                whole = (excess // SYNTHETIC_FRAME_BYTES) * SYNTHETIC_FRAME_BYTES
                del self._buf[:whole]
                self._dropped_frames += whole // SYNTHETIC_FRAME_BYTES

    def take(self) -> bytes | None:
        """Return exactly one frame's bytes, or ``None`` when fewer are buffered."""
        with self._lock:
            if len(self._buf) >= SYNTHETIC_FRAME_BYTES:
                frame = bytes(self._buf[:SYNTHETIC_FRAME_BYTES])
                del self._buf[:SYNTHETIC_FRAME_BYTES]
                return frame
        return None

    @property
    def dropped_frames(self) -> int:
        """Whole frames dropped because the consumer fell behind."""
        with self._lock:
            return self._dropped_frames


# --- Synthetic source (deterministic, mirrors tools/mockpeer/frames.go) -------


class SyntheticAudioSource:
    """A mic-free :class:`AudioSource` producing deterministic frames.

    Used by tests and when no capture device is available. It is a pure function
    of a frame counter, so downstream ``mock:<len>:<hash>`` assertions are
    reproducible on any host. While ``speaking`` is true :meth:`read_frame`
    yields nothing AND the counter does not advance, so a speak window emits zero
    frames and speech never consumes an index.
    """

    def __init__(
        self,
        frame_count: int | None = None,
        *,
        gate: HalfDuplexGate | None = None,
    ) -> None:
        """:param frame_count: stop after this many frames; ``None`` is infinite."""
        if frame_count is not None and frame_count < 0:
            raise ValueError(f"frame_count must be non-negative, got {frame_count}")
        self._frame_count = frame_count
        self._gate = gate if gate is not None else HalfDuplexGate()
        self._index = 0
        self._open = False

    @property
    def gate(self) -> HalfDuplexGate:
        return self._gate

    @property
    def speaking(self) -> bool:
        return self._gate.speaking

    @speaking.setter
    def speaking(self, value: bool) -> None:
        self._gate.speaking = value

    @property
    def done(self) -> bool:
        return self._frame_count is not None and self._index >= self._frame_count

    def open(self) -> bool:
        """Always succeeds; there is no hardware to fail. Resets the counter."""
        self._open = True
        self._index = 0
        return True

    def read_frame(self) -> bytes | None:
        """Return the next deterministic frame, or ``None`` (speaking/exhausted)."""
        if not self._open or self._gate.speaking:
            return None
        if self._frame_count is not None and self._index >= self._frame_count:
            return None
        frame = synthetic_audio_frame(self._index)
        self._index += 1
        return frame

    def close(self) -> None:
        """Idempotent no-op."""
        self._open = False

    @property
    def emitted(self) -> int:
        """Frames handed out since :meth:`open`."""
        return self._index


# --- Real capture paths (lazy imports; optional hardware) --------------------


def sounddevice_available() -> bool:
    """True when ``sounddevice`` imports (i.e. PortAudio is present)."""
    try:
        import sounddevice  # noqa: F401  (lazy: import may raise OSError)
    except (ImportError, OSError):
        return False
    return True


class SoundDeviceAudioSource:
    """Real capture via ``sounddevice``/PortAudio at the mic's native rate.

    ``sounddevice`` is imported lazily inside :meth:`open` (importing it raises
    ``OSError`` when PortAudio is absent), so this module imports on any host.
    Each capture block is downmixed to mono, resampled to 16 kHz, and buffered;
    :meth:`read_frame` pops exact 640-byte frames. Blocks captured while
    ``speaking`` are discarded outright.
    """

    def __init__(
        self,
        *,
        mic_rate: int = DEFAULT_MIC_RATE_HZ,
        channels: int = 1,
        device: int | str | None = None,
        block_samples: int = CAPTURE_BLOCK_SAMPLES,
        gate: HalfDuplexGate | None = None,
        max_frames: int = MAX_BUFFERED_FRAMES,
    ) -> None:
        self._mic_rate = mic_rate
        self._channels = channels
        self._device = device
        self._block_samples = block_samples
        self._gate = gate if gate is not None else HalfDuplexGate()
        self._buffer = _PcmFrameBuffer(max_frames)
        self._stream: object | None = None
        self._closed = False

    @property
    def gate(self) -> HalfDuplexGate:
        return self._gate

    @property
    def speaking(self) -> bool:
        return self._gate.speaking

    @speaking.setter
    def speaking(self, value: bool) -> None:
        self._gate.speaking = value

    @property
    def done(self) -> bool:
        return self._closed

    def open(self) -> bool:
        """Start the input stream. ``False`` when PortAudio/hardware is absent."""
        try:
            import sounddevice as sd
        except (ImportError, OSError) as exc:
            print(f"[Pi Audio] sounddevice/PortAudio unavailable: {exc}")
            return False
        try:
            stream = sd.InputStream(
                samplerate=self._mic_rate,
                channels=self._channels,
                dtype="int16",
                blocksize=self._block_samples,
                device=self._device,
                callback=self._on_block,
            )
            try:
                stream.start()
            except Exception:
                # A stream that was constructed but failed to start must not be
                # leaked; tear it down before reporting unavailable. Mirror the
                # camera's open()-failure cleanup and never let teardown raise.
                try:
                    stream.close()
                except Exception as close_exc:  # noqa: BLE001
                    print(
                        f"[Pi Audio] error while discarding the mic stream: {close_exc}"
                    )
                raise
        except Exception as exc:  # noqa: BLE001 - hardware failure => keep running
            print(f"[Pi Audio] could not open the microphone: {exc}")
            return False
        self._stream = stream
        print(f"[Pi Audio] opened mic @ {self._mic_rate} Hz ({self._channels} ch)")
        return True

    def _on_block(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        """PortAudio callback: downmix+resample one block into the frame buffer."""
        if status:  # pragma: no cover - device-dependent
            print(f"[Pi Audio] capture status: {status}")
        if self._gate.speaking:
            return  # half-duplex: never buffer our own TTS output
        mono = downmix_to_mono(
            np.asarray(indata, dtype=np.int16).reshape(-1), self._channels
        )
        self._buffer.extend(resample_to_16k(mono, self._mic_rate).tobytes())

    def read_frame(self) -> bytes | None:
        """Return one 640-byte frame, or ``None`` (speaking / not enough bytes)."""
        if self._gate.speaking:
            return None
        return self._buffer.take()

    def close(self) -> None:
        """Stop and release the stream; safe to call more than once."""
        stream = self._stream
        self._stream = None
        self._closed = True
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:  # noqa: BLE001 - teardown must never raise
                print(f"[Pi Audio] error while closing the microphone: {exc}")


class ArecordAudioSource:
    """Fallback capture via an ``arecord`` subprocess (ALSA ``alsa-utils``).

    Reads raw ``S16_LE`` blocks from ``arecord -t raw`` on a background thread,
    downmixes/resamples exactly like :class:`SoundDeviceAudioSource`. Used when
    PortAudio is unavailable but ALSA is.
    """

    def __init__(
        self,
        *,
        mic_rate: int = DEFAULT_MIC_RATE_HZ,
        channels: int = 1,
        device: str | None = None,
        block_bytes: int = CAPTURE_BLOCK_SAMPLES * BYTES_PER_SAMPLE,
        gate: HalfDuplexGate | None = None,
        max_frames: int = MAX_BUFFERED_FRAMES,
    ) -> None:
        self._mic_rate = mic_rate
        self._channels = channels
        self._device = device
        self._block_bytes = block_bytes
        self._gate = gate if gate is not None else HalfDuplexGate()
        self._buffer = _PcmFrameBuffer(max_frames)
        self._proc: object | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def gate(self) -> HalfDuplexGate:
        return self._gate

    @property
    def speaking(self) -> bool:
        return self._gate.speaking

    @speaking.setter
    def speaking(self, value: bool) -> None:
        self._gate.speaking = value

    @property
    def done(self) -> bool:
        return self._closed

    def _command(self) -> list[str] | None:
        exe = shutil.which("arecord")
        if exe is None:
            return None
        cmd = [
            exe,
            "-q",
            "-t",
            "raw",
            "-f",
            "S16_LE",
            "-r",
            str(self._mic_rate),
            "-c",
            str(self._channels),
        ]
        if self._device:
            cmd += ["-D", self._device]
        cmd.append("-")
        return cmd

    def open(self) -> bool:
        """Spawn ``arecord``. ``False`` when the binary or device is unavailable."""
        import subprocess

        cmd = self._command()
        if cmd is None:
            print("[Pi Audio] arecord not found (install alsa-utils)")
            return False
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except OSError as exc:
            print(f"[Pi Audio] could not start arecord: {exc}")
            return False
        self._proc = proc
        self._thread = threading.Thread(
            target=self._reader_loop, args=(proc,), name="ecosys-arecord", daemon=True
        )
        self._thread.start()
        print(f"[Pi Audio] opened arecord @ {self._mic_rate} Hz ({self._channels} ch)")
        return True

    def _reader_loop(self, proc) -> None:  # noqa: ANN001
        """Read raw PCM blocks and buffer resampled frames until EOF."""
        stdout = proc.stdout
        if stdout is None:  # pragma: no cover - Popen always sets it here
            return
        while True:
            block = stdout.read(self._block_bytes)
            if not block:
                return
            if self._gate.speaking:
                continue
            mono = downmix_to_mono(np.frombuffer(block, dtype="<i2"), self._channels)
            self._buffer.extend(resample_to_16k(mono, self._mic_rate).tobytes())

    def read_frame(self) -> bytes | None:
        """Return one 640-byte frame, or ``None`` (speaking / not enough bytes)."""
        if self._gate.speaking:
            return None
        return self._buffer.take()

    def close(self) -> None:
        """Terminate ``arecord`` and join its reader thread; idempotent."""
        proc = self._proc
        thread = self._thread
        self._proc = None
        self._thread = None
        self._closed = True
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception as exc:  # noqa: BLE001 - teardown must never raise
                print(f"[Pi Audio] error while stopping arecord: {exc}")
        if thread is not None:
            thread.join(timeout=2)


# --- Selection by availability + env ----------------------------------------


def select_audio_source(
    env: Mapping[str, str] | None = None,
    *,
    gate: HalfDuplexGate | None = None,
) -> AudioSource:
    """Choose an :class:`AudioSource` from availability and ``PI_ECOSYS_AUDIO_SOURCE``.

    Values (case-insensitive):

    * ``synthetic`` -- the deterministic mic-free source.
    * ``sounddevice`` -- the PortAudio source when available, else fall back.
    * ``arecord`` -- the ALSA subprocess source when available, else fall back.
    * ``auto`` (default) or anything else -- sounddevice, then arecord, then
      synthetic.

    Falls back to synthetic so the client always runs, even with no hardware.
    """
    source_env = os.environ if env is None else env
    choice = source_env.get(ENV_AUDIO_SOURCE, "auto").strip().lower()

    if choice == "synthetic":
        return SyntheticAudioSource(gate=gate)
    if choice == "sounddevice":
        source: AudioSource = SoundDeviceAudioSource(gate=gate)
        return source if sounddevice_available() else _fallback_gate(gate)
    if choice == "arecord":
        source = ArecordAudioSource(gate=gate)
        return source if shutil.which("arecord") else _fallback_gate(gate)
    if sounddevice_available():
        return SoundDeviceAudioSource(gate=gate)
    if shutil.which("arecord"):
        return ArecordAudioSource(gate=gate)
    return _fallback_gate(gate)


def _fallback_gate(gate: HalfDuplexGate | None) -> SyntheticAudioSource:
    print("[Pi Audio] no microphone backend available; using the synthetic source")
    return SyntheticAudioSource(gate=gate)


# --- Producer ----------------------------------------------------------------

Clock = Callable[[], float]
Sink = Callable[[bytes], Awaitable[None] | None]
Sleep = Callable[[float], Awaitable[None]]


class AudioProducer:
    """Paces a source into a sink at the fixed 20 ms frame cadence.

    For every scheduled tick the producer reads one frame and **awaits the
    sink**. Awaiting is the backpressure mechanism: when the sink is task 8's
    :meth:`~ecosys_pi.stream.StreamClient.send` (an ``await`` on a bounded
    ``asyncio.Queue(64)``), a stalled hub parks this producer instead of letting
    it buffer frames in memory.

    The cadence is derived from an **injected clock** and sleep function, so tests
    drive it deterministically. Ticks are absolute (``started + tick*interval``),
    so the cadence does not drift and a slow tick does not accumulate lag. While
    the source is ``speaking`` a tick produces no frame but the clock keeps
    running -- the mic cadence is preserved across a TTS window.
    """

    def __init__(
        self,
        source: AudioSource,
        sink: Sink,
        *,
        frame_interval: float = FRAME_INTERVAL_SECONDS,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if frame_interval < 0:
            raise ValueError(f"frame_interval must be >= 0, got {frame_interval}")
        self._source = source
        self._sink = sink
        self._interval = frame_interval
        self._clock = clock
        self._sleep = sleep
        self._stop = False
        self._delivered = 0

    @property
    def delivered(self) -> int:
        """Frames successfully awaited into the sink."""
        return self._delivered

    def stop(self) -> None:
        """Ask :meth:`run` to return after the current frame is delivered."""
        self._stop = True

    async def run(self, max_frames: int | None = None) -> int:
        """Open the source and pump frames; return the number delivered.

        :param max_frames: stop after this many delivered frames; ``None`` runs
            until :meth:`stop` or source exhaustion.
        """
        if max_frames is not None and max_frames < 0:
            raise ValueError(f"max_frames must be non-negative, got {max_frames}")
        if not self._source.open():
            print("[Pi Audio] audio source unavailable; no frames will be produced")
            return 0

        started = self._clock()
        tick = 0
        try:
            while not self._stop:
                if max_frames is not None and self._delivered >= max_frames:
                    break
                frame = self._source.read_frame()
                if frame is None:
                    if self._source.done:
                        break
                else:
                    await _await_maybe(self._sink, frame)
                    self._delivered += 1
                tick += 1
                delay = (started + tick * self._interval) - self._clock()
                if delay > 0:
                    await self._sleep(delay)
            return self._delivered
        finally:
            self._source.close()


async def _await_maybe(fn: Sink, frame: bytes) -> None:
    """Call ``fn(frame)``, awaiting the result when it is awaitable."""
    result = fn(frame)
    if inspect.isawaitable(result):
        await result


def pb_frame_sink(client: StreamClient) -> Sink:
    """Wrap a task-8 :class:`StreamClient` into an awaitable byte sink.

    The returned sink turns each 640-byte PCM frame into the frozen wire message
    ``StreamFrame(audio_pcm16_16k=frame)`` and awaits ``client.send`` -- the
    bounded-queue backpressure point.
    """

    async def _sink(frame: bytes) -> None:
        from ecosys.v1 import ecosys_pb2  # lazy: keep this module proto-free

        await client.send(ecosys_pb2.StreamFrame(audio_pcm16_16k=frame))

    return _sink
