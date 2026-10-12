"""Regression tests for the controller-side fixes from the PR #1 security review.

* A host can't make the controller derive its password proof with weak
  PBKDF2 settings.
* Names and messages from other computers are always shown as plain text.
* A saved password is never sent automatically to a changed identity.
* Network advertisements can't silently re-point a saved device.
"""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtCore = pytest.importorskip("PySide6.QtCore")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from raven import protocol, security  # noqa: E402
from raven.client.session import RavenError, clean_info, parse_hello  # noqa: E402
from raven.config import RemoteHost, SettingsStore, config_dir  # noqa: E402
from raven.host.server import HostServer  # noqa: E402
from tests.support import PASSWORD, FakeCapturer, FakeInjector  # noqa: E402

Qt = QtCore.Qt


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(autouse=True)
def _delete_leftover_widgets():
    yield
    if QtWidgets.QApplication.instance() is not None:
        for widget in QtWidgets.QApplication.topLevelWidgets():
            _destroy(widget)


def _hello(**overrides):
    msg = {"type": "hello", "version": 1, "name": "Office PC", "os": "windows",
           "salt": "ab" * 16, "iterations": security.PBKDF2_ITERATIONS, "nonce": protocol.b64(b"n" * 32)}
    msg.update(overrides)
    return msg


# ------------------------------------------------------------------ hello validation

@pytest.mark.parametrize("iterations", [0, 1, 1000, security.PBKDF2_ITERATIONS - 1, 10**9, "many", None])
def test_weak_or_absurd_iteration_counts_are_refused(iterations):
    with pytest.raises(RavenError):
        parse_hello(_hello(iterations=iterations), "FP")


@pytest.mark.parametrize("bad", [{"salt": "ab"}, {"salt": "zz"}, {"nonce": protocol.b64(b"x")},
                                 {"nonce": "%%%"}, {"version": 2}, {"type": "auth_result"}])
def test_malformed_hello_is_refused(bad):
    with pytest.raises(RavenError):
        parse_hello(_hello(**bad), "FP")


def test_hello_strings_are_cleaned():
    hello = parse_hello(_hello(name="Office\x1b[2J‮PC  <b>x</b>" + "y" * 200, os="evil"), "FP")
    assert "\x1b" not in hello.name and "‮" not in hello.name
    assert len(hello.name) <= 64 and hello.os == ""
    error = _hello(type="error", error="\x07bad‮")
    with pytest.raises(RavenError, match="^bad$"):
        parse_hello(error, "FP")


def test_session_info_is_cleaned():
    info = clean_info({"name": "A\x00B", "os": "plan9", "monitors": "nope", "mac": "x" * 500})
    assert info == {"name": "AB", "os": "", "monitors": [], "mac": "x" * 32}
    assert clean_info(None)["name"] == ""


# ------------------------------------------------------------------ plain text

HTML_NAME = '<img src="file://10.0.0.5/share/a.png">Office PC'


def test_labels_and_toasts_never_render_html(app):
    from raven.gui.widgets import Toast, label, plain_tooltip

    lbl = label(HTML_NAME)
    assert lbl.textFormat() == Qt.TextFormat.PlainText and lbl.text() == HTML_NAME
    host = QtWidgets.QWidget()
    toast = Toast(host)
    toast.show_message(HTML_NAME)
    assert toast.text.textFormat() == Qt.TextFormat.PlainText
    tip = plain_tooltip(HTML_NAME)
    assert "<img" not in tip and "&lt;img" in tip


def test_device_card_shows_names_literally(app):
    from raven.gui.devices_page import Device, DeviceCard

    class Page:  # the card only needs callbacks
        def __getattr__(self, name):
            return lambda *a: None

    remote = RemoteHost(name=HTML_NAME, address="10.0.0.9", os="windows")
    card = DeviceCard(Page(), Device("found:x", remote, False, True))
    labels = [w for w in card.findChildren(QtWidgets.QLabel) if w.text() == HTML_NAME]
    assert labels and all(w.textFormat() == Qt.TextFormat.PlainText for w in labels)


