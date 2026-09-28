"""ecosys_pi: the Raspberry Pi peer for the ``ecosys.v1`` device ecosystem.

This package is the Pi-side client that pairs with the phone-manager hub over
gRPC, streams camera and microphone media, and plays back returned audio. It is
a placeholder skeleton for now; the pairing, discovery, streaming, and camera
subsystems land in later workstreams.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
