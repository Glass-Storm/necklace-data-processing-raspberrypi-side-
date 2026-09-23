"""Runtime configuration for the Pi node.

Every value can be overridden with an environment variable so the same code
runs on the bench and in production without edits.
"""

import os

# Bind to every interface (eth0 + wlan0). Binding a single hardcoded address
# breaks as soon as DHCP hands the Pi a different lease.
HOST = os.environ.get("PI_NODE_HOST", "0.0.0.0")
PORT = int(os.environ.get("PI_NODE_PORT", "8765"))

# Camera / JPEG settings
CAMERA_INDEX = int(os.environ.get("PI_NODE_CAMERA_INDEX", "0"))
FRAME_WIDTH = int(os.environ.get("PI_NODE_FRAME_WIDTH", "320"))
FRAME_HEIGHT = int(os.environ.get("PI_NODE_FRAME_HEIGHT", "240"))
JPEG_QUALITY = int(os.environ.get("PI_NODE_JPEG_QUALITY", "50"))

# Streaming rate
STREAM_FPS = float(os.environ.get("PI_NODE_FPS", "10"))
FRAME_INTERVAL_SECONDS = 1.0 / STREAM_FPS

# Fallback sample rate when the phone does not specify one.
DEFAULT_SAMPLE_RATE = int(os.environ.get("PI_NODE_SAMPLE_RATE", "16000"))

# Number of queued phone connections accepted by the listening socket.
LISTEN_BACKLOG = 1

# Identifier sent in the hello handshake so the app can confirm it found the
# right node, and distinguish between multiple Pis on the same network.
DEVICE_ID = os.environ.get("PI_NODE_DEVICE_ID", "necklace-01")

