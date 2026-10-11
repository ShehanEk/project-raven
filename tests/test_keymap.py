import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtCore = pytest.importorskip("PySide6.QtCore")
QtGui = pytest.importorskip("PySide6.QtGui")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from raven.gui.keymap import key_name, swap_command_and_control  # noqa: E402
from raven.platform.inputs import is_valid_key  # noqa: E402

Qt = QtCore.Qt


@pytest.fixture(scope="module", autouse=True)
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def press(key, text="", mods=Qt.KeyboardModifier.NoModifier):
    return QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, key, mods, text)


@pytest.mark.parametrize("event_args, expected", [
    ((Qt.Key.Key_A, "A", Qt.KeyboardModifier.ShiftModifier), "a"),
    ((Qt.Key.Key_C, "\x03", Qt.KeyboardModifier.ControlModifier), "c"),
    ((Qt.Key.Key_Return, "\r"), "enter"),
    ((Qt.Key.Key_F5,), "f5"),
    ((Qt.Key.Key_Exclam, "!"), "!"),
    ((Qt.Key.Key_Control,), "ctrl"),
    ((Qt.Key.Key_Meta,), "cmd"),
    ((Qt.Key.Key_Eacute, "é"), "é"),
    ((Qt.Key.Key_Space, " "), "space"),
    ((Qt.Key.Key_Tab, "\t"), "tab"),
])
def test_key_names(event_args, expected):
    name = key_name(press(*event_args))
    assert name == expected
    assert is_valid_key(name)


def test_swap():
    assert swap_command_and_control("cmd") == "ctrl"
    assert swap_command_and_control("ctrl") == "cmd"
    assert swap_command_and_control("a") == "a"
