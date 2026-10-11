"""macOS privacy permissions needed by the host (Screen Recording, Accessibility)."""

from __future__ import annotations

import subprocess
import sys

IS_MAC = sys.platform == "darwin"

_PANES = {
    "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
}


def status() -> dict[str, bool | None]:
    """True/False per permission on macOS; None where it doesn't apply or is unknown."""
    result: dict[str, bool | None] = {"screen": None, "accessibility": None}
    if not IS_MAC:
        return result
    try:
        import Quartz

        result["screen"] = bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:  # noqa: BLE001
        pass
    try:
        import HIServices

        result["accessibility"] = bool(HIServices.AXIsProcessTrusted())
    except Exception:  # noqa: BLE001
        pass
    return result


def request() -> None:
    """Ask macOS to show its permission prompts (each appears only once)."""
    if not IS_MAC:
        return
    try:
        import Quartz

        Quartz.CGRequestScreenCaptureAccess()
    except Exception:  # noqa: BLE001
        pass
    try:
        import HIServices

        HIServices.AXIsProcessTrustedWithOptions({HIServices.kAXTrustedCheckOptionPrompt: True})
    except Exception:  # noqa: BLE001
        pass


def open_settings(which: str) -> None:
    if IS_MAC and which in _PANES:
        subprocess.Popen(["open", _PANES[which]])
