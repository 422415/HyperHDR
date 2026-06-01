"""Standalone VapourSynth tap test -- run with your VapourSynth Python.

Loads (or synthesizes) a clip, converts to RGB, and streams each frame to
HyperHDR exactly like the animejanai sink will -- validating the
VS frame -> numpy -> FlatBuffers path in YOUR VapourSynth Python BEFORE wiring it
into mpv. If your lights follow the video, the VS side works and the only
remaining step is adding the tap to an animejanai profile.

    # use the SAME python that animejanai/VapourSynth uses
    python tools/test_vs_tap.py "C:/path/to/some_anime.mkv"
    python tools/test_vs_tap.py            # synthetic moving-hue clip if no file

Needs: vapoursynth + numpy (already present in an animejanai install). Streams to
127.0.0.1:19400 in full_frame mode (HyperHDR does the LED mapping). Ctrl-C to stop.
Watch your lights + LED Visualization "Live video" / "Color debug".
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import vapoursynth as vs

from ambient_perception.service import get_async_runner

core = vs.core
DOWNSCALE = 512


def _load(path: str | None) -> vs.VideoNode:
    if not path:
        import colorsys
        base = core.std.BlankClip(width=320, height=180, format=vs.RGB24,
                                  length=900, fpsnum=30, color=[0, 0, 0])

        def _fill(n, f):
            fo = f.copy()
            r, g, b = [int(v * 255) for v in colorsys.hsv_to_rgb((n / 100.0) % 1.0, 0.85, 1.0)]
            for p, v in enumerate((r, g, b)):
                np.asarray(fo[p])[:] = v
            return fo
        return core.std.ModifyFrame(base, base, _fill)

    for fn in ("lsmas.LWLibavSource", "bs.VideoSource", "ffms2.Source"):
        ns, name = fn.split(".")
        plug = getattr(core, ns, None)
        if plug is not None and hasattr(plug, name):
            print(f"Loading {path} via core.{fn} ...")
            return getattr(plug, name)(path)
    raise RuntimeError("No VapourSynth source filter found (need lsmas / bs / ffms2).")


def main() -> int:
    clip = _load(sys.argv[1] if len(sys.argv) > 1 else None)

    scale = min(1.0, DOWNSCALE / max(clip.width, clip.height))
    tw = max(2, int(clip.width * scale) & ~1)
    th = max(2, int(clip.height * scale) & ~1)
    if clip.format.color_family != vs.RGB:
        cs = "170m" if clip.height < 720 else "709"
        clip = core.resize.Bilinear(clip, width=tw, height=th, format=vs.RGB24, matrix_in_s=cs)
    else:
        clip = core.resize.Bilinear(clip, width=tw, height=th, format=vs.RGB24)

    fps = (clip.fps_num / clip.fps_den) if clip.fps_den else 30.0
    runner = get_async_runner()  # built-in defaults: full_frame -> 127.0.0.1:19400
    print(f"Streaming {clip.width}x{clip.height} @ {fps:.1f} fps to HyperHDR "
          f"({clip.num_frames} frames). Ctrl-C to stop.")

    try:
        for n in range(clip.num_frames):
            f = clip.get_frame(n)
            arr = np.stack([np.asarray(f[i]) for i in range(3)], axis=-1).astype(np.uint8)
            runner.submit(arr, pts_ms=n * 1000.0 / fps)
            time.sleep(max(0.0, 1.0 / fps))
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
