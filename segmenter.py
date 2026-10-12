"""Background removal via MediaPipe selfie segmentation (privacy mode 'segment').

Replaces the privacy crop's zooming behaviour with a full-frame, scene-free
image: the person — and therefore the hand and wrist — stay at natural scale
in frame, while the scene background is blacked out.

Why this mode exists (live testing, 12/10/2026):
    Crop mode re-frames every frame around the detected hand, and that
    re-framing costs recognition on the hub:
      - the crop boundary can cut through the wrist, and the hub's detector
        needs the wrist (a documented limit), so the feed flickered;
      - every crop is a new tight image, and re-detection on tight crops is
        less reliable than on full frames.
    Segmentation keeps the geometry stable AND still removes the scene, which
    is the part privacy protects. Trade-off against crop: the person's own
    silhouette (body outline) remains visible. Privacy therefore ranks
    crop > segment > off; which mode to run is the operator's call via
    PI_NODE_PRIVACY.

Fail-closed rule:
    If segmentation ever fails at runtime, the frame is returned BLACK, never
    the original — a broken privacy layer must not silently leak the scene.
    Startup treats a missing model or package as fatal, as with the detector.
"""

from __future__ import annotations

import cv2
import numpy as np


def segmenter_available() -> bool:
    """True when the mediapipe package can be imported on this machine."""
    try:
        import mediapipe  # noqa: F401
    except (ImportError, OSError):
        return False
    return True


class BackgroundRemover:
    """Wraps MediaPipe ImageSegmenter (selfie segmentation, general model)."""

    def __init__(self, model_path: str) -> None:
        """Load the model once; construction is the expensive step.

        Raises:
            FileNotFoundError: the model file is missing.
            RuntimeError: mediapipe is installed but the model failed to load.
        """
        import os

        if not os.path.isfile(model_path):
            raise FileNotFoundError(
                f"Selfie segmentation model not found at '{model_path}'. "
                "Download selfie_segmenter.tflite (float16, latest) from "
                "storage.googleapis.com/mediapipe-models/image_segmenter/"
                "selfie_segmenter/ into models/, or set PI_NODE_SEGMENTER_MODEL."
            )

        # Deferred exactly like hand_detector.py: importing this module never
        # requires mediapipe; only constructing a remover does.
        import mediapipe
        from mediapipe.tasks import python as mp_tasks
        from mediapipe.tasks.python import vision

        self._mp = mediapipe

        options = vision.ImageSegmenterOptions(
            base_options=mp_tasks.BaseOptions(model_asset_path=model_path),
            running_mode=vision.RunningMode.IMAGE,
            output_category_mask=False,
            output_confidence_masks=True,
        )
        self._segmenter = vision.ImageSegmenter.create_from_options(options)
        self._model_path = model_path
        print(f"[Pi Privacy] Background remover ready (model: {model_path})")

    def apply(self, frame_bgr: np.ndarray) -> np.ndarray:
        """Return [frame_bgr] with everything that is not a person blacked out.

        The general selfie model emits one confidence mask per category with
        the background first, so person-probability is 1 - background. With a
        single-mask variant the mask itself is the person probability.
        """
        try:
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            mp_image = self._mp.Image(
                image_format=self._mp.ImageFormat.SRGB,
                data=np.ascontiguousarray(rgb),
            )
            result = self._segmenter.segment(mp_image)
            masks = result.confidence_masks
            if not masks:
                raise ValueError("segmenter returned no masks")

            # mediapipe 1.x: mask images expose numpy_view(), not numpy(),
            # and the array carries trailing singleton dims (H, W, 1, 1) that
            # must be squeezed before the boolean comparison broadcasts.
            background = masks[0].numpy_view().squeeze()
            person = 1.0 - background if len(masks) > 1 else background
            keep = (person > 0.5)[:, :, np.newaxis]

            # Fail closed: only the masked pixels survive; everything else,
            # including any pixel touched by a processing error, is black.
            return np.where(keep, frame_bgr, 0).astype(np.uint8)
        except Exception as exc:  # noqa: BLE001 - privacy must fail closed
            print(f"[Pi Privacy] Segmentation failed, sending black frame: {exc}")
            return np.zeros_like(frame_bgr)

    def close(self) -> None:
        """Release the native model. Safe to call more than once."""
        try:
            self._segmenter.close()
            print("[Pi Privacy] Background remover released.")
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            print(f"[Pi Privacy] Error releasing segmenter: {exc}")