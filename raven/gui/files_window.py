"""Browse a remote computer's files and copy them in either direction."""

from __future__ import annotations

import datetime
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QScrollArea,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from raven.client.files import FileClient
from raven.client.session import RavenError, RemoteSession
from raven.config import RemoteHost
from raven.gui import icons
from raven.gui.async_bridge import AsyncRunner
from raven.gui.theme import theme
from raven.gui.transfers import TransferList, drop_hint, human_size
from raven.gui.widgets import IconButton, StatusDot, Toast, button, confirm, label, plain_tooltip

_ENTRY = Qt.ItemDataRole.UserRole


def local_downloads() -> Path:
    path = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    return Path(path) if path else Path.home()


class _FileTree(QTreeWidget):
    """The file list; accepts local files dropped onto it."""

    files_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setSortingEnabled(True)
        self.setHeaderLabels(["Name", "Size", "Modified"])
        header = self.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        self.drop_overlay = drop_hint("Drop to upload here")
        self.drop_overlay.setParent(self.viewport())
        self.drop_overlay.hide()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls() and all(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()
            self.drop_overlay.setGeometry(self.viewport().rect().adjusted(12, 12, -12, -12))
            self.drop_overlay.show()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self.drop_overlay.hide()

    def dropEvent(self, event) -> None:  # noqa: N802
        self.drop_overlay.hide()
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)


class _Item(QTreeWidgetItem):
    """Keeps folders first and sorts sizes/dates by value, not text."""

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        a, b = self.data(0, _ENTRY), other.data(0, _ENTRY)
        if a["dir"] != b["dir"]:
            ascending = self.treeWidget().header().sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
            return a["dir"] if ascending else b["dir"]
        column = self.treeWidget().sortColumn()
        if column == 1:
            return a["size"] < b["size"]
        if column == 2:
            return a["mtime"] < b["mtime"]
        return a["name"].lower() < b["name"].lower()


