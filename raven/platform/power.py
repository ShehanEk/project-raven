"""Shut down, restart, sleep or lock the local computer."""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable

log = logging.getLogger(__name__)

ACTIONS = ("sleep", "lock", "restart", "shutdown")

Step = list[str] | Callable[[], None]


def _windows_sleep() -> None:
    import ctypes

    # Sleeps, or hibernates when hibernation is enabled on the machine
    # (run `powercfg /hibernate off` as admin for true sleep).
    if not ctypes.windll.powrprof.SetSuspendState(False, True, False):
        raise OSError("SetSuspendState failed")


def _windows_lock() -> None:
    import ctypes

    if not ctypes.windll.user32.LockWorkStation():
        raise OSError("LockWorkStation failed")


def plan(action: str, os_name: str | None = None) -> list[Step]:
    """Return the steps to try, in order, until one succeeds."""
    if action not in ACTIONS:
        raise ValueError(f"unknown power action: {action!r}")
    os_name = os_name or _current_os()

    if os_name == "windows":
        return {
            "shutdown": [["shutdown", "/s", "/t", "0"]],
            "restart": [["shutdown", "/r", "/t", "0"]],
            "sleep": [_windows_sleep],
            "lock": [_windows_lock],
        }[action]

    if os_name == "macos":
        return {
            # System Events asks once for Automation permission; the sudo
            # fallback only works if passwordless sudo was configured.
            "shutdown": [
                ["osascript", "-e", 'tell application "System Events" to shut down'],
                ["sudo", "-n", "/sbin/shutdown", "-h", "now"],
            ],
            "restart": [
                ["osascript", "-e", 'tell application "System Events" to restart'],
                ["sudo", "-n", "/sbin/shutdown", "-r", "now"],
            ],
            "sleep": [["pmset", "sleepnow"]],
            # Locks when "Require password after screen saver begins or
            # display is turned off" is set to Immediately (the default).
            "lock": [["pmset", "displaysleepnow"]],
        }[action]

    return {
        "shutdown": [["systemctl", "poweroff"]],
        "restart": [["systemctl", "reboot"]],
        "sleep": [["systemctl", "suspend"]],
        "lock": [["loginctl", "lock-session"]],
    }[action]


def perform(action: str) -> None:
    """Run ``action``, raising ``OSError`` if every strategy fails."""
    errors = []
    for step in plan(action):
        try:
            if callable(step):
                step()
                return
            result = subprocess.run(step, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                return
            errors.append(f"{' '.join(step)}: {result.stderr.strip() or result.returncode}")
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{step}: {exc}")
    log.error("power action %s failed: %s", action, errors)
    raise OSError("; ".join(errors))


def _current_os() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"
