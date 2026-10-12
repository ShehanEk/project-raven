"""Colours, the application stylesheet, and light/dark switching."""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory


@dataclass(frozen=True)
class Palette:
    dark: bool
    bg: str
    sidebar: str
    surface: str
    surface_alt: str
    hover: str
    border: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    accent_text: str
    success: str
    warning: str
    danger: str

    def color(self, role: str) -> QColor:
        return QColor(getattr(self, role))

    def soft(self, role: str, alpha: float = 0.14) -> str:
        c = QColor(getattr(self, role))
        return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


DARK = Palette(
    dark=True,
    bg="#101217",
    sidebar="#0b0d11",
    surface="#171a21",
    surface_alt="#1f232c",
    hover="#262b36",
    border="#2a2f3b",
    text="#e8eaf0",
    muted="#8d95a8",
    accent="#7c6cff",
    accent_hover="#8f82ff",
    accent_text="#ffffff",
    success="#34d399",
    warning="#fbbf24",
    danger="#f87171",
)

LIGHT = Palette(
    dark=False,
    bg="#f5f6fa",
    sidebar="#ffffff",
    surface="#ffffff",
    surface_alt="#f1f3f8",
    hover="#e8ebf2",
    border="#e2e5ee",
    text="#121520",
    muted="#5f677a",
    accent="#5b4cf0",
    accent_hover="#4a3be0",
    accent_text="#ffffff",
    success="#059669",
    warning="#d97706",
    danger="#dc2626",
)


def _check_icon() -> str:
    """Write the checkbox tick to a file, since stylesheets can only load images by path."""
    from raven.gui import icons

    path = Path(tempfile.gettempdir()) / "raven-check.svg"
    try:
        path.write_bytes(icons.svg("check", "#ffffff", 3.0))
    except OSError:
        return ""
    return path.as_posix()


