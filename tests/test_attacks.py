"""Adversarial tests: an attacker on the LAN talking raw protocol to a real host.

Each test is an attack that must fail. Several started as findings from the
security review of PR #1 and now guard the fixes.
"""

import asyncio
import json
import os

import pytest
import websockets
from websockets.asyncio.client import connect

from raven import protocol, security
from raven.client.files import FileClient
from raven.client.session import RavenError
from raven.clip import Clip
from tests.support import INJECTORS, PASSWORD, _login



async def raw(port, max_size=2**26):
    return await connect(f"wss://127.0.0.1:{port}/", ssl=security.client_ssl_context(), proxy=None,
                         compression=None, max_size=max_size, open_timeout=5)


async def raw_login(host, ws):
    hello = json.loads(await ws.recv())
    key = security.derive_key(PASSWORD, bytes.fromhex(hello["salt"]), hello["iterations"])
    proof = security.make_proof(key, protocol.unb64(hello["nonce"]), host.fingerprint)
    await ws.send(json.dumps({"type": "auth", "proof": protocol.b64(proof)}))
    reply = json.loads(await ws.recv())
    assert reply["ok"], reply
    return hello, key, proof


async def rpc(ws, msg, rid=[1000]):
    rid[0] += 1
    await ws.send(json.dumps({**msg, "rid": rid[0]}))
    while True:
        raw_msg = await asyncio.wait_for(ws.recv(), 5)
        if isinstance(raw_msg, bytes):
            continue  # screen frames / file chunks
        m = json.loads(raw_msg)
        if m.get("rid") == rid[0]:
            return m


@pytest.fixture
def work(tmp_path):
    """An empty folder for file attacks (tmp_path also holds RAVEN_HOME)."""
    path = tmp_path / "work"
    path.mkdir()
    return path


async def alive(host):
    s, _ = await _login(host)
    await s.ping()
    await s.close()


# ------------------------------------------------------------------ pre-auth

@pytest.mark.parametrize("first", [
    b"\x01binary-before-auth",
    "not json at all",
    json.dumps({"no": "type"}),
    json.dumps({"type": "power", "action": "shutdown"}),
    json.dumps({"type": "key", "key": "a", "down": True}),
    json.dumps({"type": "fs_list", "path": "/", "rid": 1}),
    json.dumps({"type": "auth", "proof": 12345}),
    json.dumps({"type": "auth", "proof": ["x"]}),
    json.dumps({"type": "auth", "proof": "!!!not base64!!!"}),
    json.dumps({"type": "auth", "proof": "A" * 900_000}),
    json.dumps({"type": "auth"}),
    json.dumps([1, 2, 3]),
])
async def test_preauth_garbage_never_grants_anything(host, first):
    host.limiter.max_failures = 1000
    ws = await raw(host.port)
    await ws.recv()  # hello
    await ws.send(first)
    try:
        reply = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert reply.get("ok") is not True
    except websockets.ConnectionClosed:
        pass
    await asyncio.sleep(0.1)
    assert host.powered == []
    assert all(not inj.events for inj in INJECTORS)
    assert not any(k == "connected" for k, _ in host.events)
    await alive(host)


async def test_preauth_oversized_message_is_refused(host):
    ws = await raw(host.port)
    await ws.recv()
    with pytest.raises(websockets.ConnectionClosed):
        await ws.send("x" * (2 * 2**20))
        await asyncio.wait_for(ws.recv(), 5)
    await alive(host)


async def test_replayed_proof_is_rejected(host):
    ws = await raw(host.port)
    _, _, proof = await raw_login(host, ws)
    await ws.close()
    ws2 = await raw(host.port)
    await ws2.recv()
    await ws2.send(json.dumps({"type": "auth", "proof": protocol.b64(proof)}))
    assert json.loads(await ws2.recv())["ok"] is False


