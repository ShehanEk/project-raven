"""Host side of file transfer: browse folders, receive uploads, stream downloads.

An authenticated controller can already type anything on this computer, so
file access runs with the same rights as the logged-in user. The host can
switch it off with ``allow_files``.

Requests (all carry a ``rid`` and get a reply)::

    fs_places                         -> {places: [{name, path}], roots: [...]}
    fs_list {path}                    -> {path, parent, entries: [{name, path, dir, size, mtime, hidden}]}
    fs_mkdir {parent, name}           -> {path}
    fs_upload_start {dir, name, size, tid} -> {path}   then binary chunks for tid
    fs_upload_end {tid, sha256}       -> {path}
    fs_upload_abort {tid}
    fs_download {path, tid}           -> {name, size}  then chunks, then fs_done {tid, sha256}
    fs_cancel {tid}                   stop a download

Uploads are written to a hidden ``.part`` file and renamed into place only
after the SHA-256 checksum matches, so a cancelled or broken transfer never
leaves a half-written file under the real name.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import shutil
import stat
import string
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from raven import protocol

log = logging.getLogger(__name__)

CHUNK_SIZE = 256 * 1024
MAX_ENTRIES = 5000
_WINDOWS_RESERVED = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                         *(f"LPT{i}" for i in range(1, 10))}


class FileError(Exception):
    """A failure whose message is safe and useful to show the controller."""


# ---------------------------------------------------------------- helpers

def sanitize_name(name: str) -> str:
    """Reduce ``name`` to a single safe path component, or raise FileError."""
    name = str(name).replace("\\", "/").split("/")[-1].strip()
    name = _WINDOWS_RESERVED.sub("_", name).rstrip(" .")
    if not name or name in (".", ".."):
        raise FileError("Invalid file name.")
    stem = name.split(".")[0].upper()
    if stem in _WINDOWS_DEVICE_NAMES:
        name = "_" + name
    return name[:255]


def unique_path(directory: Path, name: str) -> Path:
    """``directory/name``, or ``name (2).ext`` etc. if that already exists."""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, dot, ext = name.rpartition(".")
    if not stem:  # no extension, or a dotfile like ".bashrc"
        stem, dot, ext = name, "", ""
    for n in range(2, 10_000):
        candidate = directory / f"{stem} ({n}){dot}{ext}"
        if not candidate.exists():
            return candidate
    raise FileError("Too many files with that name.")


def roots() -> list[str]:
    if sys.platform == "win32":
        listdrives = getattr(os, "listdrives", None)
        if listdrives:
            return list(listdrives())
        return [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
    found = ["/"]
    if sys.platform == "darwin" and os.path.isdir("/Volumes"):
        found += sorted(str(p) for p in Path("/Volumes").iterdir() if p.is_dir())
    return found


def places() -> list[dict]:
    home = Path.home()
    result = [{"name": "Home", "path": str(home)}]
    for name in ("Desktop", "Documents", "Downloads"):
        path = home / name
        if path.is_dir():
            result.append({"name": name, "path": str(path)})
    return result


def downloads_dir() -> Path:
    path = Path.home() / "Downloads"
    return path if path.is_dir() else Path.home()


def _is_hidden(entry: os.DirEntry, st: os.stat_result | None) -> bool:
    if entry.name.startswith("."):
        return True
    attrs = getattr(st, "st_file_attributes", 0) if st else 0
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0))


def list_dir(path: str) -> dict:
    target = Path(path).expanduser()
    if not target.is_dir():
        raise FileError("That folder doesn't exist.")
    entries = []
    truncated = False
    try:
        with os.scandir(target) as it:
            for entry in it:
                if len(entries) >= MAX_ENTRIES:
                    truncated = True
                    break
                try:
                    st = entry.stat()
                    is_dir = entry.is_dir()
                except OSError:
                    st, is_dir = None, False  # broken link or no permission
                entries.append({
                    "name": entry.name,
                    "path": str(Path(entry.path)),
                    "dir": is_dir,
                    "size": 0 if is_dir or not st else st.st_size,
                    "mtime": st.st_mtime if st else 0,
                    "hidden": _is_hidden(entry, st),
                })
    except PermissionError as exc:
        raise FileError("You don't have permission to open that folder.") from exc
    except OSError as exc:
        raise FileError(f"Can't open that folder: {exc.strerror or exc}") from exc
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    parent = target.parent
    return {
        "path": str(target),
        "parent": str(parent) if parent != target else "",
        "entries": entries,
        "truncated": truncated,
    }


# ---------------------------------------------------------------- service

@dataclass
class _Upload:
    file: object
    part_path: Path
    final_path: Path
    size: int
    received: int = 0
    hasher: object = None


class FileService:
    """File operations for one controller session."""

    def __init__(
        self,
        send_json: Callable[[dict], Awaitable[None]],
        send_bytes: Callable[[bytes], Awaitable[None]],
        on_event: Callable[[str, dict], None],
        client_name: str,
    ):
        self.send_json = send_json
        self.send_bytes = send_bytes
        self.on_event = on_event
        self.client_name = client_name
        self.uploads: dict[int, _Upload] = {}
        self.downloads: dict[int, asyncio.Task] = {}

    async def handle(self, msg: dict) -> dict | None:
        """Run one fs_* request and return the reply payload (None for no reply)."""
        kind = msg["type"]
        if kind == "fs_places":
            return {"places": places(), "roots": roots(), "downloads": str(downloads_dir())}
        if kind == "fs_list":
            return await asyncio.to_thread(list_dir, str(msg.get("path") or Path.home()))
        if kind == "fs_mkdir":
            return await asyncio.to_thread(self._mkdir, str(msg.get("parent", "")), str(msg.get("name", "")))
        if kind == "fs_upload_start":
            return await self._upload_start(msg)
        if kind == "fs_upload_end":
            return await self._upload_end(msg)
        if kind == "fs_upload_abort":
            await self._abort_upload(self._tid(msg))
            return {}
        if kind == "fs_download":
            return await self._download_start(msg)
        if kind == "fs_cancel":
            task = self.downloads.pop(self._tid(msg), None)
            if task:
                task.cancel()
            return {}
        raise FileError(f"Unknown request {kind}.")

    @staticmethod
    def _tid(msg: dict) -> int:
        tid = msg.get("tid")
        if not isinstance(tid, int) or not 0 < tid < 2**32:
            raise FileError("Invalid transfer id.")
        return tid

    @staticmethod
    def _mkdir(parent: str, name: str) -> dict:
        parent_path = Path(parent)
        if not parent_path.is_dir():
            raise FileError("The destination folder doesn't exist.")
        path = parent_path / sanitize_name(name)
        try:
            path.mkdir(exist_ok=True)
        except FileExistsError as exc:
            raise FileError(f"A file named {path.name} is in the way.") from exc
        except OSError as exc:
            raise FileError(f"Can't create folder: {exc.strerror or exc}") from exc
        return {"path": str(path)}

    # ------------------------------------------------------------ uploads

    async def _upload_start(self, msg: dict) -> dict:
        tid = self._tid(msg)
        if tid in self.uploads:
            raise FileError("Transfer id already in use.")
        directory = Path(str(msg.get("dir") or downloads_dir()))
        size = int(msg.get("size", 0))
        if not directory.is_dir():
            raise FileError("The destination folder doesn't exist.")
        if size < 0:
            raise FileError("Invalid size.")
        if shutil.disk_usage(directory).free < size:
            raise FileError("Not enough free space on the other computer.")
        name = sanitize_name(str(msg.get("name", "")))

        def open_part():
            final = unique_path(directory, name)
            part = directory / f".{final.name}.raven-part"
            return final, part, open(part, "wb")

        try:
            final, part, handle = await asyncio.to_thread(open_part)
        except PermissionError as exc:
            raise FileError("No permission to write to that folder.") from exc
        except OSError as exc:
            raise FileError(f"Can't write there: {exc.strerror or exc}") from exc
        self.uploads[tid] = _Upload(handle, part, final, size, hasher=hashlib.sha256())
        return {"path": str(final)}

    async def handle_chunk(self, tid: int, data: bytes) -> None:
        upload = self.uploads.get(tid)
        if upload is None:
            return  # aborted, or a download id echoed back by mistake
        upload.received += len(data)
        if upload.received > upload.size:
            await self._abort_upload(tid)
            await self.send_json({"type": "fs_error", "tid": tid, "error": "Received more data than announced."})
            return
        upload.hasher.update(data)
        await asyncio.to_thread(upload.file.write, data)

    async def _upload_end(self, msg: dict) -> dict:
        tid = self._tid(msg)
        upload = self.uploads.pop(tid, None)
        if upload is None:
            raise FileError("Unknown transfer.")
        await asyncio.to_thread(upload.file.close)
        ok = upload.received == upload.size and upload.hasher.hexdigest() == str(msg.get("sha256", "")).lower()
        if not ok:
            await asyncio.to_thread(upload.part_path.unlink, missing_ok=True)
            raise FileError("The file was damaged in transfer. Please try again.")

        def finish() -> Path:
            final = upload.final_path
            if final.exists():  # appeared while uploading
                final = unique_path(final.parent, final.name)
            os.replace(upload.part_path, final)
            return final

        final = await asyncio.to_thread(finish)
        self.on_event("file_received", {"client": self.client_name, "name": final.name, "path": str(final),
                                        "size": upload.size})
        return {"path": str(final)}

    async def _abort_upload(self, tid: int) -> None:
        upload = self.uploads.pop(tid, None)
        if upload:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(upload.file.close)
                await asyncio.to_thread(upload.part_path.unlink, missing_ok=True)

    # ------------------------------------------------------------ downloads

    async def _download_start(self, msg: dict) -> dict:
        tid = self._tid(msg)
        path = Path(str(msg.get("path", "")))
        if tid in self.downloads:
            raise FileError("Transfer id already in use.")
        if not path.is_file():
            raise FileError("That file doesn't exist.")
        try:
            handle = await asyncio.to_thread(open, path, "rb")
        except PermissionError as exc:
            raise FileError("You don't have permission to read that file.") from exc
        except OSError as exc:
            raise FileError(f"Can't read that file: {exc.strerror or exc}") from exc
        size = os.fstat(handle.fileno()).st_size
        # Start streaming only after the reply below has been sent.
        self.downloads[tid] = asyncio.get_running_loop().create_task(self._stream(tid, handle, path, size))
        return {"name": path.name, "size": size}

    async def _stream(self, tid: int, handle, path: Path, size: int) -> None:
        await asyncio.sleep(0)
        hasher = hashlib.sha256()
        sent = 0
        try:
            while True:
                data = await asyncio.to_thread(handle.read, CHUNK_SIZE)
                if not data:
                    break
                hasher.update(data)
                sent += len(data)
                await self.send_bytes(protocol.pack_chunk(tid, data))
            await self.send_json({"type": "fs_done", "tid": tid, "size": sent, "sha256": hasher.hexdigest()})
            self.on_event("file_sent", {"client": self.client_name, "name": path.name, "size": sent})
        except asyncio.CancelledError:
            pass
        except OSError as exc:
            with contextlib.suppress(Exception):
                await self.send_json({"type": "fs_error", "tid": tid, "error": f"Read failed: {exc}"})
        except Exception:  # noqa: BLE001 - connection gone
            log.debug("download %s stopped", tid, exc_info=True)
        finally:
            handle.close()
            self.downloads.pop(tid, None)

    async def close(self) -> None:
        for tid in list(self.uploads):
            await self._abort_upload(tid)
        for task in list(self.downloads.values()):
            task.cancel()
        for task in list(self.downloads.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
