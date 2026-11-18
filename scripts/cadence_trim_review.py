#!/usr/bin/env python3
"""Visual review UI for already measured output-domain head/tail trims."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

from fileutil import sha256_file
from workflow_app_model import WorkflowAppError


class CadenceTrimReviewWindow:
    """Show paired endpoint evidence; accept explicit 23.976-output-frame values only."""

    def __init__(self, parent: tk.Misc, context: dict[str, Any],
                 on_record: Callable[[int, int, str, Path | None, str], bool]):
        self.parent = parent
        self.context = context
        self.on_record = on_record
        self.window: tk.Toplevel | None = None
        self.head_trim = tk.StringVar(value="")
        self.tail_trim = tk.StringVar(value="")
        self.review_ack = tk.BooleanVar(value=False)
        self.note: tk.Text | None = None
        self.head_frames: list[Any] = []
        self.tail_frames: list[Any] = []
        self.head_index = 0
        self.tail_index = 0
        self.head_after: str | None = None
        self.tail_after: str | None = None
        self.head_label: ttk.Label | None = None
        self.tail_label: ttk.Label | None = None
        self.head_photo = None
        self.tail_photo = None
        self.review_assets_path: Path | None = None
        self.review_assets_sha256 = ""
        self.dvd_available = False
        self._load_context()
        video = context["manifest_data"].get("video") or {}
        if video.get("head_trim") is not None:
            self.head_trim.set(str(video["head_trim"]))
        if video.get("tail_trim") is not None:
            self.tail_trim.set(str(video["tail_trim"]))

    def _load_context(self) -> None:
        data = self.context["manifest_data"]
        video = data.get("video") or {}
        self.dvd_available = data.get("dvd_reference_available") is True
        pad = (data.get("audio") or {}).get("pad_frames")
        if type(pad) is not int or pad < 0 or not (data.get("audio") or {}).get("capture"):
            raise WorkflowAppError("verified audio.pad_frames is required before reviewing output trims")
        review = video.get("dvd_alignment_review") or {}
        if self.dvd_available:
            if review.get("status") != "APPROVED":
                raise WorkflowAppError("DVD-backed trim review requires the visually approved DVD/LD map")
            raw = str(review.get("review_assets_path") or "")
            path = Path(raw).expanduser()
            if path.is_symlink():
                raise WorkflowAppError("approved paired boundary assets may not be a symbolic link")
            if not path.is_absolute():
                if not path.parts or path.parts[0].casefold() != "probe" or ".." in path.parts:
                    raise WorkflowAppError("approved alignment review assets have an unsafe path")
                path = Path(self.context["probe"]).joinpath(*path.parts[1:])
            path = path.resolve(strict=True)
            if path.is_symlink() or not path.is_file():
                raise WorkflowAppError("approved paired boundary samples are missing or unsafe")
            digest = sha256_file(path)
            if digest != review.get("review_assets_sha256"):
                raise WorkflowAppError("paired boundary samples changed after alignment approval")
            try:
                index = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WorkflowAppError(f"cannot read approved boundary samples: {exc}") from exc
            if index.get("identity") != self.context["identity"]:
                raise WorkflowAppError("boundary samples belong to a different episode")
            if index.get("manifest_sha256") != sha256_file(self.context["manifest"]):
                raise WorkflowAppError("manifest changed after boundary samples were built")
            reasons = {
                reason for row in index.get("review_points", [])
                for reason in row.get("reasons", [])
            }
            required = {"measured DVD-origin neighborhood", "measured content-end neighborhood"}
            if not required.issubset(reasons):
                raise WorkflowAppError("paired review assets do not cover both measured episode boundaries")
            self.review_assets_path = path
            self.review_assets_sha256 = digest
            self._head_row = self._nearest(index, "measured DVD-origin neighborhood")
            self._tail_row = self._nearest(index, "measured content-end neighborhood")
        elif data.get("no_dvd_proceed_approved") is not True:
            raise WorkflowAppError("no-DVD trim review requires the explicit no-DVD route choice")

    @staticmethod
    def _nearest(index: dict[str, Any], reason: str) -> dict[str, Any]:
        rows = [row for row in index.get("review_points", [])
                if reason in row.get("reasons", []) and row.get("asset_status") == "READY"]
        if not rows:
            raise WorkflowAppError(f"no paired playback sample is available for {reason}")
        return rows[0]

    def show(self) -> None:
        self.window = tk.Toplevel(self.parent)
        self.window.title(f"Visual head/tail trim review — {self.context['identity']}")
        self.window.geometry("1050x800")
        self.window.minsize(850, 680)
        self.window.transient(self.parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(self.window, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Review already measured trims — {self.context['identity']}",
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")
        dvd = (self.context["manifest_data"].get("dvd_reference_available") is True)
        instruction = (
            "Compare the paired DVD/LD boundary samples and the independently reviewed audio fade/envelope evidence. "
            "Enter head_trim and tail_trim as already measured 23.976-output-frame counts. "
            "Do not convert DVD frame numbers, source frames, or audio samples here; preserve the separately measured "
            "DVD picture/audio lead or lag."
            if dvd else
            "No DVD reference is available. Visually review the LD master start/end in a player, then enter already "
            "measured 23.976-output-frame head/tail trims. No DVD correspondence or absolute A/V claim is made.")
        ttk.Label(body, text=instruction, wraplength=1000, justify="left").pack(anchor="w", pady=(4, 8))
        video = self.context["manifest_data"].get("video") or {}
        ttk.Label(
            body, text=(f"Measured program timeline: delta_s={video.get('delta_s', 'unavailable')} s; "
                        f"duration_s={video.get('duration_s', 'unavailable')} s; "
                        f"video.frames={video.get('frames', 'unavailable')} output frames"),
            wraplength=1000).pack(anchor="w", pady=(0, 5))

        panels = ttk.Frame(body)
        panels.pack(fill="both", expand=True)
        self.head_label = None
        self.tail_label = None
        for column, (title, label, row_name) in enumerate((
                ("Program start / head boundary", "head", "_head_row"),
                ("Program end / tail boundary", "tail", "_tail_row"))):
            pane = ttk.LabelFrame(panels, text=title, padding=6)
            pane.grid(row=0, column=column, sticky="nsew", padx=4)
            image_label = ttk.Label(pane, text=f"{label.title()}-boundary evidence unavailable")
            image_label.pack(expand=True)
            if label == "head":
                self.head_label = image_label
            else:
                self.tail_label = image_label
            if dvd:
                row = getattr(self, row_name)
                self._load_gif(row, image_label, head=(column == 0))
                ttk.Label(
                    pane,
                    text=(f"Master f{row['master_frame']} ↔ DVD f{row['dvd_frame']}  |  "
                          f"candidate offset {row['offset_frames']:+d} frames  |  "
                          f"NCC {row['ncc']:.3f}"),
                    wraplength=470).pack(anchor="w")
            else:
                ttk.Label(pane, text="Source-only visual review; no DVD sample is available.",
                          wraplength=470).pack(anchor="w")
        panels.columnconfigure(0, weight=1)
        panels.columnconfigure(1, weight=1)
        if not dvd:
            ttk.Button(body, text="Open LD master in video player…",
                       command=self._open_master).pack(anchor="w", pady=(6, 0))

        form = ttk.Frame(body)
        form.pack(fill="x", pady=(8, 0))
        ttk.Label(form, text="Head trim (23.976 output frames):").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.head_trim, width=16).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(form, text="Tail trim (23.976 output frames):").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.tail_trim, width=16).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Checkbutton(
            body,
            text=(
                "I independently adjudicated both DVD/LD picture endpoints and considered the separate audio fade/envelope "
                "while preserving measured relative A/V offset; values are output-frame counts."
                if dvd else
                "I independently reviewed both LD picture endpoints and the selected no-DVD duration/PAD evidence; "
                "this does not claim external absolute A/V alignment."),
            variable=self.review_ack, wraplength=1000).pack(anchor="w", pady=(8, 4))
        audition = ttk.Frame(body)
        audition.pack(fill="x", pady=(3, 5))
        ttk.Button(audition, text="Audition native LD audio",
                   command=lambda: self._open_media("ld_native")).pack(side="left")
        if dvd:
            ttk.Button(audition, text="Open DVD reference playback",
                       command=lambda: self._open_media("dvd")).pack(side="left", padx=6)
        ttk.Label(body, text="Human review note (required):").pack(anchor="w")
        self.note = tk.Text(body, height=3, wrap="word")
        self.note.pack(fill="x", pady=(2, 8))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Cancel", command=self.close).pack(side="right", padx=5)
        ttk.Button(buttons, text="Record reviewed trim values…",
                   command=self._record).pack(side="right")
        self.window.grab_set()
        self.window.focus_set()

    def _load_gif(self, row: dict[str, Any], label: ttk.Label, *, head: bool) -> None:
        from PIL import Image, ImageSequence, ImageTk
        path = Path(str(row["playback_path"]))
        if sha256_file(path) != row.get("playback_sha256"):
            raise WorkflowAppError("boundary GIF changed after visual alignment review")
        with Image.open(path) as image:
            frames = [ImageTk.PhotoImage(frame.convert("RGB").copy())
                      for frame in ImageSequence.Iterator(image)]
        if head:
            self.head_frames = frames
            self._tick_head()
        else:
            self.tail_frames = frames
            self._tick_tail()

    def _tick_head(self) -> None:
        if not self.window or not self.head_label or not self.head_frames:
            return
        self.head_label.configure(image=self.head_frames[self.head_index])
        self.head_index = (self.head_index + 1) % len(self.head_frames)
        self.head_after = self.window.after(120, self._tick_head)

    def _tick_tail(self) -> None:
        if not self.window or not self.tail_label or not self.tail_frames:
            return
        self.tail_label.configure(image=self.tail_frames[self.tail_index])
        self.tail_index = (self.tail_index + 1) % len(self.tail_frames)
        self.tail_after = self.window.after(120, self._tick_tail)

    def _open_master(self) -> None:
        self._open_media("stack_master")

    def _open_media(self, key: str) -> None:
        path = Path(str((self.context.get("file_paths") or {}).get(key) or ""))
        if not path.is_file():
            messagebox.showwarning("Media unavailable", f"The selected {key} source is missing.",
                                   parent=self.window)
            return
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("Could not open LD master", str(exc), parent=self.window)

    def _record(self) -> None:
        if not self.note or not self.window:
            return
        try:
            head, tail = int(self.head_trim.get()), int(self.tail_trim.get())
        except ValueError:
            messagebox.showwarning("Measured trims required",
                                   "Enter nonnegative measured output-frame counts.", parent=self.window)
            return
        if head < 0 or tail < 0 or not self.review_ack.get():
            messagebox.showwarning("Visual trim adjudication required",
                                   "Enter nonnegative frame counts and acknowledge the separate picture/audio review.",
                                   parent=self.window)
            return
        note = self.note.get("1.0", "end").strip()
        if len(note) < 8:
            messagebox.showwarning("Review note required",
                                   "Enter a review rationale of at least 8 characters.", parent=self.window)
            return
        if not messagebox.askyesno(
                "Record visually adjudicated trims",
                f"Record head_trim={head} and tail_trim={tail} in the 23.976 output-frame domain? "
                "No conversion or trim value is inferred by the app.",
                parent=self.window):
            return
        if self.on_record(head, tail, note, self.review_assets_path, self.review_assets_sha256):
            self.close()

    def close(self) -> None:
        if self.window:
            for token in (self.head_after, self.tail_after):
                if token:
                    try:
                        self.window.after_cancel(token)
                    except tk.TclError:
                        pass
            self.window.destroy()
            self.window = None
