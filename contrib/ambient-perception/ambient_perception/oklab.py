"""OKLab color math, mirroring HyperHDR's C++ implementation.

This is a small, pure-numpy port of the exact functions used by the
``advanced_ambient`` C++ mode so the perception service produces colors that
are bit-for-bit comparable (within float precision) to the baseline.

Sources mirrored (verified in the HyperHDR repo):
  * ``sources/infinite-color-engine/ColorSpace.cpp``
      - ``linear_rgb_to_oklab`` / ``oklab_to_linear_rgb`` (Bjorn Ottosson)
      - ``clamp_oklab_chroma_to_gamut`` (5-iteration bisection)
      - ``srgb_nonlinear_to_linear`` (sRGB EOTF)
  * ``include/infinite-color-engine/ColorSpace.h``
      - the M1 / M2 / inverse matrices (standard Ottosson constants)

Everything here is vectorized so an ``(H, W, 3)`` or ``(N, 3)`` array of pixels
can be converted in one call. Inputs/outputs are linear-light RGB in [0, 1]
unless a function name says otherwise.
"""
from __future__ import annotations

import numpy as np

# ---------------------------------------------------------------------------
# Matrices (standard Bjorn Ottosson constants; identical to ColorSpace.h).
# Stored row-major exactly as the C++ matrixF({...}) initializers.
# ---------------------------------------------------------------------------
_OKLAB_M1 = np.array(
    [
        [+0.4122214708, +0.5363325363, +0.0514459929],
        [+0.2119034982, +0.6806995451, +0.1073969566],
        [+0.0883024619, +0.2817188376, +0.6299787005],
    ],
    dtype=np.float64,
)

_OKLAB_M2 = np.array(
    [
        [+0.2104542553, +0.7936177850, -0.0040720468],
        [+1.9779984951, -2.4285922050, +0.4505937099],
        [+0.0259040371, +0.7827717662, -0.8086757660],
    ],
    dtype=np.float64,
)

_OKLAB_INV_M2 = np.array(
    [
        [+1.0, +0.3963377774, +0.2158037573],
        [+1.0, -0.1055613458, -0.0638541728],
        [+1.0, -0.0894841775, -1.2914855480],
    ],
    dtype=np.float64,
)

_OKLAB_INV_M1 = np.array(
    [
        [+4.0767416621, -3.3077115913, +0.2309699292],
        [-1.2684380046, +2.6097574011, -0.3413193965],
        [-0.0228834462, -0.7034186147, +1.7263020608],
    ],
    dtype=np.float64,
)

# Rec.709 / sRGB luminance weights (used for both linear and nonlinear luma in
# the C++; see calcAmbientForLeds).
REC709 = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)

# sRGB transfer-function constants, identical to ColorSpace.cpp.
_SRGB_ALPHA = 1.055010718947587
_SRGB_BETA = 0.003041282560128


def _apply_matrix(rgb: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Apply a 3x3 row-major matrix to the last axis of ``rgb``.

    Works for any leading shape: (..., 3) -> (..., 3).
    """
    return np.einsum("ij,...j->...i", matrix, rgb)


def srgb_nonlinear_to_linear(rgb: np.ndarray) -> np.ndarray:
    """sRGB EOTF: nonlinear (gamma) [0,1] -> linear-light [0,1].

    Mirrors ColorSpace.cpp::srgb_nonlinear_to_linear. The C++ does this via a
    65535-scaled integer LUT; numerically it is the standard sRGB curve below.
    """
    x = np.maximum(np.asarray(rgb, dtype=np.float64), 0.0)
    low = x / 12.92
    high = np.power((x + (_SRGB_ALPHA - 1.0)) / _SRGB_ALPHA, 2.4)
    return np.where(x < 12.92 * _SRGB_BETA, low, high)


def srgb_linear_to_nonlinear(rgb: np.ndarray) -> np.ndarray:
    """Inverse sRGB EOTF: linear-light [0,1] -> nonlinear (gamma) [0,1]."""
    x = np.maximum(np.asarray(rgb, dtype=np.float64), 0.0)
    low = x * 12.92
    high = _SRGB_ALPHA * np.power(x, 1.0 / 2.4) - (_SRGB_ALPHA - 1.0)
    return np.where(x < _SRGB_BETA, low, high)


def linear_rgb_to_oklab(rgb: np.ndarray) -> np.ndarray:
    """Linear-light sRGB -> OKLab (L, a, b). Vectorized over (..., 3)."""
    lms = _apply_matrix(np.asarray(rgb, dtype=np.float64), _OKLAB_M1)
    # cbrt that is correct for the (rare) negative lobe, matching ufast_cbrt's
    # sign behaviour. np.cbrt handles negatives correctly.
    lms_cbrt = np.cbrt(lms)
    return _apply_matrix(lms_cbrt, _OKLAB_M2)


def oklab_to_linear_rgb(lab: np.ndarray) -> np.ndarray:
    """OKLab (L, a, b) -> linear-light sRGB. Vectorized over (..., 3)."""
    lms_cbrt = _apply_matrix(np.asarray(lab, dtype=np.float64), _OKLAB_INV_M2)
    lms = lms_cbrt ** 3
    return _apply_matrix(lms, _OKLAB_INV_M1)


def clamp_oklab_chroma_to_gamut(oklab: np.ndarray) -> np.ndarray:
    """Reduce chroma (keeping L and hue) until the color is inside [0,1] RGB.

    Mirrors ColorSpace.cpp::clamp_oklab_chroma_to_gamut: a 5-iteration
    bisection on chroma magnitude per pixel. Operates on a single (3,) OKLab
    triple or a batch (N, 3).
    """
    lab = np.atleast_2d(np.asarray(oklab, dtype=np.float64))
    out = lab.copy()

    L = lab[:, 0]
    a = lab[:, 1]
    b = lab[:, 2]
    chroma = np.sqrt(a * a + b * b)

    # Pixels that are already (near) grey are in gamut; leave them untouched.
    active = chroma >= 1e-5
    if np.any(active):
        dir_a = np.zeros_like(chroma)
        dir_b = np.zeros_like(chroma)
        dir_a[active] = a[active] / chroma[active]
        dir_b[active] = b[active] / chroma[active]

        lo = np.zeros_like(chroma)
        hi = chroma.copy()
        for _ in range(5):  # 5 iterations, identical to the C++
            mid = 0.5 * (lo + hi)
            test = np.stack([L, dir_a * mid, dir_b * mid], axis=-1)
            rgb = oklab_to_linear_rgb(test)
            out_of_gamut = np.any((rgb < 0.0) | (rgb > 1.0), axis=-1)
            hi = np.where(out_of_gamut, mid, hi)
            lo = np.where(out_of_gamut, lo, mid)

        c_max = lo
        out[active, 1] = dir_a[active] * c_max[active]
        out[active, 2] = dir_b[active] * c_max[active]

    return out.reshape(np.asarray(oklab).shape)


def srgb_u8_to_linear(rgb_u8: np.ndarray) -> np.ndarray:
    """Convenience: uint8 sRGB pixels (..., 3) -> linear-light float [0,1]."""
    return srgb_nonlinear_to_linear(np.asarray(rgb_u8, dtype=np.float64) / 255.0)


def linear_to_srgb_u8(rgb_linear: np.ndarray) -> np.ndarray:
    """Convenience: linear-light float (..., 3) -> uint8 sRGB pixels."""
    nl = srgb_linear_to_nonlinear(np.clip(rgb_linear, 0.0, 1.0))
    return np.clip(np.round(nl * 255.0), 0, 255).astype(np.uint8)
