"""Optional HyperHDR ambient-lighting tap for animejanai -- self-contained.

Drop this single file into your animejanai/core/ folder (next to
animejanai_core.py). It streams the decoded frame to HyperHDR over its FlatBuffers
input so ambient lights follow the video. It is SAFE TO SHIP: it does nothing
unless something calls attach_ambient(), needs only the Python standard library +
numpy (already used by animejanai), and references no external repo. If HyperHDR
is not running, it fails silently and never affects playback.

Usage from a profile (.vpy):
    from ambient import attach_ambient
    import animejanai_core
    animejanai_core.run_animejanai_with_keybinding(
        attach_ambient(video_in, container_fps), container_fps, 1002)

Or, to drive lights WITHOUT upscaling (e.g. the upscale-off profile):
    from ambient import attach_ambient
    attach_ambient(video_in, container_fps).set_output()

Host/port default to 127.0.0.1:19400 (HyperHDR's default flatbuffers port) and can
be overridden via the AMBIENT_HOST / AMBIENT_PORT environment variables or the
attach_ambient() arguments.
"""
from __future__ import annotations

import os
import socket
import struct
import threading
import time

# FlatBuffers command/union enum values (hyperhdr_request.fbs declaration order).
_CMD_IMAGE = 2
_CMD_REGISTER = 4
_IMG_RAWIMAGE = 1


# ---------------------------------------------------------------------------
# Minimal, self-contained FlatBuffers builder (Register / RawImage / Image /
# Request only). Byte-verified against the reference flatbuffers library.
# ---------------------------------------------------------------------------
class _Builder:
    def __init__(self):
        self._buf = bytearray()
        self._minalign = 1
        self._vtable = []
        self._object_end = 0
        self._head = 0  # bytes written, measured from the end

    def _pad(self, n):
        self._buf[:0] = b"\x00" * n
        self._head += n

    def _prep(self, size, additional):
        if size > self._minalign:
            self._minalign = size
        self._pad((~(self._head + additional) + 1) & (size - 1))

    def _place_u8(self, x):
        self._buf[:0] = struct.pack("<B", x & 0xFF); self._head += 1

    def _place_u16(self, x):
        self._buf[:0] = struct.pack("<H", x & 0xFFFF); self._head += 2

    def _place_u32(self, x):
        self._buf[:0] = struct.pack("<I", x & 0xFFFFFFFF); self._head += 4

    def _place_i32(self, x):
        self._buf[:0] = struct.pack("<i", x); self._head += 4

    @property
    def offset(self):
        return self._head

    def _push_u32(self, x):
        self._prep(4, 0); self._place_u32(x)

    def _push_i32(self, x):
        self._prep(4, 0); self._place_i32(x)

    def create_byte_vector(self, data):
        self._prep(4, len(data))
        self._buf[:0] = bytes(data); self._head += len(data)
        self._push_u32(len(data))
        return self.offset

    def create_string(self, s):
        data = s.encode("utf-8")
        self._prep(4, len(data) + 1)
        self._buf[:0] = b"\x00"; self._head += 1
        self._buf[:0] = bytes(data); self._head += len(data)
        self._push_u32(len(data))
        return self.offset

    def start_table(self, num_fields):
        self._vtable = [0] * num_fields
        self._object_end = self.offset

    def add_i32(self, slot, value, default):
        if value == default:
            return
        self._push_i32(value); self._vtable[slot] = self.offset

    def add_u8(self, slot, value, default):
        if value == default:
            return
        self._prep(1, 0); self._place_u8(value); self._vtable[slot] = self.offset

    def add_offset(self, slot, off):
        if off == 0:
            return
        self._prep(4, 0)
        self._place_u32(self.offset - off + 4)
        self._vtable[slot] = self.offset

    def end_table(self):
        self._push_i32(0)
        table_off = self.offset
        last = -1
        for slot in range(len(self._vtable)):
            if self._vtable[slot] != 0:
                last = slot
        n = last + 1
        vtable_len = (n + 2) * 2
        for slot in reversed(range(n)):
            off = self._vtable[slot]
            self._place_u16(table_off - off if off != 0 else 0)
        self._place_u16(table_off - self._object_end)
        self._place_u16(vtable_len)
        vtable_off = self.offset
        start = len(self._buf) - table_off
        self._buf[start:start + 4] = struct.pack("<i", vtable_off - table_off)
        return table_off

    def finish(self, root):
        self._prep(self._minalign, 4)
        self._push_u32(self.offset - root + 4)
        return bytes(self._buf)


