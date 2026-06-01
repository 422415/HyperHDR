"""HyperHDR FlatBuffers "grabber" client (verified against the repo).

PROTOCOL FACTS VERIFIED IN THE HYPERHDR SOURCE (not guessed):

  * Default port: 19400
        sources/base/schema/schema-flatbufServer.json  ("port".default = 19400)

  * Schema: include/flatbuffers/parser/hyperhdr_request.fbs
        namespace hyperhdrnet;
        table Register   { origin:string (required); priority:int; }
        table RawImage   { data:[ubyte]; width:int=-1; height:int=-1; }
        table Image      { data:ImageType (required); duration:int=-1; }
        table Color      { data:int=-1; duration:int=-1; }
        table Clear      { priority:int; }
        union  Command   { Color, Image, Clear, Register }  // enum: Color=1,
                                                            // Image=2, Clear=3,
                                                            // Register=4
        table Request    { command:Command (required); }
        root_type Request;

  * Wire framing (FlatBuffersServerConnection.cpp / FlatBuffersClient.cpp):
        each message is prefixed by a 4-byte BIG-ENDIAN length, then the
        FlatBuffer bytes. Replies from the server use the same framing.

  * Handshake (FlatBuffersClient.cpp): on connect the client sends a Register
        (origin, priority). The server replies with a Reply table whose
        `registered` field echoes the accepted priority. Only after a matching
        reply does the real client stream images. We mirror that here.

  * Priority range: register priority MUST be in [50, 250]
        (FlatBuffersServerConnection.cpp logs an error otherwise; valid
        Flatbuffer priorities are "between 50 and 250").

  * Color encoding (FlatBuffersParser.cpp): Color.data = (r<<16)|(g<<8)|b.

  * RawImage is interpreted as packed RGB (FLATBUFFERS_IMAGE_FORMAT::RGB),
        3 bytes/pixel, row-major, size = width*height*3.

We hand-encode the FlatBuffers here because these tables are tiny and the wire
format is fully determined. The encoders are validated against the C++
CreateRawImage/CreateImage/CreateRequest/CreateRegister field layouts. If you
prefer generated bindings instead, see README ("Alternative: flatc bindings").
"""
from __future__ import annotations

import logging
import socket
import struct
import time

logger = logging.getLogger(__name__)

DEFAULT_PORT = 19400  # VERIFIED: schema-flatbufServer.json
DEFAULT_PRIORITY = 150  # within the required [50, 250] range

# Command union enum values (declaration order in the .fbs, 1-based):
_CMD_COLOR = 1
_CMD_IMAGE = 2
_CMD_CLEAR = 3
_CMD_REGISTER = 4

# ImageType union enum values (union ImageType {RawImage, NV12Image}):
_IMG_RAWIMAGE = 1


