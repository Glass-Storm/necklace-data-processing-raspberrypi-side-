"""ecosys_pi: the Raspberry Pi peer for the ``ecosys.v1`` device ecosystem.

This package is the Pi-side client that pairs with the phone-manager hub over
gRPC, holds an authenticated heartbeat, and streams microphone and camera media
on one bidirectional stream while playing returned transcripts back locally.

The subsystems are:

* :mod:`ecosys_pi.cli` -- the runnable ``python -m ecosys_pi`` entry point and
  its frozen stdout/exit contract.
* :mod:`ecosys_pi.config` / :mod:`ecosys_pi.token_store` -- configuration and the
  ``0600`` token cache.
* :mod:`ecosys_pi.discovery` -- hub resolution (explicit override, ``PI_ECOSYS_HUB``
  env var, then bounded mDNS).
* :mod:`ecosys_pi.pairing` / :mod:`ecosys_pi.heartbeat` / :mod:`ecosys_pi.stream`
  -- the pairing, heartbeat, and bidirectional-stream clients.
* :mod:`ecosys_pi.audio` / :mod:`ecosys_pi.camera` -- the mic and H.264 camera
  sources plus the paced audio producer.
* :mod:`ecosys_pi.tts` -- language-selected local text-to-speech playback.

Hardware-backed capture and playback (picamera2, PortAudio, espeak-ng, piper)
are optional: every module imports and runs without them and falls back to
deterministic synthetic sources.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