def test_identity_warning_uses_saved_name_and_plain_text(app):
    from raven.gui.connect_flow import TrustDialog

    dialog = TrustDialog(None, "Office PC<!--", "AA:BB", changed=True)
    texts = [w.text() for w in dialog.findChildren(QtWidgets.QLabel)]
    assert any("someone may be impersonating it" in t for t in texts)
    assert all(w.textFormat() == Qt.TextFormat.PlainText for w in dialog.findChildren(QtWidgets.QLabel))


def test_discovery_names_are_cleaned():
    assert security.clean_display_name("Office‮\u0000 PC\n\n<b>") == "Office PC <b>"


# ------------------------------------------------------------------ connection flow

class _Hosts:
    """Real hosts running on the GUI's network thread."""

    def __init__(self, runner):
        self.runner = runner
        self.servers = []

    def start(self, cert_dir, store=None, salt_from=None):
        cert_dir.mkdir(parents=True, exist_ok=True)
        store = store or SettingsStore(cert_dir / "settings.json")
        if salt_from is None:
            store.settings.host.set_password(PASSWORD)
        else:  # same password settings, different certificate (e.g. reinstall)
            store.settings.host.password_salt = salt_from.password_salt
            store.settings.host.password_verifier = salt_from.password_verifier
        events = []
        server = HostServer(store, cert_dir, on_event=lambda k, d: events.append(k),
                            capturer_factory=FakeCapturer, injector_factory=FakeInjector)
        self.runner.submit(server.start(port=0, bind="127.0.0.1")).result(10)
        server.events = events
        self.servers.append(server)
        return server

    def stop(self):
        for server in self.servers:
            self.runner.submit(server.disconnect_all()).result(10)
            self.runner.submit(server.stop()).result(10)


@pytest.fixture
def gui(app, tmp_path, monkeypatch):
    from raven.gui import connect_flow
    from raven.gui.async_bridge import AsyncRunner, UiCaller

    runner = AsyncRunner(UiCaller())
    hosts = _Hosts(runner)
    shown = {"trust": 0, "password": 0}

    def trust_exec(self):
        shown["trust"] += 1
        return QtWidgets.QDialog.DialogCode.Accepted

    def password_exec(self):
        shown["password"] += 1
        self.password.setText(PASSWORD)
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(connect_flow.TrustDialog, "exec", trust_exec)
    monkeypatch.setattr(connect_flow.PasswordDialog, "exec", password_exec)
    yield runner, hosts, shown
    hosts.stop()
    runner.shutdown()


def _run_flow(runner, remote, alternates=None, store=None):
    from raven.gui.connect_flow import ConnectFlow

    store = store or SettingsStore()
    result = {}
    flow = ConnectFlow(QtWidgets.QWidget(), runner, store, remote,
                       lambda session, info: result.update(session=session, info=info),
                       lambda message: result.update(error=message), alternates)
    flow.start()
    deadline = time.monotonic() + 15
    while not result and time.monotonic() < deadline:
        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 50)
    if "session" in result:
        runner.submit(result["session"].close()).result(5)
    return result


def _unused_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_saved_key_is_not_sent_after_identity_change(gui, tmp_path):
    runner, hosts, shown = gui
    real = hosts.start(tmp_path / "real")
    # Same password settings (so the salt matches) but a different
    # certificate: exactly what an impostor copying the salt would present.
    other = hosts.start(tmp_path / "other", salt_from=real.store.settings.host)
    key = security.derive_key(PASSWORD, bytes.fromhex(real.store.settings.host.password_salt))
    remote = RemoteHost(name="Office PC", address="127.0.0.1", port=other.port, fingerprint=real.fingerprint,
                        saved_key=key.hex(), saved_salt=real.store.settings.host.password_salt)

    result = _run_flow(runner, remote)

    assert shown == {"trust": 1, "password": 1}  # warned, then asked: no silent saved key
    assert "session" in result


