#!/usr/bin/env python3
"""Local desktop cockpit for intake plans and the existing allowlisted workflow runner.

The GUI never runs shell text. It saves a new intake plan, previews it through
``run_workflow.py``, and then invokes that same CLI for execution, resume, or an
explicitly selected review approval.
"""
from __future__ import annotations

import json
import hashlib
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from workflow_app_model import (WorkflowAppError, build_intake_plan,
                                read_intake_template, write_new_plan)  # noqa: E402

RUN_WORKFLOW = Path(__file__).with_name("run_workflow.py")
LOCAL_TEMPLATE = ROOT / "CONFIGURE HERE FIRST.json"
EXAMPLE_TEMPLATE = ROOT / "CONFIGURE HERE FIRST.example.json"
DEFAULT_TEMPLATE = LOCAL_TEMPLATE if LOCAL_TEMPLATE.is_file() else EXAMPLE_TEMPLATE

FIELD_LABELS = {
    "episode": "Episode code",
    "confirmed_title": "Confirmed title",
    "archive_url": "Archive URL",
    "profile": "Machine profile",
    "downloads_dir": "Download folder",
    "dvd_mode": "DVD mode",
    "dvd_path": "Exact matching DVD file",
    "cut_pad_images": "No-DVD cut-PAD images",
    "manifest_path": "New manifest path",
}


class WorkflowApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("FromStunToKill – LaserDisc Workflow")
        self.root.geometry("1000x780")
        self.root.minsize(820, 640)
        self.values = {key: tk.StringVar() for key in FIELD_LABELS}
        self.values["fingerprint_policy"] = tk.StringVar(value="balanced")
        self.template_path: Path | None = None
        self.template: dict[str, Any] | None = None
        self.plan_path: Path | None = None
        self.plan: dict[str, Any] | None = None
        self.form_editable = True
        self.field_widgets: dict[str, ttk.Widget] = {}
        self.browse_buttons: dict[str, ttk.Button] = {}
        self.process: subprocess.Popen[str] | None = None
        self.previewed_plan_hash: str | None = None
        self.pending_action: str | None = None
        self.pending_plan_hash: str | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if DEFAULT_TEMPLATE.is_file():
            try:
                self._load_template(DEFAULT_TEMPLATE)
            except WorkflowAppError as exc:
                self.status.set(str(exc))
        self.root.after(100, self._drain_output)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="FromStunToKill – LaserDisc Workflow", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(outer,
                  text="Intake values are saved to a new plan. Existing plans and the CLI remain supported.",
                  wraplength=940).pack(anchor="w", pady=(2, 10))

        file_row = ttk.Frame(outer)
        file_row.pack(fill="x", pady=(0, 8))
        ttk.Button(file_row, text="Open intake template…", command=self._choose_template).pack(side="left")
        ttk.Button(file_row, text="Open existing plan…", command=self._choose_plan).pack(side="left", padx=6)
        self.source_label = ttk.Label(file_row, text="No intake template loaded", width=70)
        self.source_label.pack(side="left", padx=8, fill="x", expand=True)

        form = ttk.LabelFrame(outer, text="Intake", padding=10)
        form.pack(fill="x")
        self.entries: dict[str, ttk.Widget] = {}
        for row, key in enumerate(FIELD_LABELS):
            ttk.Label(form, text=FIELD_LABELS[key]).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            if key == "dvd_mode":
                widget = ttk.Combobox(form, textvariable=self.values[key],
                                      values=("dvd-backed", "no-dvd"), state="readonly", width=28)
                widget.bind("<<ComboboxSelected>>", lambda _event: self._update_mode_controls())
            elif key == "cut_pad_images":
                widget = ttk.Combobox(form, textvariable=self.values[key],
                                      values=("use", "skip"), state="readonly", width=28)
            else:
                widget = ttk.Entry(form, textvariable=self.values[key])
            widget.grid(row=row, column=1, sticky="ew", pady=3)
            self.entries[key] = widget
            self.field_widgets[key] = widget
            browse = self._browse_handler(key)
            if browse:
                button = ttk.Button(form, text="Browse…", command=browse)
                button.grid(row=row, column=2, padx=(6, 0), pady=3)
                self.browse_buttons[key] = button
        ttk.Label(form, text="Fingerprint policy").grid(row=len(FIELD_LABELS), column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Combobox(form, textvariable=self.values["fingerprint_policy"],
                     values=("balanced", "full"), state="readonly", width=28).grid(
                         row=len(FIELD_LABELS), column=1, sticky="w", pady=3)
        form.columnconfigure(1, weight=1)

        self.mode_note = ttk.Label(outer, text="", wraplength=940)
        self.mode_note.pack(anchor="w", pady=(6, 8))
        self._update_mode_controls()

        actions = ttk.LabelFrame(outer, text="Plan and run", padding=8)
        actions.pack(fill="x", pady=(2, 8))
        self.save_button = ttk.Button(actions, text="Save new plan…", command=self._save_plan)
        self.save_button.pack(side="left")
        self.preview_button = ttk.Button(actions, text="Preview", command=lambda: self._launch("preview"))
        self.preview_button.pack(side="left", padx=5)
        self.run_button = ttk.Button(actions, text="Run plan…", command=lambda: self._launch("run"))
        self.run_button.pack(side="left", padx=5)
        self.resume_button = ttk.Button(actions, text="Resume", command=lambda: self._launch("resume"))
        self.resume_button.pack(side="left", padx=5)
        ttk.Label(actions, text="Review gate:").pack(side="left", padx=(14, 4))
        self.review_choice = tk.StringVar()
        self.review_box = ttk.Combobox(actions, textvariable=self.review_choice, state="readonly", width=27)
        self.review_box.pack(side="left")
        self.approve_button = ttk.Button(actions, text="Review & approve…", command=self._approve)
        self.approve_button.pack(side="left", padx=5)

        self.status = tk.StringVar(value="Load an intake plan, or open an existing workflow plan.")
        ttk.Label(outer, textvariable=self.status, wraplength=940).pack(anchor="w", pady=(0, 6))
        log_frame = ttk.LabelFrame(outer, text="Plan preview / workflow output", padding=6)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, wrap="word", state="disabled", height=14)
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._set_running(False)

    def _browse_handler(self, key: str):
        handlers = {
            "profile": self._browse_profile,
            "downloads_dir": self._browse_downloads,
            "dvd_path": self._browse_dvd,
            "manifest_path": self._browse_manifest,
        }
        return handlers.get(key)

    def _browse_profile(self) -> None:
        path = filedialog.askopenfilename(title="Select device-local machine profile",
                                          filetypes=(("JSON profiles", "*.json"), ("All files", "*.*")))
        if path:
            self.values["profile"].set(path)

    def _browse_downloads(self) -> None:
        path = filedialog.askdirectory(title="Select or create the intended download folder",
                                       mustexist=False)
        if path:
            self.values["downloads_dir"].set(path)

    def _browse_dvd(self) -> None:
        path = filedialog.askopenfilename(title="Select the exact matching DVD file",
                                          filetypes=(("Video files", "*.mkv *.mp4 *.m2ts *.vob"),
                                                     ("All files", "*.*")))
        if path:
            self.values["dvd_path"].set(path)

    def _browse_manifest(self) -> None:
        path = filedialog.asksaveasfilename(title="Choose a new manifest path (not created yet)",
                                            defaultextension=".json",
                                            filetypes=(("JSON files", "*.json"),))
        if path:
            self.values["manifest_path"].set(path)

    def _choose_template(self) -> None:
        path = filedialog.askopenfilename(title="Open an intake plan/template",
                                          initialdir=str(ROOT),
                                          filetypes=(("JSON files", "*.json"),))
        if path:
            try:
                self._load_template(Path(path))
            except WorkflowAppError as exc:
                messagebox.showerror("Cannot open intake template", str(exc), parent=self.root)

    def _load_template(self, path: Path) -> None:
        template = read_intake_template(path)
        self.template_path = path.resolve()
        self.template = template
        for key, setting in template["_operator_inputs_EDIT"].items():
            if key in self.values and isinstance(setting, dict):
                self.values[key].set(str(setting.get("value", "")))
        self.values["dvd_path"].set("")
        self.values["fingerprint_policy"].set(template.get("fingerprint_policy", "balanced"))
        self.source_label.configure(text=f"Intake template: {self.template_path}")
        self.plan_path = None
        self.plan = None
        self.previewed_plan_hash = None
        self._set_form_editable(True)
        self.status.set("Template loaded. Edit values, then save a new plan; the selected template is never modified.")
        self._update_mode_controls()

    def _choose_plan(self) -> None:
        path = filedialog.askopenfilename(title="Open an existing workflow plan",
                                          initialdir=str(ROOT),
                                          filetypes=(("JSON plans", "*.json"),))
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict) or data.get("schema") != "ld-pipeline-workflow-plan-v1":
                raise WorkflowAppError("selected file is not a supported workflow plan")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, WorkflowAppError) as exc:
            messagebox.showerror("Cannot open workflow plan", str(exc), parent=self.root)
            return
        self.plan_path = Path(path).resolve()
        self.plan = data
        self.previewed_plan_hash = None
        self.template = None
        operator = data.get("_operator_inputs_EDIT") or {}
        for key, setting in operator.items():
            if key in self.values and isinstance(setting, dict):
                self.values[key].set(str(setting.get("value", "")))
        self.source_label.configure(text=f"Workflow plan: {self.plan_path}")
        self.status.set("Existing plan loaded. Preview before running or resuming it.")
        self._set_form_editable(False)
        self._update_review_choices()
        self._update_mode_controls()

    def _update_mode_controls(self) -> None:
        mode = self.values["dvd_mode"].get().casefold()
        dvd_widget = self.entries["dvd_path"]
        cut_widget = self.entries["cut_pad_images"]
        for key, widget in self.field_widgets.items():
            if key in ("dvd_mode", "cut_pad_images"):
                continue
            widget.configure(state="normal" if self.form_editable else "disabled")
        self.entries["dvd_mode"].configure(state="readonly" if self.form_editable else "disabled")
        dvd_enabled = self.form_editable and mode == "dvd-backed"
        cut_enabled = self.form_editable and mode == "no-dvd"
        dvd_widget.configure(state="normal" if dvd_enabled else "disabled")
        cut_widget.configure(state="readonly" if cut_enabled else "disabled")
        for key, button in self.browse_buttons.items():
            enabled = self.form_editable and (key != "dvd_path" or mode == "dvd-backed")
            button.configure(state="normal" if enabled else "disabled")
        if mode == "dvd-backed":
            self.mode_note.configure(text="DVD-backed mode passes the selected exact file to initialization. Verify it is the matching episode. The new manifest path must be in the selected profile/workspace's expected manifest folder.")
        elif mode == "no-dvd":
            self.mode_note.configure(text="No-DVD mode is an explicit decision and removes DVD-derived tracks and external DVD A/V correlation. Choose the cut-PAD still policy as well.")
        else:
            self.mode_note.configure(text="Choose DVD-backed or no-DVD mode. The plan will encode that choice in both stage arguments and its audit decisions.")

    def _collect_values(self) -> dict[str, str]:
        values = {key: variable.get().strip() for key, variable in self.values.items()}
        if values["dvd_mode"].casefold() != "dvd-backed":
            values["dvd_path"] = "not-applicable"
        return values

    def _save_plan(self) -> None:
        if not self.template or not self.template_path:
            messagebox.showerror("No intake template", "Load an intake template first.", parent=self.root)
            return
        destination = filedialog.asksaveasfilename(title="Save a new immutable workflow plan",
                                                   initialdir=str(ROOT),
                                                   initialfile=f"{self.values['episode'].get().strip() or 'episode'}-intake-plan.json",
                                                   defaultextension=".json",
                                                   filetypes=(("JSON plans", "*.json"),))
        if not destination:
            return
        try:
            plan = build_intake_plan(self.template, self._collect_values())
            saved_path = write_new_plan(Path(destination), plan)
        except (WorkflowAppError, OSError) as exc:
            messagebox.showerror("Plan not saved", str(exc), parent=self.root)
            return
        self.plan_path = saved_path
        self.plan = plan
        self.previewed_plan_hash = None
        self.source_label.configure(text=f"Saved plan: {self.plan_path}")
        self._set_form_editable(False)
        self.status.set("New plan saved without modifying the intake template. Preview it before execution.")
        self._append_output(f"Saved new plan: {saved_path}\n")
        self._update_review_choices()

    def _set_form_editable(self, editable: bool) -> None:
        self.form_editable = editable
        self._update_mode_controls()
        if hasattr(self, "save_button"):
            can_save = editable and self.template is not None and self.plan_path is None
            self.save_button.configure(state="normal" if can_save else "disabled")

    def _update_review_choices(self) -> None:
        reviews = [step.get("id") for step in (self.plan or {}).get("steps", [])
                   if step.get("kind") == "review" and step.get("id")]
        self.review_box.configure(values=reviews)
        self.review_choice.set(reviews[0] if reviews else "")

    def _launch(self, action: str) -> None:
        if not self.plan_path or not self.plan_path.is_file():
            messagebox.showerror("No saved plan", "Save a new plan or open an existing plan first.", parent=self.root)
            return
        options = {"preview": [], "run": ["--execute"], "resume": ["--execute", "--resume"]}
        if action not in options:
            return
        if action in ("run", "resume") and not self._require_preview():
            return
        if action == "run":
            stages = [step.get("stage") for step in (self.plan or {}).get("steps", [])
                      if step.get("kind", "stage") == "stage"]
            text = "Run this plan's allowlisted stages?\n\n" + " → ".join(stages)
            if any(stage in ("encode-final", "remux") for stage in stages):
                text += "\n\nThis plan includes final encoding or remuxing. Confirm only if that work is approved."
            if not messagebox.askyesno("Confirm workflow execution", text, parent=self.root):
                return
        self._start_process([sys.executable, str(RUN_WORKFLOW), "--plan", str(self.plan_path), *options[action]], action)

    def _approve(self) -> None:
        if not self.plan_path or not self.plan_path.is_file():
            messagebox.showerror("No saved plan", "Open the waiting workflow's saved plan first.", parent=self.root)
            return
        if not self._require_preview():
            return
        step_id = self.review_choice.get().strip()
        step = next((row for row in (self.plan or {}).get("steps", [])
                     if row.get("kind") == "review" and row.get("id") == step_id), None)
        if not step:
            messagebox.showerror("No review gate selected", "Select a review step from the list.", parent=self.root)
            return
        evidence = "\n".join(str(item) for item in step.get("inputs", []))
        prompt = step.get("prompt", "Review the declared evidence.")
        if not messagebox.askyesno("Human review required",
                                   f"{prompt}\n\nEvidence paths:\n{evidence}\n\nApprove this gate and resume the workflow?",
                                   parent=self.root):
            return
        self._start_process([sys.executable, str(RUN_WORKFLOW), "--plan", str(self.plan_path),
                             "--execute", "--resume", "--approve", step_id], "approve")

    def _plan_digest(self) -> str | None:
        if not self.plan_path or not self.plan_path.is_file():
            return None
        return hashlib.sha256(self.plan_path.read_bytes()).hexdigest()

    def _require_preview(self) -> bool:
        current = self._plan_digest()
        if current is None or current != self.previewed_plan_hash:
            messagebox.showwarning("Preview required",
                                   "Preview this exact saved plan successfully before running, resuming, or approving it.",
                                   parent=self.root)
            return False
        return True

    def _start_process(self, command: list[str], action: str) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Workflow is running", "Wait for the current workflow command to finish.", parent=self.root)
            return
        self.pending_action = action
        self.pending_plan_hash = self._plan_digest() if action == "preview" else None
        self._append_output("\n> " + subprocess.list2cmdline(command) + "\n")
        self.status.set("Running…")
        self._set_running(True)
        threading.Thread(target=self._run_process, args=(command,), daemon=True).start()

    def _run_process(self, command: list[str]) -> None:
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            self.process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, encoding="utf-8", errors="replace",
                                            bufsize=1, creationflags=creationflags)
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.output_queue.put(line)
            code = self.process.wait()
            self.output_queue.put(f"\nGUI command exited with code {code}.\n")
            self.output_queue.put("__WORKFLOW_DONE__" + str(code))
        except OSError as exc:
            self.output_queue.put(f"Could not start workflow command: {exc}\n")
            self.output_queue.put("__WORKFLOW_DONE__2")

    def _drain_output(self) -> None:
        while True:
            try:
                line = self.output_queue.get_nowait()
            except queue.Empty:
                break
            if line.startswith("__WORKFLOW_DONE__"):
                code = line.removeprefix("__WORKFLOW_DONE__")
                if (self.pending_action == "preview" and code == "0" and
                        self.pending_plan_hash is not None and self._plan_digest() == self.pending_plan_hash):
                    self.previewed_plan_hash = self.pending_plan_hash
                self.process = None
                self._set_running(False)
                self.status.set(f"Workflow command finished (exit {code}). Review its output/state before continuing.")
                self.pending_action = None
                self.pending_plan_hash = None
            else:
                self._append_output(line)
        self.root.after(100, self._drain_output)

    def _append_output(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        for button in (self.preview_button, self.run_button, self.resume_button, self.approve_button):
            button.configure(state=state)
        if running:
            self.save_button.configure(state="disabled")
        else:
            self.save_button.configure(state="normal" if self.form_editable and self.template else "disabled")

    def _on_close(self) -> None:
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno("Workflow is running",
                                       "The workflow command will continue in the background if the GUI closes. Close anyway?",
                                       parent=self.root):
                return
        self.root.destroy()


def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Workflow GUI unavailable: {exc}", file=sys.stderr)
        return 2
    WorkflowApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
