"""Hand landmarks without MediaPipe.

MediaPipe's native library needs the ARMv8 AES instruction, which some boards
(including this one) do not expose, so ``PI_NODE_PRIVACY=landmarks`` could not
run. This module does the same job with the two plain TFLite models that are
already inside ``hand_landmarker.task`` (it is a zip), executed by OpenCV's DNN
module — no MediaPipe, no AES, no emulation.

How the two models are driven, and why:

* ``hand_detector.tflite`` (palm detection) is used only as a presence gate:
  its score output is a logit per anchor, and any anchor above 0 means a hand
  was found. That is MediaPipe's own first stage.
* ``hand_landmarks_detector.tflite`` returns 21 (x, y, z) triples. The useful
  tensor is ``Identity`` — pixels of its 224x224 input, not 0-1.
  ``forward()`` with no name returns ``Identity_3``, the local/world frame,
  which comes back as a near-constant cluster; reading that is why a naive
  port appears to detect nothing.
* Its ``Identity_2`` "hand present" score is useless as a gate (0.64 for a
  real hand, 0.59 for flat grey), and ``Identity_1`` is handedness, which is
  0.99 for one hand and 0.00 for the other, so it cannot gate either.
* It expects a hand-centred crop, so the ROI is found by iteration: run once
  over the whole frame, then re-crop around the landmarks that came back,
  which pulls the hand up to full size.
* A second, geometric guard rejects the degenerate output a "no hand" frame
  produces (a thin line of points), which the palm gate lets through on a
  fully black frame.

Only the resulting coordinates ever leave the Pi: no image is encoded or
transmitted in this mode.
"""

from __future__ import annotations

import os
import tempfile
import zipfile

import cv2
import numpy as np

from config import MIN_CROP_SIDE_PX
from hand_detector import BoundingBox, bbox_from_landmarks

PALM_MODEL_NAME = "hand_detector.tflite"
LANDMARK_MODEL_NAME = "hand_landmarks_detector.tflite"

# Palm detector input, at MediaPipe's own preprocessing values.
PALM_INPUT = 192
PALM_SCALE = 1 / 127.5
PALM_MEAN = 127.5
# Its score output is a logit, so 0 is the 0.5 confidence point.
PALM_LOGIT_MIN = 0.0

# Landmark model input; its "Identity" output is in pixels of this input.
LANDMARK_INPUT = 224
LANDMARK_SCALE = 1 / LANDMARK_INPUT

# A detected hand spans a good part of its crop; a "no hand" output collapses
# to a thin line. Measured on this hardware: a real hand fills 0.69 x 0.74 of
# the crop, while flat grey / black / noise frames come back as 0.05 x 0.59.
# The short side is what separates them, so that is what the guard tests.
HAND_MIN_SPAN = 0.10

# How much of the crop the hand must fill before cropping again stops helping.
FILL_FRACTION = 0.55
MAX_REFINEMENTS = 2
ROI_MARGIN = 1.6


def dnn_available() -> bool:
    """True when OpenCV can run the bundled TFLite models on this machine."""
    return hasattr(cv2.dnn, "readNetFromTFLite")


def extract_models(task_path: str, target_dir: str) -> tuple[str, str]:
    """Pull the two .tflite models out of the .task zip.

    Returns:
        (palm model path, landmark model path).

    Raises:
        FileNotFoundError: the task file is missing.
        RuntimeError: the zip does not contain both models.
    """
    if not os.path.isfile(task_path):
        raise FileNotFoundError(
            f"Hand landmarker model not found at '{task_path}'. Copy "
            "hand_landmarker.task from the Android app's "
            "app/src/main/assets/ into models/, or set PI_NODE_MODEL_PATH."
        )

    os.makedirs(target_dir, exist_ok=True)
    paths = {}
    with zipfile.ZipFile(task_path) as archive:
        for name in archive.namelist():
            if name in (PALM_MODEL_NAME, LANDMARK_MODEL_NAME):
                destination = os.path.join(target_dir, name)
                with archive.open(name) as source:
                    with open(destination, "wb") as handle:
                        handle.write(source.read())
                paths[name] = destination

    missing = [n for n in (PALM_MODEL_NAME, LANDMARK_MODEL_NAME) if n not in paths]
    if missing:
        raise RuntimeError(f"{task_path} is missing {', '.join(missing)}")
    return paths[PALM_MODEL_NAME], paths[LANDMARK_MODEL_NAME]


