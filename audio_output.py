"""Speaker playback for speech payloads received from the phone.

``sounddevice`` needs the system PortAudio C library. Importing it at module
scope raises ``OSError: PortAudio library not found`` on a machine that only
has the Python package, which used to kill the whole server at startup.
The import is therefore deferred until audio is actually requested, and any
failure degrades to a logged warning instead of an exception.
"""

import base64
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

    try:
        sounddevice.play(samples, samplerate=sample_rate)
        sounddevice.wait()
    except Exception as exc:  # device busy / no output device
        print(f"[Pi Audio] Playback failed: {exc}")
        return False
    return True
