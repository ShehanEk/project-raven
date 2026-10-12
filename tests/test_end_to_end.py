"""Host and controller talking over a real TLS WebSocket on localhost."""

import asyncio

import pytest

from raven.client.session import AuthError, RavenError, RemoteSession
from tests.support import INJECTORS, PASSWORD, _login, _wait_for


async def test_wrong_password_is_rejected(host):
    session = RemoteSession("127.0.0.1", host.port)
    await session.connect()
    with pytest.raises(AuthError, match="Wrong password"):
        await session.authenticate(session.derive_key("guess"))
    assert any(kind == "auth_failed" for kind, _ in host.events)


async def test_lockout_after_repeated_failures(host):
    host.limiter.max_failures = 2
    for _ in range(2):
        s = RemoteSession("127.0.0.1", host.port)
        await s.connect()
        with pytest.raises(AuthError):
            await s.authenticate(s.derive_key("guess"))
    with pytest.raises(RavenError, match="Too many"):
        await RemoteSession("127.0.0.1", host.port).connect()


async def test_login_reports_monitors(host):
    session, info = await _login(host)
    assert [m["index"] for m in info["monitors"]] == [1, 2]
    assert info["allow_input"] and info["allow_power"]
    await session.close()
    await _wait_for(lambda: any(k == "disconnected" for k, _ in host.events))


async def test_stream_and_input(host):
    session, _ = await _login(host)
    frames, messages = [], []
    closed = asyncio.Event()
    session.start_receiving(lambda jpeg, cursor: frames.append((jpeg, cursor)),
                            messages.append, lambda reason: closed.set())
    await session.send({"type": "stream_start", "monitor": 2, "fps": 30, "quality": 50, "max_width": 512})
    await _wait_for(lambda: len(frames) >= 5)
    jpeg, cursor = frames[-1]
    assert jpeg[:2] == b"\xff\xd8"
    assert cursor == (-1.0, -1.0)  # fake cursor (400, 300) is on monitor 1, not 2
    assert any(m["type"] == "stream_info" and m["width"] == 1024 for m in messages)

    await session.send({"type": "mouse_move", "x": 0.5, "y": 0.5})
    await session.send({"type": "mouse_button", "button": "left", "down": True})
    await session.send({"type": "mouse_button", "button": "left", "down": False})
    await session.send({"type": "scroll", "dx": 0, "dy": -2})
    await session.send({"type": "key", "key": "a", "down": True})
    await session.send({"type": "key", "key": "a", "down": False})
    await session.send({"type": "type_text", "text": "hello"})
    injector = INJECTORS[-1]
    await _wait_for(lambda: ("type", "hello") in injector.events)
    assert injector.events[:6] == [
        ("move", (800 + 512, 384)),  # centre of monitor 2 in global coordinates
        ("button", "left", True), ("button", "left", False),
        ("scroll", 0.0, -2.0), ("key", "a", True), ("key", "a", False),
    ]

    await session.close()
    await _wait_for(lambda: ("release_all",) in injector.events)


async def test_input_ignored_when_disabled(host):
    host.store.settings.host.allow_input = False
    session, info = await _login(host)
    assert info["allow_input"] is False
    await session.send({"type": "key", "key": "a", "down": True})
    await session.ping()  # replies arrive in order, so the key was processed
    assert not [e for e in INJECTORS[-1].events if e[0] == "key"]
    await session.close()


async def test_power_command(host):
    session, _ = await _login(host)
    await session.power("sleep")
    await _wait_for(lambda: host.powered == ["sleep"])
    with pytest.raises(RavenError, match="Unknown"):
        await session.power("explode")
    await session.close()


async def test_power_disabled(host):
    host.store.settings.host.allow_power = False
    session, _ = await _login(host)
    with pytest.raises(RavenError, match="turned off"):
        await session.power("shutdown")
    await asyncio.sleep(0.1)
    assert host.powered == []
    await session.close()


async def test_auth_proof_is_bound_to_certificate(host):
    """A proof computed for a different TLS certificate (i.e. a MITM) fails."""
    from raven import protocol, security

    session = RemoteSession("127.0.0.1", host.port)
    hello = await session.connect()
    key = session.derive_key(PASSWORD)
    proof = security.make_proof(key, hello.nonce, "AA:" + hello.fingerprint[3:])
    await session.send({"type": "auth", "proof": protocol.b64(proof)})
    msg = await session._recv_json()
    assert msg == {"type": "auth_result", "ok": False, "error": "Wrong password."}
