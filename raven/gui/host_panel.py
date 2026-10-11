"""The "This computer" tab: let other computers on the LAN control this one."""

from __future__ import annotations

import datetime

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from raven import autostart, security
from raven.config import SettingsStore
from raven.gui.async_bridge import AsyncRunner
from raven.host.service import HostService
from raven.platform import netinfo, permissions

MIN_PASSWORD = 6


class HostController(QObject):
    """Owns the HostService and reports its events as Qt signals."""

    event = Signal(str, dict)
    state_changed = Signal(bool)
    failed = Signal(str)

    def __init__(self, runner: AsyncRunner, store: SettingsStore):
        super().__init__()
        self.runner = runner
        self.store = store
        self.service = HostService(store, on_event=lambda kind, detail: self.event.emit(kind, detail))
        self.connected_clients = 0
        self.event.connect(self._track)

    @property
    def running(self) -> bool:
        return self.service.running

    def _track(self, kind: str, detail: dict) -> None:
        if kind == "connected":
            self.connected_clients += 1
        elif kind == "disconnected":
            self.connected_clients = max(0, self.connected_clients - 1)

    def start(self) -> None:
        permissions.request()
        self.runner.run(self.service.start(), lambda _: self.state_changed.emit(True), self._fail)

    def stop(self) -> None:
        self.runner.run(self.service.stop(), lambda _: self.state_changed.emit(False), self._fail)

    def restart(self) -> None:
        if self.running:
            self.runner.run(self.service.restart(), lambda _: self.state_changed.emit(True), self._fail)

    def disconnect_all(self) -> None:
        self.runner.submit(self.service.server.disconnect_all())

    def _fail(self, exc: BaseException) -> None:
        message = str(exc)
        if isinstance(exc, OSError) and "address" in message.lower():
            message = f"Port {self.store.settings.host.port} is already in use. Choose another port."
        self.failed.emit(message)
        self.state_changed.emit(self.running)


class SetPasswordDialog(QDialog):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle("Set password")
        form = QFormLayout(self)
        form.addRow(QLabel("Other computers will need this password to control this one."))
        self.first = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
        self.second = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
        form.addRow("Password:", self.first)
        form.addRow("Repeat:", self.second)
        self.error = QLabel()
        self.error.setStyleSheet("color: #c0392b;")
        form.addRow(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._check)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _check(self) -> None:
        if len(self.first.text()) < MIN_PASSWORD:
            self.error.setText(f"Use at least {MIN_PASSWORD} characters.")
        elif self.first.text() != self.second.text():
            self.error.setText("The passwords don't match.")
        else:
            self.accept()