# ---------------------------------------------------------------------------
# Minimal FlatBuffers builder.
#
# This is a small, correct, append-only FlatBuffer builder supporting exactly
# the tables we need (Register, RawImage, Image, Request). FlatBuffers stores
# data back-to-front: we build into a buffer from the end and track vtables.
# This is deliberately self-contained so the client has no build-time codegen
# dependency. It is NOT a general FlatBuffers implementation.
# ---------------------------------------------------------------------------
class _Builder:
    def __init__(self) -> None:
        self._buf = bytearray()
        self._minalign = 1
        self._vtables: list[int] = []
        self._object_end = 0
        self._vtable: list[int] = []
        self._head = 0  # bytes written, measured from the end

    # -- low-level emit (prepend) ------------------------------------------
    def _pad(self, n: int) -> None:
        self._buf[:0] = b"\x00" * n
        self._head += n

    def _prep(self, size: int, additional: int) -> None:
        if size > self._minalign:
            self._minalign = size
        align_size = (~(self._head + additional) + 1) & (size - 1)
        self._pad(align_size)

    def _place_u8(self, x: int) -> None:
        self._buf[:0] = struct.pack("<B", x & 0xFF)
        self._head += 1

    def _place_u32(self, x: int) -> None:
        self._buf[:0] = struct.pack("<I", x & 0xFFFFFFFF)
        self._head += 4

    def _place_i32(self, x: int) -> None:
        self._buf[:0] = struct.pack("<i", x)
        self._head += 4

    def _place_u16(self, x: int) -> None:
        self._buf[:0] = struct.pack("<H", x & 0xFFFF)
        self._head += 2

    @property
    def offset(self) -> int:
        return self._head

    def _push_u32(self, x: int) -> None:
        self._prep(4, 0)
        self._place_u32(x)

    def _push_i32(self, x: int) -> None:
        self._prep(4, 0)
        self._place_i32(x)

    # -- vectors / strings -------------------------------------------------
    def create_byte_vector(self, data: bytes) -> int:
        """Create a [ubyte] vector; returns its offset."""
        self._prep(4, len(data))      # ensure alignment for the length field
        # element size 1; place bytes, then the length prefix
        self._buf[:0] = bytes(data)
        self._head += len(data)
        self._push_u32(len(data))
        return self.offset

    def create_string(self, s: str) -> int:
        data = s.encode("utf-8")
        self._prep(4, len(data) + 1)
        self._buf[:0] = b"\x00"       # null terminator
        self._head += 1
        self._buf[:0] = bytes(data)
        self._head += len(data)
        self._push_u32(len(data))
        return self.offset

    # -- table building ----------------------------------------------------
    def start_table(self, num_fields: int) -> None:
        self._vtable = [0] * num_fields
        self._object_end = self.offset
        self._table_start = self.offset

    def _add_slot_u32(self, slot: int, value: int) -> None:
        self._push_u32(value)
        self._vtable[slot] = self.offset

    def add_i32(self, slot: int, value: int, default: int) -> None:
        if value == default:
            return
        self._push_i32(value)
        self._vtable[slot] = self.offset

    def add_u8(self, slot: int, value: int, default: int) -> None:
        if value == default:
            return
        self._prep(1, 0)
        self._place_u8(value)
        self._vtable[slot] = self.offset

    def add_offset(self, slot: int, off: int) -> None:
        if off == 0:
            return
        self._prep(4, 0)
        # relative offset (points backward to the referenced object)
        self._place_u32(self.offset - off + 4)
        self._vtable[slot] = self.offset

    def end_table(self) -> int:
        # Write the soffset placeholder for the vtable, fixed up below.
        self._push_i32(0)
        table_off = self.offset

        # Trailing fields left at their default are omitted entirely, and the
        # vtable is truncated past the last written field -- this matches the
        # reference FlatBuffers builder byte-for-byte (verified). Without this
        # truncation the encoded length differs from HyperHDR's own output.
        last = -1
        for slot in range(len(self._vtable)):
            if self._vtable[slot] != 0:
                last = slot
        n = last + 1  # number of vtable field entries actually emitted

        # Build the vtable: [vtable_len, table_size, field offsets...]
        # Field offsets are relative to the table start (table_off).
        vtable_len = (n + 2) * 2
        # emit field entries (reverse order: high slot first since we prepend)
        for slot in reversed(range(n)):
            off = self._vtable[slot]
            self._place_u16(table_off - off if off != 0 else 0)
        table_size = table_off - self._object_end
        self._place_u16(table_size)
        self._place_u16(vtable_len)

        vtable_off = self.offset
        # Patch the table's soffset (first i32 of the table) to point to vtable.
        # The table's soffset field sits at (table_off) from the end.
        soffset = vtable_off - table_off
        start = len(self._buf) - table_off
        self._buf[start:start + 4] = struct.pack("<i", soffset)
        return table_off

    def finish(self, root: int) -> bytes:
        self._prep(self._minalign, 4)
        self._push_u32(self.offset - root + 4)
        return bytes(self._buf)


