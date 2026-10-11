"""The "Other computers" tab: find, connect to, and power-control remote machines."""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from raven import DEFAULT_PORT, wol
from raven.client.session import RemoteSession
from raven.config import RemoteHost, SettingsStore
from raven.discovery import Browser, DiscoveredHost
from raven.gui.async_bridge import AsyncRunner
from raven.gui.connect_flow import ConnectFlow
from raven.gui.viewer import POWER_LABELS, ViewerWindow
from raven.platform.netinfo import normalize_mac

log = logging.getLogger(__name__)

OS_LABELS = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}


class _DiscoverySignals(QObject):
    found = Signal(object)
    lost = Signal(str)


class RemoteDialog(QDialog):
    """Add or edit a computer by address."""

    def __init__(self, parent: QWidget, remote: RemoteHost | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit computer" if remote else "Add computer")
        form = QFormLayout(self)
        self.name = QLineEdit(remote.name if remote else "")
        self.name.setPlaceholderText("Optional")
        self.address = QLineEdit(remote.address if remote else "")
        self.address.setPlaceholderText("e.g. 192.168.1.20 or my-mac.local")
        self.port = QSpinBox(minimum=1, maximum=65535, value=remote.port if remote else DEFAULT_PORT)
        self.mac = QLineEdit(remote.mac if remote else "")
        self.mac.setPlaceholderText("Filled in automatically after the first connection")
        form.addRow("Name:", self.name)
        form.addRow("Address:", self.address)
        form.addRow("Port:", self.port)
        form.addRow("MAC (for Wake):", self.mac)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._check)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _check(self) -> None:
        if not self.address.text().strip():
            QMessageBox.warning(self, "Raven", "Enter the computer's IP address or hostname.")
        elif self.mac.text().strip() and not normalize_mac(self.mac.text()):
            QMessageBox.warning(self, "Raven", "That MAC address doesn't look right (e.g. a4:83:e7:12:34:56).")
        else:
            self.accept()

    def apply(self, remote: RemoteHost) -> None:
        remote.name = self.name.text().strip() or self.address.text().strip()
        remote.address = self.address.text().strip()
        remote.port = self.port.value()
        remote.mac = normalize_mac(self.mac.text())


