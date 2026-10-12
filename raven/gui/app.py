"""Main window (sidebar + pages), tray icon and application start-up."""

from __future__ import annotations

import sys

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from raven import APP_NAME, __version__
from raven.config import SettingsStore
from raven.gui import icons
from raven.gui.async_bridge import AsyncRunner, UiCaller
from raven.gui.devices_page import DevicesPage
from raven.gui.host_page import HostController, HostPage
from raven.gui.theme import theme
from raven.gui.widgets import StatusDot, Toast, label
from raven.platform import netinfo


def paint_logo(p: QPainter, rect: QRectF, active: bool = True) -> None:
    size = rect.width()
    path = QPainterPath()
    path.addRoundedRect(rect, size * 0.26, size * 0.26)
    grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
    if active:
        grad.setColorAt(0, QColor("#8b7bff"))
        grad.setColorAt(1, QColor("#4f46e5"))
    else:
        grad.setColorAt(0, QColor("#4b5163"))
        grad.setColorAt(1, QColor("#2b2f3a"))
    p.fillPath(path, grad)
    # A stylised raven wing over a dot.
    s = size / 16
    ox, oy = rect.x(), rect.y()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("white"))
    wing = QPainterPath(QPointF(ox + 3 * s, oy + 10 * s))
    wing.lineTo(ox + 8 * s, oy + 4 * s)
    wing.lineTo(ox + 13 * s, oy + 10 * s)
    wing.lineTo(ox + 11 * s, oy + 10 * s)
    wing.lineTo(ox + 8 * s, oy + 6.6 * s)
    wing.lineTo(ox + 5 * s, oy + 10 * s)
    wing.closeSubpath()
    p.drawPath(wing)
    p.drawEllipse(QPointF(ox + 8 * s, oy + 11.4 * s), 1.3 * s, 1.3 * s)


def make_icon(active: bool = True) -> QIcon:
    icon = QIcon()
    for size in (16, 32, 64, 128, 256):
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_logo(p, QRectF(0, 0, size, size), active)
        p.end()
        icon.addPixmap(pix)
    return icon