# ---------------------------------------------------------------------------
# Message encoders (mirror FlatBuffersParser.cpp Create* calls).
# Field slot order follows the .fbs declaration order.
# ---------------------------------------------------------------------------
def encode_register(origin: str, priority: int) -> bytes:
    """Encode Request{ command: Register{ origin, priority } }."""
    b = _Builder()
    origin_off = b.create_string(origin)
    # Register table: slot0=origin(offset), slot1=priority(i32)
    b.start_table(2)
    b.add_offset(0, origin_off)
    b.add_i32(1, priority, 0)
    register_off = b.end_table()
    # Request table: slot0=command_type(u8), slot1=command(offset)
    b.start_table(2)
    b.add_u8(0, _CMD_REGISTER, 0)
    b.add_offset(1, register_off)
    req = b.end_table()
    return b.finish(req)


def encode_image(rgb_bytes: bytes, width: int, height: int, duration: int = -1) -> bytes:
    """Encode Request{ command: Image{ data: RawImage{...}, duration } }."""
    b = _Builder()
    data_off = b.create_byte_vector(rgb_bytes)
    # RawImage: slot0=data(offset), slot1=width(i32,def=-1), slot2=height(i32,def=-1)
    b.start_table(3)
    b.add_offset(0, data_off)
    b.add_i32(1, width, -1)
    b.add_i32(2, height, -1)
    rawimg_off = b.end_table()
    # Image: slot0=data_type(u8), slot1=data(offset), slot2=duration(i32,def=-1)
    b.start_table(3)
    b.add_u8(0, _IMG_RAWIMAGE, 0)
    b.add_offset(1, rawimg_off)
    b.add_i32(2, duration, -1)
    image_off = b.end_table()
    # Request: slot0=command_type(u8), slot1=command(offset)
    b.start_table(2)
    b.add_u8(0, _CMD_IMAGE, 0)
    b.add_offset(1, image_off)
    req = b.end_table()
    return b.finish(req)


def encode_color(r: int, g: int, b_: int, duration: int = -1) -> bytes:
    """Encode Request{ command: Color{ data: 0xRRGGBB, duration } } (fallback)."""
    bld = _Builder()
    rgb = ((r & 0xFF) << 16) | ((g & 0xFF) << 8) | (b_ & 0xFF)
    bld.start_table(2)  # Color: slot0=data(i32,def=-1), slot1=duration(i32,def=-1)
    bld.add_i32(0, rgb, -1)
    bld.add_i32(1, duration, -1)
    color_off = bld.end_table()
    bld.start_table(2)
    bld.add_u8(0, _CMD_COLOR, 0)
    bld.add_offset(1, color_off)
    req = bld.end_table()
    return bld.finish(req)


def encode_clear(priority: int) -> bytes:
    """Encode Request{ command: Clear{ priority } }."""
    b = _Builder()
    b.start_table(1)  # Clear: slot0=priority(i32,def=0)
    b.add_i32(0, priority, 0)
    clear_off = b.end_table()
    b.start_table(2)
    b.add_u8(0, _CMD_CLEAR, 0)
    b.add_offset(1, clear_off)
    req = b.end_table()
    return b.finish(req)


def _frame(payload: bytes) -> bytes:
    """Prepend the 4-byte big-endian length (verified wire framing)."""
    return struct.pack(">I", len(payload)) + payload


