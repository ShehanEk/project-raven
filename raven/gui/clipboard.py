"""Qt clipboard access for both ends of clipboard sync.

* :class:`QtClipboardBackend` lets the host server (on the network thread)
  read and write this computer's clipboard on the GUI thread.
* :class:`ClipboardSync` runs in a viewer window: it sends local copies to
  the remote computer and puts the remote computer's copies here.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import time

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QImage

from raven.clip import MAX_IMAGE_BYTES, MAX_TEXT_CHARS, Clip
from raven.gui.async_bridge import UiCaller
from raven.platform.clipboard import change_count

# Ignore clipboard-changed notifications this soon after we wrote the
# clipboard ourselves: some platforms report our own write as a change.
SELF_WRITE_GRACE = 0.4


def image_digest(image: QImage) -> str:
    """Hash of the pixels, so re-encoded copies of one picture match."""
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    h = hashlib.sha256(f"{rgba.width()}x{rgba.height()}".encode())
    h.update(bytes(rgba.constBits()))
    return "i:" + h.hexdigest()


def read_clipboard() -> Clip | None:
    """Current clipboard as a Clip (GUI thread). Text wins over images."""
    mime = QGuiApplication.clipboard().mimeData()
    if mime is None:
        return None
    if mime.hasText() and mime.text():
        text = mime.text()
        return Clip.from_text(text) if len(text) <= MAX_TEXT_CHARS else None
    if mime.hasImage():
        image = QImage(mime.imageData())
        if image.isNull():
            return None
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        png = bytes(buffer.data())
        if len(png) > MAX_IMAGE_BYTES:
            return None
        return Clip("image", png=png, digest=image_digest(image))
    return None


def write_clipboard(clip: Clip) -> None:
    """Put ``clip`` on the clipboard (GUI thread)."""
    clipboard = QGuiApplication.clipboard()
    if clip.kind == "text":
        clipboard.setText(clip.text)
    else:
        image = QImage.fromData(QByteArray(clip.png), "PNG")
        if not image.isNull():
            clipboard.setImage(image)


class _ChangeCounter(QObject):
    """Counts Qt's clipboard-changed signals; lives on the GUI thread."""

    def __init__(self):
        super().__init__()
        self.count = 0
        QGuiApplication.clipboard().dataChanged.connect(self._bump)

    def _bump(self) -> None:
        self.count += 1


class QtClipboardBackend:
    """Host clipboard backend; safe to call from the network thread."""

    supports_images = True

    def __init__(self, ui: UiCaller):
        self.ui = ui
        self.counter = _ChangeCounter()  # constructed on the GUI thread

    def change_token(self):
        # The OS counter catches changes Qt only reports when the app is
        # focused (macOS); Qt's signal covers platforms without one.
        return (self.counter.count, change_count())

    def _on_gui(self, fn):
        future: concurrent.futures.Future = concurrent.futures.Future()

        def run() -> None:
            try:
                future.set_result(fn())
            except BaseException as exc:  # noqa: BLE001
                future.set_exception(exc)

        self.ui(run)
        return asyncio.wrap_future(future)

    async def read(self) -> Clip | None:
        return await self._on_gui(read_clipboard)

    async def write(self, clip: Clip) -> None:
        await self._on_gui(lambda: write_clipboard(clip))


class ClipboardSync(QObject):
    """Two-way clipboard sync for one viewer session (GUI thread).

    ``outgoing`` carries local copies to send; call :meth:`receive` with the
    remote computer's copies.
    """

    outgoing = Signal(object)  # Clip
    received = Signal(object)  # Clip, after it was put on the local clipboard

    def __init__(self, parent=None):
        super().__init__(parent)
        self.enabled = False
        self.last_digest: str | None = None
        self._written_at = 0.0
        self._token = None
        QGuiApplication.clipboard().dataChanged.connect(self._check)
        self._timer = QTimer(self, interval=600)
        self._timer.timeout.connect(self._poll)

    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        if on:
            # Only copies made from now on are sent.
            current = read_clipboard()
            self.last_digest = current.digest if current else None
            self._token = change_count()
            self._timer.start()
        else:
            self._timer.stop()

    def _poll(self) -> None:
        token = change_count()
        if token is not None and token != self._token:
            self._token = token
            self._check()

    def _check(self) -> None:
        if not self.enabled or time.monotonic() - self._written_at < SELF_WRITE_GRACE:
            return
        clip = read_clipboard()
        if clip is None or clip.digest == self.last_digest:
            return
        self.last_digest = clip.digest
        self.outgoing.emit(clip)

    def receive(self, clip: Clip) -> None:
        if not self.enabled or clip.digest == self.last_digest:
            return
        self.last_digest = clip.digest
        self._written_at = time.monotonic()
        write_clipboard(clip)
        self._token = change_count()
        if clip.kind == "image":
            # Record the digest of the image as this platform stores it.
            current = read_clipboard()
            if current is not None and current.kind == "image":
                self.last_digest = current.digest
        self.received.emit(clip)
