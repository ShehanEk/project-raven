"""Inject mouse and keyboard events into the local session.

Keys travel over the wire as portable names: either a single character
("a", "1", "/") or one of the pynput ``Key`` member names in
:data:`SPECIAL_KEYS` ("enter", "ctrl", "cmd", "f5", ...).
"""

from __future__ import annotations

import logging

from raven.platform.capture import Monitor

log = logging.getLogger(__name__)

SPECIAL_KEYS = frozenset(
    """
    alt alt_l alt_r alt_gr backspace caps_lock cmd cmd_l cmd_r ctrl ctrl_l ctrl_r
    delete down end enter esc home left page_down page_up right shift shift_l
    shift_r space tab up insert menu num_lock pause print_screen scroll_lock
    media_play_pause media_volume_mute media_volume_down media_volume_up
    media_previous media_next
    """.split()
) | {f"f{i}" for i in range(1, 21)}

MOUSE_BUTTONS = ("left", "right", "middle")


def is_valid_key(name: str) -> bool:
    return (len(name) == 1 and name.isprintable()) or name in SPECIAL_KEYS


class InputInjector:
    """Thin wrapper over pynput that tracks what it holds down.

    pynput is imported lazily because importing it fails on machines without
    a display (CI, SSH sessions) and the rest of the app should still work.
    """

    def __init__(self):
        self._mouse = None
        self._keyboard = None
        self._held_keys: dict[str, object] = {}
        self._held_buttons: set[str] = set()
        self._scroll_remainder = [0.0, 0.0]

    def _controllers(self):
        if self._mouse is None:
            from pynput import keyboard, mouse

            self._mouse = mouse.Controller()
            self._keyboard = keyboard.Controller()
        return self._mouse, self._keyboard

    def cursor_position(self) -> tuple[float, float] | None:
        try:
            mouse, _ = self._controllers()
            return mouse.position
        except Exception:  # noqa: BLE001 - cursor overlay is best effort
            return None

    def move(self, monitor: Monitor, nx: float, ny: float) -> None:
        mouse, _ = self._controllers()
        mouse.position = monitor.denormalize(nx, ny)

    def button(self, name: str, down: bool) -> None:
        if name not in MOUSE_BUTTONS:
            return
        from pynput.mouse import Button

        mouse, _ = self._controllers()
        button = getattr(Button, name)
        if down:
            mouse.press(button)
            self._held_buttons.add(name)
        else:
            mouse.release(button)
            self._held_buttons.discard(name)

    def scroll(self, dx: float, dy: float) -> None:
        # pynput wants whole steps; carry fractions (trackpads) forward.
        self._scroll_remainder[0] += dx
        self._scroll_remainder[1] += dy
        steps_x = int(self._scroll_remainder[0])
        steps_y = int(self._scroll_remainder[1])
        self._scroll_remainder[0] -= steps_x
        self._scroll_remainder[1] -= steps_y
        if steps_x or steps_y:
            mouse, _ = self._controllers()
            mouse.scroll(steps_x, steps_y)

    def key(self, name: str, down: bool) -> None:
        if not is_valid_key(name):
            return
        _, keyboard = self._controllers()
        if down:
            key = self._resolve(name)
            if key is None:
                return
            keyboard.press(key)
            self._held_keys[name] = key
        else:
            key = self._held_keys.pop(name, None) or self._resolve(name)
            if key is not None:
                keyboard.release(key)

    def type_text(self, text: str) -> None:
        _, keyboard = self._controllers()
        keyboard.type(text)

    def release_all(self) -> None:
        """Let go of everything so no key or button stays stuck."""
        if self._mouse is None:
            return
        for name in list(self._held_keys):
            try:
                self.key(name, False)
            except Exception:  # noqa: BLE001
                log.exception("failed to release key %s", name)
        for name in list(self._held_buttons):
            try:
                self.button(name, False)
            except Exception:  # noqa: BLE001
                log.exception("failed to release button %s", name)

    @staticmethod
    def _resolve(name: str):
        from pynput.keyboard import Key, KeyCode

        if len(name) == 1:
            return KeyCode.from_char(name)
        # Not every key exists on every OS (e.g. no Insert key on macOS).
        return getattr(Key, name, None)
