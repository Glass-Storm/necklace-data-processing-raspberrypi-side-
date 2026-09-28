"""Webcam capture and JPEG encoding for the phone stream."""

import base64

import cv2

from config import (
    CAMERA_INDEX,
    FRAME_HEIGHT,
    FRAME_WIDTH,
    JPEG_QUALITY,
)


class VideoStream:
    """Wraps a single OpenCV capture device."""

    def __init__(
        self,
        camera_index: int = CAMERA_INDEX,
        width: int = FRAME_WIDTH,
        height: int = FRAME_HEIGHT,
        jpeg_quality: int = JPEG_QUALITY,
    ) -> None:
        self._camera_index = camera_index
        self._width = width
        self._height = height
        self._jpeg_quality = jpeg_quality
        self._capture = None

    def open(self) -> bool:
        """Open the camera. Returns False when no device is available."""
        self._capture = cv2.VideoCapture(self._camera_index)
        if not self._capture.isOpened():
            print(f"[Pi Camera] Could not open camera index {self._camera_index}")
            self.close()
            return False

        self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
        self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
        print(
            f"[Pi Camera] Opened index {self._camera_index} "
            f"at {self._width}x{self._height}"
        )
        return True

    def read_encoded_frame(self) -> str | None:
        """Grab one frame and return it base64 encoded, or None on failure."""
        if self._capture is None:
            return None

        ok, frame = self._capture.read()
        if not ok:
            return None

        resized = cv2.resize(frame, (self._width, self._height))
        success, buffer = cv2.imencode(
            ".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality]
        )
        if not success:
            return None

        return base64.b64encode(buffer).decode("utf-8")

    def close(self) -> None:
        """Release the camera; safe to call more than once."""
        if self._capture is not None:
            self._capture.release()
            self._capture = None
