"""The controller side of a connection to a Raven host."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from raven import PROTOCOL_VERSION, protocol, security

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 8.0


class RavenError(Exception):
    """A failure worth showing to the user as-is."""


class AuthError(RavenError):
    pass


@dataclass
class HostHello:
    name: str
    os: str
    salt: bytes
    iterations: int
    nonce: bytes
    fingerprint: str  # observed from the TLS certificate, not self-reported


FrameCallback = Callable[[bytes, tuple[float, float]], None]
MessageCallback = Callable[[dict], None]
ClosedCallback = Callable[[str], None]


KNOWN_OS = {"windows", "macos", "linux"}


def clean_message(value: object) -> str:
    """An error or status text from the other computer, safe to display."""
    return security.clean_display_name(value, max_length=300)


def parse_hello(msg: dict, fingerprint: str) -> HostHello:
    """Validate the host's greeting. Raises RavenError for anything unsafe.

    The iteration count matters: a host that asked for very few PBKDF2
    rounds would get a proof of the password that is cheap to crack offline.
    """
    if msg.get("type") == "error":
        raise RavenError(clean_message(msg.get("error")) or "The computer refused the connection.")
    if msg.get("type") != "hello" or msg.get("version") != PROTOCOL_VERSION:
        raise RavenError("The other computer runs an incompatible version of Raven.")
    try:
        iterations = int(msg["iterations"])
        salt = bytes.fromhex(str(msg["salt"]))
        nonce = protocol.unb64(str(msg["nonce"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RavenError("Unexpected reply from the other computer.") from exc
    if not security.MIN_PBKDF2_ITERATIONS <= iterations <= security.MAX_PBKDF2_ITERATIONS:
        raise RavenError("The other computer asked for unsafe password settings, so Raven didn't send your "
                         "password. It may not be the computer you think it is.")
    if len(salt) < 16 or len(nonce) < 16:
        raise RavenError("The other computer asked for unsafe password settings.")
    os_name = str(msg.get("os", ""))
    return HostHello(
        name=security.clean_display_name(msg.get("name")),
        os=os_name if os_name in KNOWN_OS else "",
        salt=salt,
        iterations=iterations,
        nonce=nonce,
        fingerprint=fingerprint,
    )


def clean_info(info: object) -> dict:
    """The host's session info with display strings cleaned."""
    info = dict(info) if isinstance(info, dict) else {}
    info["name"] = security.clean_display_name(info.get("name"))
    if info.get("os") not in KNOWN_OS:
        info["os"] = ""
    info["mac"] = str(info.get("mac") or "")[:32]
    if not isinstance(info.get("monitors"), list):
        info["monitors"] = []
    return info


def format_uri(address: str, port: int) -> str:
    host = f"[{address}]" if ":" in address and not address.startswith("[") else address
    return f"wss://{host}:{port}/"


