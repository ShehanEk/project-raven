"""Connect -> verify identity -> authenticate, with the dialogs in between."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from raven import security
from raven.client.session import AuthError, RavenError, RemoteSession
from raven.config import RemoteHost, SettingsStore
from raven.gui.async_bridge import AsyncRunner

MAX_PASSWORD_TRIES = 3


class PasswordDialog(QDialog):
    def __init__(self, parent: QWidget, host_name: str, error: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Password")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Enter the Raven password for <b>{_html(host_name)}</b>:"))
        if error:
            err = QLabel(error)
            err.setStyleSheet("color: #c0392b;")
            layout.addWidget(err)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.password)
        self.remember = QCheckBox("Remember on this computer")
        self.remember.setChecked(True)
        layout.addWidget(self.remember)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ConnectFlow:
    """Produces an authenticated :class:`RemoteSession` for ``remote``.

    ``on_ready(session, info)`` or ``on_fail(message)`` is called exactly once
    on the GUI thread (``on_fail("")`` means the user cancelled).
    """

    def __init__(
        self,
        parent: QWidget,
        runner: AsyncRunner,
        store: SettingsStore,
        remote: RemoteHost,
        on_ready: Callable[[RemoteSession, dict], None],
        on_fail: Callable[[str], None],
    ):
        self.parent = parent
        self.runner = runner
        self.store = store
        self.remote = remote
        self.on_ready = on_ready
        self.on_fail = on_fail
        self.tries = 0
        self.last_error = ""
        self.trusted_fingerprint = ""
        self.session: RemoteSession | None = None

    def start(self) -> None:
        self.session = RemoteSession(self.remote.address, self.remote.port)
        self.runner.run(self.session.connect(), self._connected, self._failed)

    def _failed(self, exc: BaseException) -> None:
        self._close()
        self.on_fail(str(exc) if isinstance(exc, RavenError) else f"Unexpected error: {exc}")

    def _close(self) -> None:
        if self.session:
            self.runner.submit(self.session.close())

    def _connected(self, hello) -> None:
        if not self._trust(hello):
            self._close()
            self.on_fail("")
            return
        remote = self.remote
        if self.tries == 0 and remote.saved_key and remote.saved_salt == hello.salt.hex():
            self._authenticate(bytes.fromhex(remote.saved_key), remember=True)
            return
        dialog = PasswordDialog(self.parent, hello.name or remote.address, self.last_error)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.password.text():
            self._close()
            self.on_fail("")
            return
        password, remember = dialog.password.text(), dialog.remember.isChecked()
        self.runner.run(
            asyncio.to_thread(self.session.derive_key, password),
            lambda key: self._authenticate(key, remember),
            self._failed,
        )

    def _trust(self, hello) -> bool:
        known = self.remote.fingerprint
        short = security.short_fingerprint(hello.fingerprint)
        if hello.fingerprint in (known, self.trusted_fingerprint):
            return True
        if known:
            answer = QMessageBox.warning(
                self.parent,
                "Identity changed",
                f"<b>{_html(hello.name)}</b> presented a different identity than last time.<br><br>"
                "This is expected only if Raven was reinstalled or its settings were reset on that "
                "computer. Otherwise someone on your network may be impersonating it.<br><br>"
                f"New fingerprint: <tt>{short}</tt><br><br>Trust the new identity?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        else:
            answer = QMessageBox.question(
                self.parent,
                "New computer",
                f"Connecting to <b>{_html(hello.name)}</b> for the first time.<br><br>"
                f"Fingerprint: <tt>{short}</tt><br><br>"
                "Check that it matches the fingerprint shown in Raven on that computer "
                "(This computer tab). Continue?",
            )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self.trusted_fingerprint = hello.fingerprint
        return True

    def _authenticate(self, key: bytes, remember: bool) -> None:
        def done(info: dict) -> None:
            hello = self.session.hello
            r = self.remote
            r.name = info.get("name") or hello.name or r.name
            r.os = info.get("os") or hello.os
            r.mac = info.get("mac") or r.mac
            r.fingerprint = hello.fingerprint
            r.saved_key = key.hex() if remember else ""
            r.saved_salt = hello.salt.hex() if remember else ""
            if self.store.settings.find_remote(r.id) is None:
                self.store.settings.remotes.append(r)
            self.store.save()
            self.on_ready(self.session, info)

        def failed(exc: BaseException) -> None:
            if not isinstance(exc, AuthError):
                self._failed(exc)
                return
            self.remote.saved_key = self.remote.saved_salt = ""
            self.tries += 1
            if self.tries >= MAX_PASSWORD_TRIES:
                self.on_fail(str(exc))
                return
            # The host closes the connection after a failed login; reconnect.
            self.last_error = str(exc)
            self.start()

        self.runner.run(self.session.authenticate(key), done, failed)


def _html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
