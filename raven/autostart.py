"""Start Raven at login so a restarted computer is reachable again."""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.raven.host"
_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command() -> list[str]:
    if getattr(sys, "frozen", False):  # PyInstaller bundle
        return [sys.executable, "--minimized"]
    return [sys.executable, "-m", "raven", "gui", "--minimized"]


def _mac_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def is_supported() -> bool:
    return sys.platform in ("win32", "darwin")


def is_enabled() -> bool:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY) as key:
                winreg.QueryValueEx(key, "Raven")
                return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return _mac_plist_path().exists()
    return False


def set_enabled(enabled: bool) -> None:
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                command = subprocess.list2cmdline(launch_command())
                winreg.SetValueEx(key, "Raven", 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, "Raven")
                except FileNotFoundError:
                    pass
    elif sys.platform == "darwin":
        path = _mac_plist_path()
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(plistlib.dumps({
                "Label": LABEL,
                "ProgramArguments": launch_command(),
                "RunAtLoad": True,
                "ProcessType": "Interactive",
            }))
        else:
            path.unlink(missing_ok=True)
