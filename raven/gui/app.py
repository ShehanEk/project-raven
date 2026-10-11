"""Main window, tray icon and application start-up."""

from __future__ import annotations

import sys

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QMessageBox, QSystemTrayIcon, QTabWidget

from raven import APP_NAME, __version__
from raven.config import SettingsStore
from raven.gui.async_bridge import AsyncRunner, UiCaller
from raven.gui.host_panel import HostController, HostPanel
from raven.gui.remote_panel import RemotePanel


def make_icon(active: bool = False) -> QIcon:
    icon = QIcon()
    for size in (16, 32, 64, 128, 256):
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
        p.fillPath(path, QColor("#1f6feb") if active else QColor("#30363d"))
        # A simple raven-ish chevron: a wing over a dot.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("white"))
        s = size / 16
        wing = QPainterPath(QPointF(3 * s, 10 * s))
        wing.lineTo(8 * s, 4 * s)
        wing.lineTo(13 * s, 10 * s)
        wing.lineTo(11 * s, 10 * s)
        wing.lineTo(8 * s, 6.6 * s)
        wing.lineTo(5 * s, 10 * s)
        wing.closeSubpath()
        p.drawPath(wing)
        p.drawEllipse(QPointF(8 * s, 11.2 * s), 1.3 * s, 1.3 * s)
        p.end()
        icon.addPixmap(pix)
    return icon


class MainWindow(QMainWindow):
    def __init__(self, runner: AsyncRunner, store: SettingsStore):
        super().__init__()
        self.runner = runner
        self.store = store
        self.quitting = False
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_icon())
        self.resize(680, 600)

        self.host_controller = HostController(runner, store)
        self.remote_panel = RemotePanel(runner, store)
        self.host_panel = HostPanel(self.host_controller, store)
        tabs = QTabWidget()
        tabs.addTab(self.remote_panel, "Other computers")
        tabs.addTab(self.host_panel, "This computer")
        self.setCentralWidget(tabs)
        if store.settings.host.enabled and not store.settings.remotes:
            tabs.setCurrentWidget(self.host_panel)

        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(make_icon(), self)
            self.tray.setToolTip(APP_NAME)
            menu = self.tray_menu = QMenu()
            menu.addAction("Open Raven", self.show_window)
            self.tray_toggle = QAction("Allow remote control", self, checkable=True)
            self.tray_toggle.toggled.connect(self.host_panel.enable.setChecked)
            menu.addAction(self.tray_toggle)
            menu.addSeparator()
            menu.addAction("Quit", self.quit)
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self._tray_activated)
            self.tray.show()
            self.host_panel.notify.connect(
                lambda title, msg: self.tray.showMessage(title, msg, QSystemTrayIcon.MessageIcon.Information, 5000)
            )
        self.host_controller.state_changed.connect(self._host_state)
        self.host_panel.start_if_enabled()

    def _host_state(self, running: bool) -> None:
        if self.tray:
            self.tray.setIcon(make_icon(running))
            self.tray.setToolTip(f"{APP_NAME} — {'remote control on' if running else 'remote control off'}")
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
                APP_NAME, "Raven is still running so other computers can connect. Quit from this icon.",
                QSystemTrayIcon.MessageIcon.Information, 4000,
            )
            return
        self.quit()
        event.accept()

    def quit(self) -> None:
        if self.quitting:
            return
        self.quitting = True
        self.remote_panel.shutdown()
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
    if sys.platform == "win32":
        app.setFont(QFont("Segoe UI", 9))

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
