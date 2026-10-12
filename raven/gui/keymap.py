"""Translate Qt key events into Raven's portable key names."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent

K = Qt.Key

SPECIAL = {
    K.Key_Shift: "shift",
    # main() sets AA_MacDontSwapCtrlAndMeta, so on every OS Control is
    # "ctrl" and Meta is Command on a Mac / the Windows key on a PC.
    K.Key_Control: "ctrl",
    K.Key_Meta: "cmd",
    K.Key_Super_L: "cmd",
    K.Key_Super_R: "cmd_r",
    K.Key_Alt: "alt",
    K.Key_AltGr: "alt_gr",
    K.Key_CapsLock: "caps_lock",
    K.Key_Return: "enter",
    K.Key_Enter: "enter",
    K.Key_Escape: "esc",
    K.Key_Tab: "tab",
    K.Key_Backtab: "tab",
    K.Key_Backspace: "backspace",
    K.Key_Delete: "delete",
    K.Key_Insert: "insert",
    K.Key_Home: "home",
    K.Key_End: "end",
    K.Key_PageUp: "page_up",
    K.Key_PageDown: "page_down",
    K.Key_Left: "left",
    K.Key_Right: "right",
    K.Key_Up: "up",
    K.Key_Down: "down",
    K.Key_Space: "space",
    K.Key_Menu: "menu",
    K.Key_Print: "print_screen",
    K.Key_ScrollLock: "scroll_lock",
    K.Key_Pause: "pause",
    K.Key_NumLock: "num_lock",
    K.Key_VolumeUp: "media_volume_up",
    K.Key_VolumeDown: "media_volume_down",
    K.Key_VolumeMute: "media_volume_mute",
    K.Key_MediaPlay: "media_play_pause",
    K.Key_MediaTogglePlayPause: "media_play_pause",
    K.Key_MediaNext: "media_next",
    K.Key_MediaPrevious: "media_previous",
}
for _i in range(1, 21):
    SPECIAL[getattr(K, f"Key_F{_i}")] = f"f{_i}"

_SWAP = {"cmd": "ctrl", "cmd_r": "ctrl_r", "ctrl": "cmd", "ctrl_r": "cmd_r"}


def key_name(event: QKeyEvent) -> str | None:
    """Portable name of the key in ``event``, or None if we can't send it."""
    key = int(event.key())
    name = _BY_INT.get(key)
    if name:
        return name
    if 0x20 < key <= 0x7E:
        # Qt reports the unshifted key for letters (Key_A) even with Ctrl or
        # Cmd held, which text() would turn into control characters. Shift is
        # sent as its own key, so the remote side applies it.
        return chr(key).lower()
    text = event.text()
    if len(text) == 1 and text.isprintable():
        return text
    if 0xA0 <= key <= 0xFF:  # Latin-1 keys such as é on European layouts
        return chr(key).lower()
    return None


def swap_command_and_control(name: str) -> str:
    return _SWAP.get(name, name)


_BY_INT = {k.value: name for k, name in SPECIAL.items()}
