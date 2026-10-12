"""Window that shows a remote screen and forwards mouse and keyboard to it."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QActionGroup,
    QColor,
    QFont,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from raven.client.files import FileClient
from raven.clip import Clip
from raven.client.session import RemoteSession
from raven.config import RemoteHost, local_os
from raven.gui.async_bridge import AsyncRunner
from raven.gui.clipboard import ClipboardSync
from raven.gui.files_window import FilesWindow
from raven.gui import icons
from raven.gui.keymap import key_name, swap_command_and_control
from raven.gui.theme import theme
from raven.gui.transfers import TransferList, drop_hint
from raven.gui.widgets import Avatar, IconButton, StatusDot, Toast, button, confirm, label

QUALITY_PRESETS = {
    "Fast": {"max_width": 1280, "quality": 50, "fps": 20},
    "Balanced": {"max_width": 1920, "quality": 65, "fps": 15},
    "Sharp": {"max_width": 2560, "quality": 80, "fps": 12},
}
POWER_LABELS = {"sleep": "Sleep", "lock": "Lock", "restart": "Restart", "shutdown": "Shut Down"}
_BUTTONS = {
    Qt.MouseButton.LeftButton: "left",
    Qt.MouseButton.RightButton: "right",
    Qt.MouseButton.MiddleButton: "middle",
}


class ScreenView(QWidget):
    """Paints the remote frame and turns local input into protocol messages."""

    send = Signal(dict)
    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(320, 200)
        self.setAcceptDrops(True)
        self.drop_overlay = drop_hint("Drop to send to the other computer's Downloads folder")
        self.drop_overlay.setParent(self)
        self.drop_overlay.hide()
        self.image: QImage | None = None
        self.remote_cursor = (-1.0, -1.0)
        self.message = "Connecting…"
        self.input_enabled = True
        self.swap_cmd_ctrl = False
        self._held: set[str] = set()
        self._pending_move: tuple[float, float] | None = None
        self._move_timer = QTimer(self, interval=15, singleShot=True)
        self._move_timer.timeout.connect(self._flush_move)

    # ---------------------------------------------------------- painting

    def set_frame(self, image: QImage, cursor: tuple[float, float]) -> None:
        self.image = image
        self.remote_cursor = cursor
        self.message = ""
        self.update()

    def set_cursor(self, cursor: tuple[float, float]) -> None:
        self.remote_cursor = cursor
        self.update()

    def set_message(self, text: str) -> None:
        self.message = text
        self.update()

    def image_rect(self) -> QRectF:
        if not self.image or self.image.isNull():
            return QRectF()
        iw, ih = self.image.width(), self.image.height()
        scale = min(self.width() / iw, self.height() / ih)
        w, h = iw * scale, ih * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(11, 13, 17))
        rect = self.image_rect()
        if not rect.isNull():
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(rect, self.image)
            cx, cy = self.remote_cursor
            # Our own pointer is visible while hovering, so only draw the
            # remote one when someone else may be moving it.
            if cx >= 0 and not self.underMouse():
                self._draw_cursor(p, QPointF(rect.x() + cx * rect.width(), rect.y() + cy * rect.height()))
        if self.message:
            if not rect.isNull():
                p.fillRect(self.rect(), QColor(0, 0, 0, 150))
            self._draw_message(p)

    def _draw_message(self, p: QPainter) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = p.font()
        font.setPointSizeF(font.pointSizeF() + 1.5)
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        metrics = p.fontMetrics()
        text_rect = metrics.boundingRect(QRect(0, 0, 420, 1000), Qt.TextFlag.TextWordWrap, self.message)
        box = QRectF(0, 0, text_rect.width() + 72, max(text_rect.height(), 20) + 32)
        box.moveCenter(QRectF(self.rect()).center())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(28, 31, 40, 235))
        p.drawRoundedRect(box, box.height() / 2 if box.height() < 60 else 16, box.height() / 2 if box.height() < 60 else 16)
        # A small spinner arc while connecting.
        if self.image is None:
            angle = (time.monotonic() * 360) % 360
            pen = QPen(QColor(124, 108, 255), 2.4)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            arc = QRectF(box.left() + 20, box.center().y() - 8, 16, 16)
            p.drawArc(arc, int(-angle * 16), 270 * 16)
            QTimer.singleShot(16, self.update)
        p.setPen(QColor(232, 234, 240))
        p.drawText(box.adjusted(44 if self.image is None else 24, 0, -24, 0),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                   self.message)

    @staticmethod
    def _draw_cursor(p: QPainter, at: QPointF) -> None:
        path = QPainterPath(at)
        for dx, dy in ((0, 16), (4, 12), (7, 18), (9, 17), (6, 11), (11, 11)):
            path.lineTo(at.x() + dx, at.y() + dy)
        path.closeSubpath()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QColor("black"))
        p.setBrush(QColor("white"))
        p.drawPath(path)

    # ---------------------------------------------------------- mouse

    def _normalized(self, pos: QPointF) -> tuple[float, float] | None:
        rect = self.image_rect()
        if rect.isNull():
            return None
        x = (pos.x() - rect.x()) / rect.width()
        y = (pos.y() - rect.y()) / rect.height()
        return min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)

    def _active(self) -> bool:
        return self.input_enabled and self.image is not None and not self.message

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not self._active():
            return
        pos = self._normalized(event.position())
        if pos:
            # Coalesce moves so a fast mouse doesn't flood the network.
            self._pending_move = pos
            if not self._move_timer.isActive():
                self._move_timer.start()

    def _flush_move(self) -> None:
        if self._pending_move:
            x, y = self._pending_move
            self._pending_move = None
            self.send.emit({"type": "mouse_move", "x": x, "y": y})

    def _button(self, event: QMouseEvent, down: bool) -> None:
        if not self._active() or event.button() not in _BUTTONS:
            return
        pos = self._normalized(event.position())
        if pos:
            self._pending_move = pos
            self._move_timer.stop()
            self._flush_move()
        self.send.emit({"type": "mouse_button", "button": _BUTTONS[event.button()], "down": down})

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.setFocus()
        self._button(event, True)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._button(event, False)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        # Qt replaces the second press with this event; the remote OS detects
        # the double click itself from two quick presses.
        self._button(event, True)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if not self._active():
            return
        delta = event.angleDelta()
        if delta.isNull():
            return
        # 120 units is one notch of a classic mouse wheel.
        self.send.emit({"type": "scroll", "dx": delta.x() / 120.0, "dy": delta.y() / 120.0})

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.update()

    # ---------------------------------------------------------- drag & drop

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if urls and all(u.isLocalFile() for u in urls):
            event.acceptProposedAction()
            self.drop_overlay.setGeometry(self.rect().adjusted(40, 40, -40, -40))
            self.drop_overlay.raise_()
            self.drop_overlay.show()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.drop_overlay.hide()

    def dropEvent(self, event) -> None:  # noqa: N802
        self.drop_overlay.hide()
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)

    # ---------------------------------------------------------- keyboard

    def focusNextPrevChild(self, next: bool) -> bool:  # noqa: N802, A002
        return False  # Tab belongs to the remote computer

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        self._key(event, True)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.isAutoRepeat():
            return  # the matching repeat press follows; keep the key "down"
        self._key(event, False)

    def _key(self, event: QKeyEvent, down: bool) -> None:
        if not self._active():
            return
        name = key_name(event)
        if not name:
            return
        if self.swap_cmd_ctrl:
            name = swap_command_and_control(name)
        if down:
            self._held.add(name)
        else:
            self._held.discard(name)
        self.send.emit({"type": "key", "key": name, "down": down})
        event.accept()

    def release_all(self) -> None:
        if self._held:
            self._held.clear()
            self.send.emit({"type": "release_all"})

    def focusOutEvent(self, event) -> None:  # noqa: N802
        # Alt+Tab away must not leave Alt stuck down on the remote side.
        self.release_all()
        super().focusOutEvent(event)


class _OverlayCard(QFrame):
    """Centred card shown over the screen when the connection drops."""

    def __init__(self, parent: QWidget, on_reconnect, on_close):
        super().__init__(parent)
        self.setObjectName("overlayCard")
        v = QVBoxLayout(self)
        v.setContentsMargins(28, 26, 28, 22)
        v.setSpacing(10)
        v.addWidget(Avatar("plug", 48, 24), 0, Qt.AlignmentFlag.AlignHCenter)
        self.title = label("Disconnected", "h2")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.title)
        self.reason = label("", "muted", wrap=True)
        self.reason.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.reason)
        v.addSpacing(6)
        row = QHBoxLayout()
        close = button("Close", "ghost")
        close.clicked.connect(on_close)
        again = button("Reconnect", "primary", "restart")
        again.clicked.connect(on_reconnect)
        row.addWidget(close)
        row.addWidget(again)
        v.addLayout(row)
        self.setFixedWidth(340)
        self.hide()

    def show_reason(self, reason: str) -> None:
        self.reason.setText(reason)
        self.adjustSize()
        self.recenter()
        self.raise_()
        self.show()

    def recenter(self) -> None:
        parent = self.parentWidget()
        self.move((parent.width() - self.width()) // 2, (parent.height() - self.height()) // 2)


class ViewerWindow(QMainWindow):
    frame_ready = Signal(QImage, tuple)
    message_received = Signal(dict)
    closed = Signal(str)
    reconnect_requested = Signal()

    def __init__(self, runner: AsyncRunner, session: RemoteSession, remote: RemoteHost, info: dict):
        super().__init__()
        self.runner = runner
        self.session = session
        self.remote = remote
        self.info = info
        self.connected = True
        self.quality = "Balanced"
        self.monitor = (info.get("monitors") or [{"index": 1}])[0]["index"]
        self._frames = 0
        self._frame_clock = time.monotonic()
        self.setWindowTitle(f"{remote.name or remote.address} — Raven")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_bar())
        self.view = ScreenView()
        self.view.send.connect(self._send)
        self.view.files_dropped.connect(lambda paths: self.send_files([Path(p) for p in paths]))
        layout.addWidget(self.view, 1)
        self.setCentralWidget(central)
        self.view.swap_cmd_ctrl = self.swap.isChecked()
        if not self.info.get("allow_input", True):
            self.view_only.setChecked(True)
            self.view_only.setEnabled(False)
            self.view_only.setToolTip("Mouse and keyboard control is turned off on that computer")

        self.toast = Toast(self.view)
        self.overlay = _OverlayCard(self.view, self._reconnect, self.close)
        self.file_client = FileClient(session)
        self.clipboard = ClipboardSync(self)
        self.clipboard.outgoing.connect(lambda clip: self._send(clip.to_message("clipboard_set")))
        self.clipboard.received.connect(
            lambda clip: self.toast.show_message(f"Copied {clip.describe()} from {self.remote.name}", "info", 1800))
        if self.info.get("allow_clipboard"):
            self._set_clipboard_sync(True)
        self.files_window: FilesWindow | None = None
        self.remote_downloads: str | None = None
        self.transfer_card = QFrame(self.view)
        self.transfer_card.setObjectName("overlayCard")
        tc = QVBoxLayout(self.transfer_card)
        tc.setContentsMargins(10, 10, 10, 10)
        self.transfers = TransferList(runner)
        self.transfers.changed.connect(self._place_transfer_card)
        self.transfers.job_finished.connect(self._transfer_finished)
        tc.addWidget(self.transfers)
        self.transfer_card.hide()

        self.frame_ready.connect(self.view.set_frame)
        self.frame_ready.connect(self._count_frame)
        self.message_received.connect(self._on_message)
        self.closed.connect(self._on_closed)
        self._fps_timer = QTimer(self, interval=1000)
        self._fps_timer.timeout.connect(self._update_stats)
        self._fps_timer.start()

        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.resize(int(screen.width() * 0.8), int(screen.height() * 0.8))

        runner.submit(self._start_receiving())
        self._start_stream()
        self.view.setFocus()

    # The callbacks below run on the network thread and may fire after the
    # window is gone, so they check ``_alive`` and tolerate deleted objects.
    _alive = True

    async def _start_receiving(self) -> None:
        self.session.start_receiving(self._on_frame_thread, self._on_message_thread, self._on_closed_thread)

    def _on_frame_thread(self, jpeg: bytes, cursor: tuple[float, float]) -> None:
        # Decode here rather than on the GUI thread to keep the UI responsive.
        image = QImage.fromData(jpeg, "JPEG")
        if not image.isNull():
            self._emit(self.frame_ready, image, cursor)

    def _on_message_thread(self, msg: dict) -> None:
        self._emit(self.message_received, msg)

    def _on_closed_thread(self, reason: str) -> None:
        self._emit(self.closed, reason)

    def _emit(self, signal, *args) -> None:
        if self._alive:
            try:
                signal.emit(*args)
            except RuntimeError:
                pass

    def _count_frame(self, *_):
        self._frames += 1

    # ---------------------------------------------------------- top bar

    def _build_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("viewerBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        bar.setFixedHeight(52)
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 0, 10, 0)
        h.setSpacing(4)

        self.state_dot = StatusDot("warning", 7)
        h.addWidget(self.state_dot)
        h.addSpacing(4)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.setContentsMargins(0, 8, 0, 8)
        names.addWidget(label(self.remote.name or self.remote.address, "h3"))
        self.stats = label("Connecting…", "muted")
        names.addWidget(self.stats)
        h.addLayout(names)
        h.addStretch()

        def tool(name, tip, checkable=False):
            b = IconButton(name, tip, size=36, icon_size=18, checkable=checkable, fixed_color="#aab1c3")
            h.addWidget(b)
            return b

        monitors = self.info.get("monitors", [])
        if len(monitors) > 1:
            self.display_box = QComboBox()
            self.display_box.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            for m in monitors:
                self.display_box.addItem(f"Display {m['index']}  ·  {m['width']}×{m['height']}", m["index"])
            self.display_box.currentIndexChanged.connect(self._display_changed)
            h.addWidget(self.display_box)
            h.addSpacing(8)

        quality = tool("sliders", "Picture quality")
        quality_menu = QMenu(quality)
        self.quality_actions = QActionGroup(self)
        for name, preset in QUALITY_PRESETS.items():
            act = quality_menu.addAction(f"{name}  ·  up to {preset['max_width']}px, {preset['fps']} fps")
            act.setCheckable(True)
            act.setChecked(name == self.quality)
            act.triggered.connect(lambda _=False, n=name: self._set_quality(n))
            self.quality_actions.addAction(act)
        quality.setMenu(quality_menu)
        quality.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        self.view_only = tool("eye", "View only — don't send mouse or keyboard", checkable=True)
        self.view_only.toggled.connect(self._set_view_only)

        self.sync_btn = tool("sync", "Sync clipboard — copy on one computer, paste on the other", checkable=True)
        self.sync_btn.toggled.connect(self._set_clipboard_sync)
        if not self.info.get("allow_clipboard"):
            self.sync_btn.setEnabled(False)
            self.sync_btn.setToolTip("Clipboard sync is turned off on that computer")

        self.swap = tool("command", "Swap ⌘ and Ctrl between Mac and PC", checkable=True)
        self.swap.setChecked((self.remote.os == "macos") != (local_os() == "macos"))
        self.swap.toggled.connect(lambda on: setattr(self.view, "swap_cmd_ctrl", on))

        files = tool("files", "Files — browse and transfer files")
        files.clicked.connect(self.open_files)
        files.setEnabled(self.info.get("allow_files", True))
        if not files.isEnabled():
            files.setToolTip("File transfer is turned off on that computer")

        paste = tool("clipboard", "Type your clipboard text on the remote computer")
        paste.clicked.connect(self._type_clipboard)

        self.full = tool("maximize", "Full screen", checkable=True)
        self.full.toggled.connect(self._set_fullscreen)

        sep = QFrame()
        sep.setFixedSize(1, 22)
        sep.setStyleSheet("background: #2a2f3b;")
        h.addSpacing(6)
        h.addWidget(sep)
        h.addSpacing(6)

        power = tool("power", "Power")
        power_menu = QMenu(power)
        for action, icon_name in (("sleep", "moon"), ("lock", "lock"), ("restart", "restart"), ("shutdown", "power")):
            act = power_menu.addAction(POWER_LABELS[action], lambda a=action: self._power(a))
            act.setIcon(icons.icon(icon_name, theme.palette.text, 16))
        power.setMenu(power_menu)
        power.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        power.setEnabled(self.info.get("allow_power", True))
        if not power.isEnabled():
            power.setToolTip("Power controls are turned off on that computer")

        disconnect = tool("x", "Disconnect")
        disconnect.fixed_color = "#f87171"
        disconnect.clicked.connect(self.close)
        return bar

    # ---------------------------------------------------------- actions

    def _send(self, msg: dict) -> None:
        if self.connected:
            self.runner.submit(self.session.send(msg))

    def _start_stream(self) -> None:
        preset = QUALITY_PRESETS[self.quality]
        self._send({"type": "stream_start", "monitor": self.monitor, **preset})

    def _display_changed(self, _index: int) -> None:
        self.monitor = self.display_box.currentData()
        self._start_stream()
        self.view.setFocus()

    def _set_quality(self, name: str) -> None:
        self.quality = name
        self._start_stream()
        self.toast.show_message(f"Quality: {name}", "info", 1500)

    def _set_view_only(self, on: bool) -> None:
        if on:
            self.view.release_all()
        self.view.input_enabled = not on
        self.view_only.set_icon_name("eye-off" if on else "eye")
        self.toast.show_message("View only — input paused" if on else "Control resumed", "info", 1500)

    def _set_fullscreen(self, on: bool) -> None:
        self.full.set_icon_name("minimize" if on else "maximize")
        self.showFullScreen() if on else self.showNormal()
        self.view.setFocus()

    def open_files(self) -> None:
        if not self.connected:
            return
        if self.files_window is None:
            self.files_window = FilesWindow(self.runner, self.session, self.remote, owns_session=False)
            self.files_window.destroyed.connect(lambda *_: setattr(self, "files_window", None))
        self.files_window.show()
        self.files_window.raise_()
        self.files_window.activateWindow()

    def send_files(self, paths: list[Path]) -> None:
        if not self.connected:
            return
        if not self.info.get("allow_files", True):
            self.toast.show_message("File transfer is turned off on that computer", "error")
            return

        async def destination() -> str:
            if self.remote_downloads is None:
                self.remote_downloads = (await self.file_client.places()).get("downloads", "")
            return self.remote_downloads

        def start(folder: str) -> None:
            self.transfers.upload(self.file_client, paths, folder, "Downloads")

        self.runner.run(destination(), start,
                        lambda exc: self.toast.show_message(f"Can't send files: {exc}", "error", 5000))

    def _place_transfer_card(self) -> None:
        rows = self.transfers.rows()
        if not rows:
            self.transfer_card.hide()
            return
        self.transfer_card.setFixedWidth(380)
        self.transfer_card.adjustSize()
        view = self.view
        self.transfer_card.move(view.width() - self.transfer_card.width() - 16,
                                view.height() - self.transfer_card.height() - 16)
        self.transfer_card.raise_()
        self.transfer_card.show()

    def _transfer_finished(self, direction: str, ok: bool) -> None:
        if ok:
            self.toast.show_message(f"Sent to {self.remote.name}'s Downloads folder", "success")
        QTimer.singleShot(6000, self._dismiss_finished_transfers)

    def _dismiss_finished_transfers(self) -> None:
        for row in self.transfers.rows():
            if row.done and row.detail.text() == "Done":
                row.deleteLater()

    def _set_clipboard_sync(self, on: bool) -> None:
        if self.sync_btn.isChecked() != on:
            self.sync_btn.blockSignals(True)
            self.sync_btn.setChecked(on)
            self.sync_btn.blockSignals(False)
        if not self.connected:
            return

        def done(_reply) -> None:
            self.clipboard.set_enabled(on)
            if on and not self.info.get("clipboard_images", True):
                self.toast.show_message("Clipboard sync on (text only on that computer)", "info", 2500)

        def failed(exc) -> None:
            self.clipboard.set_enabled(False)
            self._set_clipboard_sync_button(False)
            self.toast.show_message(f"Clipboard sync unavailable: {exc}", "error", 4000)

        if not on:
            self.clipboard.set_enabled(False)
        self.runner.run(self.session.request({"type": "clipboard_sync", "enabled": on}), done, failed)

    def _set_clipboard_sync_button(self, on: bool) -> None:
        self.sync_btn.blockSignals(True)
        self.sync_btn.setChecked(on)
        self.sync_btn.blockSignals(False)

    def _type_clipboard(self) -> None:
        text = QGuiApplication.clipboard().text()
        if text:
            self._send({"type": "type_text", "text": text[:10_000]})
            self.toast.show_message(f"Typed {len(text[:10_000])} characters", "success", 1800)
        else:
            self.toast.show_message("Your clipboard has no text", "info", 1800)
        self.view.setFocus()

    def _power(self, action: str) -> None:
        name = POWER_LABELS[action]
        if action in ("restart", "shutdown"):
            if not confirm(self, name, f"{name} {self.remote.name}? Unsaved work there may be lost."):
                return
        self._send({"type": "power", "action": action})

    def _reconnect(self) -> None:
        self.reconnect_requested.emit()
        self.close()

    def _on_message(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "cursor":
            self.view.set_cursor((float(msg.get("x", -1)), float(msg.get("y", -1))))
        elif kind == "error":
            self.view.set_message(msg.get("error", "Error"))
        elif kind == "stream_info":
            self.state_dot.set_role("success")
        elif kind == "clipboard":
            try:
                self.clipboard.receive(Clip.from_message(msg))
            except ValueError:
                pass
        elif kind == "power_result":
            action = POWER_LABELS.get(msg.get("action"), msg.get("action"))
            if msg.get("ok"):
                self.toast.show_message(f"{action} sent to {self.remote.name}", "success")
            else:
                self.toast.show_message(f"{action} failed: {msg.get('error', 'unknown error')}", "error", 6000)

    def _on_closed(self, reason: str) -> None:
        if not self.connected:
            return
        self.connected = False
        self.view.input_enabled = False
        self.view.set_message("")
        self.state_dot.set_role("danger")
        self.stats.setText("Disconnected")
        if reason.rstrip(".") in ("Disconnected", "Closed", ""):
            reason = f"The connection to {self.remote.name or self.remote.address} ended."
        self.overlay.show_reason(reason)

    def _update_stats(self) -> None:
        now = time.monotonic()
        fps = self._frames / max(now - self._frame_clock, 0.001)
        self._frames, self._frame_clock = 0, now
        image = self.view.image
        if image is not None and self.connected:
            rate = f"{fps:.0f} fps" if fps >= 0.5 else "idle"
            self.stats.setText(f"{self.remote.address}  ·  {rate}  ·  {image.width()}×{image.height()}")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.transfer_card.isVisible():
            self._place_transfer_card()
        if self.overlay.isVisible():
            self.overlay.recenter()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.files_window is not None:
            self.files_window.close()
        self.transfers.cancel_all()
        self._alive = False
        self._fps_timer.stop()
        if self.connected:
            self.view.release_all()
            self.connected = False
            self.runner.submit(self.session.close())
        super().closeEvent(event)