class FilesWindow(QMainWindow):
    def __init__(self, runner: AsyncRunner, session: RemoteSession, remote: RemoteHost, owns_session: bool):
        super().__init__()
        self.runner = runner
        self.session = session
        self.client = FileClient(session)
        self.remote = remote
        self.owns_session = owns_session
        self.path = ""
        self.parent_path = ""
        self.history: list[str] = []
        self.entries: list[dict] = []
        self.show_hidden = False
        self.download_dir = local_downloads()
        self.last_download_dir = self.download_dir
        self.connected = True
        self.setWindowTitle(f"Files — {remote.name or remote.address}")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(980, 640)
        self.setMinimumSize(720, 460)

        root = QWidget()
        root.setObjectName("page")
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(self._build_sidebar())
        h.addWidget(self._build_main(), 1)
        self.setCentralWidget(root)
        self.toast = Toast(self.centralWidget())

        QShortcut(QKeySequence.StandardKey.Refresh, self, self.refresh)
        QShortcut(QKeySequence("Backspace"), self.tree, self.go_up)

        self._closed_cb = lambda reason: self._emit_closed(reason)
        session.add_close_listener(self._closed_cb)
        self.runner.run(self.client.places(), self._on_places, self._error)

    # ---------------------------------------------------------- layout

    def _build_sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("sidebar")
        side.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        side.setFixedWidth(210)
        v = QVBoxLayout(side)
        v.setContentsMargins(14, 20, 14, 14)
        v.setSpacing(6)
        head = QHBoxLayout()
        self.state_dot = StatusDot("success", 7)
        head.addWidget(self.state_dot)
        name = label(self.remote.name or self.remote.address, "h3")
        head.addWidget(name, 1)
        v.addLayout(head)
        v.addWidget(label(self.remote.address, "muted"))
        v.addSpacing(14)
        v.addWidget(label("PLACES", "muted"))
        self.places_list = QListWidget()
        self.places_list.setObjectName("placesList")
        self.places_list.itemClicked.connect(lambda item: self.navigate(item.data(_ENTRY)))
        v.addWidget(self.places_list, 1)
        return side

    def _build_main(self) -> QWidget:
        main = QWidget()
        v = QVBoxLayout(main)
        v.setContentsMargins(20, 18, 20, 16)
        v.setSpacing(12)

        nav = QHBoxLayout()
        nav.setSpacing(4)
        self.back_btn = IconButton("arrow-left", "Back")
        self.back_btn.clicked.connect(self.go_back)
        self.up_btn = IconButton("arrow-up", "Enclosing folder")
        self.up_btn.clicked.connect(self.go_up)
        refresh = IconButton("restart", "Refresh")
        refresh.clicked.connect(self.refresh)
        for b in (self.back_btn, self.up_btn, refresh):
            nav.addWidget(b)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Folder path on the other computer")
        self.path_edit.returnPressed.connect(lambda: self.navigate(self.path_edit.text().strip()))
        nav.addWidget(self.path_edit, 1)
        self.hidden_btn = IconButton("eye-off", "Show hidden files", checkable=True)
        self.hidden_btn.toggled.connect(self._toggle_hidden)
        nav.addWidget(self.hidden_btn)
        v.addLayout(nav)

        self.banner = QFrame()
        self.banner.setObjectName("banner")
        bh = QHBoxLayout(self.banner)
        bh.setContentsMargins(12, 8, 12, 8)
        self.banner_text = label("", wrap=True)
        bh.addWidget(self.banner_text, 1)
        self.banner.hide()
        v.addWidget(self.banner)

        self.tree = _FileTree()
        self.tree.itemDoubleClicked.connect(self._activated)
        self.tree.itemSelectionChanged.connect(self._update_actions)
        self.tree.files_dropped.connect(lambda paths: self.upload([Path(p) for p in paths]))
        v.addWidget(self.tree, 1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.mkdir_btn = button("New folder", None, "folder-plus")
        self.mkdir_btn.clicked.connect(self.new_folder)
        self.upload_btn = button("Upload files…", None, "upload")
        self.upload_btn.clicked.connect(self.pick_files)
        self.upload_dir_btn = button("Upload folder…", None, "folder")
        self.upload_dir_btn.clicked.connect(self.pick_folder)
        self.count_label = label("", "muted")
        self.download_btn = button("Download", "primary", "download")
        self.download_btn.clicked.connect(self.download_selected)
        for w in (self.mkdir_btn, self.upload_btn, self.upload_dir_btn):
            actions.addWidget(w)
        actions.addStretch()
        actions.addWidget(self.count_label)
        actions.addWidget(self.download_btn)
        v.addLayout(actions)

        self.transfers = TransferList(self.runner)
        self.transfers.job_finished.connect(self._job_finished)
        self.transfers.changed.connect(self._update_transfers_area)
        self.transfers_area = QScrollArea()
        self.transfers_area.setWidgetResizable(True)
        self.transfers_area.setWidget(self.transfers)
        self.transfers_area.hide()
        v.addWidget(self.transfers_area)
        self._update_actions()
        return main

    # ---------------------------------------------------------- browsing

    def _on_places(self, reply: dict) -> None:
        self.places_list.clear()
        for place in reply.get("places", []):
            icon_name = {"Home": "home", "Desktop": "desktop", "Downloads": "download"}.get(place["name"], "folder")
            self._add_place(place["name"], place["path"], icon_name)
        for root in reply.get("roots", []):
            self._add_place(root if root != "/" else "Computer", root, "drive")
        self.remote_downloads = reply.get("downloads", "")
        if reply.get("places"):
            self.navigate(reply["places"][0]["path"], remember=False)

    def _add_place(self, name: str, path: str, icon_name: str) -> None:
        item = QListWidgetItem(name)
        item.setData(_ENTRY, path)
        item.setIcon(icons.icon(icon_name, theme.palette.muted, 18))
        item.setToolTip(plain_tooltip(path))
        self.places_list.addItem(item)

    def navigate(self, path: str, remember: bool = True) -> None:
        if not path or not self.connected:
            return
        target = path

        def shown(listing: dict) -> None:
            if remember and self.path and self.path != listing["path"]:
                self.history.append(self.path)
            self._show_listing(listing)

        self.runner.run(self.client.list(target), shown, self._error)

    def refresh(self) -> None:
        if self.path:
            self.runner.run(self.client.list(self.path), self._show_listing, self._error)

    def go_up(self) -> None:
        if self.parent_path:
            self.navigate(self.parent_path)

    def go_back(self) -> None:
        if self.history:
            self.navigate(self.history.pop(), remember=False)

    def _show_listing(self, listing: dict) -> None:
        self.path = listing["path"]
        self.parent_path = listing.get("parent", "")
        self.entries = listing["entries"]
        self.path_edit.setText(self.path)
        self.up_btn.setEnabled(bool(self.parent_path))
        self.back_btn.setEnabled(bool(self.history))
        for i in range(self.places_list.count()):
            item = self.places_list.item(i)
            item.setSelected(item.data(_ENTRY) == self.path)
        self._fill_tree()
        if listing.get("truncated"):
            self.toast.show_message("Showing the first 5,000 items only", "info")

    def _fill_tree(self) -> None:
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        pal = theme.palette
        folder_icon = icons.icon("folder", pal.accent_hover if pal.dark else pal.accent, 18)
        file_icon = icons.icon("file", pal.muted, 18)
        shown = 0
        for entry in self.entries:
            if entry["hidden"] and not self.show_hidden:
                continue
            modified = datetime.datetime.fromtimestamp(entry["mtime"]).strftime("%d %b %Y, %H:%M") \
                if entry["mtime"] else ""
            item = _Item([entry["name"], "" if entry["dir"] else human_size(entry["size"]), modified])
            item.setData(0, _ENTRY, entry)
            item.setIcon(0, folder_icon if entry["dir"] else file_icon)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.tree.addTopLevelItem(item)
            shown += 1
        self.tree.setSortingEnabled(True)
        self.count_label.setText(f"{shown} items")
        self._update_actions()

    def _toggle_hidden(self, on: bool) -> None:
        self.show_hidden = on
        self.hidden_btn.set_icon_name("eye" if on else "eye-off")
        self._fill_tree()

    def _activated(self, item: QTreeWidgetItem) -> None:
        entry = item.data(0, _ENTRY)
        if entry["dir"]:
            self.navigate(entry["path"])
        else:
            self._start_download([entry], self.download_dir)

    def _selected(self) -> list[dict]:
        return [item.data(0, _ENTRY) for item in self.tree.selectedItems()]

    def _update_actions(self) -> None:
        selected = self._selected()
        enabled = self.connected and bool(self.path)
        self.download_btn.setEnabled(enabled and bool(selected))
        self.download_btn.setText(f"Download ({len(selected)})" if len(selected) > 1 else "Download")
        for b in (self.mkdir_btn, self.upload_btn, self.upload_dir_btn):
            b.setEnabled(enabled)

    # ---------------------------------------------------------- actions

    def new_folder(self) -> None:
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:", text="New folder")
        if ok and name.strip():
            self.runner.run(self.client.mkdir(self.path, name.strip()), lambda _: self.refresh(), self._error)

    def pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Upload files", str(Path.home()))
        if paths:
            self.upload([Path(p) for p in paths])

    def pick_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Upload folder", str(Path.home()))
        if path:
            self.upload([Path(path)])

    def upload(self, sources: list[Path]) -> None:
        if not self.connected or not self.path:
            return
        dest = Path(self.path).name or self.path
        self.transfers.upload(self.client, sources, self.path, dest)

    def download_selected(self) -> None:
        items = self._selected()
        if not items:
            return
        folder = QFileDialog.getExistingDirectory(self, "Download to", str(self.download_dir))
        if folder:
            self.download_dir = Path(folder)
            self._start_download(items, self.download_dir)

    def _start_download(self, items: list[dict], folder: Path) -> None:
        self.last_download_dir = folder
        self.transfers.download(self.client, items, folder)

    def _job_finished(self, direction: str, ok: bool) -> None:
        if direction == "upload" and ok:
            self.refresh()
            self.toast.show_message("Upload complete", "success")
        elif direction == "download" and ok:
            self.toast.show_message(f"Saved to {self.last_download_dir}", "success")

    def _update_transfers_area(self) -> None:
        count = len(self.transfers.rows())
        self.transfers_area.setVisible(bool(count))
        self.transfers_area.setFixedHeight(min(count, 3) * 70 if count else 0)
        self.toast.bottom_offset = 24 + (self.transfers_area.height() + 12 if count else 0)

    def _error(self, exc: BaseException) -> None:
        message = str(exc) if isinstance(exc, RavenError) else f"Error: {exc}"
        self.toast.show_message(message, "error", 6000)

    # ---------------------------------------------------------- connection

    _alive = True

    def _emit_closed(self, reason: str) -> None:
        # Called on the network thread.
        if self._alive:
            self.runner.ui(lambda: self._on_disconnected(reason) if self._alive else None)

    def _on_disconnected(self, reason: str) -> None:
        self.connected = False
        self.state_dot.set_role("danger")
        self.banner_text.setText(f"Disconnected from {self.remote.name}. Close this window and connect again.")
        self.banner.show()
        self._update_actions()

    def closeEvent(self, event) -> None:  # noqa: N802
        active = self.transfers.active()
        if active and self.connected:
            if not confirm(self, "Transfers in progress", f"Cancel {len(active)} transfer(s) and close?"):
                event.ignore()
                return
        self.transfers.cancel_all()
        self._alive = False
        self.session.remove_close_listener(self._closed_cb)
        if self.owns_session:
            self.runner.submit(self.session.close())
        super().closeEvent(event)