async def test_lockout_race_with_parallel_guesses(host):
    host.limiter.max_failures = 5

    async def guess():
        ws = await raw(host.port)
        hello = json.loads(await ws.recv())
        if hello["type"] != "hello":
            return "locked"
        await ws.send(json.dumps({"type": "auth", "proof": protocol.b64(b"\0" * 32)}))
        try:
            return json.loads(await ws.recv()).get("error", "")
        except websockets.ConnectionClosed:
            return "closed"

    results = await asyncio.gather(*(guess() for _ in range(40)))
    # Every attempt reserves a slot before it's checked, so firing guesses
    # in parallel gets no more than the limit evaluated.
    assert results.count("Wrong password.") <= 5, results
    # After the burst, even the right password is refused for this address.
    with pytest.raises(RavenError, match="Too many"):
        await _login(host)


# ------------------------------------------------------------------ permission gates

async def test_disabled_input_power_files_clipboard_are_enforced(host, work):
    h = host.store.settings.host
    h.allow_input = h.allow_power = h.allow_files = h.allow_clipboard = False
    ws = await raw(host.port)
    await raw_login(host, ws)
    for msg in [
        {"type": "mouse_move", "x": 0.5, "y": 0.5}, {"type": "mouse_button", "button": "left", "down": True},
        {"type": "scroll", "dx": 1, "dy": 1}, {"type": "key", "key": "a", "down": True},
        {"type": "type_text", "text": "rm -rf ~"}, {"type": "release_all"},
    ]:
        await ws.send(json.dumps(msg))
    assert (await rpc(ws, {"type": "power", "action": "shutdown"}))["ok"] is False
    assert (await rpc(ws, {"type": "fs_list", "path": str(work)}))["ok"] is False
    assert (await rpc(ws, {"type": "fs_upload_start", "dir": str(work), "name": "x", "size": 3,
                           "tid": 7}))["ok"] is False
    await ws.send(protocol.pack_chunk(7, b"abc"))
    assert (await rpc(ws, {"type": "clipboard_sync", "enabled": True}))["ok"] is False
    await ws.send(json.dumps(Clip.from_text("injected").to_message("clipboard_set")))
    await rpc(ws, {"type": "ping"})
    await asyncio.sleep(0.2)
    assert [e for e in INJECTORS[-1].events if e != ("release_all",)] == []
    assert host.powered == [] and list(work.iterdir()) == []
    assert host.clip_backend.writes == []


async def test_clipboard_set_without_subscribing_is_ignored(host):
    ws = await raw(host.port)
    await raw_login(host, ws)
    await ws.send(json.dumps(Clip.from_text("sneaky").to_message("clipboard_set")))
    await rpc(ws, {"type": "ping"})
    assert host.clip_backend.writes == []


async def test_permission_revoked_mid_session(host, work):
    s, _ = await _login(host)
    fc = FileClient(s)
    await fc.list(str(work))
    host.store.settings.host.allow_files = False
    with pytest.raises(RavenError, match="turned off"):
        await fc.list(str(work))
    host.store.settings.host.allow_input = False
    await s.send({"type": "key", "key": "x", "down": True})
    await s.ping()
    assert not [e for e in INJECTORS[-1].events if e[0] == "key"]
    await s.close()


# ------------------------------------------------------------------ malformed values

@pytest.mark.parametrize("msg", [
    {"type": "mouse_move", "x": "abc", "y": None},
    {"type": "mouse_move", "x": float("nan"), "y": float("inf")},
    {"type": "mouse_move", "x": 1e308, "y": -1e308},
    {"type": "mouse_button", "button": ["left"], "down": "yes"},
    {"type": "mouse_button", "button": "left; rm -rf /", "down": True},
    {"type": "key", "key": {"$": 1}, "down": True},
    {"type": "key", "key": "\x00", "down": True},
    {"type": "key", "key": "f99", "down": True},
    {"type": "scroll", "dx": float("inf"), "dy": "x"},
    {"type": "type_text", "text": "A" * 2_000_000},
    {"type": "stream_start", "monitor": -5, "fps": 1e9, "quality": "x", "max_width": -1},
    {"type": "stream_start", "monitor": 99},
    {"type": "power", "action": "__import__('os').system('id')"},
    {"type": "power", "action": None},
    {"type": "ping", "rid": "abc"},
    {"type": "fs_list", "rid": -1, "path": None},
    {"type": "fs_list", "rid": 2**70, "path": ["x"]},
    {"type": "clipboard_sync", "enabled": "maybe", "rid": 3},
    {"type": "fs_upload_start", "rid": 4, "tid": "1", "dir": None, "name": None, "size": "big"},
    {"type": "fs_download", "rid": 5, "tid": 2**33, "path": "/etc/passwd"},
    {"type": "fs_cancel", "rid": 6, "tid": None},
    {"type": "\u202eunicode"},
])
async def test_malformed_messages_do_not_break_session(host, msg):
    ws = await raw(host.port)
    await raw_login(host, ws)
    await ws.send(json.dumps(msg, allow_nan=True))
    pong = await rpc(ws, {"type": "ping"})
    assert pong["type"] == "pong"
    for inj in INJECTORS:
        for e in inj.events:
            if e[0] == "move":
                assert all(isinstance(v, int) for v in e[1])
            if e[0] == "type":
                assert len(e[1]) <= 10_000
    assert host.powered == []


