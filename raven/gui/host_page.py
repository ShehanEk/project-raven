"""The "This computer" page: let other computers on the LAN control this one."""

from __future__ import annotations

import datetime

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from raven import autostart, security
from raven.config import SettingsStore
from raven.gui.async_bridge import AsyncRunner
from raven.gui.widgets import (
    Avatar,
    Card,
    IconButton,
    SettingRow,
    StatusDot,
    ToggleSwitch,
    button,
    divider,
    label,
)
from raven.host.service import HostService
from raven.platform import netinfo, permissions

MIN_PASSWORD = 6
MAX_ACTIVITY = 40


class HostController(QObject):
    """Owns the HostService and reports its events as Qt signals."""

    event = Signal(str, dict)
    state_changed = Signal(bool)
    failed = Signal(str)
    clients_changed = Signal(int)

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
        elif kind == "stopped":
            self.connected_clients = 0
        else:
            return
        self.clients_changed.emit(self.connected_clients)

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
        self.setMinimumWidth(400)
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 22, 24, 20)
        v.setSpacing(14)
        head = QHBoxLayout()
        head.setSpacing(12)
        head.addWidget(Avatar("key", 40, 20))
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label("Choose a password", "h2"))
        titles.addWidget(label("Other computers need it to connect to this one.", "muted"))
        head.addLayout(titles, 1)
        v.addLayout(head)
        self.first = QLineEdit(echoMode=QLineEdit.EchoMode.Password, placeholderText="Password")
        self.second = QLineEdit(echoMode=QLineEdit.EchoMode.Password, placeholderText="Repeat password")
        v.addWidget(self.first)
        v.addWidget(self.second)
        self.error = label("", "error")
        self.error.hide()
        v.addWidget(self.error)
        row = QHBoxLayout()
        row.addStretch()
        cancel = button("Cancel", "ghost")
        cancel.clicked.connect(self.reject)
        ok = button("Save password", "primary")
        ok.setDefault(True)
        ok.clicked.connect(self._check)
        row.addWidget(cancel)
        row.addWidget(ok)
        v.addLayout(row)

    def _check(self) -> None:
        if len(self.first.text()) < MIN_PASSWORD:
            self.error.setText(f"Use at least {MIN_PASSWORD} characters.")
        elif self.first.text() != self.second.text():
            self.error.setText("The passwords don't match.")
        else:
            self.accept()
            return
        self.error.show()


class _InfoChip(QFrame):
    """Label + value + copy button."""

    def __init__(self, caption: str, on_copy):
        super().__init__()
        self.setObjectName("chip")
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 8, 6, 8)
        h.setSpacing(8)
        v = QVBoxLayout()
        v.setSpacing(1)
        v.addWidget(label(caption, "muted"))
        self.value = label("", "mono", selectable=True)
        v.addWidget(self.value)
        h.addLayout(v, 1)
        copy = IconButton("copy", f"Copy {caption.lower()}", size=30, icon_size=16)
        copy.clicked.connect(lambda: on_copy(self.value.text(), caption))
        h.addWidget(copy)


