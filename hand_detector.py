"""MediaPipe hand detection for the privacy crop.

Runs Google's HandLandmarker on the raw camera frame and returns the bounding
box of the first detected hand, in pixels. The crop made with that box is the
only image data that ever leaves the Pi: the full frame stays in memory here
and is discarded once cropped.

The model is the same ``hand_landmarker.task`` the Android hub bundles in
``app/src/main/assets/``, so both ends of the link run one model file.

``mediapipe`` is imported inside the constructor rather than at module scope
so that a machine without the package can still import this module: the
aiming mode (``PI_NODE_PRIVACY=off``) runs with no detector at all, and
startup code checks availability before requiring one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import cv2
import numpy as np

from config import MIN_CROP_SIDE_PX


@dataclass(frozen=True)
class BoundingBox:
    """Pixel-space rectangle, half-open: x1/y1 lie just outside the crop."""

    x0: int
    y0: int
    x1: int
    y1: int


def mediapipe_available() -> bool:
    """True when the mediapipe package can be imported on this machine."""
    try:
        import mediapipe  # noqa: F401
    except (ImportError, OSError):
        return False
    return True


def cpu_supports_aes() -> bool:
    """True when this CPU advertises the AES instruction.

    MediaPipe's native libraries are built with AES enabled and abort the
    whole process -- "compiled with aes enabled, but this feature is not
    available on this processor" -- when the CPU lacks it. That abort happens
    inside native code, so Python cannot catch it: importing the package
    succeeds, and the process dies the first time a model runs. Checking the
    CPU flag first is the only reliable way to know whether MediaPipe can be
    used at all.
    """
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            for line in handle:
                if line.lower().startswith("features"):
                    return "aes" in line.split(":", 1)[1].split()
    except OSError:
        pass
    # Not Linux, or /proc is unavailable: do not block detection on a guess.
    return True


def mediapipe_usable() -> bool:
    """True when MediaPipe is installed *and* this CPU can run it."""
    return mediapipe_available() and cpu_supports_aes()


def bbox_from_landmarks(
    xs: list[float],
    ys: list[float],
    width: int,
    height: int,
    margin: float,
    min_side: int = MIN_CROP_SIDE_PX,
) -> BoundingBox:
    """Turn landmark coordinates into a padded, clamped pixel bounding box.

    Pure geometry, intentionally free of MediaPipe types so it can be unit
    tested without the model installed.

    Args:
        xs: normalised landmark x coordinates (0 = left, 1 = right).
        ys: normalised landmark y coordinates (0 = top, 1 = bottom).
        width: frame width in pixels.
        height: frame height in pixels.
        margin: fraction of the hand's own size added on every side.
        min_side: smallest allowed crop side in pixels.

    Returns:
        The pixel-space box, guaranteed non-empty and inside the frame.
    """
    # Landmarks can leave [0, 1] slightly on hands that touch the frame edge;
    # clamp before converting so padding arithmetic stays sane.
    x0, x1 = max(0.0, min(xs)), min(1.0, max(xs))
    y0, y1 = max(0.0, min(ys)), min(1.0, max(ys))

    hand_w = x1 - x0
    hand_h = y1 - y0

    # Pad in normalised space, then convert to pixels and clamp to the frame.
    px0 = max(0, int((x0 - margin * hand_w) * width))
    px1 = min(width, int((x1 + margin * hand_w) * width))
    py0 = max(0, int((y0 - margin * hand_h) * height))
    py1 = min(height, int((y1 + margin * hand_h) * height))

    # A hand at the frame edge can clamp down to a sliver; widen it around
    # its centre so the crop stays useful for the hub's re-detection.
    px0, px1 = _widen_to_min_side(px0, px1, min_side, width)
    py0, py1 = _widen_to_min_side(py0, py1, min_side, height)

    return BoundingBox(x0=px0, y0=py0, x1=px1, y1=py1)


def _widen_to_min_side(
    low: int, high: int, min_side: int, limit: int
) -> tuple[int, int]:
    """Grow a one-dimensional span to at least [min_side], inside [0, limit]."""
    if high - low >= min_side:
        return low, high

    centre = (low + high) // 2
    low = max(0, centre - min_side // 2)
    high = min(limit, low + min_side)
    # Clamped against the right edge: shift back left if the span is short.
    if high - low < min_side:
        low = max(0, high - min_side)
    return low, high


class HandDetector:
    """Wraps the MediaPipe HandLandmarker and exposes plain pixel boxes."""

    def __init__(
        self,
        model_path: str,
        margin: float,
        num_hands: int = 1,
        min_confidence: float = 0.5,
    ) -> None:
        """Load the model once; construction is the expensive step.

        Raises:
            FileNotFoundError: the model file is missing.
            RuntimeError: mediapipe is installed but the model failed to load.
        """
        if not os.path.isfile(model_path):
            raise FileNotFoundError(
                f"Hand landmarker model not found at '{model_path}'. "
                "Copy hand_landmarker.task from the Android app's "
                "app/src/main/assets/ into models/, or set PI_NODE_MODEL_PATH."
            )

        # Deferred so importing this module never requires mediapipe. The
        # import sits inside the constructor, which only runs in crop mode.
        import mediapipe
        from mediapipe.tasks import python as mp_tasks
        from mediapipe.tasks.python import vision

        # Kept on the instance: detect() needs the Image constructor and it
        # must stay reachable without a module-scope mediapipe import.
        self._mp = mediapipe

        # IMAGE mode with one hand, mirroring HandGestureAnalyzer.kt on the
        # hub: frames are independent JPEGs, there is no temporal tracking to
        # exploit, and one hand halves the inference cost.
        options = vision.HandLandmarkerOptions(
            base_options=mp_tasks.BaseOptions(model_asset_path=model_path),
            running_mode=vision.RunningMode.IMAGE,
            num_hands=num_hands,
            min_hand_detection_confidence=min_confidence,
            min_hand_presence_confidence=min_confidence,
            min_tracking_confidence=min_confidence,
        )
        self._landmarker = vision.HandLandmarker.create_from_options(options)
        self._margin = margin
        self._model_path = model_path
        print(f"[Pi Privacy] Hand detector ready (model: {model_path})")

    def detect_bbox(self, frame_bgr: np.ndarray) -> BoundingBox | None:
        """Locate the first hand in [frame_bgr]. None when none is found.

        One bad frame must never end the stream, so inference errors are
        caught and reported as "no hand" — the same recovery the hub's
        analyzer uses.
        """
        try:
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            mp_image = self._mp.Image(
                image_format=self._mp.ImageFormat.SRGB,
                data=np.ascontiguousarray(rgb),
            )
            result = self._landmarker.detect(mp_image)
        except Exception as exc:  # noqa: BLE001 - never kill the stream loop
            print(f"[Pi Privacy] Detection failed: {exc}")
            return None

        hands = result.hand_landmarks
        if not hands:
            return None

        height, width = frame_bgr.shape[:2]
        landmarks = hands[0]
        return bbox_from_landmarks(
            xs=[lm.x for lm in landmarks],
            ys=[lm.y for lm in landmarks],
            width=width,
            height=height,
            margin=self._margin,
        )

    def detect_landmarks(self, frame_bgr: np.ndarray):
        """Return (points, handedness) for the first hand, or (None, None).

        Points are 21 (x, y, z) tuples in normalised coordinates, exactly the
        values MediaPipe returns — the format the landmarks wire mode sends
        and the phone's classifier consumes. Handedness is the model's label
        ("Left"/"Right"), display information only.
        """
        try:
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            mp_image = self._mp.Image(
                image_format=self._mp.ImageFormat.SRGB,
                data=np.ascontiguousarray(rgb),
            )
            result = self._landmarker.detect(mp_image)
        except Exception as exc:  # noqa: BLE001 - never kill the stream loop
            print(f"[Pi Privacy] Detection failed: {exc}")
            return None, None

        hands = result.hand_landmarks
        if not hands:
            return None, None

        landmarks = hands[0]
        points = [(lm.x, lm.y, lm.z) for lm in landmarks]
        label = None
        try:
            categories = result.handedness[0]
            if categories:
                label = categories[0].category_name
        except (IndexError, AttributeError):
            label = None
        return points, label

    def close(self) -> None:
        """Release the native model. Safe to call more than once."""
        try:
            self._landmarker.close()
            print("[Pi Privacy] Hand detector released.")
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            print(f"[Pi Privacy] Error releasing detector: {exc}")
