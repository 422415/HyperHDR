"""Optional learned scene-cut detector (TransNetV2 ONNX).

The DEFAULT scene-cut path is the classical histogram in :mod:`temporal`
(no model, robust, mirrors the C++). This module is an OPTIONAL upgrade: if a
TransNetV2 ONNX model is configured, it can provide a per-frame cut probability
that the service can OR with (or replace) the histogram trigger.

TransNetV2: https://github.com/soCzech/TransNetV2 (pretrained weights provided;
an ONNX export is straightforward). It consumes a sliding window of 100 frames
at 48x27 and outputs a per-frame transition probability.

GRACEFUL FALLBACK: absent model / onnxruntime => :meth:`is_cut` always returns
False and the service relies on the histogram detector.
"""
from __future__ import annotations

import logging
import os
from collections import deque

import numpy as np

logger = logging.getLogger(__name__)

# TransNetV2 canonical input geometry.
_FRAME_H = 27
_FRAME_W = 48
_WINDOW = 100        # frames per inference window
_CENTER = _WINDOW // 2


def _resize_to(arr: np.ndarray, h: int, w: int) -> np.ndarray:
    """Nearest-neighbor resize (H,W,3) -> (h,w,3); cheap, good enough at 48x27."""
    sh, sw = arr.shape[:2]
    ys = np.linspace(0, sh - 1, h).astype(int)
    xs = np.linspace(0, sw - 1, w).astype(int)
    return arr[ys][:, xs]


class TransNetCutDetector:
    """Optional TransNetV2 ONNX cut detector with a no-op fallback."""

    def __init__(
        self,
        model_path: str | None,
        providers: list[str] | None = None,
        threshold: float = 0.5,
    ) -> None:
        self.threshold = threshold
        self._session = None
        self._input_name = None
        self._output_name = None
        self._window: deque[np.ndarray] = deque(maxlen=_WINDOW)

        if not model_path or not os.path.isfile(model_path):
            if model_path:
                logger.warning("TransNetV2: model not found at %s; using "
                               "histogram cut detection only.", model_path)
            return
        try:
            import onnxruntime as ort  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover - env dependent
            logger.warning("TransNetV2: onnxruntime unavailable (%s); "
                           "histogram cut detection only.", exc)
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
            logger.info("TransNetV2: loaded %s", model_path)
        except Exception as exc:  # pragma: no cover - env dependent
            logger.warning("TransNetV2: failed to load %s (%s)", model_path, exc)
            self._session = None

    @property
    def active(self) -> bool:
        return self._session is not None

    def is_cut(self, rgb_u8: np.ndarray) -> bool:
        """Push a frame; return True when the center frame is a transition.

        Note this is ~``_WINDOW/2`` frames latent (the model needs lookahead),
        so for low-latency use prefer the histogram detector. Returns False
        until the window fills.
        """
        if self._session is None:
            return False
        small = _resize_to(rgb_u8, _FRAME_H, _FRAME_W).astype(np.uint8)
        self._window.append(small)
        if len(self._window) < _WINDOW:
            return False
        try:
            # TODO/ASSUMPTION: input (1, 100, 27, 48, 3) uint8; output a
            # per-frame logit/prob vector. Confirm against your export.
            batch = np.stack(self._window, axis=0)[None].astype(np.uint8)
            out = self._session.run([self._output_name], {self._input_name: batch})[0]
            prob = np.asarray(out).reshape(-1)
            center = prob[min(_CENTER, prob.size - 1)]
            if center < 0.0 or center > 1.0:
                center = 1.0 / (1.0 + np.exp(-center))
            return bool(center >= self.threshold)
        except Exception as exc:  # pragma: no cover - env dependent
            logger.warning("TransNetV2: inference failed (%s); disabling.", exc)
            self._session = None
            return False