class _Logo(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(30, 30)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_logo(p, QRectF(0, 0, 30, 30))


class _StatusChip(QFrame):
    """Sidebar footer showing whether this computer accepts connections."""

    def __init__(self, on_click):
        super().__init__()
        self.setObjectName("statusChip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.on_click = on_click
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 10, 12, 10)
        h.setSpacing(8)
        self.dot = StatusDot("muted", 7)
        h.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(1)
        self.title = label("Remote access off", "h3")
        self.detail = label("This computer can't be controlled", "muted", wrap=True)
        v.addWidget(self.title)
        v.addWidget(self.detail)
        h.addLayout(v, 1)

    def set_state(self, running: bool, port: int, clients: int = 0) -> None:
        self.dot.set_role("success" if running else "muted")
        if running:
            self.title.setText("Remote access on")
            addresses = netinfo.lan_ipv4_addresses()
            where = f"{addresses[0]}:{port}" if addresses else f"port {port}"
            self.detail.setText(f"{where}" + (f" · {clients} connected" if clients else ""))
        else:
            self.title.setText("Remote access off")
            self.detail.setText("This computer can't be controlled")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.on_click()


class MainWindow(QMainWindow):
    def __init__(self, runner: AsyncRunner, store: SettingsStore):
        super().__init__()
        self.runner = runner
        self.store = store
        self.quitting = False
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_icon())
        self.resize(1040, 700)
        self.setMinimumSize(760, 520)

        self.host_controller = HostController(runner, store)
        self.devices_page = DevicesPage(runner, store)
        self.host_page = HostPage(self.host_controller, store)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.devices_page)
        self.stack.addWidget(self.host_page)

        root = QWidget()
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(self._build_sidebar())
        h.addWidget(self.stack, 1)
        self.setCentralWidget(root)

        self.toast = Toast(self.stack)
        self.devices_page.toast.connect(self.show_toast)
        self.host_page.toast.connect(self.show_toast)

        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._build_tray()
        self.host_controller.state_changed.connect(self._host_state)
        self.host_controller.clients_changed.connect(lambda _: self._host_state(self.host_controller.running))
        if store.settings.host.enabled and not store.settings.remotes:
            self.navigate(1)
        self.host_page.start_if_enabled()

    def _build_sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("sidebar")
        side.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        side.setFixedWidth(232)
        v = QVBoxLayout(side)
        v.setContentsMargins(16, 22, 16, 16)
        v.setSpacing(4)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        brand.addWidget(_Logo())
        brand.addWidget(label(APP_NAME, "brand"))
        brand.addStretch()
        v.addLayout(brand)
        v.addSpacing(22)
        v.addWidget(label("MENU", "muted"))
        v.addSpacing(4)

        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        for index, (icon_name, text) in enumerate((("devices", "Devices"), ("home", "This computer"))):
            b = QPushButton("  " + text)
            b.setObjectName("navButton")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.nav.addButton(b, index)
            v.addWidget(b)
            self._bind_nav_icon(b, icon_name)
        self.nav.idClicked.connect(self.navigate)
        self.nav.button(0).setChecked(True)

        v.addStretch()
        self.status_chip = _StatusChip(lambda: self.navigate(1))
        v.addWidget(self.status_chip)
        v.addSpacing(8)
        version = QLabel(f"Raven {__version__} · local network only")
        version.setProperty("role", "muted")
        v.addWidget(version)
        return side

    def _bind_nav_icon(self, b: QPushButton, name: str) -> None:
        def refresh(*_):
            pal = theme.palette
            color = (pal.accent_hover if pal.dark else pal.accent) if b.isChecked() else pal.muted
            b.setIcon(icons.icon(name, color, 18))

        b.toggled.connect(refresh)
        theme.changed.connect(refresh)
        refresh()

    def navigate(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.nav.button(index).setChecked(True)

    def show_toast(self, message: str, kind: str = "success") -> None:
        self.toast.show_message(message, kind, 6000 if kind == "error" else 3500)

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(make_icon(False), self)
        self.tray.setToolTip(APP_NAME)
        menu = self.tray_menu = QMenu()
        menu.addAction("Open Raven", self.show_window)
        self.tray_toggle = QAction("Remote access", self, checkable=True)
        self.tray_toggle.toggled.connect(self.host_page.set_enabled)
        menu.addAction(self.tray_toggle)
        menu.addSeparator()
        menu.addAction("Quit Raven", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()
        self.host_page.notify.connect(
            lambda title, msg: self.tray.showMessage(title, msg, QSystemTrayIcon.MessageIcon.Information, 5000)
        )

    def _host_state(self, running: bool) -> None:
        self.status_chip.set_state(running, self.store.settings.host.port, self.host_controller.connected_clients)
        if self.tray:
            self.tray.setIcon(make_icon(running))
            self.tray.setToolTip(f"{APP_NAME} — remote access {'on' if running else 'off'}")
            self.tray_toggle.blockSignals(True)
            self.tray_toggle.setChecked(running)
            self.tray_toggle.blockSignals(False)

    def _tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:  # noqa: N802
        # Keep running in the tray while this computer accepts connections.
        if not self.quitting and self.tray and self.host_controller.running:
            event.ignore()
            self.hide()
            self.tray.showMessage(
                APP_NAME, "Raven is still running so your other computers can connect. Quit from this icon.",
                QSystemTrayIcon.MessageIcon.Information, 4000,
            )
            return
        self.quit()
        event.accept()

    def quit(self) -> None:
        if self.quitting:
            return
        self.quitting = True
        self.devices_page.shutdown()
        if self.host_controller.running:
            try:
                self.runner.submit(self.host_controller.service.stop()).result(5)
            except Exception:  # noqa: BLE001
                pass
        if self.tray:
            self.tray.hide()
        QApplication.quit()


def run_gui(minimized: bool = False) -> int:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_MacDontSwapCtrlAndMeta)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(make_icon())
    theme.install(app)

    ui = UiCaller()
    runner = AsyncRunner(ui)
    store = SettingsStore()
    try:
        window = MainWindow(runner, store)
    except Exception as exc:  # noqa: BLE001
        QMessageBox.critical(None, APP_NAME, f"Raven could not start: {exc}")
        raise
    if not (minimized and window.tray and store.settings.host.enabled):
        window.show()
    code = app.exec()
    runner.shutdown()
    return code
