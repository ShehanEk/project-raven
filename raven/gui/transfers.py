"""Transfer progress rows, shared by the Files window and the viewer."""

from __future__ import annotations

import concurrent.futures
import dataclasses
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QHBoxLayout, QProgressBar, QVBoxLayout, QWidget

from raven.client.files import FileClient, Progress
from raven.client.session import RavenError
from raven.gui.async_bridge import AsyncRunner
from raven.gui.widgets import Avatar, IconButton, label, plain_tooltip


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class _RowSignals(QObject):
    progress = Signal(object)


class TransferRow(QFrame):
    finished = Signal(bool)  # ok

    def __init__(self, direction: str, title: str):
        super().__init__()
        self.setObjectName("transferRow")
        self.direction = direction
        self.future: concurrent.futures.Future | None = None
        self.result_paths: list = []
        self.signals = _RowSignals()
        self.signals.progress.connect(self._on_progress)

        h = QHBoxLayout(self)
        h.setContentsMargins(12, 10, 10, 10)
        h.setSpacing(12)
        h.addWidget(Avatar("upload" if direction == "upload" else "download", 34, 17))
        v = QVBoxLayout()
        v.setSpacing(5)
        top = QHBoxLayout()
        self.title = label(title, "h3")
        top.addWidget(self.title, 1)
        self.detail = label("Preparing…", "muted")
        top.addWidget(self.detail)
        v.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        v.addWidget(self.bar)
        h.addLayout(v, 1)
        self.action = IconButton("x", "Cancel", size=30, icon_size=16)
        self.action.clicked.connect(self._action_clicked)
        h.addWidget(self.action)
        self.done = False

    def report(self, progress: Progress) -> None:
        """Called on the network thread."""
        try:
            self.signals.progress.emit(dataclasses.replace(progress))
        except RuntimeError:
            pass  # row already deleted

    def _on_progress(self, p: Progress) -> None:
        if self.done:
            return
        self.bar.setValue(int(p.fraction * 1000))
        text = f"{human_size(p.done_bytes)} of {human_size(p.total_bytes)}"
        if p.rate:
            text += f" · {human_size(p.rate)}/s"
        if p.total_files > 1:
            text = f"{p.done_files}/{p.total_files} files · " + text
        self.detail.setText(text)

    def complete(self, paths: list) -> None:
        self.done = True
        self.result_paths = paths
        self.bar.setValue(1000)
        self.detail.setText("Done")
        if self.direction == "download" and paths:
            self.action.set_icon_name("folder-open")
            self.action.setToolTip("Show in folder")
        else:
            self.action.set_icon_name("check")
            self.action.setToolTip("Dismiss")
        self.finished.emit(True)

    def fail(self, message: str) -> None:
        self.done = True
        self.detail.setText(message)
        self.detail.setProperty("role", "error")
        self.detail.style().unpolish(self.detail)
        self.detail.style().polish(self.detail)
        self.detail.setToolTip(plain_tooltip(message))
        self.action.set_icon_name("x")
        self.action.setToolTip("Dismiss")
        self.finished.emit(False)

    def _action_clicked(self) -> None:
        if not self.done:
            if self.future:
                self.future.cancel()
            self.fail("Cancelled")
            return
        if self.direction == "download" and self.result_paths and self.detail.text() == "Done":
            folder = Path(self.result_paths[0]).parent
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
            return
        self.deleteLater()

    def cancel(self) -> None:
        if not self.done and self.future:
            self.future.cancel()


class TransferList(QWidget):
    """A vertical stack of transfer rows."""

    job_finished = Signal(str, bool)  # direction, ok
    changed = Signal()

    def __init__(self, runner: AsyncRunner):
        super().__init__()
        self.runner = runner
        self.rows_layout = QVBoxLayout(self)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(8)

    def rows(self) -> list[TransferRow]:
        return [self.rows_layout.itemAt(i).widget() for i in range(self.rows_layout.count())
                if isinstance(self.rows_layout.itemAt(i).widget(), TransferRow)]

    def active(self) -> list[TransferRow]:
        return [r for r in self.rows() if not r.done]

    def _add(self, row: TransferRow) -> None:
        self.rows_layout.insertWidget(0, row)
        row.destroyed.connect(self._row_destroyed)
        self.changed.emit()

    def _row_destroyed(self, *_) -> None:
        # Rows are deleted later than the list when the window closes.
        try:
            self.changed.emit()
        except (RuntimeError, TypeError):
            pass

    def upload(self, client: FileClient, sources: list[Path], remote_dir: str, dest_name: str) -> TransferRow:
        what = sources[0].name if len(sources) == 1 else f"{len(sources)} items"
        row = TransferRow("upload", f"{what} → {dest_name}")
        self._add(row)
        self._run(row, client.upload(sources, remote_dir, row.report), "upload")
        return row

    def download(self, client: FileClient, items: list[dict], local_dir: Path) -> TransferRow:
        what = items[0]["name"] if len(items) == 1 else f"{len(items)} items"
        row = TransferRow("download", f"{what} → {local_dir.name or local_dir}")
        self._add(row)
        self._run(row, client.download(items, local_dir, row.report), "download")
        return row

    def _run(self, row: TransferRow, coro, direction: str) -> None:
        def done(paths):
            row.complete(paths)
            self.job_finished.emit(direction, True)

        def failed(exc):
            if row.done:  # cancelled by the user
                return
            row.fail(str(exc) if isinstance(exc, (RavenError, OSError)) else f"Failed: {exc}")
            self.job_finished.emit(direction, False)

        row.future = self.runner.run(coro, done, failed)

    def cancel_all(self) -> None:
        for row in self.active():
            row.cancel()


def drop_hint(text: str) -> QFrame:
    frame = QFrame()
    frame.setObjectName("dropHint")
    v = QVBoxLayout(frame)
    v.setContentsMargins(24, 24, 24, 24)
    v.setAlignment(Qt.AlignmentFlag.AlignCenter)
    v.addWidget(Avatar("upload", 48, 24), 0, Qt.AlignmentFlag.AlignHCenter)
    hint = label(text, "h3")
    hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
    v.addWidget(hint)
    return frame

