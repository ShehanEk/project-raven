"""Host side of clipboard sync: one watcher shared by all synced sessions."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from raven.clip import Clip

log = logging.getLogger(__name__)

POLL_INTERVAL = 0.5


class ClipboardBackend(Protocol):
    supports_images: bool

    def change_token(self):
        """Cheap value that changes when the clipboard may have changed (None = unknown)."""

    async def read(self) -> Clip | None: ...

    async def write(self, clip: Clip) -> None: ...


Subscriber = Callable[[Clip], Awaitable[None]]


class ClipboardHub:
    """Pushes local clipboard changes to subscribed sessions and applies theirs.

    ``last_digest`` is the content everyone already has. A change is only
    broadcast when the clipboard holds something different, which is what
    stops a clip received from a controller from bouncing straight back.
    """

    def __init__(self, backend: ClipboardBackend, poll_interval: float = POLL_INTERVAL):
        self.backend = backend
        self.poll_interval = poll_interval
        self.subscribers: dict[object, Subscriber] = {}
        self.last_digest: str | None = None
        self._last_token = object()
        self._watcher: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def subscribe(self, key: object, callback: Subscriber) -> None:
        self.subscribers[key] = callback
        if self._watcher is None or self._watcher.done():
            # Whatever is on the clipboard now isn't news; only later copies are.
            current = await self._read()
            self.last_digest = current.digest if current else None
            self._last_token = self.backend.change_token()
            self._watcher = asyncio.create_task(self._watch())

    async def unsubscribe(self, key: object) -> None:
        self.subscribers.pop(key, None)
        if not self.subscribers and self._watcher:
            self._watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._watcher
            self._watcher = None

    async def apply(self, clip: Clip, origin: object) -> None:
        """Put a controller's clip on this computer's clipboard."""
        if self.backend.supports_images is False and clip.kind == "image":
            return
        async with self._lock:
            self.last_digest = clip.digest
            await self.backend.write(clip)
            self._last_token = self.backend.change_token()
        await self._broadcast(clip, exclude=origin)

    async def _read(self) -> Clip | None:
        try:
            return await self.backend.read()
        except Exception:  # noqa: BLE001 - a clipboard hiccup must not end the session
            log.debug("clipboard read failed", exc_info=True)
            return None

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            token = self.backend.change_token()
            if token is not None and token == self._last_token:
                continue
            async with self._lock:
                self._last_token = token
                clip = await self._read()
                if clip is None or clip.digest == self.last_digest:
                    continue
                self.last_digest = clip.digest
            await self._broadcast(clip)

    async def _broadcast(self, clip: Clip, exclude: object = None) -> None:
        for key, callback in list(self.subscribers.items()):
            if key is exclude:
                continue
            try:
                await callback(clip)
            except Exception:  # noqa: BLE001 - that session is closing
                log.debug("clipboard push failed", exc_info=True)

    async def close(self) -> None:
        for key in list(self.subscribers):
            await self.unsubscribe(key)