def _encode_register(origin, priority):
    b = _Builder()
    origin_off = b.create_string(origin)
    b.start_table(2)
    b.add_offset(0, origin_off)
    b.add_i32(1, priority, 0)
    reg = b.end_table()
    b.start_table(2)
    b.add_u8(0, _CMD_REGISTER, 0)
    b.add_offset(1, reg)
    return b.finish(b.end_table())


def _encode_image(rgb_bytes, width, height, duration=-1):
    b = _Builder()
    data_off = b.create_byte_vector(rgb_bytes)
    b.start_table(3)
    b.add_offset(0, data_off)
    b.add_i32(1, width, -1)
    b.add_i32(2, height, -1)
    rawimg = b.end_table()
    b.start_table(3)
    b.add_u8(0, _IMG_RAWIMAGE, 0)
    b.add_offset(1, rawimg)
    b.add_i32(2, duration, -1)
    img = b.end_table()
    b.start_table(2)
    b.add_u8(0, _CMD_IMAGE, 0)
    b.add_offset(1, img)
    return b.finish(b.end_table())


def _frame(payload):
    return struct.pack(">I", len(payload)) + payload


# ---------------------------------------------------------------------------
# Tiny blocking client: connect + Register, then stream Images. All errors are
# swallowed by the caller so playback is never affected.
# ---------------------------------------------------------------------------
class _Client:
    """Blocking flatbuffers client, hardened so it can never wedge mpv and so the
    color survives a pause:

    * **send timeout** -- a stalled HyperHDR can never block the mpv frame thread
      for more than ``send_timeout``; the frame is dropped instead.
    * **failure cooldown** -- after a failed send the mpv thread stops trying for
      ``cooldown`` seconds (returns immediately); all (re)connecting happens off
      the mpv thread in ``keepalive()``.
    * **keepalive** -- ``keepalive()`` (driven by a background thread) resends the
      last frame when the link goes idle, so HyperHDR's 5 s flatbuffer idle timeout
      doesn't disconnect us and drop the priority while playback is paused.
    """

    def __init__(self, host, port, priority, origin, send_timeout=1.0, cooldown=3.0):
        self.host, self.port, self.priority, self.origin = host, port, priority, origin
        self.send_timeout = send_timeout
        self.cooldown = cooldown
        self.sock = None
        self._lock = threading.Lock()
        self._last_frame = None
        self._last_send = 0.0
        self._cooldown_until = 0.0

    # -- the _locked helpers must be called while holding self._lock --
    def _connect_locked(self):
        s = socket.create_connection((self.host, self.port), 3.0)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(self.send_timeout)
        s.sendall(_frame(_encode_register(self.origin, self.priority)))
        try:
            s.recv(4096)   # consume the Register reply (content unused)
        except OSError:
            pass
        self.sock = s

    def _reset_locked(self):
        try:
            if self.sock is not None:
                self.sock.close()
        except OSError:
            pass
        self.sock = None
        self._cooldown_until = time.monotonic() + self.cooldown

    def _send_locked(self, framed):
        try:
            self.sock.settimeout(self.send_timeout)
            self.sock.sendall(framed)
            self.sock.setblocking(False)
            try:
                while self.sock.recv(4096):   # drain replies; never let OS buffer fill
                    pass
            except OSError:
                pass
            self._last_send = time.monotonic()
        except OSError:
            self._reset_locked()

    def send_image(self, rgb_bytes, width, height):
        """Called synchronously from the mpv frame thread -- must not block on
        connect, and never longer than send_timeout."""
        framed = _frame(_encode_image(rgb_bytes, width, height))
        with self._lock:
            self._last_frame = framed
            if self.sock is None or time.monotonic() < self._cooldown_until:
                return   # outage: drop this frame, keepalive() reconnects off-thread
            self._send_locked(framed)

    def keepalive(self):
        """Background thread: (re)connect if needed and resend the last frame when
        the link has gone idle, so the priority survives a pause."""
        with self._lock:
            now = time.monotonic()
            if self.sock is None:
                if now < self._cooldown_until:
                    return
                try:
                    self._connect_locked()
                    if self._last_frame is not None:
                        self._send_locked(self._last_frame)
                except OSError:
                    self._reset_locked()
                return
            if self._last_frame is not None and (now - self._last_send) >= 1.5:
                self._send_locked(self._last_frame)

    def reset(self):
        with self._lock:
            self._reset_locked()