def stylesheet(p: Palette) -> str:
    check_icon = _check_icon()
    return f"""
* {{ color: {p.text}; font-size: 13px; outline: 0; }}
QMainWindow, QDialog, QMessageBox {{ background: {p.bg}; }}
QWidget#page {{ background: {p.bg}; }}

QWidget#sidebar {{ background: {p.sidebar}; border-right: 1px solid {p.border}; }}
QPushButton#navButton {{
    text-align: left; padding: 9px 12px; border: none; border-radius: 9px;
    background: transparent; color: {p.muted}; font-weight: 600;
}}
QPushButton#navButton:hover {{ background: {p.surface_alt}; color: {p.text}; }}
QPushButton#navButton:checked {{ background: {p.soft('accent')}; color: {p.accent if not p.dark else p.accent_hover}; }}
QFrame#statusChip {{ background: {p.surface_alt}; border: 1px solid {p.border}; border-radius: 10px; }}
QFrame#statusChip:hover {{ border-color: {p.muted}; }}

QFrame#card {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 14px; }}
QFrame#card[hoverable="true"]:hover {{ border-color: {p.soft('accent', 0.6)}; }}
QFrame#divider {{ background: {p.border}; max-height: 1px; min-height: 1px; border: none; }}
QFrame#chip {{ background: {p.surface_alt}; border: 1px solid {p.border}; border-radius: 9px; }}
QFrame#avatar {{ background: {p.soft('accent')}; border-radius: 12px; }}
QFrame#emptyState {{ background: {p.surface}; border: 1px dashed {p.border}; border-radius: 16px; }}

QLabel {{ background: transparent; }}
QLabel[role="title"] {{ font-size: 24px; font-weight: 700; }}
QLabel[role="subtitle"] {{ color: {p.muted}; font-size: 13px; }}
QLabel[role="h2"] {{ font-size: 15px; font-weight: 650; }}
QLabel[role="h3"] {{ font-size: 13px; font-weight: 600; }}
QLabel[role="muted"] {{ color: {p.muted}; font-size: 12px; }}
QLabel[role="fingerprint"] {{ font-family: "SF Mono", "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace; font-size: 16px; font-weight: 700; letter-spacing: 0.5px; }}
QLabel[role="mono"] {{ font-family: "SF Mono", "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace; font-size: 12px; }}
QLabel[role="brand"] {{ font-size: 17px; font-weight: 800; letter-spacing: 0.5px; }}
QLabel[role="tag"] {{ color: {p.muted}; background: {p.surface_alt}; border-radius: 6px; padding: 2px 7px; font-size: 11px; font-weight: 600; }}
QLabel[role="error"] {{ color: {p.danger}; font-size: 12px; }}

QPushButton {{
    background: {p.surface_alt}; border: 1px solid {p.border}; border-radius: 9px;
    padding: 7px 14px; font-weight: 600;
}}
QPushButton:hover {{ background: {p.hover}; }}
QPushButton:pressed {{ background: {p.border}; }}
QPushButton:disabled {{ color: {p.muted}; background: {p.surface_alt}; border-color: {p.border}; }}
QPushButton[kind="primary"] {{ background: {p.accent}; color: {p.accent_text}; border: 1px solid {p.accent}; }}
QPushButton[kind="primary"]:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
QPushButton[kind="primary"]:disabled {{ background: {p.soft('accent', 0.35)}; border-color: transparent; color: {p.accent_text}; }}
QPushButton[kind="danger"] {{ color: {p.danger}; }}
QPushButton[kind="danger"]:hover {{ background: {p.soft('danger')}; border-color: {p.soft('danger', 0.5)}; }}
QPushButton[kind="ghost"] {{ background: transparent; border-color: transparent; }}
QPushButton[kind="ghost"]:hover {{ background: {p.surface_alt}; }}
QPushButton::menu-indicator {{ image: none; width: 0; }}

QToolButton#iconButton {{ background: transparent; border: 1px solid transparent; border-radius: 9px; }}
QToolButton#iconButton:hover {{ background: {p.hover}; }}
QToolButton#iconButton:checked {{ background: {p.soft('accent', 0.22)}; border-color: {p.soft('accent', 0.4)}; }}
QToolButton#iconButton::menu-indicator {{ image: none; width: 0; }}

QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {{
    background: {p.surface_alt}; border: 1px solid {p.border}; border-radius: 9px;
    padding: 7px 10px; selection-background-color: {p.accent}; selection-color: {p.accent_text};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {p.accent}; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {p.surface}; border: 1px solid {p.border}; border-radius: 8px; padding: 4px;
    selection-background-color: {p.surface_alt}; selection-color: {p.text};
}}

QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p.border}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {p.muted}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; background: none; }}
QScrollBar:horizontal {{ height: 0; }}

QMenu {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 10px; padding: 5px; }}
QMenu::item {{ padding: 7px 26px 7px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {p.surface_alt}; }}
QMenu::item:disabled {{ color: {p.muted}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 5px 6px; }}
QMenu::icon {{ padding-left: 10px; }}

QToolTip {{ background: {p.surface}; color: {p.text}; border: 1px solid {p.border}; padding: 5px 8px; border-radius: 6px; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid {p.border}; background: {p.surface_alt}; }}
QCheckBox::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; image: url("{check_icon}"); }}

QFrame#toast {{ background: {'#f4f5f9' if p.dark else '#1b1e27'}; border-radius: 12px; }}
QFrame#toast QLabel {{ color: {'#11131a' if p.dark else '#f4f5f9'}; font-weight: 600; }}

QWidget#viewerBar {{ background: #0b0d11; border-bottom: 1px solid #23272f; }}
QWidget#viewerBar QLabel {{ color: #e8eaf0; }}
QWidget#viewerBar QLabel[role="muted"] {{ color: #8d95a8; }}
QWidget#viewerBar QToolButton#iconButton:hover {{ background: #232833; }}
QWidget#viewerBar QComboBox {{ background: #171a21; border-color: #2a2f3b; color: #e8eaf0; padding: 4px 8px; }}
QTreeWidget {{
    background: {p.surface}; border: 1px solid {p.border}; border-radius: 12px; padding: 4px;
    alternate-background-color: {p.surface};
}}
QTreeWidget::item {{ height: 32px; border: none; padding-left: 4px; }}
QTreeWidget::item:hover {{ background: {p.surface_alt}; }}
QTreeWidget::item:selected {{ background: {p.soft('accent', 0.22)}; color: {p.text}; }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{
    background: {p.surface}; color: {p.muted}; border: none; border-bottom: 1px solid {p.border};
    padding: 6px 8px; font-size: 12px; font-weight: 600;
}}
QListWidget#placesList {{ background: transparent; border: none; }}
QListWidget#placesList::item {{ height: 34px; padding-left: 8px; border-radius: 8px; color: {p.muted}; }}
QListWidget#placesList::item:hover {{ background: {p.surface_alt}; color: {p.text}; }}
QListWidget#placesList::item:selected {{ background: {p.soft('accent')}; color: {p.accent_hover if p.dark else p.accent}; }}
QProgressBar {{ background: {p.surface_alt}; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 3px; }}
QFrame#transferRow {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 12px; }}
QFrame#dropHint {{ background: {p.soft('accent', 0.12)}; border: 2px dashed {p.accent}; border-radius: 16px; }}
QFrame#banner {{ background: {p.soft('danger', 0.14)}; border: 1px solid {p.soft('danger', 0.4)}; border-radius: 10px; }}
QFrame#overlayCard {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 16px; }}
"""


