"""Clipboard contents as they travel between computers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from raven import protocol

MAX_TEXT_CHARS = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class Clip:
    """Text, or an image as PNG bytes.

    ``digest`` identifies the content so each side can tell "this is what I
    just received" and avoid echoing it back. For images it is computed from
    the pixels by whoever produced the clip (PNG bytes differ between
    encoders even for the same picture).
    """

    kind: str  # "text" | "image"
    text: str = ""
    png: bytes = b""
    digest: str = ""

    @staticmethod
    def from_text(text: str) -> Clip:
        return Clip("text", text=text, digest=text_digest(text))

    def to_message(self, msg_type: str) -> dict:
        if self.kind == "text":
            return {"type": msg_type, "kind": "text", "text": self.text}
        return {"type": msg_type, "kind": "image", "png": protocol.b64(self.png), "digest": self.digest}

    @staticmethod
    def from_message(msg: dict) -> Clip:
        """Parse and validate a clipboard message; raises ValueError."""
        kind = msg.get("kind")
        if kind == "text":
            text = msg.get("text")
            if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
                raise ValueError("bad clipboard text")
            return Clip.from_text(text)
        if kind == "image":
            png = protocol.unb64(str(msg.get("png", "")))
            if not png.startswith(b"\x89PNG") or len(png) > MAX_IMAGE_BYTES:
                raise ValueError("bad clipboard image")
            digest = str(msg.get("digest", ""))[:128] or hashlib.sha256(png).hexdigest()
            return Clip("image", png=png, digest=digest)
        raise ValueError("unknown clipboard kind")

    def describe(self) -> str:
        if self.kind == "text":
            n = len(self.text)
            return f"{n} character{'s' if n != 1 else ''} of text"
        return "an image"


def text_digest(text: str) -> str:
    # Line endings differ between Windows and everything else.
    return "t:" + hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8", "surrogatepass")).hexdigest()
