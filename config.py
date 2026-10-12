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

# -----------------------------------------------------------------------
# Privacy crop
# -----------------------------------------------------------------------
# What leaves the camera node:
#   "crop" - every captured frame runs through hand detection and only a crop
#            around the detected hand is encoded and sent. A full frame is
#            never encoded or transmitted.
#   "off"  - full frames are streamed. Aiming/mounting mode only; opt-in.
# Unknown values fall back to "crop" (privacy-safe default, checked at startup).
PRIVACY_MODE = os.environ.get("PI_NODE_PRIVACY", "crop").strip().lower()

# MediaPipe hand landmarker model. The same hand_landmarker.task file the
# Android hub bundles in app/src/main/assets/, so both ends share one model.
MODEL_PATH = os.environ.get("PI_NODE_MODEL_PATH", "models/hand_landmarker.task")

# Fraction of the hand's own width/height added around the landmark bounding
# box before cropping. The hub re-detects from scratch in every frame it
# receives, and the detector needs context around the hand: too tight and
# re-detection suffers, too loose and scene context leaks back into the crop.
CROP_MARGIN = float(os.environ.get("PI_NODE_CROP_MARGIN", "0.30"))

# Frames to keep sending the last known crop after detection loses the hand.
# Landmark detection can drop the hand for a frame or two; without this grace
# period the phone's feed flickers to "no hand" and back. 4 frames ~ 0.4 s at
# the default 10 FPS.
NO_HAND_GRACE_FRAMES = int(os.environ.get("PI_NODE_NO_HAND_GRACE", "4"))

# Run detection on every Nth frame and reuse the last bounding box between
# detections. 1 = detect every frame. A fallback lever if a Pi cannot hold
# the stream target with per-frame inference.
DETECT_EVERY_N_FRAMES = int(os.environ.get("PI_NODE_DETECT_EVERY_N", "1"))

# Smallest crop side in pixels. A hand at the frame edge can produce a bbox
# sliver after clamping; below this size the crop is widened around its
# centre so the hub always receives something re-detectable.
MIN_CROP_SIDE_PX = int(os.environ.get("PI_NODE_MIN_CROP_PX", "48"))

# Fallback sample rate when the phone does not specify one.
DEFAULT_SAMPLE_RATE = int(os.environ.get("PI_NODE_SAMPLE_RATE", "16000"))

# Number of queued phone connections accepted by the listening socket.
LISTEN_BACKLOG = 1

# Identifier sent in the hello handshake so the app can confirm it found the
# right node, and distinguish between multiple Pis on the same network.
DEVICE_ID = os.environ.get("PI_NODE_DEVICE_ID", "necklace-01")