class RemotePanel(QWidget):
    def __init__(self, runner: AsyncRunner, store: SettingsStore):
        super().__init__()
        self.runner = runner
        self.store = store
        self.discovered: dict[str, DiscoveredHost] = {}
        self.viewers: list[ViewerWindow] = []
        self.busy = False

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Computers on your network running Raven with remote control turned on:"))
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self.open_viewer())
        self.list.currentItemChanged.connect(lambda *_: self._update_buttons())
        layout.addWidget(self.list, 1)

        manage = QHBoxLayout()
        for label, slot in (("Add…", self.add_remote), ("Edit…", self.edit_remote), ("Remove", self.remove_remote)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            manage.addWidget(button)
            setattr(self, f"_{label.strip('…').lower()}_button", button)
        manage.addStretch()
        layout.addLayout(manage)

        actions = QHBoxLayout()
        self.connect_button = QPushButton("Connect")
        self.connect_button.setDefault(True)
        self.connect_button.clicked.connect(self.open_viewer)
        actions.addWidget(self.connect_button)
        self.power_buttons = {}
        for action, label in POWER_LABELS.items():
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, a=action: self.power(a))
            actions.addWidget(button)
            self.power_buttons[action] = button
        self.wake_button = QPushButton("Wake")
        self.wake_button.setToolTip("Send a Wake-on-LAN packet to turn the computer on or wake it up.")
        self.wake_button.clicked.connect(self.wake)
        actions.addWidget(self.wake_button)
        layout.addLayout(actions)

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self._signals = _DiscoverySignals()
        self._signals.found.connect(self._on_found)
        self._signals.lost.connect(self._on_lost)
        self.browser: Browser | None = None
        try:
            self.browser = Browser(self._signals.found.emit, self._signals.lost.emit)
        except Exception as exc:  # noqa: BLE001 - discovery is optional
            log.warning("discovery unavailable: %s", exc)
        self.refresh()

    # ---------------------------------------------------------- list

    def _selected(self) -> RemoteHost | None:
        item = self.list.currentItem()
        if not item:
            return None
        kind, key = item.data(Qt.ItemDataRole.UserRole)
        if kind == "saved":
            return self.store.settings.find_remote(key)
        found = self.discovered.get(key)
        if not found:
            return None
        return RemoteHost(name=found.name, address=found.address, port=found.port, os=found.os)

    def refresh(self) -> None:
        current = self.list.currentItem()
        current_key = current.data(Qt.ItemDataRole.UserRole) if current else None
        self.list.clear()
        saved_fps = set()
        online_fps = {d.fingerprint for d in self.discovered.values()}
        for r in sorted(self.store.settings.remotes, key=lambda r: r.name.lower()):
            saved_fps.add(r.fingerprint)
            online = " · online" if r.fingerprint and r.fingerprint in online_fps else ""
            os_label = OS_LABELS.get(r.os, "")
            text = f"{r.name or r.address}\n{r.address}:{r.port}" + (f" · {os_label}" if os_label else "") + online
            self._add_item(text, ("saved", r.id))
        for name, d in sorted(self.discovered.items()):
            if d.fingerprint in saved_fps:
                continue
            text = f"{d.name}\n{d.address}:{d.port} · {OS_LABELS.get(d.os, d.os)} · found on network"
            self._add_item(text, ("found", name))
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == current_key:
                self.list.setCurrentRow(i)
                break
        else:
            if self.list.count():
                self.list.setCurrentRow(0)
        self._update_buttons()

    def _add_item(self, text: str, key: tuple[str, str]) -> None:
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, key)
        self.list.addItem(item)

    def _update_buttons(self) -> None:
        remote = self._selected()
        enabled = remote is not None and not self.busy
        for button in (self.connect_button, *self.power_buttons.values()):
            button.setEnabled(enabled)
        self.wake_button.setEnabled(bool(remote and remote.mac) and not self.busy)
        saved = bool(remote and self.store.settings.find_remote(remote.id))
        self._edit_button.setEnabled(saved)
        self._remove_button.setEnabled(saved)

    def _on_found(self, host: DiscoveredHost) -> None:
        self.discovered[host.service_name] = host
        # Follow saved computers to their new address when DHCP moves them.
        for r in self.store.settings.remotes:
            if r.fingerprint and r.fingerprint == host.fingerprint and (r.address, r.port) != (host.address, host.port):
                r.address, r.port = host.address, host.port
                self.store.save()
        self.refresh()

    def _on_lost(self, service_name: str) -> None:
        if self.discovered.pop(service_name, None):
            self.refresh()

    # ---------------------------------------------------------- manage

    def add_remote(self) -> None:
        dialog = RemoteDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            remote = RemoteHost()
            dialog.apply(remote)
            self.store.settings.remotes.append(remote)
            self.store.save()
            self.refresh()

    def edit_remote(self) -> None:
        remote = self._selected()
        if not remote:
            return
        dialog = RemoteDialog(self, remote)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            dialog.apply(remote)
            self.store.save()
            self.refresh()

    def remove_remote(self) -> None:
        remote = self._selected()
        if not remote or not self.store.settings.find_remote(remote.id):
            return
        answer = QMessageBox.question(self, "Remove", f"Forget {remote.name}? Its saved password is deleted too.")
        if answer == QMessageBox.StandardButton.Yes:
            self.store.settings.remotes.remove(remote)
            self.store.save()
            self.refresh()

    # ---------------------------------------------------------- actions

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.busy = busy
        self.status.setText(message)
        self._update_buttons()

    def _with_session(self, remote: RemoteHost, verb: str, on_ready) -> None:
        self._set_busy(True, f"{verb} {remote.name or remote.address}…")

        def ready(session: RemoteSession, info: dict) -> None:
            self._set_busy(False)
            self.refresh()
            on_ready(session, info)

        def fail(message: str) -> None:
            self._set_busy(False, message)
            self.refresh()
            if message:
                QMessageBox.warning(self, "Raven", message)

        ConnectFlow(self, self.runner, self.store, remote, ready, fail).start()

    def open_viewer(self) -> None:
        remote = self._selected()
        if not remote or self.busy:
            return

        def ready(session: RemoteSession, info: dict) -> None:
            viewer = ViewerWindow(self.runner, session, remote, info)
            self.viewers.append(viewer)
            viewer.destroyed.connect(lambda *_: self.viewers.remove(viewer) if viewer in self.viewers else None)
            viewer.show()
            viewer.raise_()
            viewer.activateWindow()

        self._with_session(remote, "Connecting to", ready)

    def power(self, action: str) -> None:
        remote = self._selected()
        if not remote or self.busy:
            return
        label = POWER_LABELS[action]
        if action in ("restart", "shutdown"):
            answer = QMessageBox.question(
                self, label, f"{label} {remote.name or remote.address}? Unsaved work there may be lost."
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        def ready(session: RemoteSession, info: dict) -> None:
            async def send():
                try:
                    await session.power(action)
                finally:
                    await session.close()

            self._set_busy(True, f"Sending {label.lower()}…")
            self.runner.run(
                send(),
                lambda _: self._set_busy(False, f"{label} sent to {remote.name}."),
                lambda exc: self._set_busy(False, f"{label} failed: {exc}"),
            )

        self._with_session(remote, "Connecting to", ready)

    def wake(self) -> None:
        remote = self._selected()
        if not remote or not remote.mac:
            return
        try:
            wol.wake(remote.mac, remote.address)
        except (OSError, ValueError) as exc:
            self.status.setText(f"Could not send wake packet: {exc}")
            return
        self.status.setText(
            f"Wake packet sent to {remote.name}. It may take a few seconds to come online."
        )

    def shutdown(self) -> None:
        for viewer in list(self.viewers):
            viewer.close()
        if self.browser:
            self.browser.close()
