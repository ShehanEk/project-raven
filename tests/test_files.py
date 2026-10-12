"""File transfer between controller and host over a real connection."""

import asyncio
import hashlib
import os

import pytest

from raven.client.files import FileClient
from raven.client.session import RavenError
from raven.host import files as host_files
from tests.support import _login, _wait_for


@pytest.fixture
def remote_root(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    return root


@pytest.fixture
def local_root(tmp_path):
    root = tmp_path / "local"
    root.mkdir()
    return root


def _write(path, size, seed=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = bytes((i * 7 + seed) % 251 for i in range(size))
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


async def test_upload_file_and_folder(host, remote_root, local_root):
    session, _ = await _login(host)
    fc = FileClient(session)
    big = local_root / "big.bin"
    digest = _write(big, 3 * host_files.CHUNK_SIZE + 123)
    folder = local_root / "photos"
    _write(folder / "a.jpg", 1000)
    _write(folder / "nested" / "b.jpg", 2000, seed=3)
    (folder / "empty").mkdir()

    reports = []
    paths = await fc.upload([big, folder], str(remote_root), reports.append)

    assert [os.path.basename(p) for p in paths] == ["big.bin", "photos"]
    assert hashlib.sha256((remote_root / "big.bin").read_bytes()).hexdigest() == digest
    assert (remote_root / "photos" / "nested" / "b.jpg").stat().st_size == 2000
    assert (remote_root / "photos" / "empty").is_dir()
    assert reports[-1].done_files == reports[-1].total_files == 3
    assert reports[-1].fraction == 1.0
    assert not list(remote_root.rglob("*.raven-part"))
    assert any(kind == "file_received" for kind, _ in host.events)

    # Uploading again keeps both copies instead of overwriting.
    await fc.upload([big], str(remote_root))
    assert (remote_root / "big (2).bin").exists()
    await session.close()


async def test_browse_and_download(host, remote_root, local_root):
    session, _ = await _login(host)
    fc = FileClient(session)
    digest = _write(remote_root / "docs" / "report.pdf", 700_000)
    _write(remote_root / "docs" / "sub" / "notes.txt", 10)
    _write(remote_root / "z.txt", 5)

    listing = await fc.list(str(remote_root))
    assert [(e["name"], e["dir"]) for e in listing["entries"]] == [("docs", True), ("z.txt", False)]
    assert listing["parent"] == str(remote_root.parent)
    places = await fc.places()
    assert places["places"][0]["name"] == "Home" and places["roots"]

    reports = []
    docs = listing["entries"][0]
    result = await fc.download([docs, listing["entries"][1]], local_root, reports.append)
    assert [p.name for p in result] == ["docs", "z.txt"]
    assert hashlib.sha256((local_root / "docs" / "report.pdf").read_bytes()).hexdigest() == digest
    assert (local_root / "docs" / "sub" / "notes.txt").exists()
    assert reports[-1].done_bytes == 700_000 + 10 + 5
    assert not list(local_root.rglob("*.raven-part"))
    await session.close()


async def test_screen_keeps_streaming_during_transfer(host, remote_root, local_root):
    session, _ = await _login(host)
    frames = []
    session.start_receiving(lambda jpeg, cursor: frames.append(1), lambda m: None, lambda r: None)
    await session.send({"type": "stream_start", "fps": 30, "max_width": 320})
    _write(remote_root / "movie.bin", 4 * 1024 * 1024)
    listing = await FileClient(session).list(str(remote_root))
    before = len(frames)
    await FileClient(session).download(listing["entries"], local_root)
    await _wait_for(lambda: len(frames) > before + 3)
    await session.close()


async def test_mkdir_and_errors(host, remote_root):
    session, _ = await _login(host)
    fc = FileClient(session)
    path = await fc.mkdir(str(remote_root), "../../escape")
    assert path == str(remote_root / "escape")  # only the last component is used
    with pytest.raises(RavenError, match="doesn't exist"):
        await fc.list(str(remote_root / "missing"))
    with pytest.raises(RavenError, match="doesn't exist"):
        await fc.download([{"name": "x", "path": str(remote_root / "nope"), "dir": False, "size": 1}], remote_root)
    await session.close()


async def test_files_disabled(host, remote_root):
    host.store.settings.host.allow_files = False
    session, info = await _login(host)
    assert info["allow_files"] is False
    with pytest.raises(RavenError, match="turned off"):
        await FileClient(session).list(str(remote_root))
    await session.close()


async def test_cancelled_upload_leaves_nothing(host, remote_root, local_root):
    session, _ = await _login(host)
    big = local_root / "huge.bin"
    _write(big, 40 * host_files.CHUNK_SIZE)
    task = asyncio.create_task(FileClient(session).upload([big], str(remote_root)))
    await _wait_for(lambda: list(remote_root.glob(".*.raven-part")))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await session.ping()
    await _wait_for(lambda: not list(remote_root.iterdir()))
    await session.close()


def test_sanitize_name():
    assert host_files.sanitize_name("../../etc/passwd") == "passwd"
    assert host_files.sanitize_name("C:\\Windows\\evil.exe") == "evil.exe"
    assert host_files.sanitize_name('a<b>:c?.txt') == "a_b__c_.txt"
    assert host_files.sanitize_name("CON.txt") == "_CON.txt"
    for bad in ["", "..", ".", "/", "   "]:
        with pytest.raises(host_files.FileError):
            host_files.sanitize_name(bad)


def test_unique_path(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / ".bashrc").write_text("x")
    assert host_files.unique_path(tmp_path, "a.txt").name == "a (2).txt"
    assert host_files.unique_path(tmp_path, ".bashrc").name == ".bashrc (2)"
    assert host_files.unique_path(tmp_path, "b.txt").name == "b.txt"
