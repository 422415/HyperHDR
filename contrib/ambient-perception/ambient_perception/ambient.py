"""Per-side ambient color estimator.

This mirrors the C++ ``ImageColorAveraging::calcAmbientForLeds`` (verified in
``sources/base/ImageColorAveraging.cpp``) and EXTENDS it with the perception
weight ``bgConfidence = (1 - foregroundMask)`` and an optional far-depth
weight, exactly as described in the algorithm spec.

The two-pass structure is identical to the C++:

  Pass A  luminance-weighted OKLab mean + chroma variance (for the sigma clip).
  Pass B  re-accumulate with a Tukey soft sigma-clip on chroma.
  Shape   soft chroma deadzone + ceiling, L carried untouched.

Weighting:
    w = linearLuma * visible * bgConfidence   (* farDepth)
  where the C++ baseline uses ``linearLuma * visible`` only; bgConfidence and
  farDepth are the premium perception additions and collapse to 1.0 when no
  model is loaded, recovering the exact baseline behaviour.

Unlike the C++ (which iterates a flat list of per-LED pixel offsets), this
module works on a rectangular sub-image (the left or right region of the
downscaled frame) plus matching per-pixel masks. The math is the same.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import oklab

# ---------------------------------------------------------------------------
# Tuning constants (defaults mirror ImageColorAveraging.cpp / the schema).
# ---------------------------------------------------------------------------
AMBIENT_CDEAD = 0.012      # OKLab chroma deadzone ("tints weaker than this -> neutral")
AMBIENT_SIGMA_K = 2.5      # soft sigma-clip radius (chroma std-devs)

# Visibility ramp constants (calcAmbientForLeds): clamp((luma-0.025)/0.18,0,1).
_VISIBLE_BLACK = 0.025
_VISIBLE_SPAN = 0.18


@dataclass
class AmbientConfig:
    """Knobs that map 1:1 onto the C++ ambient schema fields."""

    chroma_dead: float = AMBIENT_CDEAD          # Cdead
    chroma_max: float = 0.06                     # Cmax / "ambientTintStrength"
    sigma_k: float = AMBIENT_SIGMA_K
    # Below this surviving background weight (sum of w), treat the zone as a
    # full-frame close-up and let the caller decide on a fallback.
    min_weight: float = 1e-6


@dataclass
class AmbientResult:
    """Result for one side."""

    # OKLab estimate BEFORE temporal smoothing (the "raw" estimate the EMA
    # consumes). None when the zone carried no light at all.
    oklab: np.ndarray | None
    # Linear-RGB version of the same estimate (clamped to gamut + [0,1]).
    linear_rgb: np.ndarray | None
    # Total background weight that survived (diagnostic / fallback trigger).
    weight: float = 0.0
    # True when bgConfidence wiped out essentially everything (full-frame
    # close-up): the caller should fall back to a whole-frame estimate / hold.
    background_starved: bool = False


def _luma_and_visibility(rgb_u8: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (linear_rgb, linearLuma, visible) for an (H,W,3) uint8 region.

    Matches the C++ exactly: ``luma`` (for visibility) is computed on the
    NONLINEAR [0,1] values, while ``linearLuma`` (the weight) is computed on
    the linearized values.
    """
    nonlinear01 = rgb_u8.astype(np.float64) / 255.0
    luma = nonlinear01 @ oklab.REC709
    visible = np.clip((luma - _VISIBLE_BLACK) / _VISIBLE_SPAN, 0.0, 1.0)

    linear = oklab.srgb_nonlinear_to_linear(nonlinear01)
    linear_luma = linear @ oklab.REC709
    return linear, linear_luma, visible


