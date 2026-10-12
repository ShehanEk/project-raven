"""Controller side of file transfer: browse the host and copy files both ways."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from raven import protocol
from raven.client.session import RavenError, RemoteSession
from raven.host.files import CHUNK_SIZE, sanitize_name, unique_path


@dataclass
class Progress:
    """Totals for one transfer job (a file, or a folder with many files)."""

    total_bytes: int = 0
    done_bytes: int = 0
    total_files: int = 0
    done_files: int = 0
    current: str = ""
    started: float = field(default_factory=time.monotonic)
    last_report: float = 0.0

    @property
    def fraction(self) -> float:
        if self.total_bytes:
            return min(1.0, self.done_bytes / self.total_bytes)
        return 1.0 if self.total_files and self.done_files >= self.total_files else 0.0

    @property
    def rate(self) -> float:
        elapsed = time.monotonic() - self.started
        return self.done_bytes / elapsed if elapsed > 0.2 else 0.0


ProgressCallback = Callable[[Progress], None]


class FileClient:
    def __init__(self, session: RemoteSession):
        self.session = session

    # ------------------------------------------------------------ browsing

    async def places(self) -> dict:
        return await self.session.request({"type": "fs_places"})

    async def list(self, path: str) -> dict:
        return await self.session.request({"type": "fs_list", "path": path})

    async def mkdir(self, parent: str, name: str) -> str:
        reply = await self.session.request({"type": "fs_mkdir", "parent": parent, "name": name})
        return reply["path"]

    # ------------------------------------------------------------ upload

    async def upload(self, sources: list[Path], remote_dir: str, on_progress: ProgressCallback | None = None) -> list[str]:
        """Copy local files and folders into ``remote_dir`` on the host."""
        progress = Progress()
        plan: list[tuple[Path, tuple[str, ...]]] = []  # (local file or dir, relative parts)
        for src in sources:
            src = Path(src)
            if src.is_dir():
                plan.append((src, (src.name,)))
                for root, dirs, files in os.walk(src):
                    dirs.sort()
                    rel = Path(root).relative_to(src.parent).parts
                    for d in dirs:
                        plan.append((Path(root) / d, (*rel, d)))
                    for f in sorted(files):
                        plan.append((Path(root) / f, (*rel, f)))
            elif src.is_file():
                plan.append((src, (src.name,)))
            else:
                raise RavenError(f"{src} doesn't exist.")
        for local, _ in plan:
            if local.is_file():
                progress.total_files += 1
                progress.total_bytes += local.stat().st_size
        _notify(on_progress, progress)

        remote_dirs: dict[tuple[str, ...], str] = {(): remote_dir}
        results = []
        for local, rel in plan:
            parent = remote_dirs.get(rel[:-1])
            if parent is None:
                continue  # its folder couldn't be created
            if local.is_dir():
                remote_dirs[rel] = await self.mkdir(parent, local.name)
                if len(rel) == 1:
                    results.append(remote_dirs[rel])
            else:
                path = await self._upload_file(local, parent, progress, on_progress)
                if len(rel) == 1:
                    results.append(path)
        return results

    async def _upload_file(self, local: Path, remote_dir: str, progress: Progress,
                           on_progress: ProgressCallback | None) -> str:
        session = self.session
        size = local.stat().st_size
        tid = session.new_transfer_id()
        progress.current = local.name
        await session.request({"type": "fs_upload_start", "dir": remote_dir, "name": local.name,
                               "size": size, "tid": tid})
        hasher = hashlib.sha256()
        sent = 0
        try:
            with open(local, "rb") as handle:
                while True:
                    data = await asyncio.to_thread(handle.read, CHUNK_SIZE)
                    if not data:
                        break
                    hasher.update(data)
                    # send() waits while the socket buffer is full, which
                    # paces us to the network instead of buffering the file.
                    await session.send_bytes(protocol.pack_chunk(tid, data))
                    sent += len(data)
                    progress.done_bytes += len(data)
                    _notify(on_progress, progress)
            if sent != size:
                raise RavenError(f"{local.name} changed while it was being sent.")
            reply = await session.request({"type": "fs_upload_end", "tid": tid, "sha256": hasher.hexdigest()},
                                          timeout=120)
        except BaseException:
            with contextlib.suppress(Exception):
                await session.send({"type": "fs_upload_abort", "tid": tid})
            raise
        progress.done_files += 1
        _notify(on_progress, progress, force=True)
        return reply["path"]

    # ------------------------------------------------------------ download

    async def download(self, items: list[dict], local_dir: Path, on_progress: ProgressCallback | None = None) -> list[Path]:
        """Copy host entries (as returned by :meth:`list`) into ``local_dir``."""
        progress = Progress()
        plan: list[tuple[dict, tuple[str, ...]]] = []
        for item in items:
            await self._plan_download(item, (), plan)
        for item, _ in plan:
            if not item["dir"]:
                progress.total_files += 1
                progress.total_bytes += int(item.get("size", 0))
        _notify(on_progress, progress)

        local_dirs: dict[tuple[str, ...], Path] = {(): Path(local_dir)}
        results = []
        for item, rel in plan:
            parent = local_dirs[rel[:-1]]
            if item["dir"]:
                target = unique_path(parent, sanitize_name(item["name"])) if len(rel) == 1 else \
                    parent / sanitize_name(item["name"])
                target.mkdir(parents=True, exist_ok=True)
                local_dirs[rel] = target
            else:
                target = await self._download_file(item, parent, progress, on_progress)
            if len(rel) == 1:
                results.append(target)
        return results

    async def _plan_download(self, item: dict, rel: tuple[str, ...], plan: list) -> None:
        here = (*rel, item["name"])
        plan.append((item, here))
        if item["dir"]:
            listing = await self.list(item["path"])
            for child in listing["entries"]:
                await self._plan_download(child, here, plan)

    async def _download_file(self, item: dict, local_dir: Path, progress: Progress,
                             on_progress: ProgressCallback | None) -> Path:
        session = self.session
        tid = session.new_transfer_id()
        queue = session.open_transfer(tid)
        target = unique_path(local_dir, sanitize_name(item["name"]))
        part = target.with_name(f".{target.name}.raven-part")
        progress.current = item["name"]
        hasher = hashlib.sha256()
        received = 0
        try:
            await session.request({"type": "fs_download", "path": item["path"], "tid": tid})
            with open(part, "wb") as handle:
                while True:
                    message = await queue.get()
                    if isinstance(message, bytes):
                        hasher.update(message)
                        await asyncio.to_thread(handle.write, message)
                        received += len(message)
                        progress.done_bytes += len(message)
                        _notify(on_progress, progress)
                        continue
                    if message.get("type") == "fs_error":
                        raise RavenError(message.get("error", "Download failed."))
                    if message.get("sha256") != hasher.hexdigest() or message.get("size") != received:
                        raise RavenError(f"{item['name']} was damaged in transfer. Please try again.")
                    break
            os.replace(part, target)
        except BaseException:
            with contextlib.suppress(Exception):
                await session.send({"type": "fs_cancel", "tid": tid})
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)
            raise
        finally:
            session.close_transfer(tid)
        progress.done_files += 1
        _notify(on_progress, progress, force=True)
        return target


def _notify(callback: ProgressCallback | None, progress: Progress, force: bool = False) -> None:
    """Call ``callback`` at most ~10 times a second per job."""
    if callback is None:
        return
    now = time.monotonic()
    if force or now - progress.last_report >= 0.1:
        progress.last_report = now
        callback(progress)
