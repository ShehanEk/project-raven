"""Wire protocol shared by host and controller.

Transport: WebSocket over TLS. Control messages are JSON text frames with a
``type`` field. Screen frames are binary messages: a small header followed by
JPEG bytes.

Handshake::

    host  -> {"type": "hello", "version", "name", "os", "salt", "iterations", "nonce"}
    ctrl  -> {"type": "auth", "proof", "client_name"}
    host  -> {"type": "auth_result", "ok": true, "info": {...}}   (or ok: false, error)

After that the controller may send:

    stream_start {monitor, fps, quality, max_width}   stream_stop   ack
    mouse_move {x, y}  (0..1 within the streamed monitor)
    mouse_button {button, down}   scroll {dx, dy}   key {key, down}
    release_all   type_text {text}   power {action}   ping

and the host may send: error {error}, power_result {action, ok, error},
stream_info {monitor, width, height}, pong.

Requests that carry a numeric ``rid`` get a reply with the same ``rid``
(``ok: false`` plus ``error`` on failure), which lets the controller await
answers while screen frames keep flowing.

File transfer (see raven/host/files.py) uses fs_* requests plus binary
chunk messages: a header with kind 2 and a 32-bit transfer id, then data.
Clipboard sync uses clipboard_sync / clipboard_set / clipboard (raven/clip.py).

docs/PROTOCOL.md is the complete reference; keep it in sync with changes here.
"""

from __future__ import annotations

import base64
import json
import struct

FRAME_KIND_JPEG = 1
FRAME_KIND_CHUNK = 2
_FRAME_HEADER = struct.Struct("!Bff")
_CHUNK_HEADER = struct.Struct("!BI")


def encode(msg: dict) -> str:
    return json.dumps(msg, separators=(",", ":"))


def decode(data: str | bytes) -> dict:
    msg = json.loads(data)
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ValueError("malformed message")
    return msg


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def pack_frame(jpeg: bytes, cursor: tuple[float, float] = (-1.0, -1.0)) -> bytes:
    return _FRAME_HEADER.pack(FRAME_KIND_JPEG, cursor[0], cursor[1]) + jpeg


def unpack_frame(data: bytes) -> tuple[bytes, tuple[float, float]]:
    kind, cx, cy = _FRAME_HEADER.unpack_from(data)
    if kind != FRAME_KIND_JPEG:
        raise ValueError(f"unknown frame kind {kind}")
    return data[_FRAME_HEADER.size:], (cx, cy)


def pack_chunk(tid: int, data: bytes) -> bytes:
    return _CHUNK_HEADER.pack(FRAME_KIND_CHUNK, tid) + data


def unpack_chunk(data: bytes) -> tuple[int, bytes]:
    kind, tid = _CHUNK_HEADER.unpack_from(data)
    if kind != FRAME_KIND_CHUNK:
        raise ValueError(f"not a chunk: kind {kind}")
    return tid, data[_CHUNK_HEADER.size:]


def frame_kind(data: bytes) -> int:
    return data[0] if data else 0


def clamp(value, low, high, default):
    try:
        value = type(default)(value)
    except (TypeError, ValueError):
        return default
    return min(max(value, low), high)
