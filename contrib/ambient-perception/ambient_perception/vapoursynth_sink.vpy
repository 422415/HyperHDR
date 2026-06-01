# vapoursynth_sink.vpy -- integrate ambient perception into the animejanai
# VapourSynth chain.
#
# UNTESTED -- needs your GPU / VapourSynth / models. See README banner.
#
# WHAT THIS DOES
#   Branches a downscaled (~512px) copy of the SOURCE clip (PRE-upscale), runs
#   the ambient-perception service on each frame keyed by PTS, and streams the
#   two side colors to HyperHDR over FlatBuffers. The MAIN clip is returned
#   UNCHANGED so your normal animejanai upscale path is untouched.
#
# HOW TO WIRE IT IN
#   In mpv-upscale-2x_animejanai the upscale graph is defined in a .vpy that
#   animejanai loads. Two integration options:
#
#   (A) Inline (this file): paste the `attach_ambient(clip, src)` call into your
#       existing .vpy right after you have the decoded source `clip`, BEFORE the
#       vs-mlrt / TensorRT upscale node. Return the upscaled clip as usual.
#
#   (B) Companion process (no .vpy edit): run a separate process that decodes
#       frames with its own onnxruntime-gpu pipeline and calls
#       AmbientService.process_frame(). Use this if you don't want to touch the
#       animejanai chain. See README ("Companion-process design").
#
# IMPORTANT CAVEAT (HDR):
#   mpv applies HDR tone-mapping AFTER VapourSynth. For SDR content the frames
#   here are already display-referred and correct. For HDR content these frames
#   are scene-referred (PQ/HLG) and will look wrong to the SDR ambient math.
#   See README ("HDR tone-map caveat") for the approximation / framebuffer-tap
#   options. The downscaled branch below requests RGB24; for HDR you must first
#   apply an approximate tonemap (e.g. core.placebo.Tonemap or a fixed Hable
#   curve) on the branch so the colors match what you see.

import os
import vapoursynth as vs

core = vs.core

# --- locate the package + config -------------------------------------------
# Adjust these two paths for your install.
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))          # this folder
_REPO_DIR = os.path.dirname(os.path.dirname(_PKG_DIR))         # ...contrib/ambient-perception
_CONFIG = os.path.join(_REPO_DIR, "config.example.yaml")       # copy to config.yaml & edit

import sys
if _REPO_DIR not in sys.path:
    sys.path.insert(0, _REPO_DIR)

import numpy as np
from ambient_perception.service import get_service


# Downscale target for the perception branch (longest side). Smaller = faster.
_DOWNSCALE = 512


def _frame_to_numpy(frame) -> "np.ndarray":
    """Convert a planar RGB24 VapourSynth frame to an (H, W, 3) uint8 array.

    VapourSynth RGB frames are PLANAR (one plane per channel). We stack them
    into interleaved HxWx3. Plane order for vs.RGB is R, G, B.
    """
    planes = [np.asarray(frame[i]) for i in range(frame.format.num_planes)]
    return np.stack(planes, axis=-1).astype(np.uint8)


def attach_ambient(main_clip: vs.VideoNode, source_clip: vs.VideoNode | None = None,
                   config_path: str = _CONFIG) -> vs.VideoNode:
    """Attach the ambient sink to ``main_clip`` and return ``main_clip``.

    Parameters
    ----------
    main_clip:
        The clip you will continue to upscale and output. Returned unchanged.
    source_clip:
        The clip to perceive from (defaults to ``main_clip``). Pass the SOURCE
        (pre-upscale) clip so perception runs at low res and stays cheap.
    config_path:
        Path to the YAML config (HyperHDR host/port, model paths, tau, etc.).
    """
    src = source_clip if source_clip is not None else main_clip

    # Downscale + convert to RGB24 for the perception branch only.
    longest = max(src.width, src.height)
    scale = min(1.0, _DOWNSCALE / float(longest)) if longest else 1.0
    tw = max(2, int(round(src.width * scale)) & ~1)
    th = max(2, int(round(src.height * scale)) & ~1)

    # TODO/ASSUMPTION: matrix_in_s depends on your source. "709" is correct for
    # most modern anime; SD content may be "470bg"/"170m". For HDR, insert your
    # tonemap BEFORE this point (see the module header caveat).
    branch = core.resize.Bilinear(
        src, width=tw, height=th, format=vs.RGB24, matrix_in_s="709",
    )

    def _eval(n: int, f: vs.VideoFrame):
        # Runs on VapourSynth's worker thread per frame. We compute PTS from
        # the frame number and the clip fps so dt-correctness holds even when
        # rendered faster/slower than realtime.
        fps = (branch.fps_num / branch.fps_den) if branch.fps_den else 24.0
        pts_ms = (n / fps) * 1000.0 if fps else float(n) * 41.7
        try:
            svc = get_service(config_path)
            rgb = _frame_to_numpy(f)
            svc.process_frame(rgb, pts_ms=pts_ms)
        except Exception as exc:  # never break playback because of the sink
            vs.core.log_message(vs.MESSAGE_TYPE_WARNING,
                                f"ambient-perception: {exc!r}")
        return f  # passthrough; we only use the branch for its side effect

    # FrameEval-style sink: we modify the BRANCH (side-effect only) but DISCARD
    # it; the function returns `main_clip` so the upscale path is unaffected.
    _ = core.std.ModifyFrame(branch, clips=branch, selector=_eval)
    # NOTE: ModifyFrame is lazy -- frames are only produced when something pulls
    # them. To guarantee the sink runs in lockstep with playback, splice the
    # side-effect into the OUTPUT clip via FrameEval keyed on the same n:

    sink = core.std.ModifyFrame(branch, clips=branch, selector=_eval)

    def _passthrough(n: int):
        # Force the sink frame to be realized for frame n, then return main.
        sink.get_frame(n)
        return main_clip

    return core.std.FrameEval(main_clip, _passthrough)


# ---------------------------------------------------------------------------
# Example usage inside your animejanai .vpy:
#
#   clip = video_in                      # decoded source from mpv
#   clip = attach_ambient(clip)          # <-- add this line (perceives source)
#   clip = upscale_with_mlrt(clip)       # your existing vs-mlrt TensorRT 2x
#   clip.set_output()
#
# If you keep separate handles to the source and upscaled clips:
#
#   upscaled = upscale_with_mlrt(src)
#   upscaled = attach_ambient(upscaled, source_clip=src)
#   upscaled.set_output()
# ---------------------------------------------------------------------------