def estimate_side(
    rgb_u8: np.ndarray,
    *,
    side: str,
    bg_confidence: np.ndarray | None = None,
    far_weight: np.ndarray | None = None,
    cfg: AmbientConfig | None = None,
) -> AmbientResult:
    """Compute the ambient OKLab color for one side region.

    Parameters
    ----------
    rgb_u8:
        ``(H, W, 3)`` uint8 sRGB region for this side (already cropped to the
        left or right half / zone of the downscaled frame).
    side:
        Retained for API compatibility; no longer affects the estimate. The
        ambient color is a single whole-zone value (matches the simplified C++
        ``advanced_ambient``, which no longer applies an edge-falloff).
    bg_confidence:
        ``(H, W)`` float in [0,1] = ``1 - foregroundMask``. None => all 1.0
        (baseline behaviour / no segmentation model).
    far_weight:
        Optional ``(H, W)`` float in [0,1] far-depth weight. None => all 1.0.
    cfg:
        :class:`AmbientConfig`. Defaults mirror the C++.
    """
    cfg = cfg or AmbientConfig()
    h, w, _ = rgb_u8.shape
    if h == 0 or w == 0:
        return AmbientResult(None, None, 0.0, background_starved=True)

    linear, linear_luma, visible = _luma_and_visibility(rgb_u8)

    # Perception weights (collapse to 1.0 when no model is active).
    if bg_confidence is None:
        bg = np.ones((h, w), dtype=np.float64)
    else:
        bg = np.clip(bg_confidence.astype(np.float64), 0.0, 1.0)
    if far_weight is not None:
        bg = bg * np.clip(far_weight.astype(np.float64), 0.0, 1.0)

    base_weight = linear_luma * visible        # the C++ baseline weight
    weight_a = base_weight * bg                 # + perception (spec step 2)

    # If the perception masks removed nearly everything but the raw frame *did*
    # carry light, flag it so the caller can fall back instead of washing grey.
    raw_light = float(base_weight.sum())
    bg_light = float(weight_a.sum())
    background_starved = raw_light > cfg.min_weight and bg_light <= cfg.min_weight

    if bg_light <= cfg.min_weight:
        # Nothing survived: caller handles fallback (whole-frame / hold).
        return AmbientResult(None, None, bg_light, background_starved=background_starved)

    lab = oklab.linear_rgb_to_oklab(linear)     # (H, W, 3)
    a = lab[..., 1]
    b = lab[..., 2]

    # --- Pass A: weighted mean + chroma variance + horizontal centroid -----
    wsum = bg_light
    mean = (lab * weight_a[..., None]).reshape(-1, 3).sum(axis=0) / wsum
    mean_a, mean_b = mean[1], mean[2]
    chroma_sq = float((weight_a * (a * a + b * b)).sum())
    variance = max(0.0, chroma_sq / wsum - (mean_a * mean_a + mean_b * mean_b))
    sigma = np.sqrt(variance)
    inv_sigma = 1.0 / (cfg.sigma_k * sigma) if sigma > 1e-4 else 0.0

    # --- Pass B: Tukey soft sigma-clip on chroma ---------------------------
    if inv_sigma > 0.0:
        da = a - mean_a
        db = b - mean_b
        dist2 = (da * da + db * db) * (inv_sigma * inv_sigma)
        tukey = 1.0 / (1.0 + dist2)
    else:
        tukey = np.ones((h, w), dtype=np.float64)

    weight_b = weight_a * tukey
    wsum2 = float(weight_b.sum())
    if wsum2 > cfg.min_weight:
        out_lab = (lab * weight_b[..., None]).reshape(-1, 3).sum(axis=0) / wsum2
    else:
        out_lab = mean.copy()

    # --- Single chroma control: soft deadzone + ceiling --------------------
    chroma = float(np.hypot(out_lab[1], out_lab[2]))
    target_chroma = float(np.clip(chroma - cfg.chroma_dead, 0.0, cfg.chroma_max))
    chroma_scale = (target_chroma / chroma) if chroma > 1e-6 else 0.0
    out_lab = out_lab.copy()
    out_lab[1] *= chroma_scale
    out_lab[2] *= chroma_scale

    linear_out = oklab.oklab_to_linear_rgb(oklab.clamp_oklab_chroma_to_gamut(out_lab))
    linear_out = np.clip(linear_out, 0.0, 1.0)
    # Re-derive the smoothing-domain OKLab from the clamped RGB so the temporal
    # EMA operates on exactly the value we will emit (matches the C++ pipeline,
    # which smooths in OKLab of the clamped linear RGB).
    out_lab_clamped = oklab.linear_rgb_to_oklab(linear_out)

    return AmbientResult(
        oklab=out_lab_clamped,
        linear_rgb=linear_out,
        weight=bg_light,
        background_starved=False,
    )


def whole_frame_oklab(
    rgb_u8: np.ndarray,
    *,
    side: str,
    cfg: AmbientConfig | None = None,
) -> AmbientResult:
    """Fallback estimate ignoring perception (bgConfidence = 1 everywhere).

    Used when a side is background-starved (full-frame close-up) or when no
    segmentation model is loaded. This is exactly the C++ baseline behaviour.
    """
    return estimate_side(rgb_u8, side=side, bg_confidence=None, far_weight=None, cfg=cfg)
