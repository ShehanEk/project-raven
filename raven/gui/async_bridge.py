"""Run networking on a background asyncio loop and hand results back to Qt."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Callable, Coroutine
from typing import Any

from PySide6.QtCore import QObject, Signal


class UiCaller(QObject):
    """Calls functions on the Qt GUI thread, from any thread."""

    _call = Signal(object)

    def __init__(self):
        super().__init__()
        self._call.connect(lambda fn: fn())

    def __call__(self, fn: Callable[[], Any]) -> None:
        self._call.emit(fn)


class AsyncRunner:
    def __init__(self, ui: UiCaller):
        self.ui = ui
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="raven-net", daemon=True)
        self._thread.start()

    def submit(self, coro: Coroutine) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run(
        self,
        coro: Coroutine,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> concurrent.futures.Future:
        """Run ``coro`` in the background; callbacks run on the GUI thread."""
        future = self.submit(coro)

        def finished(f: concurrent.futures.Future) -> None:
            if f.cancelled():
                return
            exc = f.exception()
            if exc is not None:
                if on_error:
                    self.ui(lambda: on_error(exc))
            elif on_done:
                result = f.result()
                self.ui(lambda: on_done(result))

        future.add_done_callback(finished)
        return future

    def shutdown(self, timeout: float = 3.0) -> None:
        async def drain():
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            self.submit(drain()).result(timeout)
        except Exception:  # noqa: BLE001
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout)