async def test_random_fuzz(host):
    import random

    rnd = random.Random(1337)
    ws = await raw(host.port)
    await raw_login(host, ws)
    types = ["mouse_move", "mouse_button", "scroll", "key", "type_text", "stream_start", "stream_stop", "ack",
             "power", "fs_places", "fs_list", "fs_mkdir", "fs_upload_start", "fs_upload_end", "fs_upload_abort",
             "fs_download", "fs_cancel", "clipboard_sync", "clipboard_set", "ping", "release_all", "x"]
    values = [None, True, -1, 0, 1, 2**64, 1.5, float("inf"), "", "..", "/", "a" * 300, [], {}, ["a"],
              {"a": 1}, "\x00", "C:\\Windows", "../../etc"]
    keys = ["x", "y", "button", "down", "key", "text", "monitor", "fps", "action", "path", "parent", "name",
            "dir", "size", "tid", "sha256", "enabled", "kind", "png", "digest", "rid", "dx", "dy"]
    for _ in range(800):
        msg = {"type": rnd.choice(types)}
        for k in rnd.sample(keys, rnd.randint(0, 6)):
            msg[k] = rnd.choice(values)
        if msg["type"] == "power":
            msg["action"] = "nope"  # keep the fake from "shutting down"
        await ws.send(json.dumps(msg, allow_nan=True))
        if rnd.random() < 0.1:
            await ws.send(bytes(rnd.getrandbits(8) for _ in range(rnd.randint(0, 40))))
    assert (await rpc(ws, {"type": "ping"}))["type"] == "pong"


# ------------------------------------------------------------------ files

TRAVERSAL_NAMES = ["../../escape.txt", "..\\..\\escape2.txt", "/etc/passwd", "C:\\Windows\\evil.dll",
                   "a/../../b.txt", "..", ".", "", "  ", "x\x00y.txt", "CON", "nul.txt",
                   "evil.txt:stream", "trailing. . .", "\u202egnp.exe"]


async def test_upload_names_cannot_escape_target(host, work):
    target = work / "target"
    target.mkdir()
    ws = await raw(host.port)
    await raw_login(host, ws)
    for i, name in enumerate(TRAVERSAL_NAMES, start=1):
        reply = await rpc(ws, {"type": "fs_upload_start", "dir": str(target), "name": name, "size": 2, "tid": i})
        if reply["ok"]:
            await ws.send(protocol.pack_chunk(i, b"hi"))
            import hashlib
            end = await rpc(ws, {"type": "fs_upload_end", "tid": i, "sha256": hashlib.sha256(b"hi").hexdigest()})
            assert end["ok"], (name, end)
            assert os.path.dirname(end["path"]) == str(target), (name, end["path"])
    for i, name in enumerate(TRAVERSAL_NAMES, start=100):
        reply = await rpc(ws, {"type": "fs_mkdir", "parent": str(target), "name": name})
        if reply["ok"]:
            assert os.path.dirname(reply["path"]) == str(target), (name, reply["path"])
    outside = [p for p in work.rglob("*") if not str(p).startswith(str(target))]
    assert outside == [], outside
    names = [p.name for p in target.iterdir()]
    assert not any("\u202e" in n or "\x00" in n for n in names), names
    assert all(not n.endswith(".raven-part") for n in names), names


