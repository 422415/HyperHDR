"""Temporal stabilization: dt-correct OKLab EMA + scene-cut snap.

Mirrors the C++ ``ImageToLedManager::applyAmbientTemporal`` and
``ambientSceneCutDistance`` (verified in
``sources/base/ImageToLedManager.cpp``).

Two cooperating parts:

  * :class:`SceneCutDetector` -- a coarse 8x8x8 RGB histogram L1 distance
    between consecutive frames. A spike (dist > k*median_recent, floored at
    0.02) that is CONFIRMED by a following calm frame triggers a one-shot
    snap, with a ~200ms lockout. Two consecutive spikes (a flash) do NOT
    snap; the EMA absorbs them.
  * :class:`AmbientEma` -- per-side EMA in OKLab with
    ``alpha = 1 - exp(-dt_ms / tau)`` (tau defaults to 650ms). On a confirmed
    cut, ``alpha = 1`` (instant snap) for that frame.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------------------
# Scene-cut constants (mirror ImageToLedManager.cpp).
# ---------------------------------------------------------------------------
_HIST_STEP = 16             # pixel stride when sampling the histogram
_RECENT_LEN = 32            # ring-buffer length for the running median
_MIN_RECENT = 10            # need this many samples before cut detection arms
_THRESHOLD_FLOOR = 0.02     # minimum cut threshold (range of dist is 0..2)
_LOCKOUT_MS = 200           # anti-strobe lockout after a confirmed cut
_DT_CLAMP_MS = (1, 250)     # dt is clamped to this range before the EMA


class SceneCutDetector:
    """Coarse-histogram scene-cut detector with flash rejection.

    Call :meth:`update` once per frame with the *downscaled* frame (the same
    image the estimator sees is fine). Returns True exactly on the frame where
    a confirmed cut should snap the EMA.
    """

    def __init__(self, sensitivity: float = 4.0) -> None:
        self.sensitivity = sensitivity
        self._prev_hist: np.ndarray | None = None
        self._recent = np.zeros(_RECENT_LEN, dtype=np.float64)
        self._recent_pos = 0
        self._recent_count = 0
        self._cut_pending = False
        self._lockout_until_ms = 0.0

    def reset(self) -> None:
        self._prev_hist = None
        self._recent[:] = 0.0
        self._recent_pos = 0
        self._recent_count = 0
        self._cut_pending = False
        self._lockout_until_ms = 0.0

    def _distance(self, rgb_u8: np.ndarray) -> float:
        """8x8x8 RGB histogram L1 distance vs the previous frame (0..2).

        Mirrors ambientSceneCutDistance: bin index is
        ``((r>>5)<<6) | ((g>>5)<<3) | (b>>5)`` over a strided sample.
        """
        h, w, _ = rgb_u8.shape
        if h == 0 or w == 0:
            return 0.0
        sub = rgb_u8[::_HIST_STEP, ::_HIST_STEP, :].reshape(-1, 3)
        r = (sub[:, 0] >> 5).astype(np.int64)
        g = (sub[:, 1] >> 5).astype(np.int64)
        b = (sub[:, 2] >> 5).astype(np.int64)
        bins = (r << 6) | (g << 3) | b
        hist = np.bincount(bins, minlength=512).astype(np.float64)
        count = hist.sum()
        if count == 0:
            return 0.0
        hist /= count

        if self._prev_hist is None:
            self._prev_hist = hist
            return 0.0
        distance = float(np.abs(hist - self._prev_hist).sum())
        self._prev_hist = hist
        return distance

    def update(self, rgb_u8: np.ndarray, now_ms: float) -> bool:
        """Return True if this frame is a confirmed scene cut (snap now)."""
        dist = self._distance(rgb_u8)
        scene_cut = False

        if self._recent_count >= _MIN_RECENT:
            n = min(self._recent_count, _RECENT_LEN)
            median = float(np.partition(self._recent[:n], n // 2)[n // 2])
            threshold = max(_THRESHOLD_FLOOR, median * self.sensitivity)

            if now_ms >= self._lockout_until_ms:
                if self._cut_pending:
                    if dist <= threshold:
                        # spike followed by a calm frame => real cut
                        scene_cut = True
                        self._lockout_until_ms = now_ms + _LOCKOUT_MS
                    self._cut_pending = False  # confirmed, or it was a flash
                elif dist > threshold:
                    self._cut_pending = True   # candidate; confirm next frame

        # Record this frame's distance (baseline for the median).
        self._recent[self._recent_pos] = dist
        self._recent_pos = (self._recent_pos + 1) % _RECENT_LEN
        if self._recent_count < _RECENT_LEN:
            self._recent_count += 1

        return scene_cut


@dataclass
class AmbientEma:
    """Per-side dt-correct OKLab EMA with cut-snap.

    ``tau_ms`` is the settling time constant (C++ ``ambientSettlingMs``,
    default 650). State is one OKLab triple per side, keyed by side name.
    """

    tau_ms: float = 650.0
    _state: dict[str, np.ndarray] = field(default_factory=dict)
    _last_ts_ms: float = 0.0
    _seeded: bool = False

    def reset(self) -> None:
        self._state.clear()
        self._seeded = False
        self._last_ts_ms = 0.0

    def step(
        self,
        raw_oklab: dict[str, np.ndarray],
        now_ms: float,
        scene_cut: bool,
    ) -> dict[str, np.ndarray]:
        """Advance the EMA one frame.

        Parameters
        ----------
        raw_oklab:
            ``{side: oklab(3,)}`` raw estimates for this frame. A side whose
            value is None is held at its previous state (e.g. starved zone).
        now_ms:
            Monotonic timestamp in milliseconds (frame PTS works too).
        scene_cut:
            If True, snap (alpha = 1) this frame.

        Returns the smoothed ``{side: oklab(3,)}``.
        """
        # Seed on the first valid frame: emit the raw estimate as-is.
        if not self._seeded:
            for side, lab in raw_oklab.items():
                if lab is not None:
                    self._state[side] = np.asarray(lab, dtype=np.float64).copy()
            if self._state:
                self._seeded = True
                self._last_ts_ms = now_ms
            return {s: self._state.get(s) for s in raw_oklab}

        if scene_cut:
            alpha = 1.0
        else:
            dt = float(np.clip(now_ms - self._last_ts_ms, *_DT_CLAMP_MS))
            tau = max(1.0, self.tau_ms)
            alpha = 1.0 - np.exp(-dt / tau)
        self._last_ts_ms = now_ms

        out: dict[str, np.ndarray] = {}
        for side, lab in raw_oklab.items():
            if lab is None:
                # Hold previous state (full-frame close-up / low confidence).
                out[side] = self._state.get(side)
                continue
            raw = np.asarray(lab, dtype=np.float64)
            if side not in self._state:
                self._state[side] = raw.copy()
            else:
                state = self._state[side]
                self._state[side] = state + (raw - state) * alpha
            out[side] = self._state[side]
        return out