class HostPanel(QWidget):
    notify = Signal(str, str)  # title, message -> tray notification

    def __init__(self, controller: HostController, store: SettingsStore):
        super().__init__()
        self.controller = controller
        self.store = store
        host = store.settings.host

        layout = QVBoxLayout(self)

        self.enable = QCheckBox("Allow other computers on my network to control this computer")
        font = self.enable.font()
        font.setBold(True)
        self.enable.setFont(font)
        self.enable.toggled.connect(self._toggle)
        layout.addWidget(self.enable)
        self.status = QLabel()
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        settings = QGroupBox("Settings")
        form = QFormLayout(settings)
        self.name = QLineEdit(host.name)
        self.name.editingFinished.connect(self._name_changed)
        form.addRow("Computer name:", self.name)
        self.port = QSpinBox(minimum=1024, maximum=65535, value=host.port)
        self.port.editingFinished.connect(self._port_changed)
        form.addRow("Port:", self.port)
        self.password_button = QPushButton()
        self.password_button.clicked.connect(self.change_password)
        form.addRow("Password:", self.password_button)
        self.allow_input = QCheckBox("Allow mouse and keyboard control")
        self.allow_input.setChecked(host.allow_input)
        self.allow_input.toggled.connect(lambda on: self._set("allow_input", on))
        form.addRow(self.allow_input)
        self.allow_power = QCheckBox("Allow sleep, restart and shut down")
        self.allow_power.setChecked(host.allow_power)
        self.allow_power.toggled.connect(lambda on: self._set("allow_power", on))
        form.addRow(self.allow_power)
        self.advertise = QCheckBox("Show this computer to Raven on other computers")
        self.advertise.setChecked(host.advertise)
        self.advertise.toggled.connect(self._advertise_changed)
        form.addRow(self.advertise)
        if autostart.is_supported():
            self.login = QCheckBox("Start Raven when I log in")
            self.login.setChecked(autostart.is_enabled())
            self.login.toggled.connect(self._autostart_changed)
            form.addRow(self.login)
        layout.addWidget(settings)

        if permissions.IS_MAC:
            layout.addWidget(self._mac_permissions_box())

        activity = QGroupBox("Activity")
        act_layout = QVBoxLayout(activity)
        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(500)
        act_layout.addWidget(self.log)
        kick = QPushButton("Disconnect everyone")
        kick.clicked.connect(controller.disconnect_all)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(kick)
        act_layout.addLayout(row)
        layout.addWidget(activity, 1)

        controller.event.connect(self._on_event)
        controller.state_changed.connect(self._on_state)
        controller.failed.connect(self._on_failed)
        self._update_password_button()
        self._on_state(False)

    def _mac_permissions_box(self) -> QGroupBox:
        box = QGroupBox("macOS permissions")
        v = QVBoxLayout(box)
        note = QLabel(
            "To be controlled, Raven needs <b>Screen Recording</b> (to show the screen) and "
            "<b>Accessibility</b> (for mouse and keyboard) in System Settings → Privacy & Security. "
            "Restart Raven after granting them."
        )
        note.setWordWrap(True)
        v.addWidget(note)
        row = QHBoxLayout()
        for label, which in (("Screen Recording…", "screen"), ("Accessibility…", "accessibility")):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, w=which: permissions.open_settings(w))
            row.addWidget(button)
        row.addStretch()
        v.addLayout(row)
        return box

    # ---------------------------------------------------------- settings

    def _set(self, field: str, value) -> None:
        setattr(self.store.settings.host, field, value)
        self.store.save()

    def _name_changed(self) -> None:
        name = self.name.text().strip()
        if name and name != self.store.settings.host.name:
            self._set("name", name)
            self.controller.restart()

    def _port_changed(self) -> None:
        if self.port.value() != self.store.settings.host.port:
            self._set("port", self.port.value())
            self.controller.restart()

    def _advertise_changed(self, on: bool) -> None:
        self._set("advertise", on)
        self.controller.restart()

    def _autostart_changed(self, on: bool) -> None:
        try:
            autostart.set_enabled(on)
        except OSError as exc:
            QMessageBox.warning(self, "Raven", f"Could not change the login item: {exc}")

    def change_password(self) -> bool:
        dialog = SetPasswordDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        self.store.settings.host.set_password(dialog.first.text())
        self.store.save()
        self._update_password_button()
        self._log("Password changed.")
        return True

    def _update_password_button(self) -> None:
        has = self.store.settings.host.has_password
        self.password_button.setText("Change password…" if has else "Set password…")

    # ---------------------------------------------------------- service

    def start_if_enabled(self) -> None:
        if self.store.settings.host.enabled and self.store.settings.host.has_password:
            self.enable.setChecked(True)

    def _toggle(self, on: bool) -> None:
        if on == self.controller.running:
            return
        if on and not self.store.settings.host.has_password and not self.change_password():
            self.enable.blockSignals(True)
            self.enable.setChecked(False)
            self.enable.blockSignals(False)
            return
        self._set("enabled", on)
        self.status.setText("Starting…" if on else "Stopping…")
        if on:
            self.controller.start()
        else:
            self.controller.stop()

    def _on_state(self, running: bool) -> None:
        self.enable.blockSignals(True)
        self.enable.setChecked(running)
        self.enable.blockSignals(False)
        if not running:
            self.status.setText("Other computers cannot connect to this computer.")
            return
        addresses = netinfo.lan_ipv4_addresses() or ["(no network)"]
        fp = security.short_fingerprint(self.controller.service.fingerprint)
        self.status.setText(
            f"Ready. Connect from another computer to <b>{', '.join(addresses)}</b> "
            f"port <b>{self.store.settings.host.port}</b>.<br>"
            f"Fingerprint: <tt>{fp}</tt>"
        )

    def _on_failed(self, message: str) -> None:
        self._log(f"Error: {message}")
        QMessageBox.warning(self, "Raven", message)

    def _on_event(self, kind: str, detail: dict) -> None:
        text = {
            "started": "Remote control turned on.",
            "stopped": "Remote control turned off.",
            "connected": "{client} ({address}) connected.",
            "disconnected": "{client} ({address}) disconnected.",
            "auth_failed": "Failed login attempt from {address}.",
            "rejected": "Refused {address}: {reason}.",
            "power": "{client} requested {action}.",
            "error": "{message}",
        }.get(kind)
        if text:
            line = text.format(**detail)
            self._log(line)
            if kind in ("connected", "auth_failed", "power"):
                self.notify.emit("Raven", line)

    def _log(self, line: str) -> None:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log.appendPlainText(f"{stamp}  {line}")
