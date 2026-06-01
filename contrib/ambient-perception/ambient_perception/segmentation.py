"""Anime foreground/character segmentation (load-bearing perception model).

Provides a clean interface ``Segmenter.foreground_mask(frame) -> float[H,W]``
in [0, 1], where 1 = foreground (character) and 0 = background. The ambient
estimator consumes ``bgConfidence = 1 - foreground_mask``.

Recommended pretrained models (all convertible to ONNX):
  * SkyTNT "anime-segmentation"  (ISNet-based)  -- the default recommendation.
        https://github.com/SkyTNT/anime-segmentation
        Hugging Face: skytnt/anime-seg  (isnetis.onnx is published directly).
  * U^2-Net / ISNet general matting as a fallback.

GRACEFUL FALLBACK: if the model file is missing or onnxruntime is unavailable,
:meth:`foreground_mask` returns an all-zeros mask (=> bgConfidence = 1
everywhere => the exact C++ baseline / whole-frame estimate) and logs ONE
warning. The service keeps running.
"""
from __future__ import annotations

import logging
import os

import numpy as np

logger = logging.getLogger(__name__)

# TODO/ASSUMPTION: exact input/output tensor names and layout vary per export.
# For skytnt/anime-seg isnetis.onnx the common contract is:
#   input :  name unknown, shape (1, 3, H, W), RGB, float32, range [0,1],
#            H=W=1024 (square). Some exports want [-1,1] or BGR.
#   output:  a single (1, 1, H, W) sigmoided mask in [0,1] (1 = foreground).
# These are interfaced below and must be confirmed against the actual file
# (e.g. `python -c "import onnxruntime,sys; \
#   print([(i.name,i.shape) for i in onnxruntime.InferenceSession(sys.argv[1]).get_inputs()])"`).
_ASSUMED_INPUT_SIZE = 1024
_ASSUMED_INPUT_RANGE = (0.0, 1.0)   # set to (-1.0, 1.0) if the model needs it
_ASSUMED_RGB = True                 # set False if the model expects BGR


def _resize_bilinear(img: np.ndarray, size: int) -> np.ndarray:
    """Tiny dependency-free bilinear resize to (size, size, C).

    Avoids pulling in OpenCV/Pillow just for preprocessing. If you already have
    cv2 available, replacing this with cv2.resize is faster and fine.
    """
    h, w = img.shape[:2]
    ys = (np.linspace(0, h - 1, size)).astype(np.float64)
    xs = (np.linspace(0, w - 1, size)).astype(np.float64)
    y0 = np.floor(ys).astype(int)
    x0 = np.floor(xs).astype(int)
    y1 = np.minimum(y0 + 1, h - 1)
    x1 = np.minimum(x0 + 1, w - 1)
    wy = (ys - y0)[:, None, None]
    wx = (xs - x0)[None, :, None]
    img = img.astype(np.float64)
    top = img[y0][:, x0] * (1 - wx) + img[y0][:, x1] * wx
    bot = img[y1][:, x0] * (1 - wx) + img[y1][:, x1] * wx
    return top * (1 - wy) + bot * wy


