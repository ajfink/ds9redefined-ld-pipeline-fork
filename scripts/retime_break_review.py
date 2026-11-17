#!/usr/bin/env python3
"""Evidence table for explicitly reviewing V15 retime-break candidates."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tkinter import messagebox, ttk
import tkinter as tk
from typing import Any, Callable


class RetimeBreakReviewError(ValueError):
    """Candidate report is missing, malformed, or for a different identity."""


class RetimeBreakReviewWindow:
    """Show every candidate; require explicit per-row decisions and a review note."""

    def __init__(self, parent: tk.Misc, report_path: Path, expected_identity: str,
                 on_create_plan: Callable[[Path, dict[str, Any], str, list[str],
                                           list[str], str], bool]):
        self.parent = parent
        self.report_path = report_path.expanduser()
        self.expected_identity = expected_identity
        self.on_create_plan = on_create_plan
        self.window: tk.Toplevel | None = None
        self.rows: dict[str, dict[str, Any]] = {}
        self.decisions: dict[str, str] = {}
        self.report: dict[str, Any] = {}
        self.report_sha256 = ""
        self.tree: ttk.Treeview | None = None
        self.detail: tk.Text | None = None
        self.review_note: tk.Text | None = None
        self.create_button: ttk.Button | None = None
        self._load_report()

    def _load_report(self) -> None:
        if self.report_path.is_symlink() or not self.report_path.is_file():
            raise RetimeBreakReviewError(
                f"candidate report must be an existing regular, non-symlink file: {self.report_path}")
        payload = self.report_path.read_bytes()
        self.report_sha256 = hashlib.sha256(payload).hexdigest()
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RetimeBreakReviewError(f"candidate report is not UTF-8 JSON: {exc}") from exc
        if (not isinstance(value, dict) or
                value.get("schema") != "ld-pipeline-retime-break-candidates-v1" or
                value.get("status") not in ("REPORT_ONLY", "NO_CANDIDATES") or
                value.get("identity") != self.expected_identity or
                value.get("requires_human_review") is not True or
                value.get("manifest_updated") is not False):
            raise RetimeBreakReviewError(
                f"candidate report must be a report-only V15 report for {self.expected_identity}")
        candidates = value.get("candidates")
        if not isinstance(candidates, list) or any(not isinstance(row, dict) for row in candidates):
            raise RetimeBreakReviewError("candidate report must contain an array of candidate objects")
        labels = [str(row.get("label", "")) for row in candidates]
        if any(not label for label in labels) or len(labels) != len(set(labels)):
            raise RetimeBreakReviewError("candidate report labels must be present and unique")
        self.rows = {label: row for label, row in zip(labels, candidates)}
        self.report = value

    @staticmethod
    def _interval(value: Any) -> str:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return "—"
        try:
            return f"{float(value[0]):.3f}–{float(value[1]):.3f}s"
        except (TypeError, ValueError):
            return "invalid"

    def show(self) -> None:
        dialog = tk.Toplevel(self.parent)
        self.window = dialog
        dialog.title(f"Review retime candidates — {self.expected_identity}")
        dialog.geometry("980x700")
        dialog.minsize(760, 520)
        dialog.transient(self.parent)
        dialog.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Report-only candidates for {self.expected_identity}",
                  font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(body, text=f"Report: {self.report_path}\nSHA-256: {self.report_sha256}",
                  wraplength=930, justify="left").pack(anchor="w", pady=(4, 8))
        report_summary = {
            "parameters": self.report.get("parameters"),
            "inputs": self.report.get("inputs"),
            "manifest_sha256": self.report.get("manifest_sha256"),
        }
        summary = ttk.LabelFrame(body, text="Detection parameters and bound input evidence", padding=5)
        summary.pack(fill="x", pady=(0, 8))
        ttk.Label(summary, text=json.dumps(report_summary, indent=2, ensure_ascii=False),
                  wraplength=930, justify="left").pack(anchor="w")
        ttk.Label(body,
                  text="No candidate is preselected. Unique pairs may be recorded only after review. "
                       "A no-silence candidate may be marked merged-through only with an explicit human decision.",
                  wraplength=930, justify="left").pack(anchor="w", pady=(0, 8))

        table_frame = ttk.Frame(body)
        table_frame.pack(fill="both", expand=True)
        columns = ("label", "status", "ld_black", "dvd_pairs", "decision")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        for key, label, width in (
                ("label", "Candidate", 100), ("status", "Pair status", 175),
                ("ld_black", "LD interval", 145),
                ("dvd_pairs", "DVD candidate(s)", 285), ("decision", "Review decision", 190)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, stretch=key == "dvd_pairs")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for index, (label, row) in enumerate(self.rows.items()):
            pairs = row.get("pair_candidates") or []
            pair_text = "; ".join(
                f"{candidate.get('id', '?')} {self._interval([candidate.get('start_s'), candidate.get('end_s')])}"
                for candidate in pairs) or "none"
            self.tree.insert("", "end", iid=f"candidate-{index}",
                             values=(label, row.get("pair_status", "unknown"),
                                     self._interval(row.get("ld_black")), pair_text, "not selected"))
            setattr(self, f"_label_{index}", label)

        self.detail = tk.Text(body, height=7, wrap="word", state="disabled")
        self.detail.pack(fill="x", pady=(8, 4))
        self.tree.bind("<<TreeviewSelect>>", self._show_selected_detail)
        decisions = ttk.Frame(body)
        decisions.pack(fill="x", pady=(0, 6))
        ttk.Button(decisions, text="Mark as unique cut", command=self._mark_cut).pack(side="left")
        ttk.Button(decisions, text="Mark as merged-through…", command=self._mark_merged).pack(side="left", padx=6)
        ttk.Button(decisions, text="Clear decision", command=self._clear_decision).pack(side="left")

        ttk.Label(body, text="Human review note (required):").pack(anchor="w")
        self.review_note = tk.Text(body, height=3, wrap="word")
        self.review_note.pack(fill="x", pady=(2, 8))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Cancel", command=self.close).pack(side="right", padx=(6, 0))
        self.create_button = ttk.Button(buttons, text="Create reviewed record plan…",
                                        command=self._create_plan, state="disabled")
        self.create_button.pack(side="right")
        dialog.grab_set()
        dialog.focus_set()

    def _selected_label(self) -> str | None:
        if not self.tree:
            return None
        selected = self.tree.selection()
        if not selected:
            return None
        try:
            index = int(selected[0].removeprefix("candidate-"))
            return getattr(self, f"_label_{index}")
        except (ValueError, AttributeError):
            return None

    def _show_selected_detail(self, _event=None) -> None:
        label = self._selected_label()
        if not self.detail or not label:
            return
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", json.dumps(self.rows[label], indent=2, ensure_ascii=False))
        self.detail.configure(state="disabled")

    def _set_decision(self, label: str, decision: str) -> None:
        self.decisions[label] = decision
        if self.tree:
            selected = self.tree.selection()
            if selected:
                values = list(self.tree.item(selected[0], "values"))
                values[-1] = decision
                self.tree.item(selected[0], values=values)
        if self.create_button:
            self.create_button.configure(state="normal" if self.decisions else "disabled")

    def _mark_cut(self) -> None:
        label = self._selected_label()
        if not label:
            return
        row = self.rows[label]
        if row.get("pair_status") != "unique-pair" or len(row.get("pair_candidates") or []) != 1:
            messagebox.showwarning("Not a unique pair",
                                   "Only a unique-pair candidate can be selected as a break. "
                                   "Ambiguous, colliding, uncovered, or unmatched candidates remain blocked.",
                                   parent=self.window)
            return
        self._set_decision(label, "record unique pair")

    def _mark_merged(self) -> None:
        label = self._selected_label()
        if not label:
            return
        row = self.rows[label]
        if row.get("pair_status") != "no-dvd-silence" or row.get("pair_candidates"):
            messagebox.showwarning("Not an unmatched candidate",
                                   "Only a no-dvd-silence candidate can be explicitly adjudicated as merged-through.",
                                   parent=self.window)
            return
        if messagebox.askyesno(
                "Confirm merged-through adjudication",
                f"You are explicitly declaring {label} to be merged music-through, despite no detected DVD silence. "
                "This records only the reviewed intervals; offsets and steps remain null. Continue?",
                parent=self.window):
            self._set_decision(label, "reviewed merged-through")

    def _clear_decision(self) -> None:
        label = self._selected_label()
        if label:
            self.decisions.pop(label, None)
            if self.tree:
                selected = self.tree.selection()
                if selected:
                    values = list(self.tree.item(selected[0], "values"))
                    values[-1] = "not selected"
                    self.tree.item(selected[0], values=values)
            if self.create_button:
                self.create_button.configure(state="normal" if self.decisions else "disabled")

    def _create_plan(self) -> None:
        if not self.review_note:
            return
        note = self.review_note.get("1.0", "end").strip()
        if len(note) < 8:
            messagebox.showwarning("Review note required",
                                   "Enter a human rationale of at least 8 characters.", parent=self.window)
            return
        selected = [label for label, choice in self.decisions.items() if choice == "record unique pair"]
        merged = [label for label, choice in self.decisions.items() if choice == "reviewed merged-through"]
        if not selected and not merged:
            messagebox.showwarning("No candidates selected",
                                   "Select at least one reviewed candidate before creating a record plan.",
                                   parent=self.window)
            return
        if self.on_create_plan(self.report_path, self.report, self.report_sha256,
                               selected, merged, note):
            self.close()

    def close(self) -> None:
        if self.window:
            self.window.destroy()
            self.window = None
