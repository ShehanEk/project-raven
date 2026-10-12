"""The Devices page: cards for every computer you can control."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from raven import DEFAULT_PORT, wol
from raven.client.session import RemoteSession
from raven.config import RemoteHost, SettingsStore
from raven.discovery import Browser, DiscoveredHost
from raven.gui import icons
from raven.gui.async_bridge import AsyncRunner
from raven.gui.connect_flow import ConnectFlow
from raven.gui.files_window import FilesWindow
from raven.gui.theme import theme
from raven.gui.viewer import POWER_LABELS, ViewerWindow
from raven.gui.widgets import (
    Avatar,
    Card,
    FlowLayout,
    IconButton,
    StatusDot,
    button,
    confirm,
    divider,
    label,
    plain_tooltip,
)
from raven.platform.netinfo import normalize_mac

log = logging.getLogger(__name__)

OS_LABELS = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}
PROBE_INTERVAL_MS = 20_000


@dataclass
class Device:
    key: str
    remote: RemoteHost
    saved: bool
    online: bool | None  # None = not checked yet


class _DiscoverySignals(QObject):
    found = Signal(object)
    lost = Signal(str)


class DeviceDialog(QDialog):
    """Add or edit a computer by address."""

    def __init__(self, parent: QWidget, remote: RemoteHost | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit device" if remote else "Add device")
        self.setMinimumWidth(420)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(16)
        outer.addWidget(label("Edit device" if remote else "Add a device", "h2"))
        outer.addWidget(label(
            "Enter the address shown on the other computer's “This computer” page.", "muted", wrap=True))
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.name = QLineEdit(remote.name if remote else "")
        self.name.setPlaceholderText("e.g. Office PC")
        self.address = QLineEdit(remote.address if remote else "")
        self.address.setPlaceholderText("192.168.1.20 or my-mac.local")
        self.port = QSpinBox(minimum=1, maximum=65535, value=remote.port if remote else DEFAULT_PORT)
        self.mac = QLineEdit(remote.mac if remote else "")
        self.mac.setPlaceholderText("Filled in after the first connection")
        form.addRow("Name", self.name)
        form.addRow("Address", self.address)
        form.addRow("Port", self.port)
        form.addRow("MAC address", self.mac)
        outer.addLayout(form)
        self.error = label("", "error")
        self.error.hide()
        outer.addWidget(self.error)
        row = QHBoxLayout()
        row.addStretch()
        cancel = button("Cancel", "ghost")
        cancel.clicked.connect(self.reject)
        save = button("Save" if remote else "Add device", "primary")
        save.setDefault(True)
        save.clicked.connect(self._check)
        row.addWidget(cancel)
        row.addWidget(save)
        outer.addLayout(row)

    def _check(self) -> None:
        if not self.address.text().strip():
            self._error("Enter the computer's IP address or hostname.")
        elif self.mac.text().strip() and not normalize_mac(self.mac.text()):
            self._error("That MAC address doesn't look right (e.g. a4:83:e7:12:34:56).")
        else:
            self.accept()

    def _error(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()

    def apply(self, remote: RemoteHost) -> None:
        remote.name = self.name.text().strip() or self.address.text().strip()
        remote.address = self.address.text().strip()
        remote.port = self.port.value()
        remote.mac = normalize_mac(self.mac.text())


class DeviceCard(Card):
    def __init__(self, page: DevicesPage, device: Device, busy_text: str = ""):
        super().__init__(hoverable=True, margins=(18, 18, 18, 16), spacing=14)
        self.setMinimumWidth(300)
        self.setFixedHeight(172)
        remote = device.remote

        top = QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(Avatar("laptop" if remote.os == "macos" else "monitor"), 0, Qt.AlignmentFlag.AlignTop)
        names = QVBoxLayout()
        names.setSpacing(3)
        title = label(remote.name or remote.address, "h2")
        title.setToolTip(plain_tooltip(title.text()))
        names.addWidget(title)
        detail = f"{remote.address}" + (f" · {OS_LABELS[remote.os]}" if remote.os in OS_LABELS else "")
        names.addWidget(label(detail, "muted"))
        top.addLayout(names, 1)
        top.addLayout(self._status(device), 0)
        self.body.addLayout(top)
        tags = QHBoxLayout()
        tags.setSpacing(6)
        tags.setContentsMargins(54, 0, 0, 0)
        for text in self._tags(device):
            tags.addWidget(label(text, "tag"))
        tags.addStretch()
        self.body.addLayout(tags)
        self.body.addStretch()
        self.body.addWidget(divider())

        footer = QHBoxLayout()
        footer.setSpacing(6)
        if busy_text:
            footer.addWidget(StatusDot("accent"))
            footer.addWidget(label(busy_text, "muted"), 1)
        else:
            connect = button("Connect", "primary", "monitor")
            connect.clicked.connect(lambda: page.open_viewer(device.remote))
            footer.addWidget(connect, 1)
            if remote.mac and device.online is False:
                wake = button("Wake", None, "zap")
                wake.clicked.connect(lambda: page.wake(device.remote))
                footer.addWidget(wake)
            sleep = IconButton("moon", "Sleep")
            sleep.clicked.connect(lambda: page.power(device.remote, "sleep"))
            footer.addWidget(sleep)
            power = IconButton("power", "Power options")
            power_menu = QMenu(power)
            for action, icon_name in (("lock", "lock"), ("restart", "restart"), ("shutdown", "power")):
                act = power_menu.addAction(POWER_LABELS[action], lambda a=action: page.power(device.remote, a))
                act.setIcon(icons.icon(icon_name, theme.palette.text, 16))
            power.setMenu(power_menu)
            power.setPopupMode(IconButton.ToolButtonPopupMode.InstantPopup)
            footer.addWidget(power)
            more = IconButton("more", "More")
            more_menu = QMenu(more)
            files_action = more_menu.addAction("Files…", lambda: page.open_files(device.remote))
            files_action.setIcon(icons.icon("files", theme.palette.text, 16))
            wake_action = more_menu.addAction("Wake (Wake-on-LAN)", lambda: page.wake(device.remote))
            wake_action.setIcon(icons.icon("zap", theme.palette.text, 16))
            wake_action.setEnabled(bool(remote.mac))
            if device.saved:
                more_menu.addSeparator()
                edit = more_menu.addAction("Edit…", lambda: page.edit_device(device.remote))
                edit.setIcon(icons.icon("edit", theme.palette.text, 16))
                remove = more_menu.addAction("Remove", lambda: page.remove_device(device.remote))
                remove.setIcon(icons.icon("trash", theme.palette.danger, 16))
            more.setMenu(more_menu)
            more.setPopupMode(IconButton.ToolButtonPopupMode.InstantPopup)
            footer.addWidget(more)
        self.body.addLayout(footer)

    @staticmethod
    def _tags(device: Device) -> list[str]:
        r = device.remote
        tags = []
        if not device.saved:
            tags.append("Found on network")
        elif r.saved_key:
            tags.append("Password saved")
        elif not r.fingerprint:
            tags.append("Not connected yet")
        if r.mac:
            tags.append("Wake-on-LAN")
        return tags

    @staticmethod
    def _status(device: Device) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(2)
        if device.online:
            dot, text = "success", "Online"
        elif device.online is False:
            dot, text = "muted", "Offline"
        else:
            dot, text = "warning", "Checking"
        row.addWidget(StatusDot(dot, 7), 0, Qt.AlignmentFlag.AlignTop)
        lbl = label(text, "muted")
        row.addWidget(lbl, 0, Qt.AlignmentFlag.AlignTop)
        return row


class DevicesPage(QWidget):
    toast = Signal(str, str)  # message, kind

    def __init__(self, runner: AsyncRunner, store: SettingsStore):
        super().__init__()
        self.setObjectName("page")
        self.runner = runner
        self.store = store
        self.discovered: dict[str, DiscoveredHost] = {}
        self.reachable: dict[str, bool] = {}  # remote id -> last probe result
        self.busy: dict[str, str] = {}  # remote id -> what we're doing
        self.viewers: list[ViewerWindow] = []
        self.file_windows: list[FilesWindow] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 32, 36, 24)
        outer.setSpacing(22)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(label("Devices", "title"))
        titles.addWidget(label("Computers on your network you can view, control and power off.", "subtitle"))
        header.addLayout(titles, 1)
        add = button("Add device", "primary", "plus")
        add.clicked.connect(self.add_device)
        header.addWidget(add, 0, Qt.AlignmentFlag.AlignBottom)
        outer.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid_host = QWidget()
        self.grid = FlowLayout(self.grid_host, spacing=16)
        container = QWidget()
        cv = QVBoxLayout(container)
        cv.setContentsMargins(0, 0, 6, 0)
        cv.addWidget(self.grid_host)
        self.empty = self._empty_state()
        cv.addWidget(self.empty)
        cv.addStretch()
        scroll.setWidget(container)
        outer.addWidget(scroll, 1)

        self._signals = _DiscoverySignals()
        self._signals.found.connect(self._on_found)
        self._signals.lost.connect(self._on_lost)
        self.browser: Browser | None = None
        try:
            self.browser = Browser(self._signals.found.emit, self._signals.lost.emit)
        except Exception as exc:  # noqa: BLE001 - discovery is optional
            log.warning("discovery unavailable: %s", exc)

        self._probe_timer = QTimer(self, interval=PROBE_INTERVAL_MS)
        self._probe_timer.timeout.connect(self.probe_all)
        self._probe_timer.start()
        theme.changed.connect(self.refresh)
        self.refresh()
        QTimer.singleShot(300, self.probe_all)

    def _empty_state(self) -> QWidget:
        box = QFrame()
        box.setObjectName("emptyState")
        v = QVBoxLayout(box)
        v.setContentsMargins(40, 48, 40, 48)
        v.setSpacing(10)
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(Avatar("devices", 64, 32), 0, Qt.AlignmentFlag.AlignHCenter)
        v.addSpacing(10)
        title = label("No devices yet", "h2")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(title)
        hint = label(
            "Open Raven on another computer, go to “This computer” and turn on Remote access. "
            "It will show up here automatically — or add it by IP address.", "subtitle", wrap=True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setFixedWidth(420)
        v.addWidget(hint, 0, Qt.AlignmentFlag.AlignHCenter)
        v.addSpacing(6)
        add = button("Add device", None, "plus")
        add.clicked.connect(self.add_device)
        v.addWidget(add, 0, Qt.AlignmentFlag.AlignHCenter)
        return box

    # ---------------------------------------------------------- model

    def devices(self) -> list[Device]:
        online_fps = {d.fingerprint for d in self.discovered.values() if d.fingerprint}
        result = []
        saved_fps = set()
        for r in sorted(self.store.settings.remotes, key=lambda r: (r.name or r.address).lower()):
            saved_fps.add(r.fingerprint)
            if r.fingerprint and r.fingerprint in online_fps:
                online = True
            else:
                online = self.reachable.get(r.id)
            result.append(Device(f"saved:{r.id}", r, True, online))
        for name, d in sorted(self.discovered.items()):
            if d.fingerprint and d.fingerprint in saved_fps:
                continue
            remote = RemoteHost(id=f"found-{name}", name=d.name, address=d.address, port=d.port, os=d.os)
            result.append(Device(f"found:{name}", remote, False, True))
        return result

    def refresh(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        devices = self.devices()
        for device in devices:
            self.grid.addWidget(DeviceCard(self, device, self.busy.get(device.remote.id, "")))
        self.grid_host.setVisible(bool(devices))
        self.empty.setVisible(not devices)
        self.grid.invalidate()

    def _on_found(self, host: DiscoveredHost) -> None:
        # Advertisements are unauthenticated: they only update what we show,
        # never a saved device's address (see alternates_for()).
        self.discovered[host.service_name] = host
        self.refresh()

    def alternates_for(self, remote: RemoteHost) -> list[tuple[str, int]]:
        """Addresses where discovery saw a host claiming to be ``remote``."""
        if not remote.fingerprint:
            return []
        return [(d.address, d.port) for d in self.discovered.values()
                if d.fingerprint == remote.fingerprint and (d.address, d.port) != (remote.address, remote.port)]

    def _on_lost(self, service_name: str) -> None:
        if self.discovered.pop(service_name, None):
            self.refresh()

    def probe_all(self) -> None:
        remotes = list(self.store.settings.remotes)
        if not remotes:
            return

        async def probe(r: RemoteHost) -> tuple[str, bool]:
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection(r.address, r.port), 1.5)
                writer.close()
                return r.id, True
            except (OSError, asyncio.TimeoutError):
                return r.id, False

        async def run():
            return await asyncio.gather(*(probe(r) for r in remotes))

        def done(results) -> None:
            changed = False
            for remote_id, ok in results:
                if self.reachable.get(remote_id) != ok:
                    self.reachable[remote_id] = ok
                    changed = True
            if changed:
                self.refresh()

        self.runner.run(run(), done)

    # ---------------------------------------------------------- manage

    def add_device(self) -> None:
        dialog = DeviceDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            remote = RemoteHost()
            dialog.apply(remote)
            self.store.settings.remotes.append(remote)
            self.store.save()
            self.refresh()
            self.probe_all()

    def edit_device(self, remote: RemoteHost) -> None:
        dialog = DeviceDialog(self, remote)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.apply(remote)
            self.store.save()
            self.reachable.pop(remote.id, None)
            self.refresh()
            self.probe_all()

    def remove_device(self, remote: RemoteHost) -> None:
        if confirm(self, "Remove device", f"Remove {remote.name}? Its saved password is deleted too.") \
                and remote in self.store.settings.remotes:
            self.store.settings.remotes.remove(remote)
            self.store.save()
            self.refresh()

    # ---------------------------------------------------------- actions

    def _set_busy(self, remote: RemoteHost, text: str | None) -> None:
        if text:
            self.busy[remote.id] = text
        else:
            self.busy.pop(remote.id, None)
        self.refresh()

    def _with_session(self, remote: RemoteHost, on_ready) -> None:
        if remote.id in self.busy:
            return
        self._set_busy(remote, f"Connecting to {remote.name or remote.address}…")

        def ready(session: RemoteSession, info: dict) -> None:
            self._set_busy(remote, None)
            self.reachable[remote.id] = True
            on_ready(session, info)

        def fail(message: str) -> None:
            self._set_busy(remote, None)
            if message:
                self.toast.emit(message, "error")

        ConnectFlow(self, self.runner, self.store, remote, ready, fail, self.alternates_for(remote)).start()

    def open_viewer(self, remote: RemoteHost) -> None:
        def ready(session: RemoteSession, info: dict) -> None:
            viewer = ViewerWindow(self.runner, session, remote, info)
            viewer.reconnect_requested.connect(lambda: self.open_viewer(remote))
            self.viewers.append(viewer)
            viewer.destroyed.connect(lambda *_: self.viewers.remove(viewer) if viewer in self.viewers else None)
            viewer.show()
            viewer.raise_()
            viewer.activateWindow()

        self._with_session(remote, ready)

    def open_files(self, remote: RemoteHost) -> None:
        def ready(session: RemoteSession, info: dict) -> None:
            if not info.get("allow_files", True):
                self.toast.emit(f"File transfer is turned off on {remote.name}", "error")
                self.runner.submit(session.close())
                return
            window = FilesWindow(self.runner, session, remote, owns_session=True)
            self.file_windows.append(window)
            window.destroyed.connect(
                lambda *_: self.file_windows.remove(window) if window in self.file_windows else None)
            window.show()
            window.raise_()
            window.activateWindow()

        self._with_session(remote, ready)

    def power(self, remote: RemoteHost, action: str) -> None:
        label_text = POWER_LABELS[action]
        if action in ("restart", "shutdown"):
            if not confirm(self, label_text,
                           f"{label_text} {remote.name or remote.address}? Unsaved work there may be lost."):
                return

        def ready(session: RemoteSession, info: dict) -> None:
            async def send():
                try:
                    await session.power(action)
                finally:
                    await session.close()

            self._set_busy(remote, f"Sending {label_text.lower()}…")

            def sent(_):
                self._set_busy(remote, None)
                self.toast.emit(f"{label_text} sent to {remote.name}", "success")
                if action in ("sleep", "shutdown", "restart"):
                    self.reachable[remote.id] = False
                    QTimer.singleShot(5000, self.probe_all)

            def failed(exc):
                self._set_busy(remote, None)
                self.toast.emit(f"{label_text} failed: {exc}", "error")

            self.runner.run(send(), sent, failed)

        self._with_session(remote, ready)

    def wake(self, remote: RemoteHost) -> None:
        if not remote.mac:
            self.toast.emit("Connect once first so Raven learns this computer's MAC address.", "info")
            return
        try:
            wol.wake(remote.mac, remote.address)
        except (OSError, ValueError) as exc:
            self.toast.emit(f"Could not send wake packet: {exc}", "error")
            return
        self.toast.emit(f"Wake signal sent to {remote.name}", "success")
        for delay in (5000, 15000, 30000):
            QTimer.singleShot(delay, self.probe_all)

    def shutdown(self) -> None:
        self._probe_timer.stop()
        for window in [*self.viewers, *self.file_windows]:
            window.close()
        if self.browser:
            self.browser.close()
