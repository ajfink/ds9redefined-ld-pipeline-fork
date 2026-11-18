#!/usr/bin/env python3
"""In-app review of verified TFM metrics and cadence visual samples."""
from __future__ import annotations

import hashlib
import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

from fileutil import sha256_file
from workflow_app_model import WorkflowAppError


class CadenceReviewWindow:
    def __init__(self, parent: tk.Misc, index_path: Path, expected_identity: str,
                 on_decision: Callable[[Path, str, dict[str, str], str], bool]):
        self.parent = parent
        self.index_path = index_path
        self.expected_identity = expected_identity
        self.on_decision = on_decision
        self.window: tk.Toplevel | None = None
        self.index: dict[str, Any] = {}
        self.index_sha256 = ""
        self.metrics: dict[str, Any] = {}
        self.rows: dict[str, dict[str, Any]] = {}
        self.decisions: dict[str, str] = {}
        self.tree: ttk.Treeview | None = None
        self.detail: ttk.Label | None = None
        self.visual: ttk.Label | None = None
        self.note: tk.Text | None = None
        self.approve_button: ttk.Button | None = None
        self._frames: list[Any] = []
        self._frame_index = 0
        self._after: str | None = None
        self.rate_mismatch = False
        self.rate_ack: tk.BooleanVar | None = None
        self._load()

    @staticmethod
    def _verified_file(value: str, digest: str, label: str) -> Path:
        path = Path(value).expanduser()
        if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
            raise WorkflowAppError(f"{label} changed or is missing; regenerate cadence evidence")
        return path.resolve(strict=True)

    def _load(self) -> None:
        if self.index_path.is_symlink() or not self.index_path.is_file():
            raise WorkflowAppError("cadence visual-review index is missing or unsafe")
        try:
            payload = self.index_path.read_bytes()
            self.index_sha256 = hashlib.sha256(payload).hexdigest()
            self.index = json.loads(payload.decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowAppError(f"cannot read cadence visual-review index: {exc}") from exc
        if (self.index.get("schema") != "cadence-review-assets-v1" or
                self.index.get("identity") != self.expected_identity or
                self.index.get("candidate_is_approval") is not False):
            raise WorkflowAppError("cadence review assets are not candidate-only evidence for this episode")
        for label, key, hash_key in (
                ("manifest", "manifest_path", "manifest_sha256"),
                ("metrics", "metrics_path", "metrics_sha256"),
                ("coverage report", "verification_report_path", "verification_report_sha256"),
                ("review AVS", "review_avs_path", "review_avs_sha256"),
                ("map script", "map_script_path", "map_script_sha256"),
                ("master source", "source_path", "source_sha256"),
                ("native LD audio", "native_audio_path", "native_audio_sha256"),
                ("motion trace verification", "motion_report_path", "motion_report_sha256"),
                ("TFM log", "tfm_log_path", "tfm_log_sha256"),
                ("motion trace", "motion_trace_path", "motion_trace_sha256"),
                ("deinterlace map", "di_map_path", "di_map_sha256")):
            self._verified_file(str(self.index.get(key) or ""),
                                str(self.index.get(hash_key) or ""), label)
        if self.index.get("contact_sheet"):
            self._verified_file(str(self.index["contact_sheet"]),
                                str(self.index.get("contact_sheet_sha256") or ""),
                                "cadence contact sheet")
        metrics_path = Path(str(self.index["metrics_path"]))
        self.metrics = json.loads(metrics_path.read_text(encoding="utf-8-sig"))
        intervals = list(self.index.get("review_intervals") or [])
        if not intervals:
            raise WorkflowAppError("no visual intervals/samples were generated for cadence review")
        self.rows = {str(row["id"]): row for row in intervals}
        if len(self.rows) != len(intervals):
            raise WorkflowAppError("cadence sample identifiers are missing or duplicated")
        for row in self.rows.values():
            self._verified_file(str(row.get("playback_path") or ""),
                                str(row.get("playback_sha256") or ""),
                                "cadence playback sample")

    def show(self) -> None:
        from PIL import Image, ImageTk

        self.window = tk.Toplevel(self.parent)
        self.window.title(f"Cadence visual review — {self.expected_identity}")
        self.window.geometry("1160x850")
        self.window.minsize(900, 700)
        self.window.transient(self.parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(self.window, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Verified cadence evidence — {self.expected_identity}",
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(body, text=str(self.index.get("warning") or ""),
                  wraplength=1120).pack(anchor="w", pady=(3, 6))
        summary = (
            f"TFM frames {self.metrics.get('frame_count', '—')}  |  "
            f"period-5 agreement {self.metrics.get('period5_agreement_pct', 0):.2f}%  |  "
            f"unmatched {self.metrics.get('unmatched_count', 0)}  |  "
            f"combed {self.metrics.get('combed_count', 0)}  |  "
            f"inserts {self.metrics.get('insert_count', 0)}  |  "
            f"repair set {self.metrics.get('repair_frame_count', 0)} frames")
        ttk.Label(body, text=summary, wraplength=1120).pack(anchor="w", pady=(0, 6))
        rate_info = self.index.get("source_rate_info") or {}
        tfm_rate = rate_info.get("tfm") or {}
        motion_rate = rate_info.get("motion") or {}
        source_rate = tfm_rate.get("r_frame_rate")
        average_rate = tfm_rate.get("avg_frame_rate")
        motion_nominal, motion_average = (
            motion_rate.get("r_frame_rate"), motion_rate.get("avg_frame_rate"))
        self.rate_mismatch = bool(
            (source_rate and average_rate and source_rate != average_rate) or
            (motion_nominal and motion_average and motion_nominal != motion_average) or
            (source_rate and motion_nominal and source_rate != motion_nominal))
        ttk.Label(
            body,
            text=(f"Native source rate: r_frame_rate={source_rate or 'unavailable'}, "
                  f"avg_frame_rate={average_rate or 'unavailable'}; motion trace "
                  f"r/avg={motion_nominal or 'unavailable'}/{motion_average or 'unavailable'}; "
                  f"FFmpeg-null verified source frames={self.index.get('frame_count', 'unavailable')}"),
            foreground="#8b1a1a" if self.rate_mismatch else "#333333",
            wraplength=1120).pack(anchor="w", pady=(0, 5))
        if self.rate_mismatch:
            self.rate_ack = tk.BooleanVar(master=self.window, value=False)
            ttk.Checkbutton(
                body, text="I reviewed the native source rate mismatch and will treat this cadence map as provisional.",
                variable=self.rate_ack).pack(anchor="w", pady=(0, 5))
        self._draw_period_plot(body)

        main = ttk.Frame(body)
        main.pack(fill="both", expand=True, pady=(6, 0))
        left = ttk.Frame(main)
        left.pack(side="left", fill="both", expand=True)
        self.tree = ttk.Treeview(
            left, columns=("kind", "interval", "reasons", "decision"),
            show="headings", selectmode="browse", height=10)
        for key, title, width in (
                ("kind", "Sample type", 140), ("interval", "TFM frame / time interval", 205),
                ("reasons", "Review reason", 360), ("decision", "Visual decision", 150)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True)
        for sample_id, row in self.rows.items():
            interval = (f"f{row.get('start_frame')}–f{row.get('end_frame_inclusive')}  "
                        f"{float(row.get('start_s', 0)):.2f}–{float(row.get('end_s', 0)):.2f}s")
            self.tree.insert("", "end", iid=sample_id, values=(
                row.get("sample_kind", "review"), interval,
                "; ".join(row.get("reasons") or []), "not reviewed"))
        self.tree.bind("<<TreeviewSelect>>", self._select_sample)

        right = ttk.Frame(main, padding=(8, 0, 0, 0))
        right.pack(side="right", fill="both")
        self.visual = ttk.Label(right, text="Select an interval to inspect its raw/TFM comparison.")
        self.visual.pack(anchor="center")
        self.detail = ttk.Label(right, text="", wraplength=420, justify="left")
        self.detail.pack(fill="x", pady=(5, 0))
        controls = ttk.Frame(right)
        controls.pack(fill="x", pady=(5, 0))
        ttk.Button(controls, text="Play selected comparison", command=self._play).pack(side="left")
        ttk.Button(controls, text="Stop", command=self._stop).pack(side="left", padx=5)

        decisions = ttk.Frame(body)
        decisions.pack(fill="x", pady=(7, 3))
        ttk.Button(decisions, text="Mark reviewed / acceptable",
                   command=self._mark_acceptable).pack(side="left")
        ttk.Button(decisions, text="Mark unresolved",
                   command=self._mark_unresolved).pack(side="left", padx=5)
        ttk.Button(decisions, text="Clear decision",
                   command=self._clear_decision).pack(side="left")
        ttk.Label(body, text="Human review note (required):").pack(anchor="w")
        self.note = tk.Text(body, height=3, wrap="word")
        self.note.pack(fill="x")
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(7, 0))
        ttk.Button(actions, text="Reject / keep cadence blocked",
                   command=lambda: self._decide("REJECTED")).pack(side="right", padx=5)
        self.approve_button = ttk.Button(
            actions, text="Approve cadence for next gate",
            command=lambda: self._decide("APPROVED"), state="disabled")
        self.approve_button.pack(side="right")
        ttk.Button(actions, text="Close without decision", command=self.close).pack(side="left")
        self.window.grab_set()
        self.window.focus_set()
        if self.tree.get_children():
            self.tree.selection_set(self.tree.get_children()[0])
            self._select_sample()

    def _draw_period_plot(self, parent: ttk.Frame) -> None:
        canvas = tk.Canvas(parent, height=112, background="white", highlightthickness=0)
        canvas.pack(fill="x")
        windows = self.metrics.get("windows_30s") or []
        if not windows:
            canvas.create_text(10, 10, anchor="nw", text="No 30-second cadence windows")
            return
        width, height = 1100, 110
        left, right, top, bottom = 52, width - 10, 10, height - 22
        values = [float(row.get("period5_agreement_pct", 0)) for row in windows]
        xstep = (right - left) / max(1, len(values) - 1)
        points = [(left + index * xstep, bottom - (value / 100) * (bottom - top))
                  for index, value in enumerate(values)]
        for a, b in zip(points, points[1:]):
            canvas.create_line(*a, *b, fill="#225ea8", width=2)
        canvas.create_text(4, top, anchor="nw", text="100%")
        canvas.create_text(4, bottom, anchor="sw", text="0%")
        canvas.create_text(left, height - 2, anchor="sw",
                           text=f"{windows[0].get('start_s', 0):.0f}s")
        canvas.create_text(right, height - 2, anchor="se",
                           text=f"{windows[-1].get('end_s', 0):.0f}s")
        for point, row in zip(points, windows):
            if row.get("review_required"):
                canvas.create_oval(point[0] - 3, point[1] - 3,
                                   point[0] + 3, point[1] + 3,
                                   fill="#c0392b", outline="")

    def _selected(self) -> tuple[str, dict[str, Any]] | None:
        if not self.tree:
            return None
        selection = self.tree.selection()
        if not selection:
            return None
        key = selection[0]
        return (key, self.rows[key]) if key in self.rows else None

    def _select_sample(self, _event=None) -> None:
        selected = self._selected()
        if not selected or not self.visual or not self.detail:
            return
        key, row = selected
        from PIL import Image, ImageTk
        path = Path(str(row["playback_path"]))
        with Image.open(path) as image:
            frame = image.convert("RGB")
            frame.thumbnail((640, 280), Image.Resampling.LANCZOS)
            self._photo = ImageTk.PhotoImage(frame)
        self.visual.configure(image=self._photo, text="")
        self.detail.configure(text=(
            f"{row.get('sample_kind')} — source frames {row.get('start_frame')}.."
            f"{row.get('end_frame_inclusive')} ({row.get('start_s', 0):.2f}.."
            f"{row.get('end_s', 0):.2f}s)\n"
            f"Reasons: {'; '.join(row.get('reasons') or [])}\n"
            f"Decision: {self.decisions.get(key, 'not reviewed')}"))

    def _set_decision(self, decision: str) -> None:
        selected = self._selected()
        if not selected:
            return
        key, _row = selected
        self.decisions[key] = decision
        assert self.tree is not None
        values = list(self.tree.item(key, "values"))
        values[-1] = decision
        self.tree.item(key, values=values)
        if self.approve_button:
            self.approve_button.configure(
                state="normal" if len(self.decisions) == len(self.rows) and
                all(value == "accepted" for value in self.decisions.values()) else "disabled")
        self._select_sample()

    def _mark_acceptable(self) -> None:
        self._set_decision("accepted")

    def _mark_unresolved(self) -> None:
        self._set_decision("unresolved")

    def _clear_decision(self) -> None:
        selected = self._selected()
        if selected:
            key = selected[0]
            self.decisions.pop(key, None)
            if self.tree:
                values = list(self.tree.item(key, "values"))
                values[-1] = "not reviewed"
                self.tree.item(key, values=values)
            if self.approve_button:
                self.approve_button.configure(state="disabled")
            self._select_sample()

    def _play(self) -> None:
        selected = self._selected()
        if not selected:
            return
        from PIL import Image, ImageSequence, ImageTk
        path = Path(str(selected[1]["playback_path"]))
        if sha256_file(path) != selected[1].get("playback_sha256"):
            messagebox.showerror("Cadence sample changed", "Rebuild the visual evidence before reviewing.", parent=self.window)
            return
        with Image.open(path) as image:
            self._frames = [ImageTk.PhotoImage(frame.convert("RGB").copy())
                            for frame in ImageSequence.Iterator(image)]
        self._stop()
        self._frame_index = 0
        self._tick()

    def _tick(self) -> None:
        if not self.window or not self.visual or not self._frames:
            return
        self.visual.configure(image=self._frames[self._frame_index], text="")
        self._frame_index = (self._frame_index + 1) % len(self._frames)
        self._after = self.window.after(100, self._tick)

    def _stop(self) -> None:
        if self.window and self._after:
            try:
                self.window.after_cancel(self._after)
            except tk.TclError:
                pass
        self._after = None
        self._frames = []

    def _decide(self, decision: str) -> None:
        if not self.note or not self.window:
            return
        note = self.note.get("1.0", "end").strip()
        if len(note) < 8:
            messagebox.showwarning("Review note required",
                                   "Enter a cadence review note of at least 8 characters.", parent=self.window)
            return
        if decision == "APPROVED" and self.rate_mismatch and (
                self.rate_ack is None or not self.rate_ack.get()):
            messagebox.showwarning(
                "Frame-rate review required",
                "Review and acknowledge the source frame-rate metadata mismatch before approving cadence evidence.",
                parent=self.window)
            return
        if decision == "APPROVED" and (
                len(self.decisions) != len(self.rows) or
                any(value != "accepted" for value in self.decisions.values())):
            messagebox.showwarning(
                "Cadence intervals unresolved",
                "Review and mark every flagged/clean sample acceptable, or reject and keep cadence blocked.",
                parent=self.window)
            return
        source_rate_acknowledged = (not self.rate_mismatch or
                                    (self.rate_ack is not None and self.rate_ack.get()))
        if self.on_decision(self.index_path, decision, self.decisions, note,
                            self.index_sha256, source_rate_acknowledged):
            self.close()

    def close(self) -> None:
        self._stop()
        if self.window:
            try:
                self.window.grab_release()
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None
