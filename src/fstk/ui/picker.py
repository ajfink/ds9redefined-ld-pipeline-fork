"""Episode picker: browse the DS9 / VOY / TNG catalog with this machine's local state.

Series → Season → Episode tree (title, captures, raw EFM, size, local state). Search accepts
natural requests ("DS9 season 2 episode 18", "voyager S01E01") or title words. Entries that are
not selectable (Emissary two-part, TNG combined item) are shown with the reason and cannot be
started. Creating a workspace never downloads; the intake plan does that, behind the space gate.
With a shared workspace (ADR 0002) an "Other devices" column shows where else each episode is held,
and an episode another machine handed to this one can be adopted here.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from fstk.core.errors import FstkError

STATE_COLOURS = {"workspace": "#1565c0", "downloaded": "#2e7d32", "partial": "#ef6c00",
                 "remote": "#616161", "lookup-only": "#9e9e9e"}
SERIES_NAMES = {"DS9": "Deep Space Nine", "VOY": "Voyager", "TNG": "The Next Generation"}
ROLE_ENTRY = Qt.ItemDataRole.UserRole


class EpisodePicker(QDialog):
    def __init__(self, services, *, actor: str, parent=None):
        super().__init__(parent)
        self.handed = None    # peer Presence that handed the selected episode to this device
        self.services, self.actor = services, actor
        self.chosen = None  # EpisodeId of the created/opened workspace
        self.setWindowTitle("Choose an episode")
        self.resize(1100, 680)
        index = services.catalog()
        summary = index.summary()

        self.search = QLineEdit()
        self.search.setPlaceholderText('Search: "DS9 season 2 episode 18", "voyager S01E01", or title words')
        self.search.textChanged.connect(self.populate)
        self.series = QComboBox()
        self.series.addItems(["All series", "DS9", "VOY", "TNG"])
        self.series.currentIndexChanged.connect(self.populate)
        self.only_local = QCheckBox("Only episodes on this machine")
        self.only_local.toggled.connect(self.populate)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Episode", "Title", "Captures", "EFM", "GiB", "Local", "Other devices"])
        header = self.tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.currentItemChanged.connect(self.show_entry)
        self.tree.itemDoubleClicked.connect(lambda *_: self.accept_selection())
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)

        self.create_btn = QPushButton("Create workspace")
        self.open_btn = QPushButton("Open workspace")
        self.local_btn = QPushButton("From local folder…")
        self.adopt_btn = QPushButton("Adopt from other device")
        self.adopt_btn.clicked.connect(self.adopt_selection)
        self.adopt_btn.setEnabled(False)
        close = QPushButton("Close")
        self.create_btn.clicked.connect(self.accept_selection)
        self.open_btn.clicked.connect(self.accept_selection)
        self.local_btn.clicked.connect(self.from_local)
        close.clicked.connect(self.reject)

        from fstk.ui.tips import apply_tips

        apply_tips(self, {
            "search": "Type a series, season, episode or title words. Searches the archive catalog snapshot.",
            "series": "Limit the list to one series.",
            "only_local": "Show only episodes that already have a workspace, a download or a partial download "
                          "on this computer.",
            "tree": "The archive catalog. Double-click an episode to create or open its workspace.",
            "detail": "What is known about the selected episode: captures, sizes, where it is held.",
            "create_btn": "Create a workspace for the selected episode. Nothing is downloaded until intake runs, "
                          "and only after the free-space check.",
            "open_btn": "Open the existing workspace of the selected episode.",
            "local_btn": "Use captures you already have in a folder or on a network share, instead of downloading.",
            "adopt_btn": "Take over an episode another computer handed off (shared workspace).",
        })
        close.setToolTip("Close without choosing.")
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.series)
        top.addWidget(self.only_local)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.tree)
        split.addWidget(self.detail)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        buttons = QHBoxLayout()
        buttons.addWidget(QLabel(f"Catalog snapshot {str(summary['generated_at_utc'])[:10]} · "
                                 f"{summary['selectable']} startable episodes"))
        buttons.addStretch(1)
        for b in (self.local_btn, self.adopt_btn, self.open_btn, self.create_btn, close):
            buttons.addWidget(b)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(split, 1)
        layout.addLayout(buttons)
        self.populate()

    # ---------------------------------------------------------------- tree
    def populate(self) -> None:
        index = self.services.catalog()
        entries = index.search(self.search.text())
        series = self.series.currentText()
        if series != "All series":
            entries = [e for e in entries if e.series == series]
        self.tree.clear()
        groups: dict[tuple[str, int], QTreeWidgetItem] = {}
        tops: dict[str, QTreeWidgetItem] = {}
        cov = index.coverage()
        shown = 0
        self.presence = self.services.all_presence()
        for e in entries:
            status = self.services.local_status(e)
            if self.only_local.isChecked() and status.state not in ("workspace", "downloaded", "partial"):
                continue
            if e.series not in tops:
                top = QTreeWidgetItem([e.series, SERIES_NAMES[e.series]])
                top.setToolTip(1, cov[e.series].note)
                self.tree.addTopLevelItem(top)
                tops[e.series] = top
            key = (e.series, e.season)
            if key not in groups:
                g = QTreeWidgetItem([f"Season {e.season}"])
                tops[e.series].addChild(g)
                groups[key] = g
            gib = f"{(e.selected_bytes or 0) / 1024 ** 3:.0f}" if e.selected_bytes else ""
            others = [p for p in self.presence.get(e.episode_id.canonical, []) if not p.mine]
            item = QTreeWidgetItem([e.code, e.title_hint, str(len(e.capture_labels) or ""),
                                    "" if e.raw_efm_count is None else str(e.raw_efm_count), gib, status.state,
                                    ", ".join(f"{p.device.rsplit('-', 1)[0]} ({p.state})" for p in others)])
            item.setData(0, ROLE_ENTRY, (e, status))
            item.setForeground(5, QColor(STATE_COLOURS.get(status.state, "#616161")))
            if not e.selectable:
                for col in range(7):
                    item.setForeground(col, QColor("#9e9e9e"))
            groups[key].addChild(item)
            shown += 1
        expand = bool(self.search.text().strip()) or self.only_local.isChecked() or shown < 40
        for top in tops.values():
            top.setExpanded(True)
            for i in range(top.childCount()):
                top.child(i).setExpanded(expand)
        self.show_entry(self.tree.currentItem())

    def selected(self):
        item = self.tree.currentItem()
        return item.data(0, ROLE_ENTRY) if item is not None else None

    def show_entry(self, item, *_):
        data = item.data(0, ROLE_ENTRY) if item is not None else None
        self.create_btn.setEnabled(bool(data and data[0].selectable and data[1].state != "workspace"))
        self.open_btn.setEnabled(bool(data and data[1].state == "workspace"))
        self.adopt_btn.setEnabled(False)
        if not data:
            self.detail.setPlainText("Select an episode.")
            return
        e, st = data
        rows = getattr(self, "presence", {}).get(e.episode_id.canonical, [])
        others = [p for p in rows if not p.mine]
        self.handed = self.services.handed_to_me(e.episode_id) if others and st.state != "workspace" else None
        self.adopt_btn.setEnabled(self.handed is not None)
        lines = [f"{e.series}:{e.code}  {e.title_hint}", "",
                 f"Archive item : {e.identifier}", f"URL          : {e.url}",
                 f"Kind         : {e.kind}",
                 f"Captures     : {', '.join(e.capture_labels) or '—'}",
                 f"Raw EFM files: {'—' if e.raw_efm_count is None else e.raw_efm_count}",
                 f"Download size: {(e.selected_bytes or 0) / 1024 ** 3:.1f} GiB", "",
                 f"On this machine: {st.state} — {st.detail}"]
        if st.downloads_dir:
            lines.append(f"Capture folder : {st.downloads_dir}")
        if others:
            lines += ["", "Other devices:"] + [f"  • {p.describe()}" for p in others]
            if self.handed is not None:
                lines.append(f"  → handed to this device by {self.handed.device}: use “Adopt from other device”")
        elif getattr(self.services, "shared_problem", ""):
            lines += ["", f"Shared workspace: {self.services.shared_problem}"]
        if e.warnings:
            lines += ["", "Warnings:"] + [f"  • {w}" for w in e.warnings]
        if not e.selectable:
            lines += ["", f"Not startable: {e.reason}"]
        lines += ["", "Titles come from capture filenames; confirm against the captures/DVD.",
                  "Creating a workspace downloads nothing; the intake plan checks live Archive",
                  "metadata, MD5s and free space before any transfer."]
        self.detail.setPlainText("\n".join(lines))

    # ---------------------------------------------------------------- actions
    def accept_selection(self) -> None:
        data = self.selected()
        if not data:
            return
        e, st = data
        try:
            if st.state == "workspace":
                self.chosen = e.episode_id
            elif e.selectable:
                self.chosen = self.services.create_from_catalog(e, actor=self.actor)
            else:
                return
        except FstkError as exc:
            QMessageBox.warning(self, "Cannot create workspace", str(exc))
            return
        self.accept()

    def adopt_selection(self) -> None:
        data = self.selected()
        if not data or self.handed is None:
            return
        e, _ = data
        try:
            report = self.services.adopt(e.episode_id, source=self.handed.device, actor=self.actor)
        except FstkError as exc:
            QMessageBox.warning(self, "Cannot adopt", str(exc))
            return
        missing = [r for r in report["media"] if r["status"] != "present"]
        if missing or report["unmapped_tokens"]:
            text = "\n".join(f"{r['artifact']}: {r['status']} — {r['local_path']}" for r in missing)
            if report["unmapped_tokens"]:
                text += "\nPaths this machine profile cannot place: " + ", ".join(report["unmapped_tokens"])
            QMessageBox.information(self, "Adopted — media to bring over", text)
        self.chosen = e.episode_id
        self.accept()

    def from_local(self) -> None:
        from fstk.ui.localfolder import LocalFolderDialog

        start = str(self.services.profile.sources.get("downloads_root") or "")
        dlg = LocalFolderDialog(start, self)
        if dlg.exec() and dlg.chosen:
            self.create_local(dlg.chosen)

    def create_local(self, folder: str) -> None:
        try:
            self.chosen = self.services.create_from_local(folder, actor=self.actor)
        except (FstkError, ValueError) as exc:
            QMessageBox.warning(self, "Cannot use this folder", str(exc))
            return
        self.accept()