class DnnHandDetector:
    """Same interface as hand_detector.HandDetector, implemented with OpenCV."""

    def __init__(
        self,
        model_path: str,
        margin: float,
        num_hands: int = 1,
        min_confidence: float = 0.5,
    ) -> None:
        """Load both models. Construction is the expensive step.

        Args:
            model_path: the hand_landmarker.task file.
            margin: fraction of the hand's size added around the crop box.
            num_hands: accepted for interface parity; one hand is detected.
            min_confidence: accepted for interface parity.
        """
        del num_hands, min_confidence  # one hand, model's own thresholds

        if not dnn_available():
            raise RuntimeError(
                "This OpenCV build has no cv2.dnn.readNetFromTFLite; "
                "install opencv-python 4.5.4 or newer."
            )

        cache_dir = os.path.join(tempfile.gettempdir(), "necklace-hand-models")
        palm_path, landmark_path = extract_models(model_path, cache_dir)
        self._palm_net = cv2.dnn.readNetFromTFLite(palm_path)
        self._landmark_net = cv2.dnn.readNetFromTFLite(landmark_path)
        self._margin = margin
        print(f"[Pi Privacy] Hand detector ready (TFLite via OpenCV: {model_path})")

    # ------------------------------------------------------------------
    # Presence
    # ------------------------------------------------------------------

    def _palm_logit(self, frame_bgr: np.ndarray) -> float:
        """Highest palm-detection logit for [frame_bgr]; >0 means a hand."""
        blob = cv2.dnn.blobFromImage(
            frame_bgr,
            PALM_SCALE,
            (PALM_INPUT, PALM_INPUT),
            (PALM_MEAN, PALM_MEAN, PALM_MEAN),
            swapRB=True,
        )
        self._palm_net.setInput(blob)
        logits = self._palm_net.forward("Identity_1").reshape(-1)
        return float(logits.max())

    # ------------------------------------------------------------------
    # Landmarks
    # ------------------------------------------------------------------

    def detect_landmarks(self, frame_bgr: np.ndarray):
        """Return (points, handedness) for the first hand, or (None, None).

        Points are 21 (x, y, z) tuples normalised to the frame — the same
        shape MediaPipe returns, which is what the landmarks wire mode sends
        and the phone's classifier consumes.
        """
        height, width = frame_bgr.shape[:2]
        if height == 0 or width == 0:
            return None, None
        if self._palm_logit(frame_bgr) <= PALM_LOGIT_MIN:
            return None, None

        roi = None  # None means "the whole frame"
        best_span = -1.0
        best_points = None
        best_label = None

        for attempt in range(MAX_REFINEMENTS + 1):
            if roi is None:
                x0, y0, x1, y1 = 0, 0, width, height
            else:
                x0, y0, x1, y1 = roi

            crop = frame_bgr[y0:y1, x0:x1]
            if crop.size == 0:
                break

            crop_points, label = self._run_landmarks(crop)
            crop_w, crop_h = x1 - x0, y1 - y0
            points = [
                ((x0 + px * crop_w) / width, (y0 + py * crop_h) / height, pz)
                for px, py, pz in crop_points
            ]

            span_x, span_y = self._spans(crop_points)
            short_side = min(span_x, span_y)
            if short_side > best_span:
                best_span = short_side
                best_points = points
                best_label = label

            # Stop once the hand fills the crop: cropping again cannot help.
            if max(span_x, span_y) >= FILL_FRACTION:
                break
            if attempt == MAX_REFINEMENTS:
                break
            roi = self._square_roi(points, width, height)
            if roi is None:
                break

        if best_points is None or best_span < HAND_MIN_SPAN:
            return None, None
        return best_points, best_label

    def _run_landmarks(self, crop_bgr: np.ndarray):
        """Run the landmark model on one crop.

        Returns:
            (list of 21 (x, y, z) crop-normalised tuples, handedness label).
        """
        square = cv2.resize(crop_bgr, (LANDMARK_INPUT, LANDMARK_INPUT))
        blob = cv2.dnn.blobFromImage(
            square,
            1 / 255.0,
            (LANDMARK_INPUT, LANDMARK_INPUT),
            (0.0, 0.0, 0.0),
            swapRB=True,
        )
        self._landmark_net.setInput(blob)

        # "Identity" is the 224-space landmark tensor. forward() without a
        # name yields Identity_3, the local/world frame, which is useless here.
        raw = self._landmark_net.forward("Identity").reshape(-1, 3)
        handedness = float(self._landmark_net.forward("Identity_1").reshape(-1)[0])

        points = [
            (float(x) * LANDMARK_SCALE, float(y) * LANDMARK_SCALE,
             float(z) * LANDMARK_SCALE)
            for x, y, z in raw
        ]
        return points, ("Left" if handedness > 0.5 else "Right")

    @staticmethod
    def _spans(crop_points) -> tuple[float, float]:
        """(x, y) extent of [crop_points], as a fraction of the crop."""
        xs = [p[0] for p in crop_points]
        ys = [p[1] for p in crop_points]
        return max(xs) - min(xs), max(ys) - min(ys)

    @staticmethod
    def _square_roi(points, width: int, height: int):
        """A square region around [points], clamped to the frame."""
        xs = [p[0] * width for p in points]
        ys = [p[1] * height for p in points]
        centre_x = (min(xs) + max(xs)) / 2
        centre_y = (min(ys) + max(ys)) / 2
        side = max(max(xs) - min(xs), max(ys) - min(ys)) * ROI_MARGIN

        half = side / 2
        x0 = int(max(0, centre_x - half))
        y0 = int(max(0, centre_y - half))
        x1 = int(min(width, centre_x + half))
        y1 = int(min(height, centre_y + half))
        if x1 - x0 < MIN_CROP_SIDE_PX or y1 - y0 < MIN_CROP_SIDE_PX:
            return None
        return x0, y0, x1, y1

    # ------------------------------------------------------------------
    # Bounding box (crop mode)
    # ------------------------------------------------------------------

    def detect_bbox(self, frame_bgr: np.ndarray) -> BoundingBox | None:
        """Locate the first hand in [frame_bgr]. None when none is found."""
        points, _ = self.detect_landmarks(frame_bgr)
        if points is None:
            return None
        height, width = frame_bgr.shape[:2]
        return bbox_from_landmarks(
            xs=[p[0] for p in points],
            ys=[p[1] for p in points],
            width=width,
            height=height,
            margin=self._margin,
        )

    def close(self) -> None:
        """Nothing native to release; kept for interface parity."""
        print("[Pi Privacy] Hand detector released.")