def _start_keepalive(client, interval=1.0):
    def _loop():
        while True:
            try:
                time.sleep(interval)
                client.keepalive()
            except Exception:
                pass
    threading.Thread(target=_loop, name="ambient-keepalive", daemon=True).start()


# One client (and one keepalive thread / connection) per host:port, reused across
# profile switches so we never open duplicate connections that fight at the same
# priority.
_CLIENTS = {}


def _get_client(host, port, priority, origin):
    key = (host, port)
    client = _CLIENTS.get(key)
    if client is None:
        client = _Client(host, port, priority, origin)
        _CLIENTS[key] = client
        try:
            with client._lock:
                client._connect_locked()
        except OSError:
            client.reset()
        _start_keepalive(client)
    return client


def _is_enabled(config):
    """True if ambient is enabled, via env AMBIENT_ENABLE=1 or an [ambient]
    enabled=true section in the parsed animejanai config passed in. Default OFF."""
    if os.environ.get("AMBIENT_ENABLE", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        sec = config.get("ambient") if hasattr(config, "get") else None
        val = sec.get("enabled") if hasattr(sec, "get") else None
        return str(val).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return False


def maybe_tap(clip, container_fps, config=None, host=None, port=None, downscale=256):
    """Hook entry for animejanai_core: returns the clip tapped to HyperHDR if
    enabled, else UNCHANGED. Add one line in animejanai_core.run_animejanai:

        clip = __import__("ambient").maybe_tap(clip, container_fps, config)

    Disabled by default, so it is a no-op for anyone not using HyperHDR. Never
    raises -- on any problem it returns the clip untouched.
    """
    try:
        if not _is_enabled(config):
            return clip
        return attach_ambient(clip, container_fps, host=host, port=port, downscale=downscale)
    except Exception:
        return clip


def attach_ambient(clip, container_fps, host=None, port=None,
                   downscale=256, priority=150, origin="animejanai"):
    """Return ``clip`` UNCHANGED, with a HyperHDR ambient-stream side-effect.

    Taps a small (``downscale`` px) RGB copy of ``clip`` into its frame path via
    std.ModifyFrame and sends each frame to HyperHDR synchronously. The returned
    frames are ``clip``'s own, so any upscale downstream is byte-identical. Every
    error (incl. HyperHDR not running) is swallowed and triggers a reconnect on
    the next frame -- playback is never interrupted.
    """
    import numpy as np
    import vapoursynth as vs
    core = vs.core

    host = host or os.environ.get("AMBIENT_HOST", "127.0.0.1")
    port = int(port or os.environ.get("AMBIENT_PORT", "19400"))

    tw = min(int(downscale), clip.width)
    th = max(2, round(clip.height * tw / clip.width))

    # Convert to SDR full-range RGB24 using the clip's OWN color tags, so the
    # streamed color matches what's on screen -- crucially including HDR sources
    # (BT.2020 / PQ / HLG), which look completely wrong if assumed to be SDR 709.
    # We probe frame 0's props; whatever is tagged (matrix/transfer/primaries/range)
    # is read per-frame by resize and converted to 709 full-range. Anything left
    # "unspecified" (==2) or untagged falls back to a sane SDR default.
    try:
        props = dict(clip.get_frame(0).props)
    except Exception:
        props = {}

    is_yuv = clip.format.color_family == vs.YUV
    kw = dict(width=tw, height=th, format=vs.RGB24, dither_type="error_diffusion",
              transfer_s="709", primaries_s="709", range_s="full")
    if is_yuv and props.get("_Matrix", 2) == 2:
        kw["matrix_in_s"] = "709" if clip.height >= 720 else "170m"
    if props.get("_Transfer", 2) == 2:
        kw["transfer_in_s"] = "709"
    if props.get("_Primaries", 2) == 2:
        kw["primaries_in_s"] = "709"
    if "_ColorRange" not in props:
        kw["range_in_s"] = "limited" if is_yuv else "full"

    tap = core.resize.Bilinear(clip, **kw)

    client = _get_client(host, port, priority, origin)

    def _selector(n, f):
        try:
            tf = f[1]
            arr = np.stack([np.asarray(tf[i]) for i in range(3)], axis=-1).astype(np.uint8)
            client.send_image(np.ascontiguousarray(arr).tobytes(), tw, th)
        except Exception:
            pass   # never break playback; the client manages its own connection
        return f[0]

    return clip.std.ModifyFrame(clips=[clip, tap], selector=_selector)
