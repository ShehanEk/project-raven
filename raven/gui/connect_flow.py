"""Connect -> verify identity -> authenticate, with the dialogs in between."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from raven import security
from raven.client.session import AuthError, RavenError, RemoteSession
from raven.config import RemoteHost, SettingsStore
from raven.gui.async_bridge import AsyncRunner
from raven.gui.widgets import Avatar, button, label

MAX_PASSWORD_TRIES = 3


class _Dialog(QDialog):
    """Header with an icon tile, a body, and right-aligned buttons."""

    def __init__(self, parent: QWidget, title: str, subtitle: str, icon_name: str, role: str = "accent"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(24, 22, 24, 20)
        self.body.setSpacing(14)
        head = QHBoxLayout()
        head.setSpacing(14)
        head.addWidget(Avatar(icon_name, 44, 22, role), 0, Qt.AlignmentFlag.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(3)
        titles.addWidget(label(title, "h2"))
        titles.addWidget(label(subtitle, "muted", wrap=True))
        head.addLayout(titles, 1)
        self.body.addLayout(head)

    def add_buttons(self, ok_text: str, cancel_text: str = "Cancel", kind: str = "primary"):
        row = QHBoxLayout()
        row.addStretch()
        cancel = button(cancel_text, "ghost")
        cancel.clicked.connect(self.reject)
        ok = button(ok_text, kind)
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        self.body.addLayout(row)
        return ok


class PasswordDialog(_Dialog):
    def __init__(self, parent: QWidget, host_name: str, error: str = ""):
        super().__init__(parent, f"Connect to {host_name}",
                         "Enter the Raven password set on that computer.", "lock")
        self.password = QLineEdit(echoMode=QLineEdit.EchoMode.Password, placeholderText="Password")
        self.body.addWidget(self.password)
        if error:
            self.body.addWidget(label(error, "error"))
        self.remember = QCheckBox("Remember on this computer")
        self.remember.setChecked(True)
        self.body.addWidget(self.remember)
        self.add_buttons("Connect")
        self.password.setFocus()


class TrustDialog(_Dialog):
    """Show the host's fingerprint so the user can compare it before trusting."""

    def __init__(self, parent: QWidget, host_name: str, fingerprint: str, changed: bool):
        if changed:
            super().__init__(
                parent, "Identity changed",
                f"{host_name} presented a different identity than last time. This is expected only if "
                "Raven was reinstalled or reset there; otherwise someone may be impersonating it.",
                "alert", "warning")
        else:
            super().__init__(
                parent, f"New device: {host_name}",
                "Check that this fingerprint matches the one on that computer's “This computer” page.",
                "shield")
        chip = QFrame()
        chip.setObjectName("chip")
        v = QVBoxLayout(chip)
        v.setContentsMargins(14, 10, 14, 10)
        v.addWidget(label("Fingerprint", "muted"))
        v.addWidget(label(fingerprint, "fingerprint", selectable=True))
        self.body.addWidget(chip)
        self.add_buttons("Trust new identity" if changed else "It matches, continue",
                         kind="danger" if changed else "primary")


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
        alternates: list[tuple[str, int]] | None = None,
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
        # Other addresses where discovery saw a host claiming to be this one.
        # They are only used if the TLS identity matches the pinned one, so an
        # advertisement on the network can't redirect us to an impostor.
        self.alternates = [a for a in (alternates or []) if a != (remote.address, remote.port)]
        self.address = (remote.address, remote.port)

    def start(self) -> None:
        self._connect(self.address, list(self.alternates) if self.remote.fingerprint else [])

    def _connect(self, address: tuple[str, int], remaining: list[tuple[str, int]], first_error=None) -> None:
        self.session = RemoteSession(*address)
        session = self.session

        def connected(hello) -> None:
            if address != (self.remote.address, self.remote.port) and hello.fingerprint != self.remote.fingerprint:
                # Not our computer; never prompt about an unverified address.
                self.runner.submit(session.close())
                self._next(remaining, first_error)
                return
            self.address = address
            self._connected(hello)

        def failed(exc: BaseException) -> None:
            if remaining and isinstance(exc, RavenError):
                self._next(remaining, first_error or exc)
            else:
                self._failed(first_error or exc)

        self.runner.run(session.connect(), connected, failed)

    def _next(self, remaining: list[tuple[str, int]], error) -> None:
        if remaining:
            self._connect(remaining[0], remaining[1:], error)
        else:
            self._failed(error or RavenError(f"Could not reach {self.remote.name or self.remote.address}."))

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
        # The saved key is only ever offered to the exact identity it was
        # saved for; after an identity change the user types the password.
        if (self.tries == 0 and remote.saved_key and remote.saved_salt == hello.salt.hex()
                and remote.fingerprint and hello.fingerprint == remote.fingerprint):
            self._authenticate(bytes.fromhex(remote.saved_key), remember=True)
            return
        dialog = PasswordDialog(self.parent, remote.name or hello.name or remote.address, self.last_error)
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
        # Name the device by what the user saved, not what the host claims.
        name = self.remote.name if known and self.remote.name else (hello.name or self.remote.address)
        dialog = TrustDialog(self.parent, name, short, changed=bool(known))
        if dialog.exec() != QDialog.DialogCode.Accepted:
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
            r.address, r.port = self.address  # may be a verified new address
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