class ThemeManager(QObject):
    """Applies the palette and follows the OS light/dark setting."""

    changed = Signal()

    def __init__(self):
        super().__init__()
        self.palette: Palette = DARK

    def install(self, app: QApplication) -> None:
        app.setStyle(QStyleFactory.create("Fusion"))
        font = app.font()
        if sys.platform == "win32":
            font = QFont("Segoe UI Variable Text")
            if not font.exactMatch():
                font = QFont("Segoe UI")
        font.setPointSizeF(max(font.pointSizeF(), 9.5))
        app.setFont(font)
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(lambda _: self.apply(app))
        self.apply(app)

    def apply(self, app: QApplication) -> None:
        self.palette = DARK if self._system_is_dark() else LIGHT
        p = self.palette
        pal = QPalette()
        for role, value in (
            (QPalette.ColorRole.Window, p.bg),
            (QPalette.ColorRole.WindowText, p.text),
            (QPalette.ColorRole.Base, p.surface_alt),
            (QPalette.ColorRole.AlternateBase, p.surface),
            (QPalette.ColorRole.Text, p.text),
            (QPalette.ColorRole.Button, p.surface_alt),
            (QPalette.ColorRole.ButtonText, p.text),
            (QPalette.ColorRole.Highlight, p.accent),
            (QPalette.ColorRole.HighlightedText, p.accent_text),
            (QPalette.ColorRole.ToolTipBase, p.surface),
            (QPalette.ColorRole.ToolTipText, p.text),
            (QPalette.ColorRole.PlaceholderText, p.muted),
            (QPalette.ColorRole.Link, p.accent),
        ):
            pal.setColor(role, QColor(value))
        pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(p.muted))
        pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(p.muted))
        app.setPalette(pal)
        app.setStyleSheet(stylesheet(p))
        self.changed.emit()

    @staticmethod
    def _system_is_dark() -> bool:
        forced = os.environ.get("RAVEN_THEME", "").lower()
        if forced in ("light", "dark"):
            return forced == "dark"
        hints = QGuiApplication.styleHints()
        scheme = getattr(hints, "colorScheme", lambda: None)()
        if scheme == Qt.ColorScheme.Light:
            return False
        if scheme == Qt.ColorScheme.Dark:
            return True
        return True  # unknown: dark suits a remote-desktop app


theme = ThemeManager()
