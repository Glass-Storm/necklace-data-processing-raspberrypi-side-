"""Speaker playback for speech payloads received from the phone.

``sounddevice`` needs the system PortAudio C library. Importing it at module
scope raises ``OSError: PortAudio library not found`` on a machine that only
has the Python package, which used to kill the whole server at startup.
The import is therefore deferred until audio is actually requested, and any
failure degrades to a logged warning instead of an exception.
"""

import base64
import os
import threading

import numpy as np

from config import DEFAULT_SAMPLE_RATE

_sounddevice = None
_load_failure: str | None = None
_load_lock = threading.Lock()


def _load_sounddevice():
    """Import ``sounddevice`` once, remembering failure instead of raising."""
    global _sounddevice, _load_failure
    if _sounddevice is not None or _load_failure is not None:
        return _sounddevice

    with _load_lock:
        if _sounddevice is not None or _load_failure is not None:
            return _sounddevice
        try:
            import sounddevice
        except (ImportError, OSError) as exc:
            _load_failure = str(exc)
            print(f"[Pi Audio] Playback disabled: {exc}")
            print("[Pi Audio] Install it with: sudo apt install libportaudio2")
        else:
            _sounddevice = sounddevice
            print("[Pi Audio] PortAudio loaded, speaker playback ready.")
    return _sounddevice


def is_playback_available() -> bool:
    """True when audio samples can actually be played."""
    return _load_sounddevice() is not None


def playback_unavailable_reason() -> str | None:
    """Human readable reason playback is off, or None when it works."""
    _load_sounddevice()
    return _load_failure


def _decode_pcm(audio_b64: str) -> np.ndarray:
    """Decode base64 int16 little-endian PCM into a numpy array."""
    pcm_bytes = base64.b64decode(audio_b64)
    return np.frombuffer(pcm_bytes, dtype=np.int16)

def describe_samples(samples: np.ndarray, sample_rate: int) -> str:
    """Summarise a sample buffer for logging.

    Reports duration and peak amplitude. The peak is the useful part: a peak of
    zero means the payload decoded but contained only silence, which looks
    identical to working playback with no speaker attached.
    """
    if samples.size == 0:
        return "empty buffer"

    duration = samples.size / sample_rate
    peak = int(np.abs(samples).max())
    rms = int(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    return f"{samples.size} samples, {duration:.2f}s @ {sample_rate} Hz, peak {peak}, rms {rms}"


def dump_to_wav(path: str, samples: np.ndarray, sample_rate: int) -> bool:
    """Write mono int16 samples to a WAV file for offline inspection.

    Used when no speaker is attached: the received audio is captured here and
    played on another machine, which verifies the whole chain from the phone's
    synthesis through to the decoded PCM without audio hardware.
    """
    import wave

    try:
        with wave.open(path, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)  # 16-bit samples
            wav.setframerate(sample_rate)
            wav.writeframes(samples.astype(np.int16).tobytes())
    except (OSError, ValueError, wave.Error) as exc:
        print(f"[Pi Audio] Could not write dump file: {exc}")
        return False

    print(f"[Pi Audio] Wrote {samples.size} samples to {path}")
    return True


def play_speech_audio(audio_b64: str, sample_rate: int = DEFAULT_SAMPLE_RATE) -> bool:
    """Play base64 PCM through the 3.5mm jack. Returns False if unavailable."""
    sounddevice = _load_sounddevice()
    if sounddevice is None:
        return False

    try:
        samples = _decode_pcm(audio_b64)
    except (ValueError, TypeError) as exc:
        print(f"[Pi Audio] Could not decode audio payload: {exc}")
        return False

    if samples.size == 0:
        return False

    # Log what arrived before playing. This is the only way to distinguish
    # "played correctly" from "played silence" with no speaker attached,
    # since PortAudio reports success either way.
    print(f"[Pi Audio] Received {describe_samples(samples, sample_rate)}")

    # Optional: capture the audio to a file instead of relying on a speaker.
    # Enable with PI_NODE_AUDIO_DUMP=/tmp/out.wav
    dump_path = os.environ.get("PI_NODE_AUDIO_DUMP")
    if dump_path:
        dump_to_wav(dump_path, samples, sample_rate)

    try:
        sounddevice.play(samples, samplerate=sample_rate)
        sounddevice.wait()
    except Exception as exc:  # device busy / no output device
        print(f"[Pi Audio] Playback failed: {exc}")
        return False
    return True


