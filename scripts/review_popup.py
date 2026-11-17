#!/usr/bin/env python3
"""Modal, evidence-focused review UI for a workflow human-review gate."""
from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

from PIL import Image, ImageTk

IMAGE_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
VIDEO_SUFFIXES = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ts", ".webm"}


class HumanReviewPopup:
    """Show declared review evidence and collect an explicit approve/deny choice."""
    def __init__(self, parent: tk.Misc, step: dict,
                 approve: Callable[[], bool], deny: Callable[[], bool], *,
                 decision_enabled: bool = True,
                 get_help: Callable[[], bool] | None = None,
                 continue_help: Callable[[dict], dict[str, Any]] | None = None):
        self.parent = parent
        self.step = step
        self.approve_callback = approve
        self.deny_callback = deny
        self.decision_enabled = decision_enabled
        self.get_help_callback = get_help
        self.continue_help_callback = continue_help
        self.window: tk.Toplevel | None = None
        self.images: list[Path] = []
        self.image_index = 0
        self.photo: ImageTk.PhotoImage | None = None
        self.help_button: ttk.Button | None = None
        self.help_status = tk.StringVar()
        self.approve_button: ttk.Button | None = None
        self.deny_button: ttk.Button | None = None
        self.help_result: dict[str, Any] | None = None

    @staticmethod
    def _path(value: str) -> Path:
        return Path(os.path.expandvars(os.path.expanduser(value))).resolve(strict=False)

    def show(self) -> None:
        inputs = self.step.get("inputs") or []
        paths = [self._path(str(value)) for value in inputs]
        self.images = [path for path in paths if path.suffix.casefold() in IMAGE_SUFFIXES and path.is_file()]
        self.window = tk.Toplevel(self.parent)
        self.window.title(f"Human review — {self.step.get('id', 'review')}")
        self.window.geometry("1120x780")
        self.window.minsize(760, 560)
        self.window.transient(self.parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Left>", lambda _event: self.previous_image())
        self.window.bind("<Right>", lambda _event: self.next_image())

        body = ttk.Frame(self.window, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Review gate: {self.step.get('id', '')}",
                  font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(0, 6))
        ttk.Label(body, text=str(self.step.get("prompt", "Review the declared evidence.")),
                  wraplength=1060, justify="left").pack(fill="x", anchor="w", pady=(0, 10))

        if self.images:
            image_box = ttk.LabelFrame(body, text="Image evidence", padding=6)
            image_box.pack(fill="both", expand=True, pady=(0, 8))
            self.image_title = tk.StringVar()
            ttk.Label(image_box, textvariable=self.image_title, anchor="center").pack(fill="x", pady=(0, 4))
            self.image_label = ttk.Label(image_box, anchor="center")
            self.image_label.pack(fill="both", expand=True)
            image_controls = ttk.Frame(image_box)
            image_controls.pack(fill="x", pady=(4, 0))
            self.previous_button = ttk.Button(image_controls, text="← Previous image",
                                              command=self.previous_image)
            self.previous_button.pack(side="left")
            self.image_position = tk.StringVar()
            ttk.Label(image_controls, textvariable=self.image_position).pack(side="left", expand=True)
            self.next_button = ttk.Button(image_controls, text="Next image →",
                                          command=self.next_image)
            self.next_button.pack(side="right")
            self.render_image()

        evidence = ttk.LabelFrame(body, text="Evidence paths", padding=6)
        evidence.pack(fill="x", pady=(0, 8))
        if not paths:
            ttk.Label(evidence, text="This review gate declares no evidence paths.").pack(anchor="w")
        for path in paths:
            self._add_evidence_row(evidence, path)

        ttk.Label(body,
                  text="Denying rejects this workflow job. After correcting or reconciling evidence, create a new plan.",
                  wraplength=1060).pack(anchor="w", pady=(0, 8))
        help_row = ttk.Frame(body)
        help_row.pack(fill="x", pady=(0, 8))
        self.help_button = ttk.Button(help_row, text="Get OpenCode help", command=self._help)
        self.help_button.pack(side="left")
        ttk.Label(help_row, textvariable=self.help_status, wraplength=840).pack(
            side="left", fill="x", expand=True, padx=(8, 0))
        if not self.get_help_callback or not self.decision_enabled:
            self.help_button.configure(state="disabled")
        if not self.decision_enabled:
            self.help_status.set("Run or resume this plan until the selected review gate is waiting.")
        actions = ttk.Frame(body)
        actions.pack(fill="x")
        ttk.Button(actions, text="Cancel", command=self.close).pack(side="right", padx=(6, 0))
        self.deny_button = ttk.Button(actions, text="Deny review", command=self._deny,
                                      state="normal" if self.decision_enabled else "disabled")
        self.deny_button.pack(side="right", padx=(6, 0))
        self.approve_button = ttk.Button(actions, text="Approve and resume", command=self._approve,
                                         state="normal" if self.decision_enabled else "disabled")
        self.approve_button.pack(side="right")
        self.window.grab_set()
        self.window.focus_set()

    def help_started(self) -> None:
        self.help_result = None
        if self.help_button:
            self.help_button.configure(text="OpenCode is working...", state="disabled")
        if self.approve_button:
            self.approve_button.configure(state="disabled")
        if self.deny_button:
            self.deny_button.configure(state="disabled")
        self.help_status.set("OpenCode is open in a separate terminal. This review will stay paused.")

    def help_finished(self, result: dict[str, Any]) -> None:
        self.help_result = result
        if self.help_button:
            label = "OpenCode failed — continue" if result.get("help_failed") else "AI fixed it, continue"
            self.help_button.configure(text=label, state="normal")
        changes = result.get("runtime_code_changes") or []
        tests = [name for name in (result.get("source_changes") or [])
                 if name.casefold().startswith("tests/")]
        protected = result.get("protected_evidence_changed") or []
        status = f"Audit: {result.get('audit_directory', '')}"
        if result.get("requires_new_plan"):
            status += " | changes detected; current evidence may be stale"
        elif changes:
            status += f" | runtime changes: {len(changes)}; current evidence is stale"
        elif protected:
            status += " | protected evidence changed"
        elif tests:
            status += f" | test files changed: {len(tests)}"
        else:
            status += " | review the OpenCode notes, then choose approve or deny"
        self.help_status.set(status)

    def _help(self) -> None:
        if self.help_result is not None:
            if not self.continue_help_callback:
                return
            outcome = self.continue_help_callback(self.help_result)
            self.help_status.set(str(outcome.get("message", "")))
            can_approve = bool(outcome.get("can_approve"))
            can_deny = bool(outcome.get("can_deny"))
            can_help = bool(outcome.get("can_help", can_approve or can_deny))
            if self.approve_button:
                self.approve_button.configure(state="normal" if can_approve else "disabled")
            if self.deny_button:
                self.deny_button.configure(state="normal" if can_deny else "disabled")
            if self.help_button:
                self.help_button.configure(text=("Get OpenCode help" if can_help
                                                 else "OpenCode changes logged"),
                                           state="normal" if can_help else "disabled")
            self.help_result = None
            return
        if self.get_help_callback and self.get_help_callback():
            self.help_started()

    def _add_evidence_row(self, parent: ttk.Frame, path: Path) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        exists = path.exists()
        is_video = path.suffix.casefold() in VIDEO_SUFFIXES and path.is_file()
        is_file = path.is_file()
        text = (f"Video: {path}" if is_video else str(path)) if exists else f"{path}  [missing]"
        link = ttk.Label(row, text=text, anchor="w", wraplength=900,
                         cursor="hand2" if is_file else "")
        link.pack(side="left", fill="x", expand=True)
        if is_file:
            link.configure(foreground="#0757a6")
            link.bind("<Button-1>", lambda _event, item=path: self._open_file(item))
        if exists:
            folder = path if path.is_dir() else path.parent
            ttk.Button(row, text="Open folder",
                       command=lambda item=folder: self._open_folder(item)).pack(side="right", padx=(6, 0))

    def render_image(self) -> None:
        if not self.window or not self.images:
            return
        path = self.images[self.image_index]
        try:
            with Image.open(path) as source:
                image = source.convert("RGB")
                image.thumbnail((1030, 490), Image.Resampling.LANCZOS)
                self.photo = ImageTk.PhotoImage(image)
            self.image_label.configure(image=self.photo, text="")
            self.image_title.set(str(path))
        except (OSError, ValueError) as exc:
            self.photo = None
            self.image_label.configure(image="", text=f"Cannot display image: {exc}")
            self.image_title.set(str(path))
        self.image_position.set(f"{self.image_index + 1} / {len(self.images)}")
        state = "normal" if len(self.images) > 1 else "disabled"
        self.previous_button.configure(state=state)
        self.next_button.configure(state=state)

    def previous_image(self) -> None:
        if self.images:
            self.image_index = (self.image_index - 1) % len(self.images)
            self.render_image()

    def next_image(self) -> None:
        if self.images:
            self.image_index = (self.image_index + 1) % len(self.images)
            self.render_image()

    def _open_file(self, path: Path) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("Could not open evidence file", str(exc), parent=self.window)

    def _open_folder(self, path: Path) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("Could not open evidence folder", str(exc), parent=self.window)

    def _approve(self) -> None:
        if self.approve_callback():
            self.close()

    def _deny(self) -> None:
        if messagebox.askyesno(
                "Deny workflow review",
                "Denying this review marks the workflow job rejected and terminal. Continue?",
                parent=self.window):
            if self.deny_callback():
                self.close()

    def close(self) -> None:
        if self.window:
            try:
                self.window.grab_release()
            except tk.TclError:
                pass
            self.window.destroy()
            self.window = None
