"""Behavior tests for the ambient-perception color math.

Runnable two ways::

    python -m pytest contrib/ambient-perception/tests
    python contrib/ambient-perception/tests/test_ambient.py   # plain runner

These assert the *promises* of the design, not just that code runs:
  - OKLab round-trips within HyperHDR's (rounded-matrix) tolerance.
  - A small vivid RED blob over a muted GREEN background yields a GENTLE GREEN
    ambient color (not red, not neon) -- the headline "clothing vs background"
    and "gentle not neon" guarantees.
  - Segmentation (excluding the blob) only strengthens that result.
  - A background-starved zone (full-frame foreground) reports starvation so the
    service falls back instead of washing grey.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ambient_perception import ambient, oklab  # noqa: E402


def _muted_green_with_red_blob(h=36, w=64, blob=10):
    """Mostly muted-green frame with a small saturated-red square in the center."""
    frame = np.empty((h, w, 3), dtype=np.uint8)
    frame[:] = (60, 110, 70)            # desaturated green background
    cy, cx = h // 2, w // 2
    frame[cy - blob // 2:cy + blob // 2, cx - blob // 2:cx + blob // 2] = (255, 0, 0)
    return frame


def test_oklab_roundtrip():
    rng = np.random.default_rng(0)
    lin = rng.random((500, 3))
    back = oklab.oklab_to_linear_rgb(oklab.linear_rgb_to_oklab(lin))
    err = np.max(np.abs(back - lin))
    # HyperHDR's published inverse matrices are rounded (~0.01 round-trip error);
    # we reproduce that exactly, so allow a small tolerance.
    assert err < 0.02, f"OKLab round-trip error too large: {err}"


def test_oklab_green_is_negative_a():
    # Sanity: OKLab 'a' is the green(-)/red(+) axis.
    assert oklab.linear_rgb_to_oklab(np.array([0.0, 1.0, 0.0]))[1] < 0.0
    assert oklab.linear_rgb_to_oklab(np.array([1.0, 0.0, 0.0]))[1] > 0.0


def test_background_beats_foreground_blob():
    frame = _muted_green_with_red_blob()
    res = ambient.estimate_side(frame, side="left")
    assert res.oklab is not None
    a = res.oklab[1]
    assert a < 0.0, f"expected a green tint (a<0), got a={a}"
    # Gentle, not neon: chroma at or below the ceiling (default Cmax=0.06).
    chroma = float(np.hypot(res.oklab[1], res.oklab[2]))
    assert chroma <= ambient.AmbientConfig().chroma_max + 1e-6, f"chroma {chroma} exceeds Cmax"
    # Emitted color reads green: g channel dominates.
    rgb = oklab.linear_to_srgb_u8(res.linear_rgb)
    assert rgb[1] > rgb[0] and rgb[1] >= rgb[2], f"emitted color not green-ish: {tuple(rgb)}"


def test_segmentation_only_helps():
    frame = _muted_green_with_red_blob()
    h, w, _ = frame.shape
    a_noseg = ambient.estimate_side(frame, side="left").oklab[1]

    # Foreground mask = 1 on the red blob, 0 elsewhere -> bgConfidence excludes it.
    fg = np.zeros((h, w), dtype=np.float64)
    cy, cx = h // 2, w // 2
    fg[cy - 5:cy + 5, cx - 5:cx + 5] = 1.0
    a_seg = ambient.estimate_side(frame, side="left", bg_confidence=1.0 - fg).oklab[1]

    # Removing the red foreground must not make the result LESS green.
    assert a_seg <= a_noseg + 1e-9, f"segmentation made it redder: {a_seg} > {a_noseg}"


def test_background_starved_fallback():
    frame = _muted_green_with_red_blob()
    h, w, _ = frame.shape
    # Foreground everywhere (full-frame close-up): no background survives.
    res = ambient.estimate_side(frame, side="left", bg_confidence=np.zeros((h, w)))
    assert res.oklab is None and res.background_starved, "should report background starvation"
    # The service's fallback recovers a usable color.
    fb = ambient.whole_frame_oklab(frame, side="left")
    assert fb.oklab is not None


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\nAll {len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
