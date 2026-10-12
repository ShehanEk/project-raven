"""The host side: lets authenticated controllers on the LAN see and drive this computer."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from pathlib import Path

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from raven import PROTOCOL_VERSION, protocol, security
from raven.config import SettingsStore, local_os
from raven.host.files import FileError, FileService
from raven.platform import netinfo, power
from raven.platform.capture import Monitor, ScreenCapturer
from raven.platform.inputs import InputInjector
from raven.protocol import clamp

log = logging.getLogger(__name__)

EventCallback = Callable[[str, dict], None]

MAX_FRAMES_IN_FLIGHT = 2
AUTH_TIMEOUT = 30.0
FAILED_AUTH_DELAY = 1.0


class HostServer:
    """WebSocket/TLS server. All methods run on one asyncio event loop."""

    def __init__(
        self,
        store: SettingsStore,
        cert_dir: Path,
        on_event: EventCallback | None = None,
        capturer_factory: Callable[[], ScreenCapturer] = ScreenCapturer,
        injector_factory: Callable[[], InputInjector] = InputInjector,
        power_runner: Callable[[str], None] = power.perform,
        power_delay: float = 1.0,
    ):
        self.store = store
        self.on_event = on_event or (lambda kind, detail: None)
        self.capturer_factory = capturer_factory
        self.injector_factory = injector_factory
        self.power_runner = power_runner
        self.power_delay = power_delay
        self.cert_path, self.key_path, self.fingerprint = security.ensure_certificate(cert_dir)
        self.limiter = security.AuthRateLimiter()
        self.sessions: set[_Session] = set()
        self._server: Server | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int | None:
        if not self._server:
            return None
        return self._server.sockets[0].getsockname()[1]

    async def start(self, port: int | None = None, bind: str | None = None) -> None:
        if self._server:
            return
        if not self.store.settings.host.has_password:
            raise RuntimeError("Set a password before allowing remote control.")
        port = self.store.settings.host.port if port is None else port
        self._server = await serve(
            self._handle,
            bind,
            port,
            ssl=security.server_ssl_context(self.cert_path, self.key_path),
            process_request=self._check_origin,
            compression=None,
            max_size=2**20,
            server_header=None,
        )
        log.info("host listening on port %s", self.port)
        self.on_event("started", {"port": self.port})

    async def stop(self) -> None:
        if not self._server:
            return
        server, self._server = self._server, None
        server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), 5)
        self.on_event("stopped", {})

    async def disconnect_all(self) -> None:
        for session in list(self.sessions):
            await session.ws.close(1001, "Disconnected by host")

    # ------------------------------------------------------------ handshake

    def _check_origin(self, connection: ServerConnection, request):
        address = connection.remote_address[0] if connection.remote_address else ""
        if not security.is_lan_address(address):
            self.on_event("rejected", {"address": address, "reason": "not on local network"})
            return connection.respond(HTTPStatus.FORBIDDEN, "Raven only accepts local network connections.\n")
        return None

    async def _handle(self, ws: ServerConnection) -> None:
        address = ws.remote_address[0]
        host = self.store.settings.host
        if self.limiter.is_locked(address):
            await self._refuse(ws, "Too many failed attempts. Try again in a minute.")
            return
        if not host.has_password:
            await self._refuse(ws, "Remote control is not set up on this computer.")
            return

        nonce = security.new_nonce()
        await ws.send(protocol.encode({
            "type": "hello",
            "version": PROTOCOL_VERSION,
            "name": host.name,
            "os": local_os(),
            "salt": host.password_salt,
            "iterations": security.PBKDF2_ITERATIONS,
            "nonce": protocol.b64(nonce),
        }))

        try:
            msg = protocol.decode(await asyncio.wait_for(ws.recv(), AUTH_TIMEOUT))
            proof = protocol.unb64(str(msg.get("proof", "")))
            ok = msg["type"] == "auth" and security.verify_proof(
                bytes.fromhex(host.password_verifier), nonce, self.fingerprint, proof
            )
        except (asyncio.TimeoutError, ConnectionClosed):
            return
        except ValueError:
            ok = False
            msg = {}

        if not ok:
            self.limiter.record_failure(address)
            await asyncio.sleep(FAILED_AUTH_DELAY)
            self.on_event("auth_failed", {"address": address})
            await self._refuse(ws, "Wrong password.", result=True)
            return

        self.limiter.record_success(address)
        client_name = str(msg.get("client_name", ""))[:64] or address
        session = _Session(self, ws, client_name)
        self.sessions.add(session)
        self.on_event("connected", {"address": address, "client": client_name})
        try:
            await session.run()
        finally:
            self.sessions.discard(session)
            await session.cleanup()
            self.on_event("disconnected", {"address": address, "client": client_name})

    @staticmethod
    async def _refuse(ws: ServerConnection, error: str, result: bool = False) -> None:
        with contextlib.suppress(ConnectionClosed):
            if result:
                await ws.send(protocol.encode({"type": "auth_result", "ok": False, "error": error}))
            else:
                await ws.send(protocol.encode({"type": "error", "error": error}))
            await ws.close(1008, error[:100])


class _Session:
    """One authenticated controller connection."""

    def __init__(self, server: HostServer, ws: ServerConnection, client_name: str):
        self.server = server
        self.ws = ws
        self.client_name = client_name
        self.injector = server.injector_factory()
        # mss handles must stay on one thread, so each session gets its own.
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="raven-capture")
        self.capturer = server.capturer_factory()
        self.monitors: list[Monitor] = []
        self.monitor: Monitor | None = None
        self.stream_task: asyncio.Task | None = None
        self.in_flight = 0
        self.acked = asyncio.Event()
        self.files = FileService(self.send, ws.send, server.on_event, client_name)

    @property
    def settings(self):
        return self.server.store.settings.host

    async def _in_executor(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self.executor, fn, *args)

    async def send(self, msg: dict) -> None:
        await self.ws.send(protocol.encode(msg))

    async def reply(self, request: dict, payload: dict | None = None, error: str | None = None) -> None:
        """Answer a request that carried a ``rid``; requests without one get no reply."""
        rid = request.get("rid")
        if not isinstance(rid, int):
            return
        msg = {"type": "reply", "rid": rid, "ok": error is None}
        if error is not None:
            msg["error"] = error
        msg.update(payload or {})
        await self.send(msg)

    async def run(self) -> None:
        try:
            self.monitors = await self._in_executor(self.capturer.monitors)
        except Exception as exc:  # noqa: BLE001 - e.g. no display / no permission
            log.warning("cannot list monitors: %s", exc)
            self.monitors = []
        self.monitor = self.monitors[0] if self.monitors else None
        local_ip = self.ws.local_address[0] if self.ws.local_address else None
        await self.send({
            "type": "auth_result",
            "ok": True,
            "info": {
                "name": self.settings.name,
                "os": local_os(),
                "mac": netinfo.mac_for_address(local_ip) or netinfo.mac_for_address(netinfo.primary_ipv4()),
                "monitors": [{"index": m.index, "width": m.width, "height": m.height} for m in self.monitors],
                "allow_input": self.settings.allow_input,
                "allow_power": self.settings.allow_power,
                "allow_files": self.settings.allow_files,
            },
        })
        async for raw in self.ws:
            if isinstance(raw, bytes):
                if protocol.frame_kind(raw) == protocol.FRAME_KIND_CHUNK and self.settings.allow_files:
                    try:
                        await self.files.handle_chunk(*protocol.unpack_chunk(raw))
                    except Exception:  # noqa: BLE001
                        log.exception("error writing upload chunk")
                continue
            try:
                msg = protocol.decode(raw)
            except ValueError:
                continue
            try:
                await self.dispatch(msg)
            except ConnectionClosed:
                raise
            except Exception:  # noqa: BLE001 - one bad message must not kill the session
                log.exception("error handling %s", msg.get("type"))

    async def dispatch(self, msg: dict) -> None:
        kind = msg["type"]
        if kind == "ack":
            self.in_flight = max(0, self.in_flight - 1)
            self.acked.set()
        elif kind == "ping":
            await self.send({"type": "pong", "t": msg.get("t"), **({"rid": msg["rid"]} if "rid" in msg else {})})
        elif kind == "stream_start":
            await self.start_stream(msg)
        elif kind == "stream_stop":
            await self.stop_stream()
        elif kind == "power":
            await self.handle_power(str(msg.get("action", "")), msg.get("rid"))
        elif kind.startswith("fs_"):
            await self.handle_files(msg)
        elif kind in _INPUT_MESSAGES:
            if not self.settings.allow_input:
                return
            self.handle_input(kind, msg)

    def handle_input(self, kind: str, msg: dict) -> None:
        inj = self.injector
        if kind == "mouse_move":
            if self.monitor:
                inj.move(self.monitor, clamp(msg.get("x"), 0.0, 1.0, 0.0), clamp(msg.get("y"), 0.0, 1.0, 0.0))
        elif kind == "mouse_button":
            inj.button(str(msg.get("button")), bool(msg.get("down")))
        elif kind == "scroll":
            inj.scroll(clamp(msg.get("dx"), -50.0, 50.0, 0.0), clamp(msg.get("dy"), -50.0, 50.0, 0.0))
        elif kind == "key":
            inj.key(str(msg.get("key", "")), bool(msg.get("down")))
        elif kind == "release_all":
            inj.release_all()
        elif kind == "type_text":
            text = str(msg.get("text", ""))[:10_000]
            if text:
                inj.type_text(text)

    async def handle_files(self, msg: dict) -> None:
        if not self.settings.allow_files:
            await self.reply(msg, error="File transfer is turned off on this computer.")
            return
        try:
            payload = await self.files.handle(msg)
        except FileError as exc:
            await self.reply(msg, error=str(exc))
        except (OSError, ValueError, TypeError) as exc:
            await self.reply(msg, error=f"File operation failed: {exc}")
        else:
            await self.reply(msg, payload)

    async def handle_power(self, action: str, rid=None) -> None:
        extra = {"rid": rid} if isinstance(rid, int) else {}
        if action not in power.ACTIONS:
            await self.send({"type": "power_result", "action": action, "ok": False,
                             "error": "Unknown action.", **extra})
            return
        if not self.settings.allow_power:
            await self.send({
                "type": "power_result", "action": action, "ok": False,
                "error": "Power control is turned off on this computer.", **extra,
            })
            return
        await self.send({"type": "power_result", "action": action, "ok": True, **extra})
        self.server.on_event("power", {"action": action, "client": self.client_name})
        # Give the reply a moment to reach the controller before we go dark.
        asyncio.get_running_loop().call_later(
            self.server.power_delay, lambda: asyncio.ensure_future(self._run_power(action))
        )

    async def _run_power(self, action: str) -> None:
        try:
            await asyncio.to_thread(self.server.power_runner, action)
        except Exception as exc:  # noqa: BLE001
            self.server.on_event("error", {"message": f"{action} failed: {exc}"})
            with contextlib.suppress(ConnectionClosed):
                await self.send({"type": "power_result", "action": action, "ok": False, "error": str(exc)})

    # ------------------------------------------------------------ streaming

    async def start_stream(self, msg: dict) -> None:
        await self.stop_stream()
        if not self.monitors:
            await self.send({
                "type": "error",
                "error": "Cannot capture the screen. On macOS, allow Screen Recording for Raven.",
            })
            return
        index = clamp(msg.get("monitor"), 1, 64, 1)
        self.monitor = next((m for m in self.monitors if m.index == index), self.monitors[0])
        fps = clamp(msg.get("fps"), 1, 60, 15)
        quality = clamp(msg.get("quality"), 10, 95, 70)
        max_width = clamp(msg.get("max_width"), 320, 7680, 1920)
        self.in_flight = 0
        self.stream_task = asyncio.create_task(self._stream(self.monitor, fps, quality, max_width))

    async def stop_stream(self) -> None:
        task, self.stream_task = self.stream_task, None
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _wait_for_window(self) -> None:
        while self.in_flight >= MAX_FRAMES_IN_FLIGHT:
            self.acked.clear()
            try:
                await asyncio.wait_for(self.acked.wait(), 5)
            except asyncio.TimeoutError:
                self.in_flight = 0  # acks lost; don't stall forever

    async def _stream(self, monitor: Monitor, fps: int, quality: int, max_width: int) -> None:
        loop = asyncio.get_running_loop()
        interval = 1.0 / fps
        self.capturer.reset()
        await self.send({"type": "stream_info", "monitor": monitor.index,
                         "width": monitor.width, "height": monitor.height})
        last_cursor = None
        try:
            while True:
                started = loop.time()
                await self._wait_for_window()
                jpeg, _ = await self._in_executor(self.capturer.grab_jpeg, monitor, max_width, quality)
                pos = self.injector.cursor_position()
                cursor = monitor.normalize(*pos) if pos else (-1.0, -1.0)
                if jpeg is not None:
                    await self.ws.send(protocol.pack_frame(jpeg, cursor))
                    self.in_flight += 1
                elif cursor != last_cursor:
                    await self.send({"type": "cursor", "x": cursor[0], "y": cursor[1]})
                last_cursor = cursor
                await asyncio.sleep(max(0.0, interval - (loop.time() - started)))
        except ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("stream failed")
            with contextlib.suppress(ConnectionClosed):
                await self.send({"type": "error", "error": f"Screen capture failed: {exc}"})

    async def cleanup(self) -> None:
        await self.stop_stream()
        await self.files.close()
        with contextlib.suppress(Exception):
            self.injector.release_all()
        await asyncio.get_running_loop().run_in_executor(self.executor, self.capturer.close)
        self.executor.shutdown(wait=False)


_INPUT_MESSAGES = frozenset({"mouse_move", "mouse_button", "scroll", "key", "release_all", "type_text"})
