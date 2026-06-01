# vapoursynth_sink.vpy -- ambient perception tap for mpv-upscale-2x_animejanai.
#
# UNTESTED -- needs your GPU / VapourSynth / models. See the README banner.
#
# VERIFIED against the real animejanai release layout:
#   * mpv loads a profile via  vf=vapoursynth="...animejanai/profiles/<name>.vpy"
#     (portable_config/mpv.conf), which injects the globals `video_in` and
#     `container_fps`.
#   * Each profile is tiny, e.g. animejanai_balanced.vpy:
#         import animejanai_core
#         animejanai_core.run_animejanai_with_keybinding(video_in, container_fps, 1002)
#   * run_animejanai converts the source with matrix_in_s = "170m" if height<720
#     else "709"  (animejanai_core.py). We mirror that for our RGB tap.
#
# WHAT THIS DOES
#   Taps a downscaled (~512px) RGB copy of the SOURCE clip (pre-upscale) into the
#   MAIN frame path via std.ModifyFrame, so the tap is pulled exactly once per
#   displayed frame, in sync, WITHOUT altering the clip that gets upscaled. Each
#   tapped frame is handed to a NON-BLOCKING background runner that does the
#   perception + HyperHDR feed off-thread (so the upscale pipeline never stalls).
#
# HDR CAVEAT: mpv tone-maps AFTER VapourSynth. For SDR anime the tap is correct.
#   For HDR (PQ/HLG) sources, insert an approximate tonemap on the tap before
#   sampling (see README "HDR tone-map caveat").
#
# ------------------------------------------------------------------- install --
# 1. Point AMBIENT_PERCEPTION_DIR at the folder that CONTAINS the
#    `ambient_perception` package (this repo's contrib/ambient-perception), or
#    rely on the default below if you copy this package next to your profiles.
# 2. Copy config.example.yaml -> config.yaml, set your HyperHDR host/port and
#    (optionally) model paths, and point AMBIENT_PERCEPTION_CONFIG at it.
# 3. Either:
#    (A) Save this file as animejanai/profiles/animejanai_ambient.vpy and add an
#        mpv.conf profile:
#            [upscale-on-ambient]
#            vf=vapoursynth="~~/../animejanai/profiles/animejanai_ambient.vpy":buffered-frames=1:concurrent-frames=8
#        then bind a key to `apply-profile upscale-on-ambient` in input.conf; or
#    (B) In an existing profile .vpy, import attach_ambient() from here and wrap
#        video_in before calling run_animejanai_with_keybinding (see bottom).

import os
import sys

import vapoursynth as vs

core = vs.core

# --- locate the ambient_perception package + config -------------------------
_DEFAULT_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG_DIR = os.environ.get("AMBIENT_PERCEPTION_DIR", _DEFAULT_PKG_DIR)
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

_CONFIG = os.environ.get(
    "AMBIENT_PERCEPTION_CONFIG", os.path.join(_PKG_DIR, "config.example.yaml")
)
# Keybinding/profile id forwarded to animejanai_core (1002 == "balanced" in the
# stock release). Override per profile via the env var if you wish.
_KEYBINDING = int(os.environ.get("AMBIENT_KEYBINDING", "1002"))

# Downscale target for the perception tap (longest side). Smaller = cheaper.
_DOWNSCALE = 512

import numpy as np  # noqa: E402
from ambient_perception.service import get_async_runner  # noqa: E402


def _frame_to_numpy(frame) -> "np.ndarray":
    """Planar RGB24 VapourSynth frame -> (H, W, 3) uint8 array.

    VapourSynth RGB is PLANAR. TODO/ASSUMPTION: plane order is treated as R,G,B;
    if your lights come out with red/blue swapped, reverse the plane order here.
    """
    planes = [np.asarray(frame[i]) for i in range(frame.format.num_planes)]
    return np.stack(planes, axis=-1).astype(np.uint8)


def attach_ambient(clip: vs.VideoNode, source_clip: vs.VideoNode | None = None,
                   config_path: str = _CONFIG, fps: float | None = None) -> vs.VideoNode:
    """Return ``clip`` UNCHANGED, with an ambient-perception side-effect attached.

    The tap is spliced into ``clip``'s frame path (std.ModifyFrame), so it is
    evaluated once per displayed frame, in order, but the returned frames are
    ``clip``'s own frames -- the upscale path downstream is byte-identical.

    Parameters
    ----------
    clip:          the clip you continue to upscale/output (returned unchanged).
    source_clip:   what to perceive from (defaults to ``clip``); pass the SOURCE
                   pre-upscale clip so perception stays cheap.
    config_path:   YAML config path (HyperHDR host/port, models, tau, ...).
    fps:           frames-per-second for PTS (use mpv's ``container_fps``).
    """
    src = source_clip if source_clip is not None else clip

    # Mirror animejanai_core's source colorspace heuristic.
    colorspace = "170m" if src.height < 720 else "709"

    longest = max(src.width, src.height) or 1
    scale = min(1.0, _DOWNSCALE / float(longest))
    tw = max(2, int(round(src.width * scale)) & ~1)
    th = max(2, int(round(src.height * scale)) & ~1)
    tap = core.resize.Bilinear(src, width=tw, height=th, format=vs.RGB24,
                               matrix_in_s=colorspace)

    runner = get_async_runner(config_path)

    if fps is None:
        fps = (src.fps_num / src.fps_den) if src.fps_den else 24.0
    fps = float(fps) or 24.0

    def _selector(n: int, f):
        # f == [clip_frame, tap_frame]. Cheap copy + non-blocking submit; the
        # heavy inference + HyperHDR send happen on the runner's worker thread.
        try:
            runner.submit(_frame_to_numpy(f[1]), pts_ms=(n / fps) * 1000.0)
        except Exception as exc:  # never break playback for the sink
            core.log_message(vs.MESSAGE_TYPE_WARNING, f"ambient-perception: {exc!r}")
        return f[0]

    return clip.std.ModifyFrame(clips=[clip, tap], selector=_selector)


# --- profile entry point ----------------------------------------------------
# When mpv loads this file as a vf=vapoursynth profile it injects `video_in` and
# `container_fps`. Importing this module elsewhere (e.g. tests) skips this block.
if "video_in" in globals():
    import animejanai_core  # provided on sys.path by the animejanai install

    tapped = attach_ambient(video_in, fps=float(container_fps))  # noqa: F821
    animejanai_core.run_animejanai_with_keybinding(
        tapped, container_fps, _KEYBINDING  # noqa: F821
    )

# --- alternative: splice into your OWN profile .vpy -------------------------
#   import animejanai_core
#   from animejanai.profiles.animejanai_ambient import attach_ambient  # or sys.path
#   animejanai_core.run_animejanai_with_keybinding(
#       attach_ambient(video_in, fps=float(container_fps)), container_fps, 1002)
