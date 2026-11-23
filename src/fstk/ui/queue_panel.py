"""Queue tab: episodes' work run one after another, unattended (``fstk.workflow.queue``)."""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from fstk.core.errors import FstkError
from fstk.workflow.queue import ATTENTION, DONE, KINDS, QUEUED, RUNNING, STATUS_TEXT

COLUMNS = ["#", "Episode", "Task", "Status", "Progress", "Now", "Added"]
KIND_SHORT = {"intake": "Intake", "delivery": "Delivery", "reencode": "Re-encode"}
STATUS_COLOUR = {"done": "#2e7d32", "running": "#1565c0", "needs-review": "#b26a00", "blocked": "#b26a00",
                 "failed": "#c62828", "stopped": "#6d6d6d", "held": "#6d6d6d", "queued": ""}


class QueueBridge(QObject):
    """Runner callbacks arrive on worker threads; these signals bring them to the UI thread."""

    changed = Signal(str)
    finished = Signal(str, str, str, str)       # episode, kind, status, detail
    idle = Signal()


class AddSeveralDialog(QDialog):
    def __init__(self, episodes: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add episodes to the queue")
        self.resize(420, 480)
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Task:"))
        self.kind = QComboBox()
        self.kind.setToolTip("What to queue for each ticked episode: intake, delivery, or both in order.")
        self.kind.addItem("Intake, then delivery", "both")
        for k, text in KINDS.items():
            self.kind.addItem(text, k)
        row.addWidget(self.kind, 1)
        lay.addLayout(row)
        self.list = QListWidget()
        self.list.setToolTip("Tick the episodes to add to the queue.")
        for e in episodes:
            it = QListWidgetItem(e)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked)
            self.list.addItem(it)
        lay.addWidget(self.list, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        from fstk.ui.tips import apply_common

        apply_common(self)

    def chosen(self) -> tuple[list[str], str]:
        eps = [self.list.item(i).text() for i in range(self.list.count())
               if self.list.item(i).checkState() == Qt.CheckState.Checked]
        return eps, self.kind.currentData()


class QueuePanel(QWidget):
    def __init__(self, runner, *, current_episode, episodes, notify=None, sleep=None, parent=None):
        super().__init__(parent)
        self.runner, self.store = runner, runner.store
        self.current_episode, self.all_episodes = current_episode, episodes
        self.notify, self.sleep = notify, sleep
        self.bridge = QueueBridge()
        runner.on_change = self.bridge.changed.emit
        runner.on_finished = lambda it: self.bridge.finished.emit(it.episode, it.kind, it.status, it.detail)
        runner.on_idle = self.bridge.idle.emit
        self.bridge.changed.connect(lambda _id: self.refresh())
        self.bridge.finished.connect(self._finished)
        self.bridge.idle.connect(self._idle)

        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.start_button = QPushButton("Start queue")
        self.start_button.setToolTip("Run the queued items in order. One heavy job (stack, decode, encode) runs at a "
                                     "time; the next episode can download meanwhile.")
        self.start_button.clicked.connect(self.toggle_running)
        bar.addWidget(self.start_button)
        self.add_button = QToolButton()
        self.add_button.setText("Add to queue")
        self.add_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.add_menu = QMenu(self)
        self.add_menu.aboutToShow.connect(self._fill_add_menu)
        self.add_button.setMenu(self.add_menu)
        bar.addWidget(self.add_button)
        self.buttons: dict[str, QPushButton] = {}
        for key, text, tip, slot in (
                ("up", "Move up", "Run earlier", lambda: self._move(-1)),
                ("down", "Move down", "Run later", lambda: self._move(1)),
                ("retry", "Retry", "Run again: an item that stopped continues its own job where it stopped",
                 self._retry),
                ("stop", "Stop item", "Stop this running item (it can be retried later)", self._stop_item),
                ("remove", "Remove", "Take the item out of the queue (its work so far is kept)", self._remove),
                ("clear", "Clear finished", "Remove the items that are done", self._clear)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            bar.addWidget(b)
            self.buttons[key] = b
        bar.addStretch(1)
        self.sleep_box = QCheckBox("Sleep the computer when the queue is done")
        self.sleep_box.setChecked(self.store.sleep_when_done)
        self.sleep_box.toggled.connect(self._sleep_toggled)
        bar.addWidget(self.sleep_box)
        lay.addLayout(bar)
        self.summary = QLabel()
        lay.addWidget(self.summary)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        head = self.table.horizontalHeader()
        for col, mode in ((0, QHeaderView.ResizeMode.ResizeToContents), (5, QHeaderView.ResizeMode.Stretch)):
            head.setSectionResizeMode(col, mode)
        self.table.setColumnWidth(1, 120)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 170)
        self.table.setColumnWidth(4, 150)
        self.table.setColumnWidth(6, 140)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        lay.addWidget(self.table, 1)

        from fstk.ui.tips import apply_tips

        apply_tips(self, {
            "add_button": "Add the selected episode, or several, to the queue: intake, delivery, or a re-encode "
                          "with the current settings.",
            "sleep_box": "Put the computer to sleep a minute after the queue finishes (cancellable). Use it for "
                         "overnight runs.",
            "table": "The queue in running order. Each item shows its status, progress and what it is doing; "
                     "items that need you are marked and the queue moves on.",
        })
        self.tick = QTimer(self)
        self.tick.setInterval(1000)
        self.tick.timeout.connect(self.refresh)
        self.tick.start()
        self.sleep_timer = QTimer(self)
        self.sleep_timer.setSingleShot(True)
        self.sleep_timer.timeout.connect(self._sleep_now)
        self.refresh()

    # ------------------------------------------------------------------ view
    def _selected(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item_id = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        try:
            return self.store.get(item_id)
        except FstkError:
            return None

    def refresh(self) -> None:
        self.store.sync()                       # changes saved by another window
        keep = self._selected()
        items = list(self.store.items)
        self.table.setRowCount(len(items))
        for r, it in enumerate(items):
            cells = [str(r + 1), it.episode, KIND_SHORT.get(it.kind, it.kind), STATUS_TEXT.get(it.status, it.status),
                     None, (it.step if it.status == RUNNING else it.detail), _local(it.added_utc)]
            for c, text in enumerate(cells):
                if c == 4:
                    continue
                cell = self.table.item(r, c)
                if cell is None:
                    cell = QTableWidgetItem()
                    self.table.setItem(r, c, cell)
                cell.setText(text or "")
                if c == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, it.id)
                if c == 3:
                    colour = STATUS_COLOUR.get(it.status, "")
                    cell.setData(Qt.ItemDataRole.ForegroundRole, _qcolor(colour) if colour else None)
                if c == 5:
                    cell.setToolTip(text or "")
            bar = self.table.cellWidget(r, 4)
            if bar is None:
                bar = QProgressBar()
                bar.setRange(0, 1000)
                self.table.setCellWidget(r, 4, bar)
            value = 1.0 if it.status == DONE else (it.progress if it.status == RUNNING else 0.0)
            bar.setValue(int(value * 1000))
            bar.setFormat(f"{value:.0%}" if it.status in (RUNNING, DONE) else "")
        if keep is not None and keep in items:
            self.table.selectRow(items.index(keep))
        running = sum(1 for i in items if i.status == RUNNING)
        waiting = sum(1 for i in items if i.status in (QUEUED, "held"))
        attention = sum(1 for i in items if i.status in ATTENTION)
        from fstk.workflow.engine import HEAVY

        heavy = HEAVY.holder_label
        other = self.runner.other_runner()
        parts = [(f"Queue running in another window ({other})" if other and self.runner.running
                  else "Queue running" if self.runner.running else "Queue stopped"),
                 f"{running} running", f"{waiting} waiting"]
        if attention:
            parts.append(f"{attention} need attention")
        if heavy:
            parts.append(f"heavy job: {heavy}")
        self.summary.setText(" · ".join(parts))
        self.start_button.setText("Stop queue" if self.runner.running and not other else "Start queue")
        self.start_button.setToolTip("Start nothing new; running items carry on" if self.runner.running
                                     else self.start_button.toolTip())
        self._update_buttons()

    def _update_buttons(self) -> None:
        it = self._selected()
        st = it.status if it else None
        self.buttons["up"].setEnabled(it is not None)
        self.buttons["down"].setEnabled(it is not None)
        self.buttons["retry"].setEnabled(st is not None and st not in (QUEUED, RUNNING, "held"))
        self.buttons["stop"].setEnabled(st == RUNNING)
        self.buttons["remove"].setEnabled(st is not None and st != RUNNING)
        self.buttons["clear"].setEnabled(any(i.status == DONE for i in self.store.items))

    # ------------------------------------------------------------------ actions
    def _fill_add_menu(self) -> None:
        self.add_menu.clear()
        ep = self.current_episode()
        if ep:
            for label, kinds in ((f"{ep}: intake, then delivery", ("intake", "delivery")),
                                 (f"{ep}: intake", ("intake",)), (f"{ep}: delivery", ("delivery",)),
                                 (f"{ep}: re-encode with current settings", ("reencode",))):
                self.add_menu.addAction(label, lambda k=kinds, e=ep: self.add(e, k))
            self.add_menu.addSeparator()
        self.add_menu.addAction("Several episodes…", self._add_several)

    def add(self, episode: str, kinds: tuple[str, ...]) -> None:
        added = []
        for k in kinds:
            try:
                self.store.add(episode, k)
                added.append(k)
            except FstkError as exc:
                QMessageBox.information(self, "Not added", str(exc))
        if added:
            self.refresh()

    def _add_several(self) -> None:
        dlg = AddSeveralDialog(self.all_episodes(), self)
        if not dlg.exec():
            return
        eps, kind = dlg.chosen()
        for ep in eps:
            self.add(ep, ("intake", "delivery") if kind == "both" else (kind,))

    def toggle_running(self) -> None:
        try:
            if self.runner.running and self.runner.other_runner() is None:
                self.runner.stop()
            else:
                self.runner.start()
        except FstkError as exc:
            QMessageBox.information(self, "Queue", str(exc))
        self.refresh()

    def _move(self, delta: int) -> None:
        it = self._selected()
        if it:
            self.store.move(it.id, delta)
            self.refresh()

    def _retry(self) -> None:
        it = self._selected()
        if it:
            self.store.retry(it.id)
            self.refresh()

    def _stop_item(self) -> None:
        it = self._selected()
        if it and QMessageBox.question(self, "Stop this item?", f"Stop {it.episode} {KIND_SHORT[it.kind]}? Finished "
                                       "steps are kept; Retry continues where it stopped.") \
                == QMessageBox.StandardButton.Yes:
            self.runner.cancel(it.id)

    def _remove(self) -> None:
        it = self._selected()
        if it:
            try:
                self.store.remove(it.id)
            except FstkError as exc:
                QMessageBox.information(self, "Not removed", str(exc))
            self.refresh()

    def _clear(self) -> None:
        self.store.clear_finished()
        self.refresh()

    # ------------------------------------------------------------------ outcomes
    def _finished(self, episode: str, kind: str, status: str, detail: str) -> None:
        self.refresh()
        if self.notify:
            self.notify(f"{episode} {KIND_SHORT.get(kind, kind)}: {STATUS_TEXT.get(status, status)}",
                        detail[:200])

    def _sleep_toggled(self, on: bool) -> None:
        with self.store.lock:
            self.store.sleep_when_done = on
            self.store.save()
        if not on and self.sleep_timer.isActive():
            self.sleep_timer.stop()

    def _idle(self) -> None:
        self.refresh()
        if self.notify:
            self.notify("Queue finished", "Every queued item has finished or needs your attention.")
        if self.sleep_box.isChecked() and self.sleep is not None:
            if self.notify:
                self.notify("Going to sleep in 1 minute", "Untick “Sleep the computer when the queue is done” "
                                                          "to stay awake.")
            self.sleep_timer.start(60_000)

    def _sleep_now(self) -> None:
        if self.sleep_box.isChecked() and not self.runner.busy() and self.sleep is not None:
            self.sleep()


def _local(utc: str) -> str:
    from datetime import datetime

    try:
        return datetime.fromisoformat(utc.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return utc[:16]


def _qcolor(hex_colour: str):
    from PySide6.QtGui import QColor

    return QColor(hex_colour)
