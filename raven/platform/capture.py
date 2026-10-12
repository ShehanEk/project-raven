"""Screen capture and JPEG encoding."""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image


@dataclass(frozen=True)
class Monitor:
    index: int  # 1-based, as in mss
    left: int
    top: int
    width: int
    height: int

    def normalize(self, x: float, y: float) -> tuple[float, float]:
        """Map a global position to 0..1 within this monitor (-1 if outside)."""
        nx = (x - self.left) / self.width
        ny = (y - self.top) / self.height
        if 0 <= nx <= 1 and 0 <= ny <= 1:
            return nx, ny
        return -1.0, -1.0

    def denormalize(self, nx: float, ny: float) -> tuple[int, int]:
        nx = min(max(nx, 0.0), 1.0)
        ny = min(max(ny, 0.0), 1.0)
        return (
            self.left + round(nx * (self.width - 1)),
            self.top + round(ny * (self.height - 1)),
        )


def _new_mss():
    import mss

    factory = getattr(mss, "MSS", None) or mss.mss
    return factory()


class ScreenCapturer:
    """Grabs one monitor and encodes it as JPEG.

    mss handles are not thread-safe, so create and use one capturer per
    thread (the host runs each stream in its own single-thread executor).
    """

    def __init__(self):
        self._sct = None
        self._last_raw: bytes | None = None

    @property
    def sct(self):
        if self._sct is None:
            self._sct = _new_mss()
        return self._sct

    def monitors(self) -> list[Monitor]:
        mons = self.sct.monitors
        # Index 0 is the union of all screens; real monitors start at 1.
        return [
            Monitor(i, m["left"], m["top"], m["width"], m["height"])
            for i, m in enumerate(mons)
            if i > 0
        ]

    def grab_jpeg(
        self,
        monitor: Monitor,
        max_width: int = 1920,
        quality: int = 70,
        force: bool = False,
    ) -> tuple[bytes | None, tuple[int, int]]:
        """Return (jpeg, pixel_size). jpeg is None if the screen is unchanged."""
        shot = self.sct.grab(
            {"left": monitor.left, "top": monitor.top, "width": monitor.width, "height": monitor.height}
        )
        raw = bytes(shot.bgra)
        if not force and raw == self._last_raw:
            return None, shot.size
        self._last_raw = raw
        img = Image.frombuffer("RGB", shot.size, raw, "raw", "BGRX", 0, 1)
        return encode_jpeg(img, max_width, quality), shot.size

    def reset(self) -> None:
        """Make the next grab return a frame even if nothing changed."""
        self._last_raw = None

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None


def encode_jpeg(img: Image.Image, max_width: int, quality: int) -> bytes:
    if max_width and img.width > max_width:
        height = max(1, round(img.height * max_width / img.width))
        img = img.resize((max_width, height), Image.Resampling.BILINEAR, reducing_gap=2.0)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=False)
    return buf.getvalue()
