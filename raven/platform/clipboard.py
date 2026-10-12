"""OS clipboard helpers that don't need Qt.

* :func:`change_count` asks the OS for a counter that changes whenever the
  clipboard does (macOS pasteboard changeCount, Windows sequence number), so
  watchers can poll cheaply instead of reading the whole clipboard.
* :class:`CommandClipboard` reads and writes plain text with the platform's
  command-line tools. The headless ``raven host`` uses it, since it has no Qt.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys

from raven.clip import Clip


def change_count() -> int | None:
    """A number that changes when the clipboard changes, or None if unknown."""
    if sys.platform == "darwin":
        try:
            from AppKit import NSPasteboard

            return int(NSPasteboard.generalPasteboard().changeCount())
        except Exception:  # noqa: BLE001 - pyobjc missing
            return None
    if sys.platform == "win32":
        try:
            import ctypes

            return int(ctypes.windll.user32.GetClipboardSequenceNumber())
        except Exception:  # noqa: BLE001
            return None
    return None


def _commands() -> tuple[list[str], list[str]] | None:
    """(read, write) commands for text on this system, or None."""
    if sys.platform == "darwin":
        return ["pbpaste"], ["pbcopy"]
    if sys.platform == "win32":
        ps = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
        return (
            ps + ["[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"],
            ps + ["[Console]::InputEncoding=[Text.Encoding]::UTF8; "
                  "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
        )
    if shutil.which("wl-paste") and shutil.which("wl-copy"):
        return ["wl-paste", "--no-newline"], ["wl-copy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-o"], ["xclip", "-selection", "clipboard", "-i"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--output"], ["xsel", "--clipboard", "--input"]
    return None


class CommandClipboard:
    """Text-only clipboard backend using pbcopy/pbpaste, PowerShell or xclip."""

    supports_images = False

    def __init__(self):
        self._commands = _commands()

    @property
    def available(self) -> bool:
        return self._commands is not None

    def change_token(self):
        return change_count()

    async def read(self) -> Clip | None:
        if not self._commands:
            return None
        try:
            result = await asyncio.to_thread(
                subprocess.run, self._commands[0], capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        text = result.stdout.decode("utf-8", errors="replace")
        if sys.platform == "win32" and text.endswith("\r\n"):
            text = text[:-2]  # Get-Clipboard -Raw adds a trailing newline
        return Clip.from_text(text) if text else None

    async def write(self, clip: Clip) -> None:
        if not self._commands or clip.kind != "text":
            return
        await asyncio.to_thread(
            subprocess.run, self._commands[1], input=clip.text.encode("utf-8"), capture_output=True, timeout=5)
