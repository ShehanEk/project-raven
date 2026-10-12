"""Clipboard sync between controller and host."""

import asyncio

import pytest

from raven import protocol
from raven.client.session import RavenError
from raven.clip import Clip, text_digest
from tests.support import _login, _wait_for

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-data" * 1000


async def _synced(host):
    session, info = await _login(host)
    received = []
    session.add_listener(lambda m: received.append(m) if m["type"] == "clipboard" else None)
    reply = await session.request({"type": "clipboard_sync", "enabled": True})
    assert reply["images"] is True
    return session, info, received


async def test_host_copy_reaches_controller(host):
    host.clip_backend.local_copy(Clip.from_text("already there"))
    session, info, received = await _synced(host)
    assert info["allow_clipboard"] and info["clipboard_images"]
    await asyncio.sleep(0.7)
    assert received == []  # existing content isn't pushed, only new copies

    host.clip_backend.local_copy(Clip.from_text("héllo from the host"))
    await _wait_for(lambda: received)
    assert received[0]["kind"] == "text" and received[0]["text"] == "héllo from the host"

    host.clip_backend.local_copy(Clip("image", png=PNG, digest="px:1"))
    await _wait_for(lambda: len(received) == 2)
    assert Clip.from_message(received[1]).png == PNG
    await session.close()


async def test_controller_copy_reaches_host_without_echo(host):
    session, _, received = await _synced(host)
    await session.send(Clip.from_text("from controller").to_message("clipboard_set"))
    await _wait_for(lambda: host.clip_backend.writes)
    assert host.clip_backend.clip.text == "from controller"
    await asyncio.sleep(1.2)  # several watcher polls
    assert received == []  # not bounced back to where it came from
    await session.close()


async def test_two_controllers_share(host):
    a, _, got_a = await _synced(host)
    b, _, got_b = await _synced(host)
    await a.send(Clip.from_text("shared").to_message("clipboard_set"))
    await _wait_for(lambda: got_b)
    assert got_b[0]["text"] == "shared"
    await asyncio.sleep(0.7)
    assert got_a == []
    await a.close()
    await b.close()


async def test_sync_off_stops_pushes(host):
    session, _, received = await _synced(host)
    await session.request({"type": "clipboard_sync", "enabled": False})
    host.clip_backend.local_copy(Clip.from_text("secret"))
    await session.send(Clip.from_text("ignored").to_message("clipboard_set"))
    await asyncio.sleep(0.8)
    assert received == [] and host.clip_backend.writes == []
    await session.close()


async def test_clipboard_disabled_by_host(host):
    host.store.settings.host.allow_clipboard = False
    session, info = await _login(host)
    assert info["allow_clipboard"] is False
    with pytest.raises(RavenError, match="turned off"):
        await session.request({"type": "clipboard_sync", "enabled": True})
    await session.close()


async def test_large_image_allowed_after_login(host):
    session, _, _ = await _synced(host)
    big = b"\x89PNG" + bytes(3 * 1024 * 1024)
    await session.send({"type": "clipboard_set", "kind": "image", "png": protocol.b64(big), "digest": "px:big"})
    await _wait_for(lambda: host.clip_backend.writes)
    assert host.clip_backend.clip.png == big
    await session.close()


def test_clip_validation():
    with pytest.raises(ValueError):
        Clip.from_message({"kind": "image", "png": protocol.b64(b"GIF89a")})
    with pytest.raises(ValueError):
        Clip.from_message({"kind": "video"})
    assert text_digest("a\r\nb") == text_digest("a\nb")