class HostPage(QWidget):
    notify = Signal(str, str)  # title, message -> tray notification
    toast = Signal(str, str)  # message, kind

    def __init__(self, controller: HostController, store: SettingsStore):
        super().__init__()
        self.setObjectName("page")
        self.controller = controller
        self.store = store
        host = store.settings.host

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(scroll)
        content = QWidget()
        scroll.setWidget(content)
        page = QVBoxLayout(content)
        page.setContentsMargins(36, 32, 36, 32)
        page.setSpacing(18)

        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(label("This computer", "title"))
        titles.addWidget(label("Let your other computers see and control this one.", "subtitle"))
        page.addLayout(titles)
        page.addSpacing(4)

        # ---- hero: remote access switch
        hero = Card(margins=(22, 20, 22, 20), spacing=16)
        top = QHBoxLayout()
        top.setSpacing(16)
        self.hero_icon = Avatar("shield", 52, 26)
        top.addWidget(self.hero_icon)
        text = QVBoxLayout()
        text.setSpacing(3)
        line = QHBoxLayout()
        line.setSpacing(6)
        line.addWidget(label("Remote access", "h2"))
        self.state_dot = StatusDot("muted", 7)
        line.addWidget(self.state_dot)
        self.state_text = label("Off", "muted")
        line.addWidget(self.state_text)
        line.addStretch()
        text.addLayout(line)
        self.hero_hint = label("", "subtitle", wrap=True)
        text.addWidget(self.hero_hint)
        top.addLayout(text, 1)
        self.enable = ToggleSwitch()
        self.enable.toggled.connect(self._toggle)
        top.addWidget(self.enable)
        hero.body.addLayout(top)

        self.info_row = QWidget()
        chips = QHBoxLayout(self.info_row)
        chips.setContentsMargins(0, 0, 0, 0)
        chips.setSpacing(10)
        self.address_chip = _InfoChip("Address", self._copy)
        self.fingerprint_chip = _InfoChip("Fingerprint", self._copy)
        chips.addWidget(self.address_chip, 1)
        chips.addWidget(self.fingerprint_chip, 1)
        hero.body.addWidget(self.info_row)
        page.addWidget(hero)

        # ---- permissions
        perms = Card(spacing=6)
        perms.body.addWidget(label("What other computers can do", "h2"))
        perms.body.addSpacing(4)
        self.allow_input = self._switch(host.allow_input, lambda on: self._set("allow_input", on))
        perms.body.addWidget(SettingRow("mouse", "Mouse & keyboard control",
                                        "Click, type and scroll on this computer. Off means view only.",
                                        self.allow_input))
        perms.body.addWidget(divider())
        self.allow_power = self._switch(host.allow_power, lambda on: self._set("allow_power", on))
        perms.body.addWidget(SettingRow("power", "Power controls",
                                        "Sleep, lock, restart and shut down this computer.", self.allow_power))
        perms.body.addWidget(divider())
        self.allow_files = self._switch(host.allow_files, lambda on: self._set("allow_files", on))
        perms.body.addWidget(SettingRow("files", "File transfer",
                                        "Browse this computer's files, and send files to and from it.",
                                        self.allow_files))
        perms.body.addWidget(divider())
        self.advertise = self._switch(host.advertise, self._advertise_changed)
        perms.body.addWidget(SettingRow("wifi", "Visible on the network",
                                        "Appear automatically in Raven on your other computers.", self.advertise))
        if autostart.is_supported():
            perms.body.addWidget(divider())
            self.login = self._switch(autostart.is_enabled(), self._autostart_changed)
            perms.body.addWidget(SettingRow("login", "Start at login",
                                            "Stay reachable after this computer restarts.", self.login))
        page.addWidget(perms)

        # ---- identity
        ident = Card()
        ident.body.addWidget(label("Name, port & password", "h2"))
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)

        def caption(text: str):
            lbl = label(text, "muted")
            lbl.setMinimumHeight(36)
            lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            return lbl

        self.name = QLineEdit(host.name)
        self.name.setMinimumHeight(36)
        self.name.editingFinished.connect(self._name_changed)
        form.addRow(caption("Computer name"), self.name)
        self.port = QSpinBox(minimum=1024, maximum=65535, value=host.port)
        self.port.setMinimumHeight(36)
        self.port.setMaximumWidth(140)
        self.port.editingFinished.connect(self._port_changed)
        form.addRow(caption("Port"), self.port)
        self.password_button = button("", None, "key")
        self.password_button.setMinimumHeight(36)
        self.password_button.clicked.connect(self.change_password)
        pw_row = QHBoxLayout()
        pw_row.addWidget(self.password_button)
        pw_row.addStretch()
        form.addRow(caption("Password"), pw_row)
        ident.body.addLayout(form)
        page.addWidget(ident)

        if permissions.IS_MAC:
            page.addWidget(self._mac_permissions_card())

        # ---- activity
        activity = Card(spacing=8)
        head = QHBoxLayout()
        head.addWidget(label("Activity", "h2"))
        self.clients_label = label("", "muted")
        head.addWidget(self.clients_label)
        head.addStretch()
        kick = button("Disconnect all", "danger", "x")
        kick.clicked.connect(controller.disconnect_all)
        head.addWidget(kick)
        activity.body.addLayout(head)
        self.activity_list = QVBoxLayout()
        self.activity_list.setSpacing(0)
        activity.body.addLayout(self.activity_list)
        self.activity_empty = label("Nothing yet. Connections and power requests will appear here.", "muted")
        activity.body.addWidget(self.activity_empty)
        page.addWidget(activity)
        page.addStretch()

        controller.event.connect(self._on_event)
        controller.state_changed.connect(self._on_state)
        controller.failed.connect(self._on_failed)
        controller.clients_changed.connect(self._on_clients)
        self._update_password_button()
        self._on_state(False)
        self._on_clients(0)

    @staticmethod
    def _switch(checked: bool, slot) -> ToggleSwitch:
        sw = ToggleSwitch()
        sw.setChecked(checked)
        sw.toggled.connect(slot)
        return sw

    def _mac_permissions_card(self) -> Card:
        card = Card(spacing=6)
        card.body.addWidget(label("macOS permissions", "h2"))
        card.body.addWidget(label(
            "Required to be controlled. Restart Raven after granting them in System Settings.", "muted", wrap=True))
        state = permissions.status()
        for i, (which, icon_name, title, desc) in enumerate((
            ("screen", "monitor", "Screen Recording", "Lets other computers see this screen."),
            ("accessibility", "keyboard", "Accessibility", "Lets other computers use the mouse and keyboard."),
        )):
            if i:
                card.body.addWidget(divider())
            granted = state.get(which)
            if granted:
                control = label("✓ Allowed", "muted")
            else:
                control = button("Open Settings", None, "sliders")
                control.clicked.connect(lambda _=False, w=which: permissions.open_settings(w))
            card.body.addWidget(SettingRow(icon_name, title, desc, control))
        return card

    # ---------------------------------------------------------- settings

    def _copy(self, text: str, what: str) -> None:
        QGuiApplication.clipboard().setText(text)
        self.toast.emit(f"{what} copied", "success")

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
            self._refresh_info()

    def _advertise_changed(self, on: bool) -> None:
        self._set("advertise", on)
        self.controller.restart()

    def _autostart_changed(self, on: bool) -> None:
        try:
            autostart.set_enabled(on)
        except OSError as exc:
            self.toast.emit(f"Could not change the login item: {exc}", "error")

    def change_password(self) -> bool:
        dialog = SetPasswordDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        self.store.settings.host.set_password(dialog.first.text())
        self.store.save()
        self._update_password_button()
        self._add_activity("key", "Password changed")
        self.toast.emit("Password saved", "success")
        return True

    def _update_password_button(self) -> None:
        has = self.store.settings.host.has_password
        self.password_button.setText("Change password…" if has else "Set password…")

    # ---------------------------------------------------------- service

    def start_if_enabled(self) -> None:
        if self.store.settings.host.enabled and self.store.settings.host.has_password:
            self.enable.setChecked(True)

    def set_enabled(self, on: bool) -> None:
        self.enable.setChecked(on)

    def _toggle(self, on: bool) -> None:
        if on == self.controller.running:
            return
        if on and not self.store.settings.host.has_password and not self.change_password():
            self.enable.blockSignals(True)
            self.enable.setChecked(False)
            self.enable.blockSignals(False)
            return
        self._set("enabled", on)
        self.state_text.setText("Starting…" if on else "Stopping…")
        if on:
            self.controller.start()
        else:
            self.controller.stop()

    def _on_state(self, running: bool) -> None:
        self.enable.blockSignals(True)
        self.enable.setChecked(running)
        self.enable.blockSignals(False)
        self.state_dot.set_role("success" if running else "muted")
        self.state_text.setText("On" if running else "Off")
        self.info_row.setVisible(running)
        if running:
            self.hero_hint.setText("Your other computers on this network can connect with the password.")
            self._refresh_info()
        else:
            self.hero_hint.setText("Turn on to let your other computers see and control this one.")

    def _refresh_info(self) -> None:
        addresses = netinfo.lan_ipv4_addresses()
        port = self.store.settings.host.port
        self.address_chip.value.setText(f"{addresses[0]}:{port}" if addresses else f"(no network):{port}")
        if len(addresses) > 1:
            self.address_chip.setToolTip("Also: " + ", ".join(addresses[1:]))
        self.fingerprint_chip.value.setText(security.short_fingerprint(self.controller.service.fingerprint))
        self.fingerprint_chip.setToolTip(
            "Check that this matches what Raven shows when you connect for the first time.")

    def _on_clients(self, count: int) -> None:
        self.clients_label.setText(
            "· nobody connected" if count == 0 else f"· {count} connected" if count > 1 else "· 1 device connected")

    def _on_failed(self, message: str) -> None:
        self._add_activity("alert", message, "danger")
        self.toast.emit(message, "error")

    def _on_event(self, kind: str, detail: dict) -> None:
        spec = {
            "started": ("shield", "Remote access turned on", "success"),
            "stopped": ("shield", "Remote access turned off", "muted"),
            "connected": ("monitor", "{client} connected from {address}", "success"),
            "disconnected": ("x", "{client} disconnected", "muted"),
            "auth_failed": ("alert", "Wrong password from {address}", "warning"),
            "rejected": ("alert", "Refused {address}: {reason}", "warning"),
            "power": ("power", "{client} requested {action}", "accent"),
            "file_received": ("download", "Received {name} from {client}", "accent"),
            "file_sent": ("upload", "{client} downloaded {name}", "accent"),
            "error": ("alert", "{message}", "danger"),
        }.get(kind)
        if not spec:
            return
        icon_name, template, role = spec
        text = template.format(**detail)
        self._add_activity(icon_name, text, role)
        if kind in ("connected", "auth_failed", "power", "file_received"):
            self.notify.emit("Raven", text)

    def _add_activity(self, icon_name: str, text: str, role: str = "accent") -> None:
        self.activity_empty.hide()
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 7, 0, 7)
        h.setSpacing(10)
        h.addWidget(StatusDot(role, 6))
        h.addWidget(label(text, wrap=True), 1)
        h.addWidget(label(datetime.datetime.now().strftime("%H:%M"), "muted"))
        self.activity_list.insertWidget(0, row)
        while self.activity_list.count() > MAX_ACTIVITY:
            item = self.activity_list.takeAt(self.activity_list.count() - 1)
            if item.widget():
                item.widget().deleteLater()