def test_unverified_alternate_address_is_skipped_silently(gui, tmp_path):
    runner, hosts, shown = gui
    real = hosts.start(tmp_path / "real")
    impostor = hosts.start(tmp_path / "impostor")
    key = security.derive_key(PASSWORD, bytes.fromhex(real.store.settings.host.password_salt))
    remote = RemoteHost(name="Office PC", address="127.0.0.1", port=_unused_port(), fingerprint=real.fingerprint,
                        saved_key=key.hex(), saved_salt=real.store.settings.host.password_salt)
    store = SettingsStore()
    store.settings.remotes.append(remote)

    # Discovery claims two new addresses; only the real host's identity matches.
    result = _run_flow(runner, remote, [("127.0.0.1", impostor.port), ("127.0.0.1", real.port)], store)

    assert "session" in result, result
    assert shown == {"trust": 0, "password": 0}  # impostor never got to prompt
    assert "connected" not in impostor.events and "auth_failed" not in impostor.events
    assert remote.port == real.port  # verified address is remembered


def test_alternate_with_wrong_identity_fails_without_prompt(gui, tmp_path):
    runner, hosts, shown = gui
    real = hosts.start(tmp_path / "real")
    impostor = hosts.start(tmp_path / "impostor")
    remote = RemoteHost(name="Office PC", address="127.0.0.1", port=_unused_port(), fingerprint=real.fingerprint)

    result = _run_flow(runner, remote, [("127.0.0.1", impostor.port)])

    assert result.get("error") and shown == {"trust": 0, "password": 0}
    assert remote.port != impostor.port


def test_advertisements_do_not_rewrite_saved_devices(app, tmp_path):
    from raven.discovery import DiscoveredHost
    from raven.gui.async_bridge import AsyncRunner, UiCaller
    from raven.gui.devices_page import DevicesPage

    runner = AsyncRunner(UiCaller())
    store = SettingsStore()
    remote = RemoteHost(name="Office PC", address="192.168.1.20", port=47470, fingerprint="AA:BB")
    store.settings.remotes.append(remote)
    page = DevicesPage(runner, store)
    try:
        page._on_found(DiscoveredHost("evil._raven._tcp.local.", "Office PC", "192.168.1.66", 47470,
                                      "windows", "AA:BB"))
        assert (remote.address, remote.port) == ("192.168.1.20", 47470)
        assert SettingsStore().settings.remotes == [] or \
            SettingsStore().settings.remotes[0].address == "192.168.1.20"
        assert page.alternates_for(remote) == [("192.168.1.66", 47470)]
    finally:
        page.shutdown()
        runner.shutdown()
        _destroy(page)


def _destroy(widget):
    """Delete a widget now, while Qt is running; leaving it to Python's exit
    cleanup can crash the interpreter on the way out."""
    import gc

    widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    gc.collect()


def test_rate_limiter_has_a_host_wide_budget():
    limiter = security.AuthRateLimiter(max_failures=5, max_global_failures=8)
    for i in range(8):
        address = f"10.0.0.{i}"
        assert limiter.begin(address)
        limiter.finish(address, False)
    assert not limiter.begin("10.0.0.200")  # a fresh address is refused too


def test_rate_limiter_counts_attempts_in_flight():
    limiter = security.AuthRateLimiter(max_failures=3)
    assert [limiter.begin("a") for _ in range(5)] == [True, True, True, False, False]
    limiter.finish("a", None)  # left without guessing: slot freed, no failure
    assert limiter.begin("a")
    for _ in range(3):
        limiter.finish("a", False)
    assert not limiter.begin("a")


def test_file_names_lose_direction_tricks():
    from raven.host.files import sanitize_name

    assert sanitize_name("photo‮gnp.exe") == "photognp.exe"
    assert config_dir().exists()
