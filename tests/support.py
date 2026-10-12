"""Fakes and helpers shared by the end-to-end tests."""

import asyncio

import pytest
from PIL import Image

from raven.client.session import RemoteSession
from raven.config import SettingsStore, config_dir
from raven.host.server import HostServer
from raven.platform.capture import Monitor, encode_jpeg


class FakeCapturer:
    def __init__(self):
        self.counter = 0

    def monitors(self):
        return [Monitor(1, 0, 0, 800, 600), Monitor(2, 800, 0, 1024, 768)]

    def grab_jpeg(self, monitor, max_width=1920, quality=70, force=False):
        self.counter += 1
        img = Image.new("RGB", (monitor.width, monitor.height), (self.counter % 255, 0, 0))
        return encode_jpeg(img, max_width, quality), (monitor.width, monitor.height)

    def reset(self):
        pass

    def close(self):
        pass


class FakeInjector:
    def __init__(self):
        self.events = []
        INJECTORS.append(self)

    def cursor_position(self):
        return (400, 300)

    def move(self, monitor, x, y):
        self.events.append(("move", monitor.denormalize(x, y)))

    def button(self, name, down):
        self.events.append(("button", name, down))

    def scroll(self, dx, dy):
        self.events.append(("scroll", dx, dy))

    def key(self, name, down):
        self.events.append(("key", name, down))

    def type_text(self, text):
        self.events.append(("type", text))

    def release_all(self):
        self.events.append(("release_all",))


INJECTORS: list[FakeInjector] = []
PASSWORD = "hunter22"


@pytest.fixture
async def host():
    INJECTORS.clear()
    store = SettingsStore()
    store.settings.host.set_password(PASSWORD)
    events = []
    powered = []
    server = HostServer(
        store, config_dir(),
        on_event=lambda kind, detail: events.append((kind, detail)),
        capturer_factory=FakeCapturer,
        injector_factory=FakeInjector,
        power_runner=powered.append,
        power_delay=0.05,
    )
    server.events, server.powered = events, powered
    await server.start(port=0, bind="127.0.0.1")
    yield server
    await server.disconnect_all()
    await server.stop()


async def _login(server, password=PASSWORD):
    session = RemoteSession("127.0.0.1", server.port, client_name="pytest")
    hello = await session.connect()
    assert hello.fingerprint == server.fingerprint
    info = await session.authenticate(session.derive_key(password))
    return session, info


async def _wait_for(predicate, timeout=5):
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met")
