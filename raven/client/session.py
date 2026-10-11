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
        self._receiver: asyncio.Task | None = None

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
        if msg["type"] == "error":
            await self.close()
            raise RavenError(msg.get("error", "The computer refused the connection."))
        if msg["type"] != "hello" or msg.get("version") != PROTOCOL_VERSION:
            await self.close()
            raise RavenError("The other computer runs an incompatible version of Raven.")
        self.hello = HostHello(
            name=str(msg.get("name", "")),
            os=str(msg.get("os", "")),
            salt=bytes.fromhex(msg["salt"]),
            iterations=int(msg["iterations"]),
            nonce=protocol.unb64(msg["nonce"]),
            fingerprint=fingerprint,
        )
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
            raise AuthError(msg.get("error", "Authentication failed."))
        self.info = msg.get("info", {})
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

    def start_receiving(self, on_frame: FrameCallback, on_message: MessageCallback, on_closed: ClosedCallback) -> None:
        self._receiver = asyncio.create_task(self._receive(on_frame, on_message, on_closed))

    async def _receive(self, on_frame: FrameCallback, on_message: MessageCallback, on_closed: ClosedCallback):
        reason = "Disconnected."
        try:
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    try:
                        jpeg, cursor = protocol.unpack_frame(raw)
                    except Exception:  # noqa: BLE001
                        continue
                    on_frame(jpeg, cursor)
                    await self.send({"type": "ack"})
                else:
                    try:
                        on_message(protocol.decode(raw))
                    except ValueError:
                        continue
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
            on_closed(reason)

    async def power(self, action: str, timeout: float = 10) -> None:
        """Send a power command and wait for the host to accept it."""
        await self.send({"type": "power", "action": action})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            msg = await self._recv_json(timeout=max(0.1, deadline - loop.time()))
            if msg["type"] == "power_result" and msg.get("action") == action:
                if not msg.get("ok"):
                    raise RavenError(msg.get("error", f"{action} failed."))
                return

    async def close(self) -> None:
        if self._receiver and self._receiver is not asyncio.current_task():
            self._receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._receiver
        if self.ws:
            with contextlib.suppress(Exception):
                await self.ws.close()