class RemoteSession:
    """Connect, verify, authenticate, then exchange messages with one host.

    Usage::

        session = RemoteSession(address, port)
        hello = await session.connect()          # check hello.fingerprint
        info = await session.authenticate(key)   # key = derive_key(pw, hello.salt)
        session.start_receiving(on_frame, on_message, on_closed)
    """

    def __init__(self, address: str, port: int, client_name: str | None = None):
        self.address = address
        self.port = port
        self.client_name = client_name or socket.gethostname()
        self.ws: ClientConnection | None = None
        self.hello: HostHello | None = None
        self.info: dict = {}
        self.on_frame: FrameCallback | None = None
        self.closed_reason: str | None = None
        self._receiver: asyncio.Task | None = None
        self._listeners: list[MessageCallback] = []
        self._close_listeners: list[ClosedCallback] = []
        self._pending: dict[int, asyncio.Future] = {}
        self._transfers: dict[int, asyncio.Queue] = {}
        self._next_rid = 0
        self._next_tid = 0

    async def connect(self) -> HostHello:
        try:
            self.ws = await asyncio.wait_for(
                connect(
                    format_uri(self.address, self.port),
                    ssl=security.client_ssl_context(),
                    proxy=None,  # LAN only: never route through an HTTP proxy
                    compression=None,
                    max_size=64 * 2**20,
                    open_timeout=CONNECT_TIMEOUT,
                    user_agent_header=None,
                ),
                CONNECT_TIMEOUT + 1,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise RavenError(f"Could not reach {self.address}:{self.port} ({exc}).") from exc
        except Exception as exc:  # noqa: BLE001 - websockets handshake errors
            raise RavenError(f"Could not connect to {self.address}:{self.port}: {exc}") from exc

        ssl_object = self.ws.transport.get_extra_info("ssl_object")
        fingerprint = security.fingerprint_der(ssl_object.getpeercert(binary_form=True))
        msg = await self._recv_json()
        try:
            self.hello = parse_hello(msg, fingerprint)
        except RavenError:
            await self.close()
            raise
        return self.hello

    def derive_key(self, password: str) -> bytes:
        assert self.hello
        return security.derive_key(password, self.hello.salt, self.hello.iterations)

    async def authenticate(self, key: bytes) -> dict:
        assert self.ws and self.hello
        proof = security.make_proof(key, self.hello.nonce, self.hello.fingerprint)
        await self.send({"type": "auth", "proof": protocol.b64(proof), "client_name": self.client_name})
        msg = await self._recv_json(timeout=30)
        if msg["type"] != "auth_result" or not msg.get("ok"):
            await self.close()
            raise AuthError(clean_message(msg.get("error")) or "Authentication failed.")
        self.info = clean_info(msg.get("info"))
        # From here on one task reads everything and routes it: screen frames,
        # replies to requests, file chunks and unsolicited messages.
        self._receiver = asyncio.create_task(self._read_loop())
        return self.info

    async def _recv_json(self, timeout: float = CONNECT_TIMEOUT) -> dict:
        assert self.ws
        try:
            raw = await asyncio.wait_for(self.ws.recv(), timeout)
            return protocol.decode(raw)
        except ConnectionClosed as exc:
            raise RavenError(exc.rcvd.reason if exc.rcvd and exc.rcvd.reason else "Connection closed.") from exc
        except asyncio.TimeoutError as exc:
            raise RavenError("The other computer did not answer.") from exc
        except ValueError as exc:
            raise RavenError("Unexpected reply from the other computer.") from exc

    async def send(self, msg: dict) -> None:
        if self.ws:
            await self.ws.send(protocol.encode(msg))

    async def send_bytes(self, data: bytes) -> None:
        if self.ws:
            await self.ws.send(data)

    @property
    def is_closed(self) -> bool:
        return self.closed_reason is not None

    # ------------------------------------------------------------ routing

    def start_receiving(self, on_frame: FrameCallback, on_message: MessageCallback, on_closed: ClosedCallback) -> None:
        """Register the viewer's callbacks (kept for the viewer's convenience)."""
        self.on_frame = on_frame
        self.add_listener(on_message)
        self.add_close_listener(on_closed)

    def add_listener(self, callback: MessageCallback) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: MessageCallback) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def add_close_listener(self, callback: ClosedCallback) -> None:
        if self.closed_reason is not None:
            callback(self.closed_reason)
        else:
            self._close_listeners.append(callback)

    def remove_close_listener(self, callback: ClosedCallback) -> None:
        if callback in self._close_listeners:
            self._close_listeners.remove(callback)

    def new_transfer_id(self) -> int:
        self._next_tid += 1
        return self._next_tid

    def open_transfer(self, tid: int) -> asyncio.Queue:
        """Queue receiving the chunks (bytes) and the final fs_done/fs_error (dict) for ``tid``."""
        queue: asyncio.Queue = asyncio.Queue()
        self._transfers[tid] = queue
        return queue

    def close_transfer(self, tid: int) -> None:
        self._transfers.pop(tid, None)

    async def request(self, msg: dict, timeout: float = 20) -> dict:
        """Send ``msg`` and wait for the host's reply; raise RavenError on failure."""
        if self.closed_reason is not None:
            raise RavenError(self.closed_reason)
        self._next_rid += 1
        rid = self._next_rid
        future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            await self.send({**msg, "rid": rid})
            reply = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise RavenError("The other computer did not answer.") from exc
        except ConnectionClosed as exc:
            raise RavenError("Connection closed.") from exc
        finally:
            self._pending.pop(rid, None)
        if reply.get("ok") is False:
            raise RavenError(reply.get("error") or "The request failed.")
        return reply

    async def _read_loop(self) -> None:
        reason = "Disconnected."
        try:
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    await self._on_binary(raw)
                    continue
                try:
                    msg = protocol.decode(raw)
                except ValueError:
                    continue
                rid = msg.get("rid")
                if rid in self._pending:
                    future = self._pending.pop(rid)
                    if not future.done():
                        future.set_result(msg)
                    continue
                tid = msg.get("tid")
                if msg["type"] in ("fs_done", "fs_error") and tid in self._transfers:
                    self._transfers[tid].put_nowait(msg)
                    continue
                for listener in list(self._listeners):
                    try:
                        listener(msg)
                    except Exception:  # noqa: BLE001
                        log.exception("message listener failed")
        except ConnectionClosed as exc:
            if exc.rcvd and exc.rcvd.reason:
                reason = exc.rcvd.reason
        except asyncio.CancelledError:
            reason = "Closed."
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("receive loop failed")
            reason = str(exc)
        finally:
            self._finish(reason)

    async def _on_binary(self, raw: bytes) -> None:
        kind = protocol.frame_kind(raw)
        if kind == protocol.FRAME_KIND_JPEG:
            try:
                jpeg, cursor = protocol.unpack_frame(raw)
            except Exception:  # noqa: BLE001
                return
            if self.on_frame:
                self.on_frame(jpeg, cursor)
            await self.send({"type": "ack"})
        elif kind == protocol.FRAME_KIND_CHUNK:
            tid, data = protocol.unpack_chunk(raw)
            queue = self._transfers.get(tid)
            if queue is not None:
                queue.put_nowait(data)

    def _finish(self, reason: str) -> None:
        if self.closed_reason is not None:
            return
        self.closed_reason = reason
        error = RavenError(f"Connection lost: {reason}")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        for queue in self._transfers.values():
            queue.put_nowait({"type": "fs_error", "error": f"Connection lost: {reason}"})
        for callback in list(self._close_listeners):
            try:
                callback(reason)
            except Exception:  # noqa: BLE001
                log.exception("close listener failed")

    # ------------------------------------------------------------ commands

    async def ping(self) -> float:
        loop = asyncio.get_running_loop()
        started = loop.time()
        await self.request({"type": "ping"})
        return loop.time() - started

    async def power(self, action: str, timeout: float = 10) -> None:
        """Send a power command and wait for the host to accept it."""
        reply = await self.request({"type": "power", "action": action}, timeout)
        if not reply.get("ok"):
            raise RavenError(reply.get("error", f"{action} failed."))

    async def close(self) -> None:
        if self.ws:
            with contextlib.suppress(Exception):
                await self.ws.close()
        if self._receiver and self._receiver is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(self._receiver), 3)
            if not self._receiver.done():
                self._receiver.cancel()
        self._finish("Closed.")
