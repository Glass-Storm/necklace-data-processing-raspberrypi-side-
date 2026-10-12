"""Webcam capture, privacy crop and JPEG encoding for the phone stream.

In the default privacy mode (``PI_NODE_PRIVACY=crop``) every captured frame
first runs through the hand detector and only a crop around the detected hand
is encoded and returned — the full frame never leaves this class. When no
hand is present the caller receives a no-hand marker instead of an image, so
nothing observable about the scene is transmitted at all.

``PI_NODE_PRIVACY=off`` disables the crop and streams full frames. That mode
exists only for aiming the camera during mounting and must never be the
default.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import cv2

from config import (
    CAMERA_INDEX,
    CROP_MARGIN,
    DETECT_EVERY_N_FRAMES,
    FRAME_HEIGHT,
    FRAME_WIDTH,
    JPEG_QUALITY,
    NO_HAND_GRACE_FRAMES,
)
from hand_detector import BoundingBox, HandDetector

VIDEO_MESSAGE_TYPE = "video_frame"
NO_HAND_MESSAGE_TYPE = "no_hand"

@dataclass
class CapturedFrame:
    """One stream unit: either an encoded crop or a no-hand marker.

    Attributes:
        message_type: VIDEO_MESSAGE_TYPE or NO_HAND_MESSAGE_TYPE.
        image: base64 JPEG for a video frame, None for a no-hand marker.
    """

    message_type: str
    image: str | None = None


class VideoStream:
    """Wraps a single OpenCV capture device and the privacy crop."""

    def __init__(
        self,
        detector: HandDetector | None = None,
        camera_index: int = CAMERA_INDEX,
        width: int = FRAME_WIDTH,
        height: int = FRAME_HEIGHT,
        jpeg_quality: int = JPEG_QUALITY,
        grace_frames: int = NO_HAND_GRACE_FRAMES,
        detect_every_n: int = DETECT_EVERY_N_FRAMES,
    ) -> None:
        self._detector = detector
        self._camera_index = camera_index
        self._width = width
        self._height = height
        self._jpeg_quality = jpeg_quality
        self._grace_frames = grace_frames
        self._detect_every_n = max(1, detect_every_n)
        self._capture = None

        # Detection state. The last box is reused across (a) frames where
        # detection is skipped for throughput and (b) the grace period after
        # the hand is lost, so the phone's feed never flickers.
        self._last_bbox: BoundingBox | None = None
        self._frames_since_detection = 0
        self._frames_since_hand = 0

    def open(self) -> bool:
        """Open the camera. Returns False when no device is available."""
        self._capture = cv2.VideoCapture(self._camera_index)
        if not self._capture.isOpened():
            print(f"[Pi Camera] Could not open camera index {self._camera_index}")
            self.close()
            return False

        self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
        self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
        mode = "privacy crop" if self._detector is not None else "full frame"
        print(
            f"[Pi Camera] Opened index {self._camera_index} "
            f"at {self._width}x{self._height} ({mode})"
        )
        return True

    def read_encoded_frame(self) -> CapturedFrame | None:
        """Grab one frame and return it base64 encoded, or None on failure.

        Returns a CapturedFrame rather than a bare string now: the caller
        needs to know whether the payload is an image or a no-hand marker,
        and the type field carries that without a second channel.
        """
        if self._capture is None:
            return None

        ok, frame = self._capture.read()
        if not ok:
            return None

        resized = cv2.resize(frame, (self._width, self._height))

        if self._detector is None:
            # Aiming mode: no detector configured, full frame is the payload.
            return self._encode(resized, VIDEO_MESSAGE_TYPE)

        return self._crop_pipeline(resized)

    # -------------------------------------------------------------------
    # Privacy crop pipeline
    # -------------------------------------------------------------------

    def _crop_pipeline(self, frame) -> CapturedFrame:
        """Detect (or reuse) a hand box and encode the crop it defines."""

        # Throughput lever: on frames where detection is skipped, the last
        # box is reused as-is. With detect_every_n = 1 this never fires.
        if self._frames_since_detection < self._detect_every_n - 1:
            self._frames_since_detection += 1
            if self._last_bbox is not None:
                return self._crop_and_encode(frame, self._last_bbox)
            return CapturedFrame(message_type=NO_HAND_MESSAGE_TYPE)

        self._frames_since_detection = 0
        bbox = self._detector.detect_bbox(frame)

        if bbox is not None:
            self._last_bbox = bbox
            self._frames_since_hand = 0
            return self._crop_and_encode(frame, bbox)

        # Detection lost the hand. Hold the last crop for the grace period
        # so a single dropped detection does not flicker the phone's feed.
        self._frames_since_hand += 1
        if self._last_bbox is not None and self._frames_since_hand <= self._grace_frames:
            return self._crop_and_encode(frame, self._last_bbox)

        # Hand genuinely gone: drop the box so a stale crop cannot resurface
        # when the hand reappears somewhere else in the scene.
        self._last_bbox = None
        return CapturedFrame(message_type=NO_HAND_MESSAGE_TYPE)

    def _crop_and_encode(self, frame, bbox: BoundingBox) -> CapturedFrame | None:
        """Slice the hand crop out of [frame] and JPEG-encode it."""
        crop = frame[bbox.y0:bbox.y1, bbox.x0:bbox.x1]
        if crop.size == 0:
            print("[Pi Camera] Crop was empty, sending no-hand marker.")
            return CapturedFrame(message_type=NO_HAND_MESSAGE_TYPE)
        return self._encode(crop, VIDEO_MESSAGE_TYPE)

    def _encode(self, image, message_type: str) -> CapturedFrame | None:
        """JPEG-encode [image] to base64. None on an encoding failure."""
        success, buffer = cv2.imencode(
            ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality]
        )
        if not success:
            return None
        return CapturedFrame(
            message_type=message_type,
            image=base64.b64encode(buffer).decode("utf-8"),
        )

    def close(self) -> None:
        """Release the camera; safe to call more than once."""
        if self._capture is not None:
            self._capture.release()
            self._capture = None