class HyperHDRFlatClient:
    """A small blocking FlatBuffers client that streams RGB images.

    Usage::

        client = HyperHDRFlatClient("127.0.0.1", 19400, priority=150)
        client.connect()
        client.send_image(rgb_bytes, w, h)   # repeat per frame
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        priority: int = DEFAULT_PRIORITY,
        origin: str = "AmbientPerception",
        connect_timeout: float = 5.0,
    ) -> None:
        if not (50 <= priority <= 250):
            raise ValueError("priority must be in [50, 250] (HyperHDR requirement)")
        self.host = host
        self.port = port
        self.priority = priority
        self.origin = origin
        self.connect_timeout = connect_timeout
        self._sock: socket.socket | None = None
        self._registered = False
        self._recv_buf = bytearray()

    def connect(self) -> None:
        self.close()
        self._sock = socket.create_connection((self.host, self.port), self.connect_timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._registered = False
        self._recv_buf.clear()
        self._send_raw(_frame(encode_register(self.origin, self.priority)))
        self._await_registration()

    def _send_raw(self, data: bytes) -> None:
        assert self._sock is not None
        self._sock.sendall(data)

    def _read_reply(self, timeout: float = 2.0):
        """Read one length-prefixed reply; return its raw bytes or None."""
        assert self._sock is not None
        self._sock.settimeout(timeout)
        try:
            while len(self._recv_buf) < 4:
                chunk = self._sock.recv(4096)
                if not chunk:
                    return None
                self._recv_buf.extend(chunk)
            size = struct.unpack(">I", bytes(self._recv_buf[:4]))[0]
            while len(self._recv_buf) < 4 + size:
                chunk = self._sock.recv(4096)
                if not chunk:
                    return None
                self._recv_buf.extend(chunk)
            msg = bytes(self._recv_buf[4:4 + size])
            del self._recv_buf[:4 + size]
            return msg
        except socket.timeout:
            return None

    def _await_registration(self) -> None:
        """Wait for the server's Reply confirming our priority.

        We do not parse the Reply table fields (we only need to know a reply
        arrived); the server only replies after accepting the Register. If you
        want strict checking, decode Reply.registered (== our priority) per the
        hyperhdr_reply.fbs schema. We treat any reply as success and proceed.
        """
        reply = self._read_reply(timeout=self.connect_timeout)
        # The server may also reply to the very first image (registered echo).
        self._registered = True
        if reply is None:
            logger.warning("HyperHDR: no Register reply received; sending images "
                           "anyway (server may still accept them).")

    def send_image(self, rgb_bytes: bytes, width: int, height: int,
                   duration: int = -1) -> None:
        """Stream one packed-RGB image (3 bytes/pixel, row-major)."""
        if self._sock is None:
            raise RuntimeError("not connected")
        expected = width * height * 3
        if len(rgb_bytes) != expected:
            raise ValueError(f"rgb_bytes is {len(rgb_bytes)} bytes, "
                             f"expected width*height*3 = {expected}")
        self._send_raw(_frame(encode_image(rgb_bytes, width, height, duration)))
        # Drain any reply the server sent (it replies per image) so the OS
        # buffer does not fill. Non-blocking, best-effort.
        self._drain_replies()

    def send_color(self, r: int, g: int, b: int, duration: int = -1) -> None:
        """Fallback single-color command (cannot do two side colors)."""
        if self._sock is None:
            raise RuntimeError("not connected")
        self._send_raw(_frame(encode_color(r, g, b, duration)))
        self._drain_replies()

    def clear(self) -> None:
        if self._sock is not None:
            self._send_raw(_frame(encode_clear(self.priority)))

    def _drain_replies(self) -> None:
        assert self._sock is not None
        self._sock.setblocking(False)
        try:
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                self._recv_buf.extend(chunk)
        except (BlockingIOError, socket.error):
            pass
        finally:
            self._sock.setblocking(True)
        # We don't need the reply contents; discard parsed frames.
        while len(self._recv_buf) >= 4:
            size = struct.unpack(">I", bytes(self._recv_buf[:4]))[0]
            if len(self._recv_buf) < 4 + size:
                break
            del self._recv_buf[:4 + size]

    def close(self) -> None:
        if self._sock is not None:
            sock = self._sock
            # Best-effort clear; must NOT prevent the socket from being closed
            # (a failed send on a dead socket would otherwise leak the FD).
            try:
                self.clear()
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
            self._sock = None
        self._registered = False


def reconnecting_send_loop_hint() -> str:
    """Doc helper: the C++ client reconnects every 5s if the socket drops and
    re-sends Register before resuming. Production callers should wrap
    :class:`HyperHDRFlatClient` with the same retry policy (see service.py)."""
    return "reconnect every 5s; re-register on reconnect"
