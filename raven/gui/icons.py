"""Line icons drawn from inline SVG, tinted to the current theme."""

from __future__ import annotations

import weakref

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# 24x24 viewBox, stroked with currentColor.
_PATHS = {
    "monitor": '<rect x="2.5" y="3.5" width="19" height="13" rx="2"/><path d="M8 20.5h8M12 16.5v4"/>',
    "laptop": '<rect x="4" y="4.5" width="16" height="11" rx="1.5"/><path d="M2 19.5h20"/>',
    "devices": '<rect x="2.5" y="4" width="14" height="10" rx="1.5"/><path d="M6 18h7"/>'
               '<rect x="17.5" y="8" width="4" height="12" rx="1"/>',
    "home": '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V20h14V9.5"/>',
    "moon": '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
    "lock": '<rect x="4.5" y="10.5" width="15" height="10" rx="2"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/>',
    "restart": '<path d="M20 12a8 8 0 1 1-2.4-5.7"/><path d="M20 4v5h-5"/>',
    "power": '<path d="M12 3v8"/><path d="M6.3 6.8a8 8 0 1 0 11.4 0"/>',
    "zap": '<path d="M13 2.5 4.5 13.5H12l-1 8 8.5-11H12z"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "more": '<circle cx="5.5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="18.5" cy="12" r="1.2"/>',
    "edit": '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
    "trash": '<path d="M4 7h16M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13"/>',
    "copy": '<rect x="8.5" y="8.5" width="12" height="12" rx="2"/><path d="M15.5 8.5V5.5a2 2 0 0 0-2-2h-8a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h3"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "x": '<path d="M6 6l12 12M18 6 6 18"/>',
    "shield": '<path d="M12 3 4.5 6v6c0 4.5 3.2 7.8 7.5 9 4.3-1.2 7.5-4.5 7.5-9V6z"/><path d="M9 12l2 2 4-4"/>',
    "mouse": '<rect x="6.5" y="3" width="11" height="18" rx="5.5"/><path d="M12 7v3"/>',
    "keyboard": '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6.5 10h1M10 10h1M13.5 10h1M17 10h.5M7.5 14h9"/>',
    "wifi": '<path d="M2.5 9a14 14 0 0 1 19 0M5.5 12.5a9.5 9.5 0 0 1 13 0M8.8 15.8a4.8 4.8 0 0 1 6.4 0"/><circle cx="12" cy="19" r=".8"/>',
    "login": '<path d="M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4"/><path d="M10 16.5 14.5 12 10 7.5M14.5 12H3.5"/>',
    "eye": '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "eye-off": '<path d="M3 3l18 18"/><path d="M10.6 5.1A10.6 10.6 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4M6.6 6.6C3.8 8.4 2 12 2 12s3.6 7 10 7a10 10 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    "command": '<path d="M9 6v12M15 6v12M6 9h12M6 15h12"/><path d="M9 6a3 3 0 1 0-3 3M15 6a3 3 0 1 1 3 3M9 18a3 3 0 1 1-3-3M15 18a3 3 0 1 0 3-3"/>',
    "clipboard": '<rect x="5" y="4.5" width="14" height="17" rx="2"/><path d="M9 4.5V3h6v1.5M9 11h6M9 15h4"/>',
    "maximize": '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
    "minimize": '<path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"/>',
    "sliders": '<path d="M4 6h10M18 6h2M4 12h3M11 12h9M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    "activity": '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M16 7l3 3"/>',
    "tag": '<path d="M3 12V4h8l10 10-8 8z"/><circle cx="7.5" cy="8.5" r="1.2"/>',
    "plug": '<path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.3-4.3"/>',
    "alert": '<path d="M12 3 2 20h20z"/><path d="M12 10v4M12 17v.5"/>',
}

_FILLED = {"more"}


def svg(name: str, color: str, stroke: float = 1.8) -> bytes:
    fill = color if name in _FILLED else "none"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="{fill}" stroke="{color}" '
        f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{_PATHS[name]}</svg>'
    ).encode()


def pixmap(name: str, color: QColor | str, size: int = 18, ratio: float = 2.0) -> QPixmap:
    color = QColor(color).name()
    renderer = QSvgRenderer(QByteArray(svg(name, color)))
    pix = QPixmap(int(size * ratio), int(size * ratio))
    pix.setDevicePixelRatio(ratio)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(p, QRectF(0, 0, size, size))
    p.end()
    return pix


def icon(name: str, color: QColor | str, size: int = 18) -> QIcon:
    return QIcon(pixmap(name, color, size))


def paint(painter: QPainter, name: str, color: QColor | str, rect: QRectF) -> None:
    QSvgRenderer(QByteArray(svg(name, QColor(color).name()))).render(painter, rect)


def bind(widget, name: str, role: str = "text", size: int = 18) -> None:
    """Give a button/action an icon that follows theme changes."""
    from raven.gui.theme import theme

    ref = weakref.ref(widget)

    def refresh() -> None:
        w = ref()
        if w is None:
            return
        try:
            w.setIcon(icon(name, theme.palette.color(role), size))
            if hasattr(w, "setIconSize"):
                w.setIconSize(QSize(size, size))
        except RuntimeError:  # C++ object already deleted
            pass

    refresh()
    theme.changed.connect(refresh)
