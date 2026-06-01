"""Optional depth model (Depth-Anything-V2 ONNX) -> far-depth weight.

The ambient weight can optionally be multiplied by a "far" weight so distant
background (which usually casts the dominant ambient light) outvotes near
midground. This is OPTIONAL and low-weight: if absent, the service skips it and
``far_weight = 1`` everywhere (no effect).

Model: Depth-Anything-V2 (https://github.com/DepthAnything/Depth-Anything-V2).
ONNX exports are widely available (e.g. fabio-sim/Depth-Anything-ONNX).

GRACEFUL FALLBACK: absent model / onnxruntime => :meth:`far_weight` returns
all-ones (no effect) and logs one warning.
"""
from __future__ import annotations

import logging
import os

import numpy as np

logger = logging.getLogger(__name__)

# TODO/ASSUMPTION: Depth-Anything-V2 typically wants (1,3,H,W) RGB float32,
# ImageNet-normalized, H=W=518. Output is (1,H,W) RELATIVE inverse depth
# (larger = nearer) but conventions vary across exports -- confirm and flip if
# your model emits metric/"larger = farther" depth. We normalize per-frame and
# treat LARGER output as NEARER (so far_weight = 1 - normalized).
_INPUT_SIZE = 518
_IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def _resize_bilinear(img: np.ndarray, size) -> np.ndarray:
    """Bilinear resize. ``size`` is an int (square) or an ``(out_h, out_w)`` tuple.

    NON-SQUARE SAFE: passing an int gives a square output (used for the square
    model input); passing ``(h, w)`` resizes back to the real frame shape.
    """
    out_h, out_w = (size, size) if isinstance(size, int) else size
    h, w = img.shape[:2]
    ys = np.linspace(0, h - 1, out_h)
    xs = np.linspace(0, w - 1, out_w)
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


class DepthEstimator:
    """Optional Depth-Anything-V2 wrapper producing a far-depth weight."""

    def __init__(
        self,
        model_path: str | None,
        providers: list[str] | None = None,
        strength: float = 1.0,
    ) -> None:
        # ``strength`` blends the far weight toward 1.0 (no effect) at 0.
        self.strength = float(np.clip(strength, 0.0, 1.0))
        self._session = None
        self._input_name = None
        self._output_name = None

        if not model_path:
            return
        if not os.path.isfile(model_path):
            logger.warning("Depth: model not found at %s; far_weight=1.", model_path)
            return
        try:
            import onnxruntime as ort  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover - env dependent
            logger.warning("Depth: onnxruntime unavailable (%s); far_weight=1.", exc)
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
            logger.info("Depth: loaded %s", model_path)
        except Exception as exc:  # pragma: no cover - env dependent
            logger.warning("Depth: failed to load %s (%s); far_weight=1.",
                           model_path, exc)
            self._session = None

    @property
    def active(self) -> bool:
        return self._session is not None

    def far_weight(self, rgb_u8: np.ndarray) -> np.ndarray:
        """Return ``(H, W)`` weight in [0,1]; ~1 for far pixels, lower for near.

        All-ones (no effect) when no model is active.
        """
        h, w = rgb_u8.shape[:2]
        if self._session is None:
            return np.ones((h, w), dtype=np.float32)
        try:
            small = _resize_bilinear(rgb_u8, _INPUT_SIZE) / 255.0
            small = (small - _IMAGENET_MEAN) / _IMAGENET_STD
            inp = np.transpose(small, (2, 0, 1))[None].astype(np.float32)
            out = self._session.run([self._output_name], {self._input_name: inp})[0]
            depth = np.squeeze(np.asarray(out, dtype=np.float32))
            if depth.ndim != 2:
                return np.ones((h, w), dtype=np.float32)
            d = depth - depth.min()
            rng = d.max()
            if rng <= 1e-6:
                return np.ones((h, w), dtype=np.float32)
            near = d / rng                      # 1 = nearest (see ASSUMPTION)
            far = 1.0 - near                    # 1 = farthest
            # Resize back to the REAL frame shape (h, w), not a square (h, h).
            far = _resize_bilinear(far[..., None], (h, w))[..., 0]
            # Blend toward 1.0 by strength so depth is only a gentle nudge.
            far = 1.0 - self.strength * (1.0 - np.clip(far, 0.0, 1.0))
            return far.astype(np.float32)
        except Exception as exc:  # pragma: no cover - env dependent
            logger.warning("Depth: inference failed (%s); far_weight=1.", exc)
            self._session = None
            return np.ones((h, w), dtype=np.float32)
