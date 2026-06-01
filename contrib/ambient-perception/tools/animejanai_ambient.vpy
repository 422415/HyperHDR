# Ready-to-use animejanai profile: streams the decoded frame to HyperHDR for
# ambient lighting WHILE still doing the normal 2x upscale.
#
# This is a complete mpv/VapourSynth profile -- point mpv at it (mpv.conf) or copy
# it into your animejanai\profiles folder. It does NOT alter the upscale: it taps a
# downscaled copy of the source and returns the source frame unchanged.
#
# Adjust the two marked lines if needed:
#   * _PKG_DIR  -> the folder that contains the "ambient_perception" package
#                  (your HyperHDR repo's contrib\ambient-perception).
#   * the 1002 on the last line -> the keybinding/profile id your normal profile
#                  uses (1002 == "balanced" in the stock release).
import sys
import numpy as np
import vapoursynth as vs

_PKG_DIR = r"C:\Users\42241\Documents\GitHub\HyperHDR\contrib\ambient-perception"   # <-- ADJUST IF YOUR REPO MOVES
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

from ambient_perception.service import get_async_runner

core = vs.core
_runner = get_async_runner()    # streams to HyperHDR: localhost:19400, full_frame (numpy only)

_cs = "170m" if video_in.height < 720 else "709"
_tw = min(512, video_in.width)
_th = max(2, round(video_in.height * _tw / video_in.width))
_tap = core.resize.Bilinear(video_in, width=_tw, height=_th, format=vs.RGB24, matrix_in_s=_cs)


def _ambient(n, f):
    tf = f[1]   # the downscaled RGB24 tap frame
    _runner.submit(
        np.stack([np.asarray(tf[i]) for i in range(3)], -1).astype(np.uint8),
        pts_ms=n * 1000.0 / float(container_fps or 24),
    )
    return f[0]  # source frame, unchanged -> upscale path is byte-identical


_video = video_in.std.ModifyFrame(clips=[video_in, _tap], selector=_ambient)

import animejanai_core
animejanai_core.run_animejanai_with_keybinding(_video, container_fps, 1002)   # <-- match your profile's id