class Segmenter:
    """ONNX anime-segmentation wrapper with a no-op fallback."""

    def __init__(
        self,
        model_path: str | None,
        providers: list[str] | None = None,
        input_size: int = _ASSUMED_INPUT_SIZE,
    ) -> None:
        self.model_path = model_path
        self.input_size = input_size
        self._session = None
        self._input_name = None
        self._output_name = None
        self._warned = False

        if not model_path:
            logger.warning("Segmentation: no model path configured; "
                           "bgConfidence=1 everywhere (whole-frame estimate).")
            return
        if not os.path.isfile(model_path):
            logger.warning("Segmentation: model file not found at %s; "
                           "bgConfidence=1 everywhere (whole-frame estimate).",
                           model_path)
            return

        try:
            import onnxruntime as ort  # noqa: PLC0415 (optional dependency)
        except Exception as exc:  # pragma: no cover - env dependent
            logger.warning("Segmentation: onnxruntime unavailable (%s); "
                           "bgConfidence=1 everywhere.", exc)
            return

        providers = providers or [
            "TensorrtExecutionProvider",
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ]
        try:
            self._session = ort.InferenceSession(model_path, providers=providers)
            self._input_name = self._session.get_inputs()[0].name
            self._output_name = self._session.get_outputs()[0].name
            logger.info("Segmentation: loaded %s with providers %s",
                        model_path, self._session.get_providers())
        except Exception as exc:  # pragma: no cover - env dependent
            logger.warning("Segmentation: failed to load %s (%s); "
                           "bgConfidence=1 everywhere.", model_path, exc)
            self._session = None

    @property
    def active(self) -> bool:
        return self._session is not None

    def foreground_mask(self, rgb_u8: np.ndarray) -> np.ndarray:
        """Return an ``(H, W)`` float foreground mask in [0,1] (1 = character).

        ``rgb_u8`` is an ``(H, W, 3)`` uint8 RGB frame (the downscaled frame).
        Falls back to all-zeros (no foreground) if no model is active.
        """
        h, w = rgb_u8.shape[:2]
        if self._session is None:
            return np.zeros((h, w), dtype=np.float32)

        try:
            inp = self._preprocess(rgb_u8)
            out = self._session.run([self._output_name], {self._input_name: inp})[0]
            mask = self._postprocess(out, (h, w))
            return mask
        except Exception as exc:  # pragma: no cover - env dependent
            if not self._warned:
                logger.warning("Segmentation: inference failed (%s); falling "
                               "back to bgConfidence=1 for this and future "
                               "frames until it recovers.", exc)
                self._warned = True
            return np.zeros((h, w), dtype=np.float32)

    # -- model-specific glue (confirm against your actual ONNX file) --------
    def _preprocess(self, rgb_u8: np.ndarray) -> np.ndarray:
        small = _resize_bilinear(rgb_u8, self.input_size) / 255.0  # (S,S,3) [0,1]
        if not _ASSUMED_RGB:
            small = small[..., ::-1]
        lo, hi = _ASSUMED_INPUT_RANGE
        small = lo + small * (hi - lo)
        # NCHW float32
        return np.transpose(small, (2, 0, 1))[None].astype(np.float32)

    def _postprocess(self, out: np.ndarray, hw: tuple[int, int]) -> np.ndarray:
        # TODO/ASSUMPTION: assume output is (1,1,S,S) or (1,S,S) in [0,1].
        arr = np.asarray(out, dtype=np.float32)
        arr = np.squeeze(arr)
        if arr.ndim != 2:
            # Unexpected shape; be safe and treat as no foreground.
            return np.zeros(hw, dtype=np.float32)
        # If the model emitted logits (outside [0,1]), squash with a sigmoid.
        if arr.min() < -1e-3 or arr.max() > 1.0 + 1e-3:
            arr = 1.0 / (1.0 + np.exp(-arr))
        mask = _resize_bilinear(arr[..., None], hw[0])[..., 0]
        # _resize_bilinear is square; do a second pass for width if needed.
        if mask.shape != hw:
            mask = _resize_to(arr, hw)
        return np.clip(mask, 0.0, 1.0).astype(np.float32)


def _resize_to(arr2d: np.ndarray, hw: tuple[int, int]) -> np.ndarray:
    """Bilinear resize a 2D array to (H, W) (non-square safe)."""
    h, w = hw
    sh, sw = arr2d.shape
    ys = np.linspace(0, sh - 1, h)
    xs = np.linspace(0, sw - 1, w)
    y0 = np.floor(ys).astype(int)
    x0 = np.floor(xs).astype(int)
    y1 = np.minimum(y0 + 1, sh - 1)
    x1 = np.minimum(x0 + 1, sw - 1)
    wy = (ys - y0)[:, None]
    wx = (xs - x0)[None, :]
    a = arr2d
    top = a[y0][:, x0] * (1 - wx) + a[y0][:, x1] * wx
    bot = a[y1][:, x0] * (1 - wx) + a[y1][:, x1] * wx
    return top * (1 - wy) + bot * wy
