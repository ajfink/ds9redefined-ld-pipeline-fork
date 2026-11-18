"""Tk dialog for reviewing and applying explicit dependency updates."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from dependency_updates import check_updates, install_selected


class DependencyUpdateWindow:
    def __init__(self, parent: tk.Misc, profile_path: str | Path,
                 busy_changed: Callable[[bool], None] | None = None):
        self.parent = parent
        self.profile_path = Path(profile_path).expanduser().resolve()
        self.busy_changed = busy_changed or (lambda _busy: None)
        self.window = tk.Toplevel(parent)
        self.window.title("Dependency update")
        self.window.geometry("820x540")
        self.window.minsize(680, 420)
        self.window.transient(parent.winfo_toplevel())
        self.window.protocol("WM_DELETE_WINDOW", self._request_close)
        self.results_queue: queue.Queue[tuple[str, dict]] = queue.Queue()
        self.busy = True
        self.busy_notified = False
        self.operation = "check"
        self.cancel_event = threading.Event()
        self.installation_sequence_started = threading.Event()
        self.update_worker_finished = threading.Event()
        self.update_worker_finished.set()
        self.detached = False
        self.close_when_done = False
        self.close_dialog: tk.Toplevel | None = None
        self.rows: list[dict] = []
        self.variables: dict[str, tk.BooleanVar] = {}
        self.status_labels: dict[str, ttk.Label] = {}
        self.active_update_ids: list[str] = []

        body = ttk.Frame(self.window, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Installed vs. newest available versions",
                  font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(body, text=f"Profile: {self.profile_path}", wraplength=770).pack(anchor="w", pady=(3, 8))
        self.status = tk.StringVar(value="Checking package indexes and installed tools…")
        ttk.Label(body, textvariable=self.status, wraplength=770).pack(anchor="w", pady=(0, 8))

        table = ttk.LabelFrame(body, text="Available updates", padding=6)
        table.pack(fill="both", expand=True)
        header = ttk.Frame(table)
        header.pack(fill="x", pady=(0, 5))
        ttk.Label(header, text="Software", width=25).pack(side="left")
        ttk.Label(header, text="Installed", width=19).pack(side="left")
        ttk.Label(header, text="Newest", width=19).pack(side="left")
        ttk.Label(header, text="Source", width=12).pack(side="left")
        self.rows_frame = ttk.Frame(table)
        self.rows_frame.pack(fill="both", expand=True, anchor="nw")
        self.notes = tk.Text(body, height=3, wrap="word", state="disabled")
        self.notes.pack(fill="x", pady=(8, 0))

        controls = ttk.Frame(body)
        controls.pack(fill="x", pady=(10, 0))
        self.select_all_button = ttk.Button(controls, text="Select all available",
                                            command=self._select_all)
        self.select_all_button.pack(side="left")
        self.clear_button = ttk.Button(controls, text="Clear all", command=self._clear_all)
        self.clear_button.pack(side="left", padx=6)
        self.update_button = ttk.Button(controls, text="Update now", command=self._update_now,
                                        state="disabled")
        self.update_button.pack(side="right")
        self.close_button = ttk.Button(controls, text="Close", command=self._request_close)
        self.close_button.pack(side="right", padx=6)

        self._set_busy(True)
        threading.Thread(target=self._check_worker, name="dependency-check", daemon=True).start()
        self._schedule_poll(100)

    def _check_worker(self) -> None:
        try:
            report = check_updates(self.profile_path)
        except Exception as exc:
            report = {"ok": False, "error": str(exc), "rows": [], "notes": []}
        self.results_queue.put(("check", report))

    def _update_worker(self, selected: list[str]) -> None:
        def progress(item_id: str | None, event: str, text: str) -> None:
            if (event == "status" and text == "Updating..." and
                    not self.cancel_event.is_set()):
                self.installation_sequence_started.set()
            self.results_queue.put(("progress", {"id": item_id, "event": event, "text": text}))

        try:
            report = install_selected(self.profile_path, selected, progress=progress,
                                      cancel_event=self.cancel_event)
        except Exception as exc:
            report = {"ok": False, "error": str(exc), "results": []}
        finally:
            self.update_worker_finished.set()
        self.results_queue.put(("update", report))

    def _set_busy(self, busy: bool) -> None:
        if self.busy_notified != busy:
            self.busy_changed(busy)
            self.busy_notified = busy

    def _schedule_poll(self, milliseconds: int) -> None:
        try:
            self.parent.winfo_toplevel().after(milliseconds, self._poll)
        except tk.TclError:
            pass

    def _poll(self) -> None:
        try:
            kind, report = self.results_queue.get_nowait()
        except queue.Empty:
            if not self.detached:
                self._schedule_poll(100)
            return
        if kind == "progress":
            if not self.detached:
                self._show_progress(report)
            self._schedule_poll(50)
            return
        self.busy = False
        self._set_busy(False)
        if self.detached:
            return
        if kind == "check":
            self._show_check_report(report)
        else:
            self._show_update_report(report)
        if self.close_when_done:
            self.detached = True
            self.window.destroy()

    def _show_check_report(self, report: dict) -> None:
        self.rows = list(report.get("rows") or [])
        for child in self.rows_frame.winfo_children():
            child.destroy()
        self.variables.clear()
        self.status_labels.clear()
        available_count = 0
        for row in self.rows:
            line = ttk.Frame(self.rows_frame)
            line.pack(fill="x", anchor="w", pady=2)
            available = bool(row.get("available") and row.get("selectable"))
            variable = tk.BooleanVar(value=available)
            self.variables[row["id"]] = variable
            check = ttk.Checkbutton(line, text=row.get("name", row["id"]),
                                    variable=variable, state="normal" if available else "disabled",
                                    width=23)
            check.pack(side="left")
            ttk.Label(line, text=str(row.get("installed") or "not installed"), width=19).pack(side="left")
            ttk.Label(line, text=str(row.get("latest") or "—"), width=19).pack(side="left")
            ttk.Label(line, text=str(row.get("source") or "—"), width=12).pack(side="left")
            detail = ttk.Label(line, text=str(row.get("detail") or ""), wraplength=730)
            detail.pack(side="left", fill="x", expand=True)
            self.status_labels[row["id"]] = detail
            available_count += int(available)
        self._set_notes(report.get("notes") or ([report["error"]] if report.get("error") else []))
        if report.get("error"):
            self.status.set("Version check did not complete: " + report["error"])
        elif available_count:
            self.status.set(f"Check complete: {available_count} update(s) available. Select the updates to install.")
        else:
            self.status.set("Check complete: no supported updates are currently available.")
        self.update_button.configure(state="normal" if available_count else "disabled")

    def _show_update_report(self, report: dict) -> None:
        self._append_note("=== Update results ===")
        results = report.get("results") or []
        for row in results:
            item_id = row.get("id")
            label = ("Stopped" if row.get("cancelled") else
                     "Updated!" if row.get("ok") else "Failed")
            if item_id in self.status_labels:
                self.status_labels[item_id].configure(text=label)
            line = f"{row.get('name', item_id or 'dependency')}: {label}"
            if not row.get("ok") and row.get("detail"):
                detail = str(row["detail"]).strip().splitlines()
                if detail:
                    line += " - " + detail[-1]
            self._append_note(line)
        if report.get("error"):
            self._append_note("Failed: " + report["error"])
            for item_id in self.active_update_ids:
                if (item_id in self.status_labels and
                        self.status_labels[item_id].cget("text") in ("Queued...", "Updating...")):
                    self.status_labels[item_id].configure(text="Failed")
        successes = sum(bool(row.get("ok")) for row in results)
        total = len(results) if results else len(self.active_update_ids)
        if report.get("cancelled"):
            self.status.set(f"Updates stopped: {successes}/{total} successful before cancellation.")
        else:
            self.status.set(f"Update run finished: {successes}/{total} successful. Recheck before processing.")
        self.update_button.configure(state="disabled")

    def _show_progress(self, event: dict) -> None:
        item_id = event.get("id")
        text = str(event.get("text") or "")
        if event.get("event") == "status" and item_id in self.status_labels:
            self.status_labels[item_id].configure(text=text)
        elif text:
            self._append_note(text)

    def _set_notes(self, lines: list[str]) -> None:
        self.notes.configure(state="normal")
        self.notes.delete("1.0", "end")
        self.notes.insert("end", "\n".join(str(line) for line in lines))
        self.notes.see("end")
        self.notes.configure(state="disabled")

    def _append_note(self, line: str) -> None:
        self.notes.configure(state="normal")
        self.notes.insert("end", str(line) + "\n")
        line_count = int(self.notes.index("end-1c").split(".", 1)[0])
        if line_count > 1500:
            self.notes.delete("1.0", f"{line_count - 1500}.0")
        self.notes.see("end")
        self.notes.configure(state="disabled")

    def _select_all(self) -> None:
        for row in self.rows:
            if row.get("available") and row.get("selectable"):
                self.variables[row["id"]].set(True)

    def _clear_all(self) -> None:
        for variable in self.variables.values():
            variable.set(False)

    def _update_now(self) -> None:
        selected = [item_id for item_id, variable in self.variables.items() if variable.get()]
        if not selected:
            messagebox.showwarning("No updates selected", "Select at least one available update.",
                                   parent=self.window)
            return
        names = [str(row.get("name", row["id"])) for row in self.rows if row["id"] in selected]
        if not messagebox.askyesno(
                "Install selected updates",
                "The selected dependency updates will now be installed:\n\n" +
                "\n".join(f"• {name}" for name in names) +
                "\n\nThis may change the active Python environment or managed applications. Continue?",
                parent=self.window):
            return
        self.busy = True
        self.operation = "update"
        self.cancel_event = threading.Event()
        self.installation_sequence_started.clear()
        self.update_worker_finished.clear()
        self.active_update_ids = selected
        self._set_busy(True)
        self.status.set("Installing selected updates… keep this window open.")
        self._set_notes(["Starting selected updates; installer output will appear here."])
        for item_id in selected:
            if item_id in self.status_labels:
                self.status_labels[item_id].configure(text="Queued...")
        self.update_button.configure(state="disabled")
        self.select_all_button.configure(state="disabled")
        self.clear_button.configure(state="disabled")
        threading.Thread(target=self._update_worker, args=(selected,),
                         name="dependency-install", daemon=True).start()

    def _request_close(self) -> None:
        if self.close_when_done:
            return
        if not self._requires_close_choices():
            # Before the first installer starts, closing is immediate and cancels
            # the recheck so no background installation can begin unexpectedly.
            if self.busy:
                self.detached = True
                if not (self.operation == "update" and self.update_worker_finished.is_set()):
                    self.cancel_event.set()
                self._set_busy(False)
            self.window.destroy()
            return
        if self.close_dialog is not None:
            try:
                self.close_dialog.lift()
                return
            except tk.TclError:
                self.close_dialog = None
        dialog = tk.Toplevel(self.window)
        self.close_dialog = dialog
        dialog.title("Close dependency update")
        dialog.transient(self.window)
        dialog.resizable(False, False)
        ttk.Label(
            dialog,
            text=("An update operation is still running. Choose what to do:"
                  if self.busy else "Choose whether to close this dependency update window."),
            wraplength=430, padding=12).pack(fill="x")
        choices = ttk.Frame(dialog, padding=(12, 0, 12, 12))
        choices.pack(fill="x")
        for label, action in (
                ("Stop updates and close", "stop"),
                ("Continue updates and close", "continue-close"),
                ("Continue updates and stay", "stay")):
            ttk.Button(choices, text=label,
                       command=lambda selected=action: self._choose_close(selected)).pack(
                           fill="x", pady=3)
        dialog.protocol("WM_DELETE_WINDOW", lambda: self._choose_close("stay"))
        dialog.grab_set()
        dialog.lift()

    def _requires_close_choices(self) -> bool:
        return (self.busy and self.operation == "update" and
                self.installation_sequence_started.is_set() and
                not self.update_worker_finished.is_set())

    def _choose_close(self, choice: str) -> None:
        dialog = self.close_dialog
        self.close_dialog = None
        if dialog is not None:
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass
        if choice == "stay":
            return
        if choice == "stop" and self.busy and self.operation == "update":
            self.close_when_done = True
            self.status.set("Stopping selected updates; waiting for the current installer to stop...")
            self.cancel_event.set()
            self.select_all_button.configure(state="disabled")
            self.clear_button.configure(state="disabled")
            self.update_button.configure(state="disabled")
            return
        if choice == "stop" and self.busy and self.operation == "check":
            # Checking package indexes is read-only; closing discards the result and
            # never starts an installation.
            self.cancel_event.set()
            self._set_busy(False)
        elif choice == "continue-close" and self.busy and self.operation == "update":
            # Keep the host app gated until the detached installer finishes.
            self.detached = True
            self.window.destroy()
            return
        elif choice == "continue-close" and self.busy and self.operation == "check":
            self.detached = True
            self._set_busy(False)
        self.detached = True
        self.window.destroy()
