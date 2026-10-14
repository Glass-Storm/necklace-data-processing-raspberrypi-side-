"""ecosys_pi: the Raspberry Pi peer for the ``ecosys.v1`` device ecosystem.

This package is the Pi-side client that discovers the phone-manager hub,
streams microphone and camera media on one **unauthenticated** bidirectional
stream, and plays the audio the hub sends back. Pairing/PIN and bearer tokens
were removed from the runtime.

The subsystems are:

* :mod:`ecosys_pi.cli` -- the runnable ``python -m ecosys_pi`` entry point and
  its frozen stdout/exit contract.
* :mod:`ecosys_pi.config` -- configuration (env > ``.env`` > ``config.json`` >
  defaults).
* :mod:`ecosys_pi.discovery` / :mod:`ecosys_pi.network` -- hub resolution
  (explicit override, ``PI_ECOSYS_HUB``, then the default gateway) and the
  ``nmcli`` hotspot join.
* :mod:`ecosys_pi.stream` -- the bidirectional-stream client.
* :mod:`ecosys_pi.audio` / :mod:`ecosys_pi.camera` -- the mic and JPEG camera
  sources plus the paced audio producer.
* :mod:`ecosys_pi.tts` -- playback of hub-synthesized speech (PCM16 @ 16 kHz).

Hardware-backed capture and playback (picamera2, PortAudio, ALSA ``aplay``) are
optional: every module imports and runs without them and falls back to
deterministic synthetic sources.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