async def test_upload_integrity_attacks(host, work):
    import hashlib

    ws = await raw(host.port)
    await raw_login(host, ws)
    d = str(work)
    # chunk for a transfer that never started
    await ws.send(protocol.pack_chunk(55, b"orphan"))
    # announce 3 bytes, send 10
    assert (await rpc(ws, {"type": "fs_upload_start", "dir": d, "name": "over.txt", "size": 3, "tid": 1}))["ok"]
    await ws.send(protocol.pack_chunk(1, b"0123456789"))
    end = await rpc(ws, {"type": "fs_upload_end", "tid": 1, "sha256": hashlib.sha256(b"0123456789").hexdigest()})
    assert end["ok"] is False
    # wrong checksum
    assert (await rpc(ws, {"type": "fs_upload_start", "dir": d, "name": "bad.txt", "size": 3, "tid": 2}))["ok"]
    await ws.send(protocol.pack_chunk(2, b"abc"))
    assert (await rpc(ws, {"type": "fs_upload_end", "tid": 2, "sha256": "0" * 64}))["ok"] is False
    # reused transfer id
    assert (await rpc(ws, {"type": "fs_upload_start", "dir": d, "name": "r.txt", "size": 1, "tid": 3}))["ok"]
    assert (await rpc(ws, {"type": "fs_upload_start", "dir": d, "name": "r2.txt", "size": 1, "tid": 3}))["ok"] is False
    # invalid ids and sizes
    for tid, size in [(0, 1), (-1, 1), (2**32, 1), ("1", 1), (4, -5), (5, 10**18)]:
        assert (await rpc(ws, {"type": "fs_upload_start", "dir": d, "name": "z.txt", "size": size,
                               "tid": tid}))["ok"] is False, (tid, size)
    await ws.close()
    await asyncio.sleep(0.3)
    leftovers = sorted(p.name for p in work.iterdir())
    assert leftovers == [], leftovers  # nothing half-written, .part files cleaned on disconnect


async def test_concurrent_same_name_uploads(host, work):
    a, _ = await _login(host)
    b, _ = await _login(host)
    pa, pb = work / "srcA", work / "srcB"
    pa.mkdir()
    pb.mkdir()
    (pa / "same.bin").write_bytes(b"A" * 3_000_000)
    (pb / "same.bin").write_bytes(b"B" * 3_000_000)
    dest = work / "dest"
    dest.mkdir()
    paths_a, paths_b = await asyncio.gather(FileClient(a).upload([pa / "same.bin"], str(dest)),
                                            FileClient(b).upload([pb / "same.bin"], str(dest)))
    # Both succeed, each reported file holds exactly what that sender sent,
    # and neither replaced the other.
    assert sorted(p.name for p in dest.iterdir()) == ["same (2).bin", "same.bin"]
    assert open(paths_a[0], "rb").read() == b"A" * 3_000_000
    assert open(paths_b[0], "rb").read() == b"B" * 3_000_000
    await a.close()
    await b.close()


async def test_symlink_loop_download(host, work):
    remote = work / "remote"
    (remote / "loop").mkdir(parents=True)
    (remote / "loop" / "f.txt").write_text("x")
    os.symlink(remote / "loop", remote / "loop" / "again")
    s, _ = await _login(host)
    fc = FileClient(s)
    listing = await fc.list(str(remote))
    local = work / "local"
    local.mkdir()
    # The folder link inside is skipped instead of followed forever.
    await asyncio.wait_for(fc.download(listing["entries"], local), 8)
    assert sorted(str(p.relative_to(local)) for p in local.rglob("*")) == ["loop", "loop/f.txt"]
    await s.close()


async def test_post_auth_message_limit(host):
    ws = await raw(host.port)
    await raw_login(host, ws)
    with pytest.raises(websockets.ConnectionClosed):
        await ws.send("x" * (33 * 2**20))
        await asyncio.wait_for(ws.recv(), 10)


async def test_turning_off_remote_access_kicks_sessions(host):
    s, _ = await _login(host)
    closed = asyncio.Event()
    s.add_close_listener(lambda r: closed.set())
    await host.disconnect_all()
    await asyncio.wait_for(closed.wait(), 5)

