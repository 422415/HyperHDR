# Ready-to-use animejanai profile: streams the decoded frame to HyperHDR for
# ambient lighting WHILE still doing the normal 2x upscale.
#
# This version sends SYNCHRONOUSLY (no background thread -- more reliable inside
# mpv's VapourSynth worker context) and writes a small diagnostic log to
# C:\ambient_debug.log so we can see what's happening if no color appears.
#
# Drop this file next to your other animejanai profiles. Adjust the two marked
# lines if needed: _PKG_DIR (your repo's contrib\ambient-perception), and the
# 1002 on the last line (the upscale id your normal profile uses).
import sys
import time
import traceback

import numpy as np
import vapoursynth as vs

_PKG_DIR = r"C:\Users\42241\Documents\GitHub\HyperHDR\contrib\ambient-perception"   # <-- ADJUST IF YOUR REPO MOVES
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

from ambient_perception.hyperhdr_client import HyperHDRFlatClient

_LOG = r"C:\ambient_debug.log"


def _log(msg):
    try:
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except Exception:
        pass


core = vs.core
_state = {"client": None, "calls": 0, "sent": 0, "last_err": ""}

_cs = "170m" if video_in.height < 720 else "709"
_tw = min(256, video_in.width)                     # small = cheap; plenty for LED mapping
_th = max(2, round(video_in.height * _tw / video_in.width))
_tap = core.resize.Bilinear(video_in, width=_tw, height=_th, format=vs.RGB24, matrix_in_s=_cs)
_log(f"=== profile loaded; video_in {video_in.width}x{video_in.height} -> tap {_tw}x{_th} cs={_cs} ===")


def _ambient(n, f):
    _state["calls"] += 1
    try:
        if _state["client"] is None:
            c = HyperHDRFlatClient("127.0.0.1", 19400, priority=150, origin="AmbientMpv")
            c.connect()
            _state["client"] = c
            _log("connected to HyperHDR 127.0.0.1:19400")
        tf = f[1]
        arr = np.stack([np.asarray(tf[i]) for i in range(3)], axis=-1).astype(np.uint8)
        _state["client"].send_image(np.ascontiguousarray(arr).tobytes(), _tw, _th)
        _state["sent"] += 1
        if _state["sent"] == 1 or _state["sent"] % 120 == 0:
            _log(f"sent {_state['sent']} frames (selector calls={_state['calls']})")
    except Exception as e:
        if repr(e) != _state["last_err"]:
            _state["last_err"] = repr(e)
            _log("ERROR: " + repr(e) + "\n" + traceback.format_exc())
        _state["client"] = None   # force reconnect next frame
    return f[0]


_video = video_in.std.ModifyFrame(clips=[video_in, _tap], selector=_ambient)

import animejanai_core
animejanai_core.run_animejanai_with_keybinding(_video, container_fps, 1002)   # <-- match your profile's id
