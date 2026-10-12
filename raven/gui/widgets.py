"""Small building blocks for the modern UI: switches, cards, icon buttons, toasts."""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
)

from raven.gui import icons
from raven.gui.theme import theme


def label(text: str = "", role: str | None = None, wrap: bool = False, selectable: bool = False) -> QLabel:
    lbl = QLabel(text)
    if role:
        lbl.setProperty("role", role)
    lbl.setWordWrap(wrap)
    if selectable:
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lbl


def set_role(widget: QWidget, role: str) -> None:
    """Change a dynamic style property and re-apply the stylesheet."""
    widget.setProperty("role", role)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def button(text: str, kind: str | None = None, icon_name: str | None = None, icon_role: str = "text"):
    from PySide6.QtWidgets import QPushButton

    b = QPushButton(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if kind:
        b.setProperty("kind", kind)
    if icon_name:
        role = "accent_text" if kind == "primary" else ("danger" if kind == "danger" else icon_role)
        icons.bind(b, icon_name, role, 16)
    return b


class Card(QFrame):
    def __init__(self, parent=None, hoverable: bool = False, margins=(18, 16, 18, 16), spacing: int = 12):
        super().__init__(parent)
        self.setObjectName("card")
        if hoverable:
            self.setProperty("hoverable", "true")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(*margins)
        self.body.setSpacing(spacing)


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("divider")
    return line


class IconButton(QToolButton):
    """Square icon button whose icon is painted in the current theme colour."""

    def __init__(self, name: str, tooltip: str = "", role: str = "muted", size: int = 34,
                 icon_size: int = 18, checkable: bool = False, fixed_color: str | None = None):
        super().__init__()
        self.setObjectName("iconButton")
        self.name = name
        self.role = role
        self.fixed_color = fixed_color
        self._icon_size = icon_size
        self.setToolTip(tooltip)
        self.setCheckable(checkable)
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def set_icon_name(self, name: str) -> None:
        self.name = name
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = theme.palette
        if not self.isEnabled():
            color = QColor(pal.muted)
            color.setAlphaF(0.45)
        elif self.isChecked():
            color = QColor(pal.accent_hover if pal.dark else pal.accent)
        elif self.fixed_color:
            color = QColor(self.fixed_color)
        else:
            color = pal.color(self.role) if not self.underMouse() else pal.color("text")
        s = self._icon_size
        rect = QRectF((self.width() - s) / 2, (self.height() - s) / 2, s, s)
        icons.paint(p, self.name, color, rect)


class ToggleSwitch(QAbstractButton):
    """An animated on/off switch."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(44, 24)
        self._offset = 0.0
        self._anim = QPropertyAnimation(self, b"offset", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(44, 24)

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, value: float) -> None:
        self._offset = value
        self.update()

    offset = Property(float, _get_offset, _set_offset)

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def setChecked(self, on: bool) -> None:  # noqa: N802
        super().setChecked(on)
        if not self.signalsBlocked():
            return
        self._anim.stop()
        self._set_offset(1.0 if on else 0.0)

    def showEvent(self, event) -> None:  # noqa: N802
        self._set_offset(1.0 if self.isChecked() else 0.0)
        super().showEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        pal = theme.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        off, on = QColor(pal.hover if pal.dark else "#d5d9e3"), QColor(pal.accent)
        t = self._offset
        track = QColor(
            round(off.red() + (on.red() - off.red()) * t),
            round(off.green() + (on.green() - off.green()) * t),
            round(off.blue() + (on.blue() - off.blue()) * t),
        )
        if not self.isEnabled():
            track.setAlphaF(0.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), self.height() / 2, self.height() / 2)
        d = self.height() - 6
        x = 3 + t * (self.width() - d - 6)
        p.setBrush(QColor("white"))
        p.drawEllipse(QRectF(x, 3, d, d))


class StatusDot(QWidget):
    def __init__(self, role: str = "muted", size: int = 8):
        super().__init__()
        self.role = role
        self.setFixedSize(size + 6, size + 6)
        self._size = size

    def set_role(self, role: str) -> None:
        self.role = role
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = theme.palette.color(self.role)
        halo = QColor(color)
        halo.setAlphaF(0.25)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(self.rect())
        p.setBrush(color)
        p.drawEllipse(QRectF(3, 3, self._size, self._size))


class Avatar(QFrame):
    """Rounded tile with an icon, used for devices and section headers."""

    def __init__(self, name: str, size: int = 42, icon_size: int = 22, role: str = "accent"):
        super().__init__()
        self.setObjectName("avatar")
        self.setFixedSize(size, size)
        self.name = name
        self.role = role
        self._icon_size = icon_size

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = theme.palette
        color = pal.accent_hover if (pal.dark and self.role == "accent") else getattr(pal, self.role)
        s = self._icon_size
        icons.paint(p, self.name, color, QRectF((self.width() - s) / 2, (self.height() - s) / 2, s, s))


class SettingRow(QWidget):
    """Icon, title, description on the left; a control (usually a switch) on the right."""

    def __init__(self, icon_name: str, title: str, description: str, control: QWidget):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 4, 0, 4)
        row.setSpacing(14)
        row.addWidget(Avatar(icon_name, 36, 18), 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(label(title, "h3"))
        self.description = label(description, "muted", wrap=True)
        text.addWidget(self.description)
        row.addLayout(text, 1)
        row.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)


class FlowLayout(QLayout):
    """Lays out children left-to-right, wrapping, like a responsive grid."""

    def __init__(self, parent=None, spacing: int = 16):
        super().__init__(parent)
        self._items: list = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def addWidget(self, widget: QWidget) -> None:  # noqa: N802
        self.addChildWidget(widget)
        self.addItem(QWidgetItem(widget))

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _layout(self, rect: QRect, apply: bool) -> int:
        if not self._items:
            return 0
        # Stretch items so each row fills the width exactly.
        base = max(item.sizeHint().width() for item in self._items)
        cols = max(1, (rect.width() + self._spacing) // (base + self._spacing))
        width = (rect.width() - self._spacing * (cols - 1)) // cols
        x, y, row_height = rect.x(), rect.y(), 0
        for i, item in enumerate(self._items):
            if i and i % cols == 0:
                x = rect.x()
                y += row_height + self._spacing
                row_height = 0
            h = item.sizeHint().height()
            if apply:
                item.setGeometry(QRect(QPoint(x, y), QSize(width, h)))
            x += width + self._spacing
            row_height = max(row_height, h)
        return y + row_height - rect.y()


class Toast(QFrame):
    """A short message that fades in at the bottom of a window, then away."""

    def __init__(self, host: QWidget):
        super().__init__(host)
        self.setObjectName("toast")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 11, 18, 11)
        layout.setSpacing(10)
        self.dot = StatusDot("success", 7)
        layout.addWidget(self.dot)
        self.text = QLabel()
        layout.addWidget(self.text)
        self.effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.effect)
        self.anim = QPropertyAnimation(self.effect, b"opacity", self)
        self.anim.setDuration(180)
        self.anim.finished.connect(self._finished)
        self.timer = QTimer(self, singleShot=True)
        self.timer.timeout.connect(self._fade_out)
        self.hide()

    def show_message(self, text: str, kind: str = "success", ms: int = 3500) -> None:
        self.text.setText(text)
        self.dot.set_role({"error": "danger", "info": "accent"}.get(kind, kind))
        start = self.effect.opacity() if self.isVisible() else 0.0
        self.adjustSize()
        self._place()
        self.raise_()
        self.show()
        self.anim.stop()
        self.anim.setStartValue(start)
        self.anim.setEndValue(1.0)
        self.anim.start()
        self.timer.start(ms)

    def _place(self) -> None:
        host = self.parentWidget()
        self.move((host.width() - self.width()) // 2, host.height() - self.height() - 24)

    def _fade_out(self) -> None:
        self.anim.stop()
        self.anim.setStartValue(self.effect.opacity())
        self.anim.setEndValue(0.0)
        self.anim.start()

    def _finished(self) -> None:
        if self.effect.opacity() == 0.0:
            self.hide()


def rounded_path(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def expanding(widget: QWidget) -> QWidget:
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    return widget
