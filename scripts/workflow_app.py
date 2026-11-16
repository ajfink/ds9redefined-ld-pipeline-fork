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
import shutil
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
                                choose_existing_profile, create_default_profile,
                                default_profile_path, episode_code_from_url_identifier,
                                read_intake_template, resolve_episode_code,
                                write_new_plan)  # noqa: E402

RUN_WORKFLOW = Path(__file__).with_name("run_workflow.py")
LOCAL_TEMPLATE = ROOT / "CONFIGURE HERE FIRST.json"
EXAMPLE_TEMPLATE = ROOT / "CONFIGURE HERE FIRST.example.json"
DEFAULT_TEMPLATE = LOCAL_TEMPLATE if LOCAL_TEMPLATE.is_file() else EXAMPLE_TEMPLATE

FIELD_LABELS = {
    "archive_url": "Archive URL",
    "confirmed_title": "Confirmed title",
    "downloads_dir": "Download folder",
    "dvd_mode": "DVD mode",
    "dvd_path": "Exact matching DVD file",
    "cut_pad_images": "No-DVD cut-PAD images",
    "manifest_path": "New manifest path",
}

FIELD_HELP = {
    "archive_url": ("Paste the canonical HTTPS archive.org/details/<item> URL. The app derives the episode code "
                    "from the item ID when possible, otherwise it can inspect Archive metadata."),
    "confirmed_title": ("The title written into the new episode manifest and audit record. Confirm it against "
                         "the selected DVD/capture evidence; it is not inferred from an episode code."),
    "downloads_dir": ("Exact destination for the verified TBC/JSON/EFM download. Choose the intended volume "
                      "and leave enough free space; the workflow checks the destination before downloading."),
    "dvd_mode": ("DVD-backed uses the exact matching DVD file and enables DVD-derived measurements/tracks. "
                 "No-DVD is an explicit alternative with no DVD 5.1/SRT or external DVD A/V correlation."),
    "dvd_path": ("Select the exact matching local DVD file for this episode. This path is passed to initialization "
                 "and recorded in the manifest; it is not a substitute for verifying episode identity."),
    "cut_pad_images": ("Only applies to no-DVD intake when verified shared cut-PAD stills are available. 'use' "
                       "allows a uniquely verified still for a confirmed cut-PAD; 'skip' forbids those stills."),
    "manifest_path": ("New manifest output path. Its filename must match the episode code, it must not already "
                      "exist, and in shared mode it must be in the selected episode workspace's manifest folder."),
    "episode": ("Detected from the Archive item ID or its metadata. Confirm the URL/item is the intended episode; "
                "the code is not manually entered."),
    "profile": ("Machine profile used by the workflow. Existing profiles in the default per-user profile folder "
                "are reused; otherwise the app creates one from the portable template."),
    "fingerprint_policy": ("Balanced checks large files using size, timestamps, and edge samples. Full reads and "
                           "hashes every input, which is more thorough but can take a long time for large media."),
}


class ToolTip:
    """Small delayed hover help for form labels; no third-party GUI dependency."""
    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.window = None
        self.after_id = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._cancel, add="+")
        widget.bind("<ButtonPress>", self._cancel, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self.after_id = self.widget.after(550, self._show)

    def _show(self) -> None:
        self.after_id = None
        if self.window or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f"+{x}+{y}")
        ttk.Label(self.window, text=self.text, justify="left", wraplength=360,
                  padding=(8, 5), relief="solid", borderwidth=1).pack()

    def _cancel(self, _event=None) -> None:
        if self.after_id:
            self.widget.after_cancel(self.after_id)
            self.after_id = None
        if self.window:
            self.window.destroy()
            self.window = None


class WorkflowApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("FromStunToKill – LaserDisc Workflow")
        self.root.geometry("1000x780")
        self.root.minsize(820, 640)
        self.values = {key: tk.StringVar() for key in (*FIELD_LABELS, "episode", "profile")}
        self.values["fingerprint_policy"] = tk.StringVar(value="balanced")
        self.episode_display = tk.StringVar(value="Not resolved from Archive URL")
        self.resolved_archive_url = ""
        self._suppress_archive_trace = False
        self.profile_path: Path | None = None
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
        self.values["archive_url"].trace_add("write", self._archive_url_changed)
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
        self.open_profile_button = ttk.Button(file_row, text="Open machine profile…", command=self._open_machine_profile)
        self.open_profile_button.pack(side="left", padx=6)
        self.source_label = ttk.Label(file_row, text="No intake template loaded", width=70)
        self.source_label.pack(side="left", padx=8, fill="x", expand=True)
        self.profile_note = ttk.Label(outer, text="Machine profile: resolving default per-user profile…", wraplength=940)
        self.profile_note.pack(anchor="w", pady=(0, 8))
        ToolTip(self.profile_note, FIELD_HELP["profile"])

        form = ttk.LabelFrame(outer, text="Intake", padding=10)
        form.pack(fill="x")
        self.entries: dict[str, ttk.Widget] = {}
        episode_field_label = ttk.Label(form, text="Episode code (detected)")
        episode_field_label.grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        ToolTip(episode_field_label, FIELD_HELP["episode"])
        self.episode_label = ttk.Label(form, textvariable=self.episode_display)
        self.episode_label.grid(row=0, column=1, sticky="w", pady=3)
        ToolTip(self.episode_label, FIELD_HELP["episode"])
        for row, key in enumerate(FIELD_LABELS, start=1):
            field_label = ttk.Label(form, text=FIELD_LABELS[key])
            field_label.grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            ToolTip(field_label, FIELD_HELP[key])
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
            if key == "archive_url":
                self.resolve_button = ttk.Button(form, text="Resolve episode", command=self._resolve_episode)
                self.resolve_button.grid(row=row, column=2, padx=(6, 0), pady=3)
            browse = self._browse_handler(key)
            if browse:
                button = ttk.Button(form, text="Browse…", command=browse)
                button.grid(row=row, column=2, padx=(6, 0), pady=3)
                self.browse_buttons[key] = button
        policy_row = len(FIELD_LABELS) + 1
        fingerprint_label = ttk.Label(form, text="Fingerprint policy")
        fingerprint_label.grid(row=policy_row, column=0, sticky="w", padx=(0, 8), pady=3)
        ToolTip(fingerprint_label, FIELD_HELP["fingerprint_policy"])
        ttk.Combobox(form, textvariable=self.values["fingerprint_policy"],
                     values=("balanced", "full"), state="readonly", width=28).grid(
                         row=policy_row, column=1, sticky="w", pady=3)
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
            "downloads_dir": self._browse_downloads,
            "dvd_path": self._browse_dvd,
            "manifest_path": self._browse_manifest,
        }
        return handlers.get(key)

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

    def _prepare_default_profile(self, template: dict[str, Any]) -> Path:
        default = default_profile_path()
        selected = choose_existing_profile(default, template)
        created = not selected.is_file()
        selected, created = create_default_profile(selected, ROOT)
        detail = "existing profile" if not created else "created from the platform template"
        if created and os.name == "nt":
            powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
            if powershell:
                command = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                           str(ROOT / "preflight.ps1"), "-Profile", str(selected), "-Write"]
                try:
                    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                            encoding="utf-8", errors="replace", timeout=180)
                    output = (result.stdout or "") + (result.stderr or "")
                    self._append_output("First-run profile discovery:\n" + output)
                    detail = f"created; preflight discovery exit={result.returncode}"
                except (OSError, subprocess.TimeoutExpired) as exc:
                    detail = f"created from template; automatic discovery unavailable ({exc})"
            else:
                detail = "created from template; PowerShell discovery is unavailable"
        elif created:
            detail = "created from the portable platform template; configure missing roots/tools"
        self.profile_path = selected
        self.values["profile"].set(str(selected))
        self.profile_note.configure(text=f"Machine profile ({detail}): {selected}")
        return selected

    def _open_machine_profile(self) -> None:
        path = self.profile_path
        if self.plan and isinstance(self.plan.get("profile"), str):
            path = Path(self.plan["profile"])
        if not path or not path.is_file():
            messagebox.showerror("Machine profile unavailable", "Load an intake template or an existing plan first.", parent=self.root)
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("Could not open machine profile", str(exc), parent=self.root)

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
        self.profile_path = self._prepare_default_profile(template)
        self._suppress_archive_trace = True
        self.values["episode"].set("")
        for key, setting in template["_operator_inputs_EDIT"].items():
            if key in self.values and key not in ("episode", "profile") and isinstance(setting, dict):
                self.values[key].set(str(setting.get("value", "")))
        self.values["dvd_path"].set("")
        self.values["fingerprint_policy"].set(template.get("fingerprint_policy", "balanced"))
        self._suppress_archive_trace = False
        self._archive_url_changed()
        self.source_label.configure(text=f"Intake template: {self.template_path}")
        self.plan_path = None
        self.plan = None
        self.previewed_plan_hash = None
        self._set_form_editable(True)
        self.status.set("Template loaded. Edit values, then save a new plan; the selected template is never modified.")
        self._update_mode_controls()

    def _archive_url_changed(self, *_args) -> None:
        if self._suppress_archive_trace:
            return
        url = self.values["archive_url"].get().strip()
        self.resolved_archive_url = ""
        self.values["episode"].set("")
        if not url:
            self.episode_display.set("Enter an Archive URL to detect the episode")
        else:
            try:
                episode = episode_code_from_url_identifier(url)
            except WorkflowAppError as exc:
                self.episode_display.set(str(exc))
            else:
                if episode:
                    self.values["episode"].set(episode)
                    self.resolved_archive_url = url
                    self.episode_display.set(f"{episode} (from Archive item URL)")
                else:
                    self.episode_display.set("Not in item URL; click Resolve episode to inspect Archive metadata")
        if hasattr(self, "resolve_button"):
            self._update_mode_controls()

    def _resolve_episode(self) -> None:
        url = self.values["archive_url"].get().strip()
        if not url:
            messagebox.showwarning("Archive URL required", "Enter the Archive item URL first.", parent=self.root)
            return
        if self.resolved_archive_url == url and self.values["episode"].get():
            self.status.set("Episode code was detected in the Archive item URL.")
            return
        self.resolving_episode = True
        self._update_mode_controls()
        self.status.set("Resolving episode code from Archive metadata…")

        def resolve() -> None:
            try:
                episode, source = resolve_episode_code(url)
                payload = {"url": url, "episode": episode, "source": source}
                self.output_queue.put("__EPISODE_RESOLVED__" + json.dumps(payload))
            except WorkflowAppError as exc:
                payload = {"url": url, "error": str(exc)}
                self.output_queue.put("__EPISODE_RESOLVE_ERROR__" + json.dumps(payload))

        threading.Thread(target=resolve, daemon=True).start()

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
        episode = str(data.get("episode", ""))
        self.values["episode"].set(episode)
        self.episode_display.set(f"{episode} (from saved plan)" if episode else "Not specified in this plan")
        profile = str(data.get("profile", ""))
        self.values["profile"].set(profile)
        self.profile_path = Path(profile) if profile else None
        self.profile_note.configure(text=f"Plan machine profile: {profile or '<missing>'}")
        archive_input = operator.get("archive_url")
        self.resolved_archive_url = (str(archive_input.get("value", ""))
                                     if isinstance(archive_input, dict) else "")
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
        if hasattr(self, "resolve_button"):
            enabled = self.form_editable and bool(self.values["archive_url"].get().strip()) and not self.resolving_episode
            self.resolve_button.configure(state="normal" if enabled else "disabled")
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
        archive_url = self.values["archive_url"].get().strip()
        if not self.values["episode"].get() or self.resolved_archive_url != archive_url:
            messagebox.showwarning("Resolve episode first",
                                   "The episode code is derived from the Archive URL. Resolve it before saving the plan.",
                                   parent=self.root)
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
            if line.startswith("__EPISODE_RESOLVED__"):
                payload = json.loads(line.removeprefix("__EPISODE_RESOLVED__"))
                self.resolving_episode = False
                if payload.get("url") == self.values["archive_url"].get().strip():
                    self.values["episode"].set(payload["episode"])
                    self.resolved_archive_url = payload["url"]
                    self.episode_display.set(f"{payload['episode']} (from {payload['source']})")
                    self.status.set("Episode code resolved. Confirm the Archive item/title before saving the plan.")
                self._update_mode_controls()
            elif line.startswith("__EPISODE_RESOLVE_ERROR__"):
                payload = json.loads(line.removeprefix("__EPISODE_RESOLVE_ERROR__"))
                self.resolving_episode = False
                if payload.get("url") == self.values["archive_url"].get().strip():
                    self.status.set(payload["error"])
                    messagebox.showerror("Episode code not resolved", payload["error"], parent=self.root)
                self._update_mode_controls()
            elif line.startswith("__WORKFLOW_DONE__"):
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
        if hasattr(self, "resolve_button"):
            can_resolve = (not running and self.form_editable and
                           bool(self.values["archive_url"].get().strip()) and not self.resolving_episode)
            self.resolve_button.configure(state="normal" if can_resolve else "disabled")
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
