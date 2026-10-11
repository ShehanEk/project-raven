"""Window that shows a remote screen and forwards mouse and keyboard to it."""

from __future__ import annotations

import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QToolBar,
    QToolButton,
    QWidget,
)

from raven.client.session import RemoteSession
from raven.config import RemoteHost, local_os
from raven.gui.async_bridge import AsyncRunner
from raven.gui.keymap import key_name, swap_command_and_control

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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(320, 200)
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
        p.fillRect(self.rect(), QColor(24, 24, 27))
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
            p.setPen(QColor(230, 230, 235))
            font = p.font()
            font.setPointSize(font.pointSize() + 3)
            p.setFont(font)
            if not rect.isNull():
                p.fillRect(self.rect(), QColor(0, 0, 0, 150))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.message)

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


class ViewerWindow(QMainWindow):
    frame_ready = Signal(QImage, tuple)
    message_received = Signal(dict)
    closed = Signal(str)

    def __init__(self, runner: AsyncRunner, session: RemoteSession, remote: RemoteHost, info: dict):
        super().__init__()
        self.runner = runner
        self.session = session
        self.remote = remote
        self.info = info
        self.connected = True
        self._frames = 0
        self._frame_clock = time.monotonic()
        self.setWindowTitle(f"{remote.name or remote.address} — Raven")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self.view = ScreenView(self)
        self.view.send.connect(self._send)
        self.setCentralWidget(self.view)
        self._build_toolbar()
        self.fps_label = QLabel()
        self.statusBar().addPermanentWidget(self.fps_label)
        self.statusBar().showMessage(f"Connected to {remote.name} ({remote.address})")

        self.frame_ready.connect(self.view.set_frame)
        self.message_received.connect(self._on_message)
        self.closed.connect(self._on_closed)
        self._fps_timer = QTimer(self, interval=1000)
        self._fps_timer.timeout.connect(self._update_fps)
        self._fps_timer.start()

        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.resize(int(screen.width() * 0.8), int(screen.height() * 0.8))

        self.frame_ready.connect(self._count_frame)
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

    def _build_toolbar(self) -> None:
        bar = QToolBar("Remote")
        bar.setMovable(False)
        bar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.addToolBar(bar)

        self.monitor_box = QComboBox()
        for m in self.info.get("monitors", []):
            self.monitor_box.addItem(f"Display {m['index']} ({m['width']}×{m['height']})", m["index"])
        self.monitor_box.currentIndexChanged.connect(lambda _: self._start_stream())
        self.monitor_box.setEnabled(self.monitor_box.count() > 1)
        bar.addWidget(self.monitor_box)

        self.quality_box = QComboBox()
        self.quality_box.addItems(list(QUALITY_PRESETS))
        self.quality_box.setCurrentText("Balanced")
        self.quality_box.currentIndexChanged.connect(lambda _: self._start_stream())
        bar.addWidget(self.quality_box)
        bar.addSeparator()

        self.view_only = QAction("View only", self, checkable=True)
        self.view_only.toggled.connect(self._set_view_only)
        if not self.info.get("allow_input", True):
            self.view_only.setChecked(True)
            self.view_only.setEnabled(False)
            self.view_only.setToolTip("Mouse and keyboard control is turned off on that computer.")
        bar.addAction(self.view_only)

        self.swap = QAction("Swap ⌘/Ctrl", self, checkable=True)
        self.swap.setToolTip("Press Command on a Mac to send Control to a PC, and vice versa.")
        remote_mac = self.remote.os == "macos"
        self.swap.setChecked(remote_mac != (local_os() == "macos"))
        self.swap.toggled.connect(lambda on: setattr(self.view, "swap_cmd_ctrl", on))
        self.view.swap_cmd_ctrl = self.swap.isChecked()
        bar.addAction(self.swap)

        paste = QAction("Type clipboard", self)
        paste.setToolTip("Type the text on your clipboard into the remote computer.")
        paste.triggered.connect(self._type_clipboard)
        bar.addAction(paste)
        bar.addSeparator()

        full = QAction("Full screen", self, checkable=True)
        full.toggled.connect(lambda on: self.showFullScreen() if on else self.showNormal())
        bar.addAction(full)

        power_menu = QMenu(self)
        for action, label in POWER_LABELS.items():
            power_menu.addAction(label, lambda a=action: self._power(a))
        power_button = QToolButton()
        power_button.setText("Power")
        power_button.setMenu(power_menu)
        power_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        power_button.setEnabled(self.info.get("allow_power", True))
        bar.addWidget(power_button)

    # ---------------------------------------------------------- actions

    def _send(self, msg: dict) -> None:
        if self.connected:
            self.runner.submit(self.session.send(msg))

    def _start_stream(self) -> None:
        preset = QUALITY_PRESETS[self.quality_box.currentText()]
        monitor = self.monitor_box.currentData() or 1
        self._send({"type": "stream_start", "monitor": monitor, **preset})

    def _set_view_only(self, on: bool) -> None:
        if on:
            self.view.release_all()
        self.view.input_enabled = not on

    def _type_clipboard(self) -> None:
        text = QGuiApplication.clipboard().text()
        if text:
            self._send({"type": "type_text", "text": text[:10_000]})
        self.view.setFocus()

    def _power(self, action: str) -> None:
        label = POWER_LABELS[action]
        if action in ("restart", "shutdown"):
            answer = QMessageBox.question(
                self, label, f"{label} {self.remote.name}? Unsaved work there may be lost."
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._send({"type": "power", "action": action})

    def _on_message(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "cursor":
            self.view.set_cursor((float(msg.get("x", -1)), float(msg.get("y", -1))))
        elif kind == "error":
            self.view.set_message(msg.get("error", "Error"))
        elif kind == "stream_info":
            self.statusBar().showMessage(
                f"{self.remote.name}: display {msg.get('monitor')} at {msg.get('width')}×{msg.get('height')}"
            )
        elif kind == "power_result":
            action = POWER_LABELS.get(msg.get("action"), msg.get("action"))
            if msg.get("ok"):
                self.statusBar().showMessage(f"{action} sent to {self.remote.name}.")
            else:
                QMessageBox.warning(self, "Power", f"{action} failed: {msg.get('error', 'unknown error')}")

    def _on_closed(self, reason: str) -> None:
        if not self.connected:
            return
        self.connected = False
        self.view.input_enabled = False
        self.view.set_message(f"Disconnected\n{reason}")
        self.statusBar().showMessage("Disconnected")

    def _update_fps(self) -> None:
        now = time.monotonic()
        fps = self._frames / max(now - self._frame_clock, 0.001)
        self._frames, self._frame_clock = 0, now
        image = self.view.image
        if image is not None and self.connected:
            self.fps_label.setText(f"{fps:.0f} fps · {image.width()}×{image.height()}")

    def closeEvent(self, event) -> None:  # noqa: N802
        self._alive = False
        self._fps_timer.stop()
        if self.connected:
            self.view.release_all()
            self.connected = False
            self.runner.submit(self.session.close())
        super().closeEvent(event)
