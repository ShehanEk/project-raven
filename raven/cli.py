"""Command line entry point.

    raven                       open the desktop app
    raven host                  run only the host (headless, e.g. on a server)
    raven power HOST ACTION     sleep | lock | restart | shutdown a remote computer
    raven wake MAC              send a Wake-on-LAN packet
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import signal
import sys

from raven import DEFAULT_PORT, __version__, security
from raven.config import RemoteHost, SettingsStore
from raven.platform import power


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="raven", description="LAN-only remote control.")
    parser.add_argument("--version", action="version", version=f"Raven {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")
    # A bare --minimized (used by login items) means "open the app hidden".
    parser.add_argument("--minimized", action="store_true", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")

    gui = sub.add_parser("gui", help="open the desktop app (default)")
    gui.add_argument("--minimized", action="store_true", help="start hidden in the tray")

    host = sub.add_parser("host", help="run the host without a window")
    host.add_argument("--set-password", action="store_true", help="choose a new password first")
    host.add_argument("--port", type=int)

    pw = sub.add_parser("power", help="power action on a remote computer")
    pw.add_argument("address")
    pw.add_argument("action", choices=power.ACTIONS)
    pw.add_argument("--port", type=int, default=DEFAULT_PORT)

    wake = sub.add_parser("wake", help="send Wake-on-LAN")
    wake.add_argument("mac")
    wake.add_argument("--ip", help="last known IP, to also hit its subnet broadcast")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command in (None, "gui"):
        from raven.gui.app import run_gui

        return run_gui(minimized=args.minimized)
    if args.command == "host":
        return _run_host(args)
    if args.command == "power":
        return asyncio.run(_run_power(args))
    if args.command == "wake":
        from raven.wol import wake as send_wake

        send_wake(args.mac, args.ip)
        print(f"Wake-on-LAN packet sent to {args.mac}.")
        return 0
    return 1


def _ask_new_password() -> str:
    while True:
        first = getpass.getpass("New password for remote control: ")
        if len(first) < 6:
            print("Use at least 6 characters.")
            continue
        if getpass.getpass("Repeat password: ") == first:
            return first
        print("Passwords did not match.")


def _run_host(args) -> int:
    from raven.host.service import HostService

    store = SettingsStore()
    host = store.settings.host
    if args.set_password or not host.has_password:
        host.set_password(_ask_new_password())
    if args.port:
        host.port = args.port
    host.enabled = True
    store.save()

    def on_event(kind: str, detail: dict) -> None:
        logging.getLogger("raven.host").info("%s %s", kind, detail)

    async def serve() -> None:
        from raven.platform.clipboard import CommandClipboard

        clipboard = CommandClipboard()
        service = HostService(store, on_event=on_event, clipboard=clipboard if clipboard.available else None)
        await service.start()
        print(f"Raven host '{host.name}' listening on port {service.server.port}")
        print(f"Certificate fingerprint: {security.short_fingerprint(service.fingerprint)}")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass  # Windows: Ctrl+C raises KeyboardInterrupt instead
        await stop.wait()
        await service.stop()

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    return 0


async def _run_power(args) -> int:
    from raven.client.session import RavenError, RemoteSession

    store = SettingsStore()
    remote = next((r for r in store.settings.remotes if r.address == args.address and r.port == args.port), None)
    session = RemoteSession(args.address, args.port)
    try:
        hello = await session.connect()
        if remote and remote.fingerprint and remote.fingerprint != hello.fingerprint:
            print("WARNING: this computer's identity changed since you last connected.", file=sys.stderr)
            print("If you did not reinstall Raven there, someone may be intercepting the connection.",
                  file=sys.stderr)
            return 2
        if not remote or not remote.fingerprint:
            print(f"Connecting to '{hello.name}' for the first time.")
            print(f"Fingerprint: {security.short_fingerprint(hello.fingerprint)}")
            if input("Does it match the one shown on that computer? [y/N] ").strip().lower() != "y":
                return 1
        if remote and remote.saved_key and remote.saved_salt == hello.salt.hex():
            key = bytes.fromhex(remote.saved_key)
        else:
            key = session.derive_key(getpass.getpass(f"Password for {hello.name}: "))
        info = await session.authenticate(key)
        if remote is None:
            remote = RemoteHost(address=args.address, port=args.port)
            store.settings.remotes.append(remote)
        remote.name, remote.os, remote.fingerprint = hello.name, hello.os, hello.fingerprint
        remote.mac = info.get("mac") or remote.mac
        store.save()
        await session.power(args.action)
        print(f"{hello.name}: {args.action} requested.")
        return 0
    except RavenError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        await session.close()
