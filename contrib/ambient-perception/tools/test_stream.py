"""Standalone smoke test: stream a moving pattern to HyperHDR over FlatBuffers.

Proves the RECEIVING half works (HyperHDR + the "VapourSynth/mpv capture" toggle +
the flatbuffers client) WITHOUT VapourSynth/mpv, models, or numpy. If your lights
show a slowly cycling color, the feed path is good and only the mpv-side tap remains.

IMPORTANT: run this ON THE COMPUTER WHERE HYPERHDR RUNS (it streams to HyperHDR
over localhost:19400). If you run it elsewhere on the LAN, pass that machine's IP
with --host (HyperHDR's flatbuffers port must be reachable / not firewalled).

    cd contrib/ambient-perception
    python tools/test_stream.py                 # localhost
    python tools/test_stream.py --host 192.168.1.96   # HyperHDR on another box

Needs only the Python standard library + this repo's hyperhdr_client.py (no numpy).
You can leave "VapourSynth/mpv capture" enabled in HyperHDR, or off -- priority 150
outranks the screen grabber (245), so the test wins either way. Watch the lights and
the LED Visualization "Live video" + "Color debug" panels.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ambient_perception.hyperhdr_client import HyperHDRFlatClient  # noqa: E402


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    i = int(h * 6) % 6
    f = h * 6 - int(h * 6)
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    r, g, b = [(v, t, p), (q, v, p), (p, v, t),
               (p, q, v), (t, p, v), (v, p, q)][i]
    return int(r * 255), int(g * 255), int(b * 255)


def _frame(width: int, height: int, hue: float) -> bytes:
    # One color per column (horizontal hue gradient); repeat the row for every line.
    row = bytearray()
    for x in range(width):
        row += bytes(_hsv_to_rgb((hue + (x / width) * 0.3) % 1.0, 0.85, 1.0))
    return bytes(row) * height


def main() -> int:
    ap = argparse.ArgumentParser(description="Stream a test pattern to HyperHDR.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=19400)
    ap.add_argument("--priority", type=int, default=150)   # must be in [50, 250]
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--width", type=int, default=80)
    ap.add_argument("--height", type=int, default=45)
    ap.add_argument("--fps", type=float, default=30.0)
    a = ap.parse_args()

    client = HyperHDRFlatClient(a.host, a.port, priority=a.priority, origin="AmbientTest")
    print(f"Connecting to HyperHDR at {a.host}:{a.port} (priority {a.priority}) ...")
    client.connect()
    print(f"Connected. Streaming for {a.seconds:.0f}s -- watch your lights "
          f"and the LED Visualization panels.")

    t0 = time.time()
    frames = 0
    try:
        while time.time() - t0 < a.seconds:
            hue = ((time.time() - t0) * 0.2) % 1.0
            client.send_image(_frame(a.width, a.height, hue), a.width, a.height)
            frames += 1
            time.sleep(max(0.0, 1.0 / a.fps))
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
    print(f"Done ({frames} frames sent); cleared the source.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
