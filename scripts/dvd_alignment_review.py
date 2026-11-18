#!/usr/bin/env python3
"""In-app visual review of a candidate DVD/LD alignment path and paired samples."""
from __future__ import annotations

import json
import hashlib
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

from fileutil import sha256_file
from workflow_app_model import WorkflowAppError


class DvdAlignmentReviewWindow:
    """Plot candidate offsets, show paired frames, and animate short A/B samples."""

    def __init__(self, parent: tk.Misc, index_path: Path,
                 on_decision: Callable[[Path, str, str], bool],
                 expected_identity: str,
                 on_rerun: Callable[[dict[str, Any]], bool] | None = None):
        self.parent = parent
        self.index_path = index_path.expanduser()
        self.on_decision = on_decision
        self.on_rerun = on_rerun
        self.expected_identity = expected_identity
        self.window: tk.Toplevel | None = None
        self.index: dict[str, Any] = {}
        self.index_sha256 = ""
        self.points: dict[str, dict[str, Any]] = {}
        self.tree: ttk.Treeview | None = None
        self.graph: tk.Canvas | None = None
        self.image_label: ttk.Label | None = None
        self.animation_label: ttk.Label | None = None
        self.note: tk.Text | None = None
        self._sheet_photo = None
        self._animation_frames: list[Any] = []
        self._animation_index = 0
        self._animation_after: str | None = None
        self.cadence_warning = False
        self.cadence_ack: tk.BooleanVar | None = None
        self.edge_ack: tk.BooleanVar | None = None
        self._load()

    def _checked_file(self, value: str, digest: str, label: str) -> Path:
        path = Path(value).expanduser()
        if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
            raise WorkflowAppError(f"{label} changed or is missing; regenerate the review assets")
        return path.resolve(strict=True)

    def _load(self) -> None:
        if self.index_path.is_symlink() or not self.index_path.is_file():
            raise WorkflowAppError("alignment review index is missing or unsafe")
        try:
            payload = self.index_path.read_bytes()
            self.index_sha256 = hashlib.sha256(payload).hexdigest()
            self.index = json.loads(payload.decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowAppError(f"cannot load alignment review assets: {exc}") from exc
        if (self.index.get("schema") != "dvd-alignment-review-assets-v1" or
                self.index.get("candidate_is_approval") is not False or
                self.index.get("identity") != self.expected_identity):
            raise WorkflowAppError("alignment review assets are not candidate-only evidence for this episode")
        self._checked_file(str(self.index.get("manifest_path") or ""),
                           str(self.index.get("manifest_sha256") or ""), "episode manifest")
        for key, hash_key, label in (
                ("alignment_path", "alignment_sha256", "alignment candidate"),
                ("diagnostics_path", "diagnostics_sha256", "alignment diagnostics"),
                ("master_source", "master_source_sha256", "master source"),
                ("dvd_source", "dvd_source_sha256", "DVD source"),
                ("master_descriptors", "master_descriptors_sha256", "master descriptors"),
                ("master_descriptors_meta", "master_descriptors_meta_sha256", "master descriptor metadata"),
                ("dvd_descriptors", "dvd_descriptors_sha256", "DVD descriptors"),
                ("dvd_descriptors_meta", "dvd_descriptors_meta_sha256", "DVD descriptor metadata")):
            self._checked_file(str(self.index.get(key) or ""), str(self.index.get(hash_key) or ""), label)
        contact = self.index.get("contact_sheet")
        if contact:
            self._checked_file(str(contact), str(self.index.get("contact_sheet_sha256") or ""),
                               "paired contact sheet")
        self.points = {
            f"sample-{row['master_frame']}-{row['offset_frames']}": row
            for row in self.index.get("review_points", [])
            if row.get("asset_status") == "READY"
        }
        if not self.points:
            raise WorkflowAppError("no verified paired-frame samples are available for visual review")
        for row in self.points.values():
            row["_playback_path"] = str(self._checked_file(
                str(row.get("playback_path") or ""),
                str(row.get("playback_sha256") or ""), "paired playback"))

    def show(self) -> None:
        from PIL import Image, ImageTk

        self.window = tk.Toplevel(self.parent)
        self.window.title(f"DVD/LD alignment review — {self.expected_identity}")
        self.window.geometry("1180x900")
        self.window.minsize(950, 720)
        self.window.transient(self.parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(self.window, padding=10)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Candidate DVD/LD alignment — {self.expected_identity}",
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(body, text=str(self.index.get("warning") or ""),
                  wraplength=1140, justify="left").pack(anchor="w", pady=(3, 6))
        self.range_edge_hit = any(row.get("search_edge") for row in self.index.get("path", []))
        if self.range_edge_hit:
            ttk.Label(
                body, text="Candidate path touches the configured search boundary. Widen/review the search before approval.",
                foreground="#8b1a1a", wraplength=1140).pack(anchor="w", pady=(0, 5))
            self.edge_ack = tk.BooleanVar(master=self.window, value=False)
            ttk.Checkbutton(
                body, text="I reviewed the edge samples and approve only the sampled, bounded candidate path.",
                variable=self.edge_ack).pack(anchor="w", pady=(0, 5))
        summary = self.index.get("descriptor_summary") or {}
        timing_lines = []
        cadence_warning = False
        for role in ("master", "dvd"):
            row = summary.get(role) or {}
            r_rate, avg_rate = row.get("ffprobe_r_frame_rate"), row.get("ffprobe_avg_frame_rate")
            timing_lines.append(
                f"{role}: {row.get('frames', 'unavailable')} descriptors, grid "
                f"{row.get('output_grid_fps', 'unavailable')} fps; source r/avg={r_rate}/{avg_rate}")
            if r_rate and avg_rate and r_rate != avg_rate:
                cadence_warning = True
        self.cadence_warning = cadence_warning
        ttk.Label(body, text="  |  ".join(timing_lines), wraplength=1140,
                  foreground="#8b1a1a" if cadence_warning else "#333333").pack(anchor="w", pady=(0, 5))
        if cadence_warning:
            self.cadence_ack = tk.BooleanVar(master=dialog, value=False)
            ttk.Checkbutton(
                body, text="I reviewed the source r_frame_rate/avg_frame_rate mismatch and will treat this path as provisional.",
                variable=self.cadence_ack).pack(anchor="w", pady=(0, 5))
        plot_frame = ttk.LabelFrame(body, text="Continuity-constrained candidate path", padding=4)
        plot_frame.pack(fill="x", pady=(0, 6))
        self.graph = tk.Canvas(plot_frame, height=150, background="white", highlightthickness=0)
        self.graph.pack(fill="x", expand=True)
        self.graph.bind("<Configure>", lambda _event: self._draw_graph())
        self._draw_graph()

        middle = ttk.Frame(body)
        middle.pack(fill="both", expand=True)
        left = ttk.Frame(middle)
        left.pack(side="left", fill="both", expand=True)
        columns = ("time", "offset", "ncc", "margin", "edge", "reason")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", height=8)
        for column, title, width in (
                ("time", "Master time / frame", 145), ("offset", "DVD − master", 100),
                ("ncc", "Match score", 90), ("margin", "Local margin", 90),
                ("edge", "Search edge", 90), ("reason", "Why sampled", 280)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True)
        for key, row in self.points.items():
            self.tree.insert("", "end", iid=key, values=(
                f"{row['master_time_s']:.3f}s / f{row['master_frame']}",
                f"{row['offset_frames']:+d} frames", f"{row['ncc']:.3f}",
                f"{row['local_margin']:.3f}", "yes" if row["search_edge"] else "no",
                "; ".join(row["reasons"])))
        self.tree.bind("<<TreeviewSelect>>", self._selected)

        right = ttk.Frame(middle, padding=(8, 0, 0, 0))
        right.pack(side="right", fill="both")
        ttk.Label(right, text="Paired contact sheet").pack(anchor="w")
        self.image_label = ttk.Label(right)
        self.image_label.pack(fill="both", expand=True)
        if self.index.get("contact_sheet"):
            with Image.open(self.index["contact_sheet"]) as image:
                image.thumbnail((500, 410), Image.Resampling.LANCZOS)
                self._sheet_photo = ImageTk.PhotoImage(image.copy())
            self.image_label.configure(image=self._sheet_photo)

        playback = ttk.LabelFrame(body, text="Short paired playback sample", padding=5)
        playback.pack(fill="x", pady=(6, 0))
        self.animation_label = ttk.Label(playback, text="Select a sample row and press Play.")
        self.animation_label.pack(anchor="center")
        controls = ttk.Frame(playback)
        controls.pack(fill="x", pady=(4, 0))
        ttk.Button(controls, text="Play selected", command=self._play_selected).pack(side="left")
        ttk.Button(controls, text="Stop", command=self._stop_animation).pack(side="left", padx=5)

        ttk.Label(body, text="Visual review note (required to record either decision):").pack(
            anchor="w", pady=(8, 0))
        self.note = tk.Text(body, height=3, wrap="word")
        self.note.pack(fill="x")
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(6, 0))
        ttk.Button(actions, text="Reject candidate", command=lambda: self._decide("REJECTED")).pack(
            side="right", padx=(6, 0))
        ttk.Button(actions, text="Approve alignment", command=lambda: self._decide("APPROVED")).pack(
            side="right")
        if self.on_rerun:
            ttk.Button(actions, text="Rebuild / widen search…",
                       command=self._rerun_search).pack(side="right", padx=6)
        ttk.Button(actions, text="Close without decision", command=self.close).pack(side="left")
        self.window.grab_set()
        self.window.focus_set()
        if self.tree.get_children():
            self.tree.selection_set(self.tree.get_children()[0])
            self._selected()

    def _draw_graph(self) -> None:
        if not self.graph:
            return
        canvas = self.graph
        canvas.delete("all")
        rows = self.index.get("path") or []
        if len(rows) < 2:
            canvas.create_text(12, 20, anchor="nw", text="No plotted alignment samples")
            return
        width, height = max(100, canvas.winfo_width()), max(80, canvas.winfo_height())
        left, right, top, bottom = 54, width - 14, 12, height - 28
        xs = [float(row["master_time_s"]) for row in rows]
        ys = [float(row["offset_frames"]) for row in rows]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        if x0 == x1:
            x1 = x0 + 1
        if y0 == y1:
            y1 = y0 + 1
        canvas.create_line(left, bottom, right, bottom, fill="#555")
        canvas.create_line(left, top, left, bottom, fill="#555")
        canvas.create_text(4, top, anchor="nw", text="offset\nframes", fill="#333")
        canvas.create_text(left, height - 3, anchor="sw", text=f"{x0:.0f}s")
        canvas.create_text(right, height - 3, anchor="se", text=f"{x1:.0f}s")
        canvas.create_text(4, bottom, anchor="sw", text=f"{y0:.0f}")
        canvas.create_text(4, top, anchor="nw", text=f"{y1:.0f}")
        warn = float((self.index.get("parameters") or {}).get("warn_ncc", 0.5))
        points = []
        for row in rows:
            x = left + (float(row["master_time_s"]) - x0) / (x1 - x0) * (right - left)
            y = bottom - (float(row["offset_frames"]) - y0) / (y1 - y0) * (bottom - top)
            points.append((x, y))
        for first, second in zip(points, points[1:]):
            canvas.create_line(*first, *second, fill="#286090", width=2)
        for point, row in zip(points, rows):
            color = "#c0392b" if float(row["ncc"]) < warn else (
                "#d97706" if row["search_edge"] else "#198754")
            radius = 3
            canvas.create_oval(point[0] - radius, point[1] - radius,
                               point[0] + radius, point[1] + radius,
                               fill=color, outline="")

    def _selected(self, _event=None) -> None:
        if not self.tree or not self.animation_label:
            return
        selection = self.tree.selection()
        if not selection:
            return
        row = self.points.get(selection[0])
        if row:
            self.animation_label.configure(
                text=f"Selected: master f{row['master_frame']} ↔ DVD f{row['dvd_frame']} "
                     f"(candidate offset {row['offset_frames']:+d})")

    def _play_selected(self) -> None:
        if not self.tree or not self.animation_label:
            return
        selection = self.tree.selection()
        if not selection:
            return
        from PIL import Image, ImageSequence, ImageTk
        row = self.points[selection[0]]
        try:
            with Image.open(row["_playback_path"]) as image:
                frames = [ImageTk.PhotoImage(frame.convert("RGB").copy())
                          for frame in ImageSequence.Iterator(image)]
        except OSError as exc:
            messagebox.showerror("Playback unavailable", str(exc), parent=self.window)
            return
        if not frames:
            return
        self._stop_animation()
        self._animation_frames = frames
        self._animation_index = 0
        self._tick_animation()

    def _tick_animation(self) -> None:
        if not self.window or not self.animation_label or not self._animation_frames:
            return
        self.animation_label.configure(image=self._animation_frames[self._animation_index], text="")
        self._animation_index = (self._animation_index + 1) % len(self._animation_frames)
        self._animation_after = self.window.after(100, self._tick_animation)

    def _stop_animation(self) -> None:
        if self.window and self._animation_after:
            try:
                self.window.after_cancel(self._animation_after)
            except tk.TclError:
                pass
        self._animation_after = None
        self._animation_frames = []

    def _decide(self, decision: str) -> None:
        if not self.note or self.window is None:
            return
        if decision == "APPROVED" and self.cadence_warning and (
                self.cadence_ack is None or not self.cadence_ack.get()):
            messagebox.showwarning(
                "Frame-rate review required",
                "Review and acknowledge the source frame-rate mismatch before approving this candidate path.",
                parent=self.window)
            return
        if decision == "APPROVED" and self.range_edge_hit and (
                self.edge_ack is None or not self.edge_ack.get()):
            messagebox.showwarning(
                "Search-edge review required",
                "Review the edge samples, widen/re-run the search if appropriate, or explicitly acknowledge "
                "that approval covers only this bounded candidate path.",
                parent=self.window)
            return
        note = self.note.get("1.0", "end").strip()
        if len(note) < 8:
            messagebox.showwarning("Review note required",
                                   "Enter a review rationale of at least 8 characters.", parent=self.window)
            return
        if decision == "APPROVED" and not messagebox.askyesno(
                "Approve candidate alignment",
                "This records the exact candidate-path hash as visually approved. "
                "It does not approve retime offsets, steps, or audio origin. Continue?",
                parent=self.window):
            return
        rate_acknowledged = (not self.cadence_warning or
                             (self.cadence_ack is not None and self.cadence_ack.get()))
        if self.on_decision(self.index_path, decision, note,
                            self.index_sha256, rate_acknowledged):
            self.close()

    def _rerun_search(self) -> None:
        if not self.on_rerun:
            return
        parameters = self.index.get("parameters") or {}
        if self.on_rerun({
                "min_offset": parameters.get("min_offset_frames", -3000),
                "max_offset": parameters.get("max_offset_frames", 500),
                "sample_step": parameters.get("sample_step_frames", 15),
                "warn_ncc": parameters.get("warn_ncc", 0.5),
        }):
            self.close()

    def close(self) -> None:
        self._stop_animation()
        if self.window:
            try:
                self.window.grab_release()
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None
