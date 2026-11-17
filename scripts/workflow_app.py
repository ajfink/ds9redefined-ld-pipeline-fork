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
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
import tkinter as tk
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from workflow_app_model import (WorkflowAppError, build_intake_plan,
                                build_archive_picker_tree,
                                archive_series_from_url_identifier, default_profile_path,
                                 episode_code_from_url_identifier,
                                 load_archive_picker_catalog, read_intake_template,
                                 resolve_archive_picker_selection, resolve_series_episode_identity,
                                 resolve_local_source,
                                 inspect_retime_candidate_context,
                                 build_retime_candidate_plan,
                                 build_retime_candidate_record_plan,
                                 create_app_device_profile,
                                 inspect_stack_console, build_stack_console_plan,
                                write_new_plan)  # noqa: E402
from pad_review import PadReviewWindow, PreviewCache  # noqa: E402
from pad_classification_review import PadClassificationReviewWindow  # noqa: E402
from episode_dashboard import EpisodeGateDashboard  # noqa: E402
from job_monitor import STALL_WARNING_SECONDS, process_status_text  # noqa: E402
from workflow_workbenches import (AudioDiagnosticWindow, CadenceWorkbenchWindow,
                                  DeliveryPlanWindow)  # noqa: E402
from post_run_cleanup import (CleanupError, PostRunCleanupWindow,
                              cleanup_review_available)  # noqa: E402
from review_popup import HumanReviewPopup  # noqa: E402
from retime_break_review import (RetimeBreakReviewError,
                                 RetimeBreakReviewWindow)  # noqa: E402
from opencode_help import (OpenCodeHelpError, create_help_audit,
                           finalize_help_audit, launch_opencode_help)  # noqa: E402

RUN_WORKFLOW = Path(__file__).with_name("run_workflow.py")
LOCAL_TEMPLATE = ROOT / "CONFIGURE HERE FIRST.json"
EXAMPLE_TEMPLATE = ROOT / "CONFIGURE HERE FIRST.example.json"
DEFAULT_TEMPLATE = LOCAL_TEMPLATE if LOCAL_TEMPLATE.is_file() else EXAMPLE_TEMPLATE
def _form_value_from_template(value: Any) -> str:
    """Do not show instructional placeholders as if they were chosen intake values."""
    text = str(value or "")
    upper = text.upper()
    if "REPLACE_" in upper or "YOUR_ACCOUNT" in upper:
        return ""
    return text


def _next_intake_plan_filename(episode: str, directory: Path = ROOT) -> str:
    """Suggest a fresh immutable plan name without overwriting prior plans."""
    base = f"{episode.strip().upper() or 'episode'}-intake-plan"
    return _next_available_filename(f"{base}.json", directory)


def _next_available_filename(filename: str, directory: Path) -> str:
    requested = Path(filename).name
    base = Path(requested).stem
    suffix = Path(requested).suffix or ".json"
    candidate = f"{base}{suffix}"
    index = 2
    while (Path(directory) / candidate).exists():
        candidate = f"{base}-{index}{suffix}"
        index += 1
    return candidate


def _device_plan_directory(profile_path: Path, bundle_root: Path = ROOT) -> Path:
    """Return this device profile's ignored plans folder; reject other locations."""
    profile = Path(profile_path).expanduser().resolve(strict=False)
    devices = (Path(bundle_root).resolve(strict=False) / "devices").resolve(strict=False)
    try:
        relative = profile.relative_to(devices)
    except ValueError as exc:
        raise WorkflowAppError("new intake plans must be stored under this device's devices folder") from exc
    if (len(relative.parts) != 3 or relative.parts[1] != "local device config" or
            relative.parts[2] != "machine.json"):
        raise WorkflowAppError("selected profile is not in the verified device config folder")
    directory = devices / relative.parts[0] / "plans"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _episode_plan_directory(plan: dict[str, Any], bundle_root: Path = ROOT) -> Path:
    """Resolve and validate the active device/episode plans directory."""
    profile_path = Path(str(plan.get("profile", ""))).expanduser().resolve(strict=True)
    instance_value = plan.get("instance_root")
    if not isinstance(instance_value, str) or not instance_value.strip():
        raise WorkflowAppError("stage plans require the active device episode workspace")
    instance = Path(instance_value).expanduser().resolve(strict=True)
    from shared_workspace import (LockError, validate_device_config_profile,
                                  validate_instance)
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
        if not validate_device_config_profile(profile_path, profile, bundle_root):
            raise WorkflowAppError("stage-plan profile is not this app's device-scoped profile")
        identity = str(plan.get("identity") or
                       f"{plan.get('series', 'DS9')}:{plan.get('episode', '')}")
        validated = validate_instance(str(instance), profile_path, profile, identity)
    except WorkflowAppError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, LockError, TypeError, ValueError) as exc:
        raise WorkflowAppError(f"cannot validate device episode plan directory: {exc}") from exc
    directory = validated / "plans"
    directory.mkdir(parents=True, exist_ok=True)
    return directory

FIELD_LABELS = {
    "series": "Series",
    "archive_url": "Archive URL",
    "confirmed_title": "Episode title (optional)",
    "downloads_dir": "Download destination",
    "dvd_mode": "DVD mode",
    "dvd_path": "Exact matching DVD file",
    "cut_pad_images": "No-DVD cut-PAD images",
}

FIELD_HELP = {
    "series": ("Processing series for this episode. DS9 and Voyager (VOY) have separate identities, manifests, "
               "workflow state, and output workspaces; TNG combined extras are not single-episode inputs."),
    "source_mode": ("Archive URL mode downloads and verifies Archive files. Open local skips downloading and "
                    "needs matching TBC/JSON capture files; download-manifest.csv is optional. Local fingerprints "
                    "record files but do not prove Archive origin/authenticity."),
    "archive_url": ("Paste a canonical HTTPS archive.org/details/<item> URL, or use Choose one to browse the "
                    "bundled catalog. The app resolves the title and workspace from live Archive metadata."),
    "confirmed_title": ("Archive URL mode fills this from Archive TBC filenames/metadata and locks the field. "
                         "Local mode leaves it editable. It may be left blank; the generated manifest will use an explicit "
                         "'Title not supplied (SERIES:SxxExx)' placeholder until you confirm the title from evidence."),
    "downloads_dir": ("The folder where this Archive download will be saved. It starts with the profile-derived "
                       "episode folder, but you may type an absolute folder path or use Browse. In shared/device "
                       "mode it must match roots.downloads_root/downloads_template; change that profile setting to "
                       "move a shared download folder."),
    "dvd_mode": ("DVD-backed uses the exact matching DVD file and enables DVD-derived measurements/tracks. "
                 "No-DVD is an explicit alternative with no DVD 5.1/SRT or external DVD A/V correlation."),
    "dvd_path": ("Select the exact matching local DVD file for this episode. This path is passed to initialization "
                 "and recorded in the manifest; it is not a substitute for verifying episode identity."),
    "cut_pad_images": ("Only applies to no-DVD intake; it is not required or used when DVD-backed mode is selected. "
                       "When applicable, 'use' allows a uniquely verified still for a confirmed cut-PAD; 'skip' forbids those stills."),
    "manifest_path": ("Automatically selected new manifest output. Shared/device-workspace profiles use the "
                       "collision-safe devices/<device-instance>/episodes/<code>/manifest layout; local profiles "
                       "use the existing local manifest location. The manifest is never overwritten."),
    "local_source_path": ("Select an existing folder or one TBC/JSON/EFM file in it. The app checks capture/sidecar "
                          "pairing and fingerprints local files; a download manifest is not required. This records "
                          "local provenance, not Archive authenticity."),
    "episode": ("Canonical identity is SERIES:SxxExx. The app derives the episode code from Archive/local evidence; "
                 "legacy unqualified episode codes continue to mean DS9."),
    "profile": ("Machine-specific workflow settings are stored in the Git-ignored local device config folder "
                "under this device's unique devices/<device>-<id> folder. Use Open machine profile to inspect or edit it."),
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
        self.values = {key: tk.StringVar() for key in
                       (*FIELD_LABELS, "episode", "profile", "manifest_path", "instance_root",
                         "source_mode", "local_source_path")}
        self.values["source_mode"].set("archive-url")
        self.source_mode_var = self.values["source_mode"]
        self.previous_source_mode = "archive-url"
        self.values["fingerprint_policy"] = tk.StringVar(value="balanced")
        self.verbose_logging_var = tk.BooleanVar(value=False)
        self.opencode_help_enabled_var = tk.BooleanVar(value=False)
        self.episode_display = tk.StringVar(value="Not resolved from Archive URL")
        self.manifest_display = tk.StringVar(value="Resolve the Archive URL to select the episode manifest location")
        self.resolved_archive_url = ""
        self.resolving_episode = False
        self.archive_picker_window: tk.Toplevel | None = None
        self._suppress_archive_trace = False
        self.profile_path: Path | None = None
        self.template_path: Path | None = None
        self.template: dict[str, Any] | None = None
        self.plan_path: Path | None = None
        self.plan: dict[str, Any] | None = None
        self.plan_from_intake = False
        self.intake_plan_snapshot: str | None = None
        self.auto_advance_after_run = False
        self.auto_run_after_preview = False
        self.auto_start_rife_after_pad_record = False
        self.form_editable = True
        self.field_widgets: dict[str, ttk.Widget] = {}
        self.browse_buttons: dict[str, ttk.Button] = {}
        self.process: subprocess.Popen[str] | None = None
        self.active_command: list[str] | None = None
        self.active_process_action = ""
        self.active_process_pid: int | None = None
        self.process_started_monotonic: float | None = None
        self.process_last_output_monotonic: float | None = None
        self.process_last_status_second = -1
        self.process_output_tail: deque[str] = deque(maxlen=80)
        self.previewed_plan_hash: str | None = None
        self.pending_action: str | None = None
        self.pending_plan_hash: str | None = None
        self.pending_review_step: str | None = None
        self.waiting_review_step: str | None = None
        self.auto_advance_after_run = False
        self.auto_run_after_preview = False
        self.auto_start_rife_after_pad_record = False
        self.active_review_popup: HumanReviewPopup | None = None
        self.opencode_help_audit: dict[str, Any] | None = None
        self.opencode_help_process: subprocess.Popen | None = None
        self.opencode_help_step: str | None = None
        self.opencode_help_results: dict[str, dict[str, Any]] = {}
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.pad_review_windows: list[PadReviewWindow] = []
        self.pad_review_caches: set[PreviewCache] = set()
        self.pad_classification_window: PadClassificationReviewWindow | None = None
        self.episode_dashboard: EpisodeGateDashboard | None = None
        self.active_workbench: AudioDiagnosticWindow | CadenceWorkbenchWindow | DeliveryPlanWindow | None = None
        self.post_run_cleanup_ready = False
        self.post_run_cleanup_window: PostRunCleanupWindow | None = None
        self.values["archive_url"].trace_add("write", self._archive_url_changed)
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if DEFAULT_TEMPLATE.is_file():
            try:
                self._load_template(DEFAULT_TEMPLATE)
            except WorkflowAppError as exc:
                self.status.set(str(exc))
                self.profile_note.configure(text=f"App-local machine profile unavailable: {exc}")
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
        self.profile_note = ttk.Label(
            outer, text="Machine profile: preparing this device's private workspace configuration…",
            wraplength=940)
        self.profile_note.pack(anchor="w", pady=(0, 8))
        ToolTip(self.profile_note, FIELD_HELP["profile"])

        form = ttk.LabelFrame(outer, text="Intake", padding=10)
        form.pack(fill="x")
        self.entries: dict[str, ttk.Widget] = {}
        self.field_labels: dict[str, ttk.Label] = {}
        self.field_rows: dict[str, int] = {}
        self.source_radio_buttons: list[ttk.Radiobutton] = []
        self.source_mode_var = self.values["source_mode"]
        episode_field_label = ttk.Label(form, text="Series / episode identity")
        episode_field_label.grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
        ToolTip(episode_field_label, FIELD_HELP["episode"])
        self.episode_label = ttk.Label(form, textvariable=self.episode_display)
        self.episode_label.grid(row=0, column=1, sticky="w", pady=3)
        ToolTip(self.episode_label, FIELD_HELP["episode"])
        source_label = ttk.Label(form, text="Source")
        source_label.grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        ToolTip(source_label, FIELD_HELP["source_mode"])
        archive_radio = ttk.Radiobutton(form, text="Archive URL", variable=self.source_mode_var,
                                        value="archive-url", command=self._source_mode_changed)
        archive_radio.grid(row=1, column=1, sticky="w", pady=3)
        ToolTip(archive_radio, "Download this Archive item after the normal identity and free-space checks.")
        local_radio = ttk.Radiobutton(form, text="Open local", variable=self.source_mode_var,
                                      value="local", command=self._source_mode_changed)
        local_radio.grid(row=1, column=1, sticky="w", padx=(125, 0), pady=3)
        ToolTip(local_radio, FIELD_HELP["source_mode"])
        self.source_radio_buttons.extend((archive_radio, local_radio))

        for row, key in enumerate(FIELD_LABELS, start=2):
            field_label = ttk.Label(form, text=FIELD_LABELS[key])
            field_label.grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            ToolTip(field_label, FIELD_HELP[key])
            self.field_labels[key] = field_label
            self.field_rows[key] = row
            if key == "series":
                widget = ttk.Combobox(form, textvariable=self.values[key],
                                      values=("DS9", "VOY"), state="readonly", width=28)
            elif key == "dvd_mode":
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
                self.choose_archive_button = ttk.Button(
                    form, text="Choose one", command=self._open_archive_picker)
                self.choose_archive_button.grid(row=row, column=2, padx=(6, 0), pady=3)
                self.resolve_button = ttk.Button(form, text="Resolve title/workspace", command=self._resolve_episode)
                self.resolve_button.grid(row=row, column=3, padx=(6, 0), pady=3)
            browse = self._browse_handler(key)
            if browse:
                button_text = "Browse folder" if key == "downloads_dir" else "Select DVD…"
                button = ttk.Button(form, text=button_text, command=browse)
                button.grid(row=row, column=2, padx=(6, 0), pady=3)
                self.browse_buttons[key] = button
        self.local_source_frame = ttk.Frame(form)
        self.local_source_frame.grid(row=self.field_rows["archive_url"], column=0,
                                     columnspan=4, sticky="ew", pady=3)
        self.local_source_label = ttk.Label(self.local_source_frame, text="Existing local folder or file")
        self.local_source_label.grid(row=0, column=0, sticky="w", padx=(0, 8))
        ToolTip(self.local_source_label, FIELD_HELP["local_source_path"])
        self.local_source_entry = ttk.Entry(self.local_source_frame,
                                            textvariable=self.values["local_source_path"], state="readonly")
        self.local_source_entry.grid(row=0, column=1, sticky="ew")
        self.local_folder_button = ttk.Button(self.local_source_frame, text="Folder…",
                                              command=self._browse_local_folder)
        self.local_folder_button.grid(row=0, column=2, padx=(6, 0))
        self.local_file_button = ttk.Button(self.local_source_frame, text="File…",
                                            command=self._browse_local_file)
        self.local_file_button.grid(row=0, column=3, padx=(6, 0))
        self.local_source_frame.columnconfigure(1, weight=1)
        self.local_source_frame.grid_remove()

        manifest_row = len(FIELD_LABELS) + 2
        manifest_label = ttk.Label(form, text="Manifest path (automatic)")
        manifest_label.grid(row=manifest_row, column=0, sticky="w", padx=(0, 8), pady=3)
        manifest_value = ttk.Label(form, textvariable=self.manifest_display, wraplength=680)
        manifest_value.grid(row=manifest_row, column=1, sticky="w", pady=3)
        ToolTip(manifest_label, FIELD_HELP["manifest_path"])
        ToolTip(manifest_value, FIELD_HELP["manifest_path"])
        policy_row = manifest_row + 1
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
        self.preview_button = ttk.Button(actions, text="Preview", command=lambda: self._launch("preview"))
        self.preview_button.pack(side="left", padx=5)
        ToolTip(self.preview_button,
                "For a new intake, this automatically saves an immutable plan under your unique device folder, then previews it. "
                "Review the preview before running anything.")
        self.run_button = ttk.Button(actions, text="Run", command=lambda: self._launch("run"))
        self.run_button.pack(side="left", padx=5)
        self.resume_button = ttk.Button(actions, text="Resume", command=lambda: self._launch("resume"))
        self.resume_button.pack(side="left", padx=5)
        self.post_run_cleanup_button = ttk.Button(
            actions, text="Storage cleanup…", command=self._open_post_run_cleanup, state="disabled")
        self.post_run_cleanup_button.pack(side="left", padx=5)
        ToolTip(self.post_run_cleanup_button,
                "After a passing full QC run, review this episode's exact source-download and large-audio cleanup candidates. Nothing is automatic.")
        self.episode_status_button = ttk.Button(
            actions, text="Episode status…", command=self._open_episode_dashboard)
        self.episode_status_button.pack(side="left", padx=5)
        ToolTip(self.episode_status_button,
                "Run the existing read-only validator for individual gates. WARN/LIMIT stays visible and is never reported as PASS.")
        ttk.Label(actions, text="Review gate:").pack(side="left", padx=(14, 4))
        self.review_choice = tk.StringVar()
        self.review_box = ttk.Combobox(actions, textvariable=self.review_choice, state="readonly", width=27)
        self.review_box.pack(side="left")
        self.approve_button = ttk.Button(actions, text="Open review…", command=self._approve)
        self.approve_button.pack(side="left", padx=5)
        self.pad_review_button = ttk.Button(actions, text="Review PAD output", command=self._review_pad_frames)
        self.pad_review_button.pack(side="left", padx=5)
        self.pad_classify_button = ttk.Button(
            actions, text="Classify PAD candidates", command=self._classify_pad_candidates)
        self.pad_classify_button.pack(side="left", padx=5)

        self.status = tk.StringVar(value="Load an intake plan, or open an existing workflow plan.")
        ttk.Label(outer, textvariable=self.status, wraplength=940).pack(anchor="w", pady=(0, 6))
        self.process_status_var = tk.StringVar(value="No active workflow subprocess.")
        ttk.Label(outer, textvariable=self.process_status_var, wraplength=940,
                  foreground="#5c4600").pack(anchor="w", pady=(0, 6))
        self.console_section = ttk.LabelFrame(outer, text="Console and guided stages", padding=6)
        self.console_section.pack(fill="both", expand=True)
        console_header = ttk.Frame(self.console_section)
        console_header.pack(fill="x")
        self.console_expanded = True
        self.console_toggle_button = ttk.Button(console_header, text="Hide console", command=self._toggle_console)
        self.console_toggle_button.pack(side="left")
        self.process_snapshot_button = ttk.Button(
            console_header, text="Save process diagnostics…", command=self._save_process_diagnostics,
            state="disabled")
        self.process_snapshot_button.pack(side="left", padx=(6, 0))
        self.verbose_logging_check = ttk.Checkbutton(
            console_header, text="Verbose logging (show all stage output)",
            variable=self.verbose_logging_var)
        self.verbose_logging_check.pack(side="left", padx=(10, 0))
        ToolTip(self.verbose_logging_check,
                "Standard mode shows warnings, errors, review gates, and summaries while retaining complete stage output in the workflow log. "
                "Verbose mode streams all stage output into this window. On stage failure, standard mode also shows the recent output tail.")
        self.opencode_help_check = ttk.Checkbutton(
            console_header, text="Enable optional OpenCode help",
            variable=self.opencode_help_enabled_var)
        self.opencode_help_check.pack(side="left", padx=(10, 0))
        ToolTip(self.opencode_help_check,
                "Off by default. The intake, V15 stages, candidate reports, and human approvals work without AI. "
                "When enabled, OpenCode help remains separate and audit-logged; it cannot approve or choose measurements.")
        self.review_retime_button = ttk.Button(
            console_header, text="Review/record candidates…",
            command=self._review_retime_break_candidates)
        self.review_retime_button.pack(side="right", padx=(0, 6))
        ToolTip(self.review_retime_button,
                "Open a saved V15 report, explicitly classify candidates, and build a separately reviewed record plan. "
                "No offsets or steps are inferred.")
        self.prepare_retime_button = ttk.Button(
            console_header, text="Prepare retime report…",
            command=self._retime_break_candidates)
        self.prepare_retime_button.pack(side="right", padx=(0, 6))
        ToolTip(self.prepare_retime_button,
                "Create a report-only V15 LD-threshold and DVD-silence candidate plan. "
                "All episode-specific pairing parameters must be supplied explicitly.")
        self.select_efm_button = ttk.Button(
            console_header, text="Select verified EFM capture…", command=self._select_efm_capture)
        self.select_efm_button.pack(side="right", padx=(0, 6))
        ToolTip(self.select_efm_button,
                "Only verified per-capture EFM provenance reports are eligible. The selected WAV's capture and Orc input paths are checked before the manifest is updated.")
        self.workbench_button = ttk.Menubutton(console_header, text="Guided workbenches")
        workbench_menu = tk.Menu(self.workbench_button, tearoff=False)
        workbench_menu.add_command(label="Audio diagnostics…", command=self._open_audio_workbench)
        workbench_menu.add_command(label="Cadence workbench…", command=self._open_cadence_workbench)
        workbench_menu.add_command(label="Delivery plan…", command=self._open_delivery_workbench)
        self.workbench_button.configure(menu=workbench_menu)
        self.workbench_button.pack(side="right", padx=(0, 6))
        ToolTip(self.workbench_button,
                "Build report-only audio diagnostics, motion/cadence evidence plans, or an explicit QC-only/remux/full-delivery plan.")
        self.verify_efm_button = ttk.Button(
            console_header, text="Verify EFM decode…", command=self._verify_efm_capture)
        self.verify_efm_button.pack(side="right", padx=(0, 6))
        ToolTip(self.verify_efm_button,
                "Bind one EFMSink log/WAV/report to the manifest-declared TBC capture. This does not rank or select the capture.")
        self.stage_console_button = ttk.Button(console_header, text="Build next stack plan", command=self._stage_console)
        self.stage_console_button.pack(side="right")
        ToolTip(self.stage_console_button,
                "Builds a no-overwrite, allowlisted plan for input validation and reference selection, or for stack rendering when the manifest already has a measured stack frame count. "
                "Rendering requires your explicit peak-space estimate and ends at a human review gate.")
        log_frame = ttk.LabelFrame(self.console_section, text="Plan preview / workflow output", padding=6)
        self.console_body = log_frame
        log_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.log = tk.Text(log_frame, wrap="word", state="disabled", height=14)
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._set_running(False)

    def _toggle_console(self) -> None:
        if self.console_expanded:
            self.console_body.pack_forget()
            self.console_expanded = False
            self.console_toggle_button.configure(text="Show console")
        else:
            self.console_body.pack(fill="both", expand=True, pady=(6, 0))
            self.console_expanded = True
            self.console_toggle_button.configure(text="Hide console")

    def _browse_handler(self, key: str):
        handlers = {
            "downloads_dir": self._browse_download_folder,
            "dvd_path": self._browse_dvd,
        }
        return handlers.get(key)

    def _browse_download_folder(self) -> None:
        current = self.values["downloads_dir"].get().strip()
        initial = current if current and Path(current).expanduser().is_dir() else None
        path = filedialog.askdirectory(title="Choose Archive download destination",
                                       initialdir=initial, mustexist=False)
        if path:
            selected = Path(path).expanduser().resolve(strict=False)
            self.values["downloads_dir"].set(str(selected))

    def _browse_local_folder(self) -> None:
        path = filedialog.askdirectory(title="Select an existing local LaserDisc capture folder",
                                       mustexist=True)
        if path:
            self._local_source_selected(Path(path))

    def _browse_local_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select one local LaserDisc capture or sidecar file",
            filetypes=(("Decoded TBC and possible RF sources",
                        "*.tbc *.tbc.json *.efm *.efm.7z download-manifest.csv *.lds *.ldf *.s16 *.raw *.flac *.wav"),
                       ("All files", "*.*")))
        if path:
            self._local_source_selected(Path(path))

    def _local_source_selected(self, path: Path) -> None:
        try:
            source = resolve_local_source(path)
        except WorkflowAppError as exc:
            messagebox.showerror("Local source not accepted", str(exc), parent=self.root)
            return
        raw_rf = source.get("raw_rf_files") or []
        if raw_rf and not messagebox.askyesno(
                "Raw RF files also present",
                "This folder contains likely raw RF capture(s): " + ", ".join(raw_rf[:6]) +
                "\n\nRaw RF is not decoded by this intake. Continue using only the decoded TBC captures?",
                parent=self.root):
            return
        self.source_mode_var.set("local")
        self.values["source_mode"].set("local")
        self.previous_source_mode = "local"
        self.values["confirmed_title"].set("")
        if source.get("series_inferred"):
            self.values["series"].set(source["series"])
        selected_series = self.values["series"].get().strip().upper() or "DS9"
        self.values["local_source_path"].set(source["selected_path"])
        self.values["downloads_dir"].set(source["source_dir"])
        self._suppress_archive_trace = True
        self.values["archive_url"].set("")
        self._suppress_archive_trace = False
        self._update_source_controls()
        self._finish_episode_resolution(f"local:{source['selected_path']}", source["episode"],
                                        "local source inventory", series=selected_series)

    def _source_mode_changed(self) -> None:
        mode = self.source_mode_var.get()
        previous = self.previous_source_mode
        if mode == "archive-url" and previous == "local":
            self.values["archive_url"].set("")
            self.values["downloads_dir"].set("")
        self.values["source_mode"].set(mode)
        self.previous_source_mode = mode
        self.values["confirmed_title"].set("")
        self.values["local_source_path"].set("")
        self.values["manifest_path"].set("")
        self.values["instance_root"].set("")
        self.manifest_display.set("Resolve the selected source to select the episode manifest location")
        self.resolved_archive_url = ""
        if mode == "archive-url":
            self._archive_url_changed()
        else:
            self.values["episode"].set("")
            self.episode_display.set("Select a verified local folder/file to detect the episode")
        self._update_mode_controls()

    def _browse_dvd(self) -> None:
        path = filedialog.askopenfilename(title="Select the exact matching DVD file",
                                          filetypes=(("Video files", "*.mkv *.mp4 *.m2ts *.vob"),
                                                     ("All files", "*.*")))
        if path:
            self.values["dvd_path"].set(path)

    def _prepare_default_profile(self) -> Path:
        # Bootstrap the stable per-device workspace first; its ignored config folder
        # then owns the machine profile. Unrelated per-user profiles are never scanned
        # as candidates, so duplicate legacy configs cannot block app startup.
        selected, created = create_app_device_profile(ROOT)
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
        self.profile_path = self._prepare_default_profile()
        self._suppress_archive_trace = True
        self.source_mode_var.set("archive-url")
        self.previous_source_mode = "archive-url"
        self.values["series"].set("DS9")
        self.values["episode"].set("")
        self.values["manifest_path"].set("")
        self.values["instance_root"].set("")
        self.values["local_source_path"].set("")
        self.values["source_mode"].set("archive-url")
        for key, setting in template["_operator_inputs_EDIT"].items():
            if key in self.values and key not in (
                "episode", "profile", "manifest_path", "instance_root", "downloads_dir",
                    "archive_url",
                    "source_mode", "local_source_path") and isinstance(setting, dict):
                self.values[key].set(_form_value_from_template(setting.get("value", "")))
        self.values["archive_url"].set("")
        self.values["dvd_path"].set("")
        self.values["fingerprint_policy"].set(template.get("fingerprint_policy", "balanced"))
        self._suppress_archive_trace = False
        self._archive_url_changed()
        self.source_label.configure(text=f"Intake template: {self.template_path}")
        self._reset_post_run_cleanup()
        self.plan_path = None
        self.plan = None
        self.plan_from_intake = False
        self.intake_plan_snapshot = None
        self.auto_advance_after_run = False
        self.auto_run_after_preview = False
        self.auto_start_rife_after_pad_record = False
        self.previewed_plan_hash = None
        self.waiting_review_step = None
        self.opencode_help_results.clear()
        self._set_form_editable(True)
        self.status.set("Template loaded. Edit values, then Preview to auto-save a new plan under this device folder and review it.")
        self._update_mode_controls()

    def _archive_url_changed(self, *_args) -> None:
        if self._suppress_archive_trace:
            return
        url = self.values["archive_url"].get().strip()
        self.resolved_archive_url = ""
        self.values["episode"].set("")
        self.values["confirmed_title"].set("")
        try:
            detected_series = archive_series_from_url_identifier(url) if url else None
        except WorkflowAppError:
            detected_series = None
        if detected_series in ("DS9", "VOY"):
            self.values["series"].set(detected_series)
        self.values["manifest_path"].set("")
        self.values["instance_root"].set("")
        self.manifest_display.set("Resolve the Archive URL to select the episode manifest location")
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
                    series = self.values["series"].get().strip().upper() or "DS9"
                    self.episode_display.set(f"{series}:{episode} (from Archive item URL)")
                    self._set_profile_download_dir(episode, series)
                else:
                    self.episode_display.set("Series/episode not in item URL; resolve metadata or choose from the catalog")
        if hasattr(self, "resolve_button"):
            self._update_mode_controls()

    def _set_profile_download_dir(self, episode: str, series: str = "DS9") -> None:
        if self.source_mode_var.get() != "archive-url" or not self.profile_path:
            return
        try:
            profile = json.loads(self.profile_path.read_text(encoding="utf-8-sig"))
            roots = profile.get("roots") or {}
            raw_root = roots.get("downloads_root")
            if not raw_root:
                self.values["downloads_dir"].set("")
                return
            template = str(roots.get("downloads_template") or "{series} {episode} LaserDisc")
            if series != "DS9" and "{series}" not in template:
                template = "{series} {episode} LaserDisc"
            leaf = template.format(episode=episode, series=series)
            root = Path(os.path.expandvars(os.path.expanduser(str(raw_root))))
            destination = root / leaf
            if not destination.is_absolute():
                raise WorkflowAppError("machine profile roots.downloads_root must be an absolute path")
            self.values["downloads_dir"].set(str(destination.resolve(strict=False)))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError, WorkflowAppError) as exc:
            self.values["downloads_dir"].set("")
            self.status.set(f"Could not derive download folder from machine profile: {exc}")

    def _resolve_episode(self) -> None:
        url = self.values["archive_url"].get().strip()
        if not url:
            messagebox.showwarning("Archive URL required", "Enter the Archive item URL first.", parent=self.root)
            return
        if self.resolving_episode:
            return
        requested_series = (self.values["series"].get().strip().upper()
                            if self.source_mode_var.get() == "local" else None)
        try:
            url_series = archive_series_from_url_identifier(url)
        except WorkflowAppError:
            url_series = None
        display_series = requested_series or url_series or "series pending"
        try:
            direct = episode_code_from_url_identifier(url)
        except WorkflowAppError as exc:
            messagebox.showerror("Archive URL invalid", str(exc), parent=self.root)
            return
        if direct:
            self.values["episode"].set(direct)
            self.episode_display.set(f"{display_series}:{direct} (fetching title/workspace)")
        self.resolving_episode = True
        self._update_mode_controls()
        self.status.set("Resolving episode title and workspace from Archive metadata…")

        def resolve() -> None:
            try:
                series, episode, title, source = resolve_series_episode_identity(
                    url, requested_series=requested_series)
                payload = {"url": url, "series": series, "episode": episode,
                           "title": title, "source": source}
                self.output_queue.put("__EPISODE_RESOLVED__" + json.dumps(payload))
            except WorkflowAppError as exc:
                payload = {"url": url, "error": str(exc)}
                self.output_queue.put("__EPISODE_RESOLVE_ERROR__" + json.dumps(payload))

        threading.Thread(target=resolve, daemon=True).start()

    def _open_archive_picker(self) -> None:
        if self.archive_picker_window and self.archive_picker_window.winfo_exists():
            self.archive_picker_window.lift()
            return
        catalog_path = ROOT / "archive_catalog" / "section_31_tree.json"
        try:
            catalog = load_archive_picker_catalog(catalog_path)
            grouped = build_archive_picker_tree(catalog)
        except WorkflowAppError as exc:
            messagebox.showerror("Archive catalog unavailable", str(exc), parent=self.root)
            return

        dialog = tk.Toplevel(self.root)
        self.archive_picker_window = dialog
        dialog.title("Choose an Archive episode")
        dialog.geometry("700x600")
        dialog.minsize(560, 420)
        dialog.transient(self.root)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=10)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Section 31 Archive catalog",
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")
        generated = (catalog.get("source") or {}).get("generated_at_utc", "unknown")
        ttk.Label(body, text=f"Metadata snapshot from {generated}. Expand Series → Season → Episode.",
                  wraplength=660).pack(anchor="w", pady=(2, 8))

        tree_frame = ttk.Frame(body)
        tree_frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse")
        tree.heading("#0", text="Series / season / episode")
        tree.column("#0", width=610, stretch=True)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        episode_nodes: dict[str, tuple[str, int, int]] = {}
        for series in ("DS9", "VOY", "TNG"):
            series_node = tree.insert("", "end", text=series, open=True)
            for season in sorted(grouped.get(series, {})):
                season_node = tree.insert(series_node, "end", text=f"Season {season}", open=False)
                for index, entry in enumerate(grouped[series][season]):
                    code = f"S{season:02d}E{entry['episode']:02d}"
                    title = entry.get("title")
                    label = f"{code} — {title}" if title else code
                    node = tree.insert(season_node, "end", text=label)
                    episode_nodes[node] = (series, season, entry["episode"])

        warning_text = tk.StringVar(
            value="Choose a standalone DS9 or Voyager episode to use it. TNG entries are shown for lookup only.")
        warning_label = ttk.Label(body, textvariable=warning_text, wraplength=660,
                                  justify="left", padding=(6, 5), relief="groove")
        warning_label.pack(fill="x", pady=(8, 4))
        ttk.Label(body,
                  text="The catalog is navigation metadata only. No media is downloaded here; live ListOnly, integrity, and space checks remain mandatory.",
                  wraplength=660).pack(anchor="w", pady=(0, 8))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        use_button = ttk.Button(buttons, text="Use selected episode", state="disabled")
        use_button.pack(side="right")
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right", padx=(0, 6))
        selected_result: dict[str, Any] | None = None

        def inspect_selection(_event=None) -> None:
            nonlocal selected_result
            selection = tree.selection()
            selected_result = None
            use_button.configure(state="disabled")
            if not selection or selection[0] not in episode_nodes:
                warning_text.set("Choose an episode entry. Series and season rows are expandable groups.")
                return
            series, season, episode = episode_nodes[selection[0]]
            try:
                selected_result = resolve_archive_picker_selection(catalog, series, season, episode)
            except WorkflowAppError as exc:
                warning_text.set(str(exc))
                return
            warnings = selected_result.get("picker_warnings", [])
            if not selected_result.get("picker_selectable"):
                detail = selected_result.get("picker_reason", "This entry is not eligible for the current workflow.")
                if warnings:
                    detail += "\nCatalog warning(s): " + "\n• ".join(warnings)
                warning_text.set(detail)
                return
            if warnings:
                warning_text.set("Catalog warning(s): " + "\n• ".join(warnings) +
                                 "\n\nYou will be asked to confirm before continuing.")
            else:
                warning_text.set(f"Standalone {series} item found. Selection will only set series/URL and resolve live metadata; it will not download media.")
            use_button.configure(state="normal")

        def use_selection() -> None:
            result = selected_result
            if not result or not result.get("picker_selectable"):
                return
            warnings = result.get("picker_warnings", [])
            if warnings and not messagebox.askyesno(
                    "Catalog warning", "This catalog item has workflow warning(s):\n\n• " +
                    "\n• ".join(warnings) +
                    "\n\nContinue to the existing live metadata/title/workspace resolver? No download will start.",
                    parent=dialog):
                return
            url = str(result.get("url", "")).strip()
            if not url:
                messagebox.showerror("Catalog URL unavailable", "The selected catalog entry has no item URL.",
                                     parent=dialog)
                return
            self.values["series"].set(str((result.get("request") or {}).get("series", "DS9")))
            self.values["archive_url"].set(url)
            dialog.destroy()
            self.root.after_idle(self._resolve_episode)

        tree.bind("<<TreeviewSelect>>", inspect_selection)
        tree.bind("<Double-1>", lambda _event: use_selection())
        use_button.configure(command=use_selection)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.bind("<Destroy>", lambda event: setattr(self, "archive_picker_window", None)
                    if event.widget is dialog else None)

    def _manifest_for_episode(self, episode: str, series: str = "DS9") -> tuple[Path, str]:
        if not self.profile_path or not self.profile_path.is_file():
            raise WorkflowAppError("default machine profile is unavailable; use Open machine profile to configure it")
        from ldcommon import Ld, device_workspace_enabled, episode_manifest_path

        keys = ("LDP_PROFILE", "LDP_EPISODE", "LDP_SERIES", "LDP_IDENTITY",
                "LDP_INSTANCE_ROOT", "LDP_MANIFEST_DIR",
                "LDP_INSTANCE_MODE", "LDP_INSTANCE_LOGS", "LDP_CUT_PAD_IMAGES")
        prior = {key: os.environ.get(key) for key in keys}
        try:
            os.environ["LDP_PROFILE"] = str(self.profile_path)
            os.environ["LDP_EPISODE"] = episode
            os.environ["LDP_SERIES"] = series
            os.environ["LDP_IDENTITY"] = f"{series}:{episode}"
            for key in keys[4:]:
                os.environ.pop(key, None)
            ld = Ld(self.profile_path)
            manifest = episode_manifest_path(episode, ld, series=series)
            instance_root = (os.environ.get("LDP_INSTANCE_ROOT", "")
                             if device_workspace_enabled(ld.profile) else "")
            return manifest.resolve(strict=False), instance_root
        finally:
            for key, value in prior.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _finish_episode_resolution(self, url: str, episode: str, source: str,
                                   title: str | None = None, series: str = "DS9") -> None:
        try:
            manifest, instance_root = self._manifest_for_episode(episode, series)
        except Exception as exc:
            self.resolving_episode = False
            self.status.set(f"Episode resolved, but manifest workspace could not be prepared: {exc}")
            messagebox.showerror("Manifest workspace unavailable", str(exc), parent=self.root)
            self._update_mode_controls()
            return
        self.values["episode"].set(episode)
        self.values["series"].set(series)
        if self.source_mode_var.get() == "archive-url":
            if not title or not title.strip():
                self.resolving_episode = False
                self.status.set("Archive metadata did not provide a human episode title; resolve the item again or use local intake.")
                messagebox.showerror("Episode title unavailable",
                                     "Archive metadata and its TBC filenames did not provide a usable episode title. "
                                     "The app will not infer it; verify the Archive item or use local intake.",
                                     parent=self.root)
                self._update_mode_controls()
                return
            self.values["confirmed_title"].set(title.strip())
            self._set_profile_download_dir(episode, series)
        self.values["manifest_path"].set(str(manifest))
        self.values["instance_root"].set(instance_root)
        self.manifest_display.set(str(manifest))
        self.resolved_archive_url = url
        self.resolving_episode = False
        self.episode_display.set(f"{series}:{episode} (from {source})")
        self.status.set(f"{series} series, episode, and manifest location resolved. Review the values, then Preview to save and review a new plan.")
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
        self._reset_post_run_cleanup()
        self.plan_path = Path(path).resolve()
        self.plan = data
        self.plan_from_intake = False
        self.intake_plan_snapshot = None
        self.auto_advance_after_run = False
        self.auto_run_after_preview = False
        self.auto_start_rife_after_pad_record = False
        self.previewed_plan_hash = None
        self.waiting_review_step = None
        self.opencode_help_results.clear()
        self.template = None
        operator = data.get("_fromstuntokill_intake") or data.get("_operator_inputs_EDIT") or {}

        def input_value(key: str) -> str:
            setting = operator.get(key)
            return str(setting.get("value", "")) if isinstance(setting, dict) else ""

        for key, setting in operator.items():
            if key in self.values and isinstance(setting, dict):
                self.values[key].set(str(setting.get("value", "")))
        series = str(input_value("series") or data.get("series", "DS9")).upper()
        self.values["series"].set(series if series in ("DS9", "VOY") else "DS9")
        episode = input_value("episode") or str(data.get("episode", ""))
        if "${input:" in episode:
            episode = ""
        self.values["episode"].set(episode)
        self.episode_display.set(f"{self.values['series'].get()}:{episode} (from saved plan)"
                                 if episode else "Not specified in this plan")
        profile = str(data.get("profile", ""))
        self.values["profile"].set(profile)
        self.profile_path = Path(profile) if profile else None
        self.profile_note.configure(text=f"Plan machine profile: {profile or '<missing>'}")
        archive_input = operator.get("archive_url")
        self.resolved_archive_url = (str(archive_input.get("value", ""))
                                     if isinstance(archive_input, dict) else "")
        manifest_input = operator.get("manifest_path")
        manifest_path = (str(manifest_input.get("value", ""))
                         if isinstance(manifest_input, dict) else
                         str(data.get("manifest_path", "")))
        self.values["manifest_path"].set(manifest_path)
        self.values["instance_root"].set(str(data.get("instance_root", "")))
        self.manifest_display.set(manifest_path or "No manifest path recorded in this plan")
        self.source_label.configure(text=f"Workflow plan: {self.plan_path}")
        source_mode_input = operator.get("source_mode")
        self.previous_source_mode = (str(source_mode_input.get("value", "archive-url"))
                                     if isinstance(source_mode_input, dict) else "archive-url")
        self.status.set("Existing plan loaded. Preview before running or resuming it.")
        self._set_form_editable(False)
        self._update_review_choices()
        self._update_mode_controls()
        if self._intake_plan_has_passed():
            self.status.set("This intake plan already passed. Automatically continuing to the next stack-reference review point.")
            self.root.after_idle(self._auto_start_stack_reference)

    def _intake_plan_has_passed(self) -> bool:
        """Recognize this exact immutable intake plan's completed workflow state."""
        plan = self.plan or {}
        if not self._is_intake_completion_plan() or not plan.get("instance_root"):
            return False
        digest = self._plan_digest()
        profile_value = plan.get("profile")
        if not digest or not isinstance(profile_value, str):
            return False
        try:
            profile_digest = hashlib.sha256(Path(profile_value).read_bytes()).hexdigest()
            logs = Path(plan["instance_root"]) / "logs"
            for state_path in logs.glob("workflow-*.json"):
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                series = str(plan.get("series", "DS9")).upper()
                episode = str(plan.get("episode", "")).upper()
                if (state.get("status") == "PASS" and
                        state.get("plan_sha256") == digest and
                        state.get("profile_sha256") == profile_digest and
                        str(state.get("episode", "")).upper() == episode and
                        str(state.get("series", "DS9")).upper() == series):
                    return True
        except OSError:
            return False
        return False

    def _update_mode_controls(self) -> None:
        mode = self.values["dvd_mode"].get().casefold()
        dvd_widget = self.entries["dvd_path"]
        cut_widget = self.entries["cut_pad_images"]
        for key, widget in self.field_widgets.items():
            if key in ("dvd_mode", "cut_pad_images"):
                continue
            if key == "series":
                widget.configure(state=("readonly" if self.form_editable and
                                        self.source_mode_var.get() == "local" and
                                        not self.resolving_episode else "disabled"))
            elif key == "downloads_dir":
                widget.configure(state="normal" if self.form_editable and self.source_mode_var.get() == "archive-url" else "disabled")
            elif key == "confirmed_title":
                widget.configure(state="normal" if self.form_editable and self.source_mode_var.get() == "local" else "disabled")
            else:
                widget.configure(state="normal" if self.form_editable else "disabled")
        self.entries["dvd_mode"].configure(state="readonly" if self.form_editable else "disabled")
        dvd_enabled = self.form_editable and mode == "dvd-backed"
        cut_enabled = self.form_editable and mode == "no-dvd"
        dvd_widget.configure(state="normal" if dvd_enabled else "disabled")
        cut_widget.configure(state="readonly" if cut_enabled else "disabled")
        for key, button in self.browse_buttons.items():
            if key == "downloads_dir":
                enabled = self.form_editable and self.source_mode_var.get() == "archive-url"
            else:
                enabled = self.form_editable and mode == "dvd-backed"
            button.configure(state="normal" if enabled else "disabled")
        dvd_visible = mode == "dvd-backed"
        for widget in (self.field_labels["dvd_path"], self.entries["dvd_path"],
                       self.browse_buttons.get("dvd_path")):
            if widget is None:
                continue
            if dvd_visible:
                widget.grid()
            else:
                widget.grid_remove()
        if hasattr(self, "resolve_button"):
            enabled = self.form_editable and bool(self.values["archive_url"].get().strip()) and not self.resolving_episode
            self.resolve_button.configure(state="normal" if enabled else "disabled")
            self.choose_archive_button.configure(
                state="normal" if self.form_editable and not self.resolving_episode else "disabled")
        if self.source_mode_var.get() == "local":
            self.mode_note.configure(text="Open local skips downloading. It inventories matching TBC/JSON/optional EFM files and records local fingerprints; Archive origin/authenticity is not asserted.")
        elif mode == "dvd-backed":
            self.mode_note.configure(text="DVD-backed mode passes the selected exact file to initialization. Verify it is the matching episode. The new manifest path must be in the selected profile/workspace's expected manifest folder.")
        elif mode == "no-dvd":
            self.mode_note.configure(text="No-DVD mode is an explicit decision and removes DVD-derived tracks and external DVD A/V correlation. Choose the cut-PAD still policy as well.")
        else:
            self.mode_note.configure(text="Choose DVD-backed or no-DVD mode. The plan will encode that choice in both stage arguments and its audit decisions.")
        self._update_source_controls()

    def _update_source_controls(self) -> None:
        if not hasattr(self, "local_source_frame"):
            return
        url_mode = self.source_mode_var.get() == "archive-url"
        for key in ("archive_url", "downloads_dir"):
            if url_mode:
                self.field_labels[key].grid()
                self.entries[key].grid()
                button = self.browse_buttons.get(key)
                if button:
                    button.grid()
            else:
                self.field_labels[key].grid_remove()
                self.entries[key].grid_remove()
                button = self.browse_buttons.get(key)
                if button:
                    button.grid_remove()
        if url_mode:
            self.field_labels["series"].grid_remove()
            self.entries["series"].grid_remove()
            self.local_source_frame.grid_remove()
            self.choose_archive_button.grid()
            self.resolve_button.grid()
        else:
            self.field_labels["series"].grid()
            self.entries["series"].grid()
            self.local_source_frame.grid()
            self.choose_archive_button.grid_remove()
            self.resolve_button.grid_remove()
        radio_state = "normal" if self.form_editable else "disabled"
        for button in self.source_radio_buttons:
            button.configure(state=radio_state)
        local_state = "normal" if self.form_editable and not url_mode else "disabled"
        self.local_folder_button.configure(state=local_state)
        self.local_file_button.configure(state=local_state)

    def _collect_values(self) -> dict[str, str]:
        values = {key: variable.get().strip() for key, variable in self.values.items()}
        if values["dvd_mode"].casefold() != "dvd-backed":
            values["dvd_path"] = "not-applicable"
        return values

    def _save_intake_plan(self) -> bool:
        if not self.template or not self.template_path:
            messagebox.showerror("No intake template", "Load an intake template first.", parent=self.root)
            return False
        source_mode = self.values["source_mode"].get()
        source_key = (self.values["archive_url"].get().strip() if source_mode == "archive-url" else
                      f"local:{self.values['local_source_path'].get().strip()}")
        if (not self.values["episode"].get() or self.resolved_archive_url != source_key or
                not self.values["manifest_path"].get()):
            messagebox.showwarning("Resolve episode first",
                                   "Resolve the selected source and automatic manifest/workspace location before saving the plan.",
                                   parent=self.root)
            return False
        try:
            plan = build_intake_plan(self.template, self._collect_values())
            plans_dir = _device_plan_directory(self.profile_path, ROOT)
            destination = plans_dir / _next_intake_plan_filename(
                self.values["episode"].get(), plans_dir)
            saved_path = write_new_plan(destination, plan)
        except (WorkflowAppError, OSError) as exc:
            detail = str(exc)
            if "refusing to overwrite existing plan" in detail:
                detail += "\n\nWorkflow plans are immutable. The existing plan was left unchanged; try Preview again to create a new plan file."
            messagebox.showerror("Plan not saved", detail, parent=self.root)
            return False
        self._reset_post_run_cleanup()
        self.plan_path = saved_path
        self.plan = plan
        self.plan_from_intake = True
        self.intake_plan_snapshot = self._current_intake_snapshot()
        self.auto_advance_after_run = False
        self.auto_run_after_preview = False
        self.auto_start_rife_after_pad_record = False
        self.previewed_plan_hash = None
        self.source_label.configure(text=f"Saved plan: {self.plan_path}")
        # Keep the intake editable through preview. If a value changes, the next
        # Preview/Run creates another immutable plan rather than mutating this one.
        self._set_form_editable(True)
        self.status.set("New immutable plan saved under this device folder. You may keep editing; preview changed values before running.")
        self._append_output(f"Auto-saved new plan: {saved_path}\n")
        self._update_review_choices()
        return True

    def _current_intake_snapshot(self) -> str:
        payload = {
            "values": self._collect_values(),
            "resolved_archive_url": self.resolved_archive_url,
            "profile": str(self.profile_path.resolve(strict=False)) if self.profile_path else "",
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _set_form_editable(self, editable: bool) -> None:
        self.form_editable = editable
        self._update_mode_controls()

    def _update_review_choices(self) -> None:
        reviews = [step.get("id") for step in (self.plan or {}).get("steps", [])
                   if step.get("kind") == "review" and step.get("id")]
        self.review_box.configure(values=reviews)
        self.review_choice.set(reviews[0] if reviews else "")

    def _is_intake_completion_plan(self) -> bool:
        steps = (self.plan or {}).get("steps", [])
        if any(step.get("kind") == "review" for step in steps):
            return False
        stages = [step.get("stage") for step in steps
                  if step.get("kind", "stage") == "stage"]
        return stages in (["init-local-episode", "validate-inputs"],
                          ["download", "init-episode", "validate-inputs"])

    def _auto_start_stack_reference(self) -> None:
        """Continue a successful intake into the next bounded stack-reference plan."""
        if self.process and self.process.poll() is None:
            return
        try:
            episode, series, profile, manifest, instance = self._current_episode_context()
            if not episode or not profile or not manifest or not instance:
                raise WorkflowAppError("completed intake has no active device episode workspace")
            context = inspect_stack_console(
                Path(profile), Path(manifest), episode, Path(instance), series=series)
            if context.get("phase") != "reference":
                raise WorkflowAppError(
                    "intake completed, but this episode already has a stack project or master. "
                    "Review its current manifest/evidence and build the next plan explicitly.")
            plan = build_stack_console_plan(
                context, fingerprint_policy=(self.values["fingerprint_policy"].get().strip() or "balanced"))
            plan["decisions"].append({
                "id": "automatic-continuation",
                "value": "operator initiated Run; continue from verified intake to the first human review gate",
            })
        except WorkflowAppError as exc:
            self.status.set(f"Intake passed; automatic stack continuation paused: {exc}")
            messagebox.showinfo("Intake complete; review needed", str(exc), parent=self.root)
            return
        if not self._save_app_stage_plan(
                plan, f"{context['identity'].replace(':', '-')}-stack-reference-plan.json",
                "stack reference"):
            return
        self.auto_run_after_preview = True
        self.status.set("Intake passed. The stack-reference plan is auto-saved; previewing it before the first human review gate.")
        self._launch("preview")

    def _launch(self, action: str, *, automatic: bool = False) -> None:
        options = {"preview": [], "run": ["--execute"], "resume": ["--execute", "--resume"]}
        if action not in options:
            return
        has_plan = bool(self.plan_path and self.plan_path.is_file())
        intake_changed = (self.plan_from_intake and
                          self.intake_plan_snapshot != self._current_intake_snapshot())
        if action in ("preview", "run") and self.template and (not has_plan or intake_changed):
            if not self._save_intake_plan():
                return
            if action == "run":
                # A new/edited form must be previewed before it can be executed.
                action = "preview"
                self.status.set("Plan auto-saved. Previewing current values; keep editing if needed, then click Run to execute.")
        elif not has_plan:
            messagebox.showerror("No workflow plan",
                                 "Preview an intake to auto-save its plan, or open an existing plan first.",
                                 parent=self.root)
            return
        if action in ("run", "resume") and not self._require_preview():
            return
        if action == "run":
            stages = [step.get("stage") for step in (self.plan or {}).get("steps", [])
                      if step.get("kind", "stage") == "stage"]
            final_delivery = any(stage in ("encode-final", "remux") for stage in stages)
            runs_qc = "qc" in stages
            if (final_delivery or runs_qc) and not automatic:
                text = "Run this plan's allowlisted stages?\n\n" + " → ".join(stages)
                if final_delivery:
                    text += "\n\nThis plan includes final encoding and/or remuxing. Confirm only if that work is approved."
                if runs_qc:
                    text += "\n\nQC fully decodes and inspects the deliverable; it may take a long time but does not encode or remux."
                if not messagebox.askyesno("Confirm workflow execution", text, parent=self.root):
                    return
            self._set_form_editable(False)
            self.auto_advance_after_run = (not automatic and self._is_intake_completion_plan())
        self._start_process([sys.executable, str(RUN_WORKFLOW), "--plan", str(self.plan_path),
                             *options[action], *self._logging_args()], action)

    def _approve(self) -> None:
        if not self.plan_path or not self.plan_path.is_file():
            messagebox.showerror("No saved plan", "Open the waiting workflow's saved plan first.", parent=self.root)
            return
        step_id = self.review_choice.get().strip()
        step = next((row for row in (self.plan or {}).get("steps", [])
                     if row.get("kind") == "review" and row.get("id") == step_id), None)
        if not step:
            messagebox.showerror("No review gate selected", "Select a review step from the list.", parent=self.root)
            return
        self._open_review_popup(step)

    def _open_review_popup(self, step: dict) -> None:
        if self.active_review_popup and self.active_review_popup.window:
            self.active_review_popup.window.lift()
            return
        step_id = str(step["id"])
        self.review_choice.set(step_id)

        def approve() -> bool:
            return self._approve_review_step(step_id)

        def deny() -> bool:
            return self._deny_review_step(step_id)

        def get_help() -> bool:
            return self._start_opencode_help(step, self.active_review_popup)

        def continue_help(result: dict[str, Any]) -> dict[str, Any]:
            return self._continue_after_opencode_help(step_id, result)

        popup = HumanReviewPopup(self.root, step, approve, deny,
                                 decision_enabled=self.waiting_review_step == step_id,
                                 get_help=(get_help if self.opencode_help_enabled_var.get() else None),
                                 continue_help=(continue_help if self.opencode_help_enabled_var.get() else None))
        self.active_review_popup = popup
        popup.show()
        prior_result = self.opencode_help_results.get(step_id)
        if popup.window and prior_result:
            popup.help_finished(prior_result)
        if (popup.window and self.opencode_help_step == step_id and self.opencode_help_audit and
                not Path(self.opencode_help_audit["done"]).is_file()):
            popup.help_started()

    def _workflow_audit_paths(self) -> tuple[Path | None, Path | None]:
        if not self.plan_path or not self.plan_path.is_file():
            return None, None
        plan = self.plan or {}
        profile_value = str(plan.get("profile") or self.values["profile"].get()).strip()
        if not profile_value:
            return None, None
        try:
            profile_path = Path(profile_value).expanduser().resolve(strict=True)
            profile = json.loads(profile_path.read_text(encoding="utf-8-sig"))
            instance = plan.get("instance_root")
            if instance:
                logs = Path(instance).expanduser().resolve(strict=False) / "logs"
            else:
                raw_logs = (profile.get("roots") or {}).get("logs")
                if not raw_logs:
                    return None, None
                logs = Path(os.path.expandvars(os.path.expanduser(str(raw_logs)))).resolve(strict=False)
            if not logs.is_dir():
                return None, None
            plan_digest = hashlib.sha256(self.plan_path.read_bytes()).hexdigest()
            job_id = plan.get("job_id")
            if job_id:
                state_path = logs / f"workflow-{job_id}.json"
            else:
                matches = []
                for candidate in logs.glob("workflow-*.json"):
                    try:
                        state = json.loads(candidate.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if (state.get("plan_sha256") == plan_digest and
                            str(state.get("episode", "")).casefold() == str(plan.get("episode", "")).casefold()):
                        matches.append((str(state.get("updated_utc", "")), candidate))
                state_path = sorted(matches, reverse=True)[0][1] if matches else None
            if state_path is None or not state_path.is_file():
                return None, None
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}
            actual_job_id = str(state.get("job_id") or job_id or "")
            log_path = logs / f"workflow-{actual_job_id}.log" if actual_job_id else None
            return state_path, log_path if log_path and log_path.is_file() else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return None, None

    def _start_opencode_help(self, step: dict, popup: HumanReviewPopup | None) -> bool:
        step_id = str(step.get("id", ""))
        if self.waiting_review_step != step_id:
            messagebox.showwarning("Review gate is not waiting",
                                   "Run or resume the plan until this review gate pauses before requesting AI help.",
                                   parent=self.root)
            return False
        if self.opencode_help_audit and not Path(self.opencode_help_audit["done"]).is_file():
            messagebox.showwarning("OpenCode help is already running",
                                   f"The active repair session is logged in {self.opencode_help_audit['directory']}.",
                                   parent=self.root)
            return False
        if not self.plan_path or not self.plan_path.is_file():
            messagebox.showerror("No saved plan", "Open the waiting workflow's saved plan first.", parent=self.root)
            return False
        plan = self.plan or {}
        profile_value = str(plan.get("profile") or self.values["profile"].get()).strip()
        if not profile_value:
            messagebox.showerror("OpenCode help unavailable", "The plan has no machine profile path.", parent=self.root)
            return False
        protected = [Path(profile_value), self.plan_path]
        operator = plan.get("_fromstuntokill_intake") or plan.get("_operator_inputs_EDIT") or {}
        manifest_input = operator.get("manifest_path")
        if isinstance(manifest_input, dict) and manifest_input.get("value"):
            protected.append(Path(str(manifest_input["value"])))
        protected.extend(Path(str(value)) for value in (step.get("inputs") or []))
        state_path, workflow_log = self._workflow_audit_paths()
        if state_path:
            protected.append(state_path)
        audit = None
        try:
            app_root = default_profile_path().parent.parent / "opencode-help"
            audit = create_help_audit(
                ROOT, app_root, plan_path=self.plan_path, plan=plan, step=step,
                app_console=self.log.get("1.0", "end-1c"), workflow_log=workflow_log,
                workflow_state=state_path, protected_paths=protected)
            if not audit.get("executable"):
                raise OpenCodeHelpError(
                    f"OpenCode CLI is not on PATH. The attempted repair and context are logged in {audit['directory']}; "
                    "install/configure it separately, then try again.")
            self.opencode_help_audit = audit
            self.opencode_help_step = step_id
            self.active_review_popup = popup
            self.opencode_help_process = launch_opencode_help(audit, ROOT, sys.executable)
            self._append_output(f"OpenCode help launched for {step_id}; audit={audit['directory']}\n")
            self.status.set(f"OpenCode is helping with {step_id}; the workflow remains paused.")
            self.root.after(500, lambda: self._poll_opencode_help(audit, step_id))
            return True
        except (OpenCodeHelpError, OSError, ValueError) as exc:
            if audit:
                (Path(audit["directory"]) / "launcher-error.txt").write_text(
                    str(exc) + "\n", encoding="utf-8")
                Path(audit["done"]).write_text(json.dumps({
                    "schema": "fromstuntokill-opencode-completion-v1",
                    "exit_code": None, "session_id": None,
                    "session_export": "launcher-failed", "finished_utc": "",
                }, indent=2) + "\n", encoding="utf-8")
                try:
                    result = finalize_help_audit(ROOT, audit, audit["protected_paths"])
                except (OSError, ValueError, json.JSONDecodeError):
                    result = {"audit_directory": str(audit["directory"]),
                              "requires_new_plan": False, "source_changes": [],
                              "protected_evidence_changed": []}
                result["help_failed"] = True
                result["help_failed"] = True
                self.opencode_help_results[step_id] = result
                if popup and popup.window:
                    self.root.after_idle(lambda: popup.help_finished(result))
            self._append_output(f"OpenCode help could not start: {exc}\n")
            messagebox.showerror("OpenCode help could not start", str(exc), parent=self.root)
            return False

    def _poll_opencode_help(self, audit: dict[str, Any], step_id: str) -> None:
        if audit is not self.opencode_help_audit:
            return
        done_path = Path(audit["done"])
        if not done_path.is_file() and os.name == "nt" and self.opencode_help_process is not None:
            if self.opencode_help_process.poll() is not None:
                done_path.write_text(json.dumps({
                    "schema": "fromstuntokill-opencode-completion-v1",
                    "exit_code": None, "session_id": None,
                    "session_export": "terminal-closed-before-completion-marker",
                    "finished_utc": "",
                }, indent=2) + "\n", encoding="utf-8")
        if not done_path.is_file():
            self.root.after(500, lambda: self._poll_opencode_help(audit, step_id))
            return
        try:
            result = finalize_help_audit(ROOT, audit, audit["protected_paths"])
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result = {"audit_directory": str(audit["directory"]), "exit_code": None,
                      "requires_new_plan": True, "source_changes": [],
                      "protected_evidence_changed": [], "audit_error": str(exc)}
        if result.get("exit_code") is None:
            result["help_failed"] = True
        audit_plan_hash = (audit.get("metadata") or {}).get("plan_sha256")
        current_plan_hash = self._plan_digest()
        same_plan = bool(audit_plan_hash and current_plan_hash == audit_plan_hash)
        if same_plan:
            self.opencode_help_results[step_id] = result
        self.opencode_help_process = None
        if (same_plan and self.active_review_popup and self.active_review_popup.window and
                self.opencode_help_step == step_id):
            self.active_review_popup.help_finished(result)
        self.status.set(f"OpenCode help finished. Audit and change diff: {audit['directory']}")
        self._append_output(f"OpenCode help finished for {step_id}; audit={audit['directory']}\n")

    def _continue_after_opencode_help(self, step_id: str,
                                      result: dict[str, Any]) -> dict[str, Any]:
        audit_dir = Path(str(result.get("audit_directory", "")))
        event = {"step_id": step_id, "continued_at_utc": datetime.now(timezone.utc).isoformat(),
                 "workflow_resumed": False, "approval_granted": False}
        try:
            (audit_dir / "continue-review.json").write_text(
                json.dumps(event, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
        stale = bool(result.get("requires_new_plan") or result.get("audit_error"))
        if stale:
            reason = ("OpenCode changed source or protected evidence. This review reflects the pre-fix run; "
                      "the app will not approve or resume it. Reconcile outputs, inspect AI-source-changes.diff, "
                      "and create a new plan. You may deny this gate to mark the old job terminal.")
            return {"can_approve": False, "can_deny": self.waiting_review_step == step_id,
                    "can_help": False, "message": reason}
        if result.get("exit_code") is None:
            message = ("OpenCode did not complete successfully; the attempted prompt and diagnostics are logged. "
                       "This button did not approve the gate; inspect the evidence and choose Approve or Deny.")
        else:
            message = ("OpenCode session, sanitized transcript, and source-change audit are saved. "
                       "This button did not approve the gate; inspect the evidence and choose Approve or Deny.")
        return {"can_approve": self.waiting_review_step == step_id,
                "can_deny": self.waiting_review_step == step_id,
                "can_help": self.waiting_review_step == step_id,
                "message": message}

    def _approve_review_step(self, step_id: str) -> bool:
        if not self.plan_path or not self.plan_path.is_file():
            messagebox.showerror("No saved plan", "Open the waiting workflow's saved plan first.", parent=self.root)
            return False
        if self.waiting_review_step != step_id:
            messagebox.showwarning("Review gate is not waiting",
                                   "Run or resume the plan until this review gate pauses before approving it.",
                                   parent=self.root)
            return False
        if not self._require_preview():
            return False
        self._start_process([sys.executable, str(RUN_WORKFLOW), "--plan", str(self.plan_path),
                             "--execute", "--resume", "--approve", step_id,
                             *self._logging_args()], "approve")
        return True

    def _deny_review_step(self, step_id: str) -> bool:
        if not self.plan_path or not self.plan_path.is_file():
            messagebox.showerror("No saved plan", "Open the waiting workflow's saved plan first.", parent=self.root)
            return False
        if self.waiting_review_step != step_id:
            messagebox.showwarning("Review gate is not waiting",
                                   "Run or resume the plan until this review gate pauses before denying it.",
                                   parent=self.root)
            return False
        if not self._require_preview():
            return False
        self._start_process([sys.executable, str(RUN_WORKFLOW), "--plan", str(self.plan_path),
                             "--execute", "--resume", "--deny", step_id,
                             *self._logging_args()], "deny")
        return True

    def _stage_console(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Workflow is running", "Wait for the current workflow command to finish.",
                                   parent=self.root)
            return
        plan = self.plan or {}
        operator = plan.get("_fromstuntokill_intake") or plan.get("_operator_inputs_EDIT") or {}

        def value(key: str) -> str:
            entry = operator.get(key)
            return str(entry.get("value", "")) if isinstance(entry, dict) else ""

        episode = str(value("episode") or plan.get("episode") or self.values["episode"].get()).strip()
        if "${input:" in episode:
            episode = self.values["episode"].get().strip()
        series = str(value("series") or plan.get("series") or self.values["series"].get() or "DS9").upper()
        profile_value = str(value("profile") or plan.get("profile") or self.values["profile"].get()).strip()
        if "${input:" in profile_value:
            profile_value = self.values["profile"].get().strip()
        manifest_value = value("manifest_path") or str(self.values["manifest_path"].get()).strip()
        instance_value = (plan.get("instance_root") or value("instance_root") or
                          self.values["instance_root"].get().strip())
        policy = value("fingerprint_policy") or self.values["fingerprint_policy"].get().strip() or "balanced"
        if not episode or not profile_value or not manifest_value:
            messagebox.showwarning("Stage console unavailable",
                                   "Resolve the episode/workspace or open a plan with a manifest and profile first.",
                                   parent=self.root)
            return
        try:
            context = inspect_stack_console(
                Path(profile_value), Path(manifest_value), episode,
                Path(instance_value) if instance_value else None, series=series)
        except WorkflowAppError as exc:
            messagebox.showinfo("No stack stage is eligible yet", str(exc), parent=self.root)
            return

        planned_gib = None
        if context["phase"] == "render":
            planned_gib = simpledialog.askfloat(
                "Stack-render space estimate",
                "Enter a defensible peak additional-space estimate in GiB from the runbook. "
                "This value is required and will be passed to the space gate; do not guess.",
                minvalue=0.01, parent=self.root)
            if planned_gib is None:
                return
        try:
            stage_plan = build_stack_console_plan(
                context, planned_gib=planned_gib, fingerprint_policy=policy)
        except WorkflowAppError as exc:
            messagebox.showerror("Could not build stack plan", str(exc), parent=self.root)
            return

        self._save_app_stage_plan(
            stage_plan, f"{series}-{episode}-{context['phase']}-plan.json",
            f"{context['phase']} stack");

    def _open_episode_dashboard(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Workflow is running",
                                   "Wait for the current workflow command before running validation gates.",
                                   parent=self.root)
            return
        if self.episode_dashboard and self.episode_dashboard.window:
            self.episode_dashboard.window.lift()
            return
        episode, series, profile, manifest, instance = self._current_episode_context()
        if not episode or not profile or not manifest:
            messagebox.showinfo("Episode status unavailable",
                                "Resolve an episode or open a plan with a profile and manifest first.",
                                parent=self.root)
            return
        try:
            if instance:
                log_root = Path(instance).expanduser() / "logs"
            else:
                profile_data = json.loads(Path(profile).expanduser().read_text(encoding="utf-8-sig"))
                raw_logs = (profile_data.get("roots") or {}).get("logs")
                if not raw_logs:
                    raise WorkflowAppError("profile has no roots.logs location for gate evidence")
                log_root = Path(os.path.expandvars(os.path.expanduser(str(raw_logs))))
                if series != "DS9":
                    log_root = log_root / series
            dashboard = EpisodeGateDashboard(
                self.root, episode, series, Path(profile), Path(manifest),
                Path(instance) if instance else None, log_root)
            self.episode_dashboard = dashboard
            dashboard.show()
        except (OSError, ValueError, RuntimeError) as exc:
            self.episode_dashboard = None
            messagebox.showerror("Episode status unavailable", str(exc), parent=self.root)

    def _open_post_run_cleanup(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Workflow is running",
                                   "Cleanup is unavailable while a workflow command is active.",
                                   parent=self.root)
            return
        if not self.post_run_cleanup_ready:
            messagebox.showinfo("Cleanup is not available",
                                "Run and pass full delivery QC before reviewing source cleanup.",
                                parent=self.root)
            return
        if (self.post_run_cleanup_window and self.post_run_cleanup_window.window and
                self.post_run_cleanup_window.window.winfo_exists()):
            self.post_run_cleanup_window.window.lift()
            return
        state_path, _workflow_log = self._workflow_audit_paths()
        if not state_path or not self.plan_path or not self.plan:
            messagebox.showerror("Cleanup evidence unavailable",
                                 "The completed workflow plan/state could not be resolved.",
                                 parent=self.root)
            return
        try:
            context = self._efm_episode_context()
            cleanup_window = PostRunCleanupWindow(
                self.root, context, self.plan, self.plan_path, state_path,
                on_complete=self._post_run_cleanup_completed)
            self.post_run_cleanup_window = cleanup_window
            cleanup_window.show()
        except (CleanupError, WorkflowAppError, OSError, ValueError) as exc:
            messagebox.showerror("Cleanup review unavailable", str(exc), parent=self.root)

    def _reset_post_run_cleanup(self) -> None:
        self.post_run_cleanup_ready = False
        if hasattr(self, "post_run_cleanup_button"):
            self.post_run_cleanup_button.configure(state="disabled")
        cleanup_window = getattr(self, "post_run_cleanup_window", None)
        if cleanup_window:
            cleanup_window.close()
            self.post_run_cleanup_window = None

    def _post_run_cleanup_completed(self, complete: bool) -> None:
        self.post_run_cleanup_ready = False
        if hasattr(self, "post_run_cleanup_button"):
            self.post_run_cleanup_button.configure(state="disabled")
        if complete:
            self.status.set("Selected cleanup items were moved to Trash; logs, metadata, and QC evidence were preserved.")
        else:
            self.status.set("Cleanup stopped after a partial Recycle Bin/Trash result. Inspect the cleanup log and remaining paths manually.")

    def _current_episode_context(self) -> tuple[str, str, str, str, str]:
        """Resolve the GUI's current series, episode, profile, manifest, and workspace values."""
        plan = self.plan or {}
        inputs = (plan.get("_fromstuntokill_intake") or
                  plan.get("_operator_inputs_EDIT") or {})

        def value(key: str) -> str:
            entry = inputs.get(key)
            return str(entry.get("value", "")) if isinstance(entry, dict) else ""

        episode = value("episode") or str(plan.get("episode") or self.values["episode"].get())
        series = value("series") or str(plan.get("series") or self.values["series"].get() or "DS9")
        profile = value("profile") or str(plan.get("profile") or self.values["profile"].get())
        manifest = value("manifest_path") or str(plan.get("manifest_path") or self.values["manifest_path"].get())
        instance = (str(plan.get("instance_root") or value("instance_root") or
                        self.values["instance_root"].get()))
        values = [episode, series, profile, manifest, instance]
        values = ["" if "${input:" in item else item.strip() for item in values]
        return values[0], values[1].upper() or "DS9", values[2], values[3], values[4]

    def _current_retime_context(self) -> dict[str, Any]:
        episode, series, profile, manifest, instance = self._current_episode_context()
        if not episode or not profile or not manifest:
            raise WorkflowAppError("resolve the episode/workspace or open a plan with a profile and manifest first")
        return inspect_retime_candidate_context(
            Path(profile), Path(manifest), episode, series,
            Path(instance) if instance else None)

    def _save_app_stage_plan(self, plan: dict[str, Any], initial_name: str,
                             scope: str) -> bool:
        try:
            plans_dir = _episode_plan_directory(plan, ROOT)
            destination = plans_dir / _next_available_filename(initial_name, plans_dir)
            saved = write_new_plan(destination, plan)
        except (WorkflowAppError, OSError) as exc:
            messagebox.showerror("Plan not saved", str(exc), parent=self.root)
            return False
        self._reset_post_run_cleanup()
        self.plan_path = saved
        self.plan = plan
        self.plan_from_intake = False
        self.intake_plan_snapshot = None
        self.auto_advance_after_run = False
        self.auto_run_after_preview = False
        self.auto_start_rife_after_pad_record = False
        self.template = None
        self.previewed_plan_hash = None
        self.waiting_review_step = None
        self.opencode_help_results.clear()
        self.source_label.configure(text=f"{scope.title()} plan: {saved}")
        self._set_form_editable(False)
        self._update_review_choices()
        self.status.set(f"Auto-saved {scope} plan under the episode workspace. Preview it before execution; required human review remains in the plan.")
        self._append_output(f"Auto-saved {scope} plan: {saved}\n")
        if not self.console_expanded:
            self._toggle_console()
        return True

    def _retime_break_candidates(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Workflow is running", "Wait for the current workflow command to finish.",
                                   parent=self.root)
            return
        try:
            context = self._current_retime_context()
        except WorkflowAppError as exc:
            messagebox.showinfo("Retime candidate preparation unavailable", str(exc), parent=self.root)
            return

        dialog = tk.Toplevel(self.root)
        dialog.title(f"Prepare retime-break candidates — {context['identity']}")
        dialog.geometry("680x530")
        dialog.minsize(600, 470)
        dialog.transient(self.root)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"V15 report-only preparation — {context['identity']}",
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(body,
                  text="This creates an LD threshold-sensitivity report and lists all plausible DVD-silence pairings. "
                       "It selects no black boundary, pairing, offset, step, or manifest value.",
                  wraplength=640, justify="left").pack(anchor="w", pady=(4, 10))
        form = ttk.Frame(body)
        form.pack(fill="both", expand=True)
        fields = [
            ("ld_thresholds", "LD sensitivity thresholds", "5 10 15",
             "Report-only 8-bit luma values; these are compared, not auto-selected."),
            ("ld_min_duration_s", "Minimum LD run duration (s)", "0.3",
             "Minimum interval duration included in the sensitivity report."),
            ("ld_threshold", "LD threshold for pairing", "",
             "Required episode-reviewed threshold; it must be in the sensitivity list above."),
            ("dvd_silence_noise_db", "DVD silence threshold (dB)", "",
             "Required episode-reviewed FFmpeg silencedetect threshold (negative dB)."),
            ("dvd_silence_min_duration_s", "DVD minimum silence duration (s)", "",
             "Required episode-reviewed silence duration."),
            ("pair_tolerance_s", "Pairing tolerance (s)", "",
             "Required maximum interval-distance for listing a possible pairing."),
            ("max_offset_spread_frames", "Maximum alignment spread (frames)", "",
             "Required limit; larger spreads are reported as transitions, not averaged."),
            ("dvd_audio_selector", "DVD 5.1 selector (optional)", "",
             "Use 0:a:N only when stream metadata is ambiguous; the helper verifies it is six-channel."),
        ]
        widgets: dict[str, ttk.Entry] = {}
        variables: dict[str, tk.StringVar] = {}
        for row, (key, label, default, help_text) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
            variable = tk.StringVar(value=default)
            entry = ttk.Entry(form, textvariable=variable, width=32)
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            ToolTip(entry, help_text)
            widgets[key] = entry
            variables[key] = variable
        form.columnconfigure(1, weight=1)
        ttk.Label(body,
                  text="The plan ends at a human review gate. No report values are written to the manifest.",
                  wraplength=640).pack(anchor="w", pady=(8, 8))
        actions = ttk.Frame(body)
        actions.pack(fill="x")

        def create_plan() -> None:
            try:
                params = {key: str(variable.get()).strip() for key, variable in variables.items()}
                raw_thresholds = re.split(r"[\s,]+", params["ld_thresholds"])
                params["ld_thresholds"] = [float(value) for value in raw_thresholds if value]
                plan = build_retime_candidate_plan(
                    context, params,
                    fingerprint_policy=self.values["fingerprint_policy"].get().strip() or "balanced")
            except (WorkflowAppError, ValueError) as exc:
                messagebox.showerror("Candidate plan not built", str(exc), parent=dialog)
                return
            if self._save_app_stage_plan(
                    plan, f"{context['identity'].replace(':', '-')}-retime-candidates-plan.json",
                    "retime candidate preparation"):
                dialog.destroy()

        ttk.Button(actions, text="Cancel", command=dialog.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(actions, text="Build plan…", command=create_plan).pack(side="right")

    def _review_retime_break_candidates(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Workflow is running", "Wait for the current workflow command to finish.",
                                   parent=self.root)
            return
        try:
            context = self._current_retime_context()
        except WorkflowAppError as exc:
            messagebox.showinfo("Candidate review unavailable", str(exc), parent=self.root)
            return
        report_path = filedialog.askopenfilename(
            title=f"Open a report-only candidate report for {context['identity']}",
            initialdir=str(context["probe"]),
            filetypes=(("JSON candidate reports", "*.json"),))
        if not report_path:
            return

        def create_record_plan(path: Path, report: dict[str, Any], digest: str,
                               selected: list[str], merged: list[str], note: str) -> bool:
            try:
                plan = build_retime_candidate_record_plan(
                    context, path, selected, merged, note,
                    fingerprint_policy=self.values["fingerprint_policy"].get().strip() or "balanced")
            except WorkflowAppError as exc:
                messagebox.showerror("Record plan not built", str(exc), parent=self.root)
                return False
            return self._save_app_stage_plan(
                plan, f"{context['identity'].replace(':', '-')}-record-retime-candidates-plan.json",
                "reviewed retime candidate record")

        try:
            review = RetimeBreakReviewWindow(
                self.root, Path(report_path), context["identity"], create_record_plan)
        except RetimeBreakReviewError as exc:
            messagebox.showerror("Candidate report unavailable", str(exc), parent=self.root)
            return
        review.show()

    def _logging_args(self) -> list[str]:
        return ["--verbose"] if self.verbose_logging_var.get() else ["--standard-output"]

    def _review_pad_frames(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Workflow is running", "Open PAD review after the workflow command finishes.",
                                   parent=self.root)
            return
        plan = self.plan or {}
        operator = plan.get("_fromstuntokill_intake") or plan.get("_operator_inputs_EDIT") or {}
        def value(key: str) -> str:
            entry = operator.get(key)
            return str(entry.get("value", "")) if isinstance(entry, dict) else ""
        episode = str(value("episode") or plan.get("episode") or self.values["episode"].get()).strip()
        if "${input:" in episode:
            episode = self.values["episode"].get().strip()
        series = str(value("series") or plan.get("series") or self.values["series"].get() or "DS9").upper()
        profile_value = str(value("profile") or plan.get("profile") or self.values["profile"].get()).strip()
        if "${input:" in profile_value:
            profile_value = self.values["profile"].get().strip()
        manifest_value = (value("manifest_path") or
                          str(self.values["manifest_path"].get()).strip())
        if not episode or not profile_value or not manifest_value:
            messagebox.showwarning("PAD review unavailable",
                                   "Resolve the episode/workspace or open a plan with an episode, profile, and manifest path first.",
                                   parent=self.root)
            return
        profile, manifest = Path(profile_value).expanduser(), Path(manifest_value).expanduser()
        if not profile.is_file() or not manifest.is_file():
            messagebox.showerror("PAD review unavailable",
                                 "The selected machine profile or episode manifest is missing.", parent=self.root)
            return
        instance_value = plan.get("instance_root")
        instance_root = Path(instance_value).expanduser() if isinstance(instance_value, str) and instance_value else None

        def released(cache: PreviewCache) -> None:
            cache.cleanup()
            self.pad_review_caches.discard(cache)

        reviewer = PadReviewWindow(self.root, episode, profile, manifest, instance_root,
                                   on_close=released, series=series)
        reviewer.show()
        if reviewer.window is not None:
            self.pad_review_windows = [window for window in self.pad_review_windows if window.window is not None]
            self.pad_review_windows.append(reviewer)
            if reviewer.cache:
                self.pad_review_caches.add(reviewer.cache)

    def _efm_episode_context(self) -> dict[str, Any]:
        episode, series, profile_value, manifest_value, instance_value = self._current_episode_context()
        if not all((episode, profile_value, manifest_value, instance_value)):
            raise WorkflowAppError("resolve/open an episode plan with a device workspace before EFM review")
        from ldcommon import Ld, PipelineError, load_episode
        from shared_workspace import validate_device_config_profile, validate_instance
        profile_path = Path(profile_value).expanduser().resolve(strict=True)
        manifest_path = Path(manifest_value).expanduser().resolve(strict=True)
        instance = Path(instance_value).expanduser().resolve(strict=True)
        prior = {key: os.environ.get(key) for key in (
            "LDP_PROFILE", "LDP_SERIES", "LDP_EPISODE", "LDP_IDENTITY", "LDP_INSTANCE_ROOT",
            "LDP_MANIFEST_DIR", "LDP_INSTANCE_LOGS", "LDP_CUT_PAD_IMAGES", "LDP_INSTANCE_MODE")}
        try:
            os.environ.update({
                "LDP_PROFILE": str(profile_path), "LDP_SERIES": series, "LDP_EPISODE": episode,
                "LDP_IDENTITY": f"{series}:{episode}", "LDP_INSTANCE_ROOT": str(instance),
                "LDP_MANIFEST_DIR": str(instance / "manifest"), "LDP_INSTANCE_LOGS": str(instance / "logs"),
                "LDP_CUT_PAD_IMAGES": str(instance / "cut-pad-images"),
                "LDP_INSTANCE_MODE": str((json.loads((instance / "workspace.json").read_text(encoding="utf-8-sig"))
                                            .get("storage_mode") or "shared-release")),
            })
            ld = Ld(profile_path)
            validate_device_config_profile(profile_path, ld.profile, ROOT)
            validate_instance(str(instance), profile_path, ld.profile, f"{series}:{episode}")
            ep = load_episode(f"{series}:{episode}", ld, series=series)
            captures = ep.data.get("captures") or []
            if not captures:
                raise WorkflowAppError("episode manifest has no capture inventory")
            file_paths = {}
            wanted_paths = {"ld_native", "ld_corrected", "ld_synced", "dvd", "align_npy",
                            "stack_project", "stack_master", "tfm_log", "motion",
                            "master_descriptors", "dvd_descriptors", "avs_encode", "avs_padfix",
                            "padfixed_video", "padfixed", "pre_upscale", "retimed_51", "retimed_srt",
                            "dvd51_source", "srt_source"}
            wanted_paths.update(str(key) for key in (ep.data.get("files") or {}) if key)
            wanted_paths.update(key for key in (ep.data.get("deliverable"),
                                                 ep.data.get("ld_audio_source")) if key)
            for key in wanted_paths:
                try:
                    file_paths[key] = str(ep.path(key))
                except PipelineError:
                    continue
            audio_root_raw = ld.root("audio")
            downloads_raw = ep.downloads()
            protected_roots = {}
            for root_name in ("dvd_root", "prores", "out"):
                try:
                    protected_roots[root_name] = str(ld.root(root_name).resolve(strict=False))
                except PipelineError:
                    continue
            return {"episode": episode, "series": series, "identity": f"{series}:{episode}",
                    "profile": profile_path, "manifest": manifest_path, "instance": instance,
                    "probe": ld.probe().resolve(strict=True), "audio_root": audio_root_raw.resolve(strict=True),
                    "audio_root_raw": audio_root_raw, "downloads": downloads_raw.resolve(strict=True),
                    "downloads_raw": downloads_raw, "captures": captures,
                    "manifest_data": ep.data, "file_paths": file_paths,
                    "protected_roots": protected_roots,
                    "platform": str(ld.profile.get("os") or "").casefold(), "ld": ld}
        except WorkflowAppError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, PipelineError,
                RuntimeError, TypeError, ValueError) as exc:
            raise WorkflowAppError(f"cannot resolve active EFM workspace: {exc}") from exc
        finally:
            for key, value in prior.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _verify_efm_capture(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Workflow is running", "Wait for the current command before verifying an EFM decode.", parent=self.root)
            return
        try:
            context = self._efm_episode_context()
        except WorkflowAppError as exc:
            messagebox.showinfo("EFM verification unavailable", str(exc), parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Verify EFM decode provenance — {context['identity']}")
        dialog.geometry("900x350")
        dialog.transient(self.root)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Verify source/log/WAV identity — {context['identity']}",
                  font=("Segoe UI", 11, "bold")).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        labels = [str(row.get("label", "")) for row in context["captures"]]
        capture = tk.StringVar(value=labels[0] if labels else "")
        ttk.Label(body, text="Capture").grid(row=1, column=0, sticky="w", pady=4)
        capture_box = ttk.Combobox(body, textvariable=capture, values=labels, state="readonly", width=14)
        capture_box.grid(row=1, column=1, sticky="w", pady=4)
        values = {key: tk.StringVar() for key in ("orc_log", "wav", "efm_report")}
        rows = (("orc_log", "Orc EFMSink log", "*.log"),
                ("wav", "Decoded EFM WAV", "*.wav"),
                ("efm_report", "EFMSink text report", "*.txt"))
        for row_index, (key, label, pattern) in enumerate(rows, start=2):
            ttk.Label(body, text=label).grid(row=row_index, column=0, sticky="w", pady=4)
            ttk.Entry(body, textvariable=values[key]).grid(row=row_index, column=1, columnspan=2,
                                                           sticky="ew", pady=4)
            def browse(which=key, file_pattern=pattern):
                selected = filedialog.askopenfilename(
                    parent=dialog, title=f"Choose {which.replace('_', ' ')}",
                    initialdir=str(context["instance"] / "logs"),
                    filetypes=(("Expected file", file_pattern), ("All files", "*.*")))
                if selected:
                    values[which].set(selected)
            ttk.Button(body, text="Browse…", command=browse).grid(row=row_index, column=3, padx=(6, 0), pady=4)

        def set_defaults(*_args):
            cap = capture.get().strip()
            wav_candidates = [context["audio_root"] / context["episode"] / f"{cap}-efm.wav",
                              context["audio_root"] / f"{context['episode']}-{cap}-efm.wav",
                              context["audio_root"] / f"{cap}-efm.wav"]
            wav_candidates = [path for path in wav_candidates if path.is_file()]
            if len(wav_candidates) == 1:
                wav = wav_candidates[0]
                values["wav"].set(str(wav))
                values["efm_report"].set(str(Path(str(wav) + ".txt")))
            log_candidates = sorted((context["instance"] / "logs").glob(f"*{cap}*.log"))
            if len(log_candidates) == 1:
                values["orc_log"].set(str(log_candidates[0]))
        capture_box.bind("<<ComboboxSelected>>", set_defaults)
        set_defaults()
        body.columnconfigure(1, weight=1)

        def make_plan():
            cap = capture.get().strip()
            if not all(values[key].get().strip() for key in values):
                messagebox.showerror("EFM evidence required", "Select the Orc log, WAV, and EFMSink text report.", parent=dialog)
                return
            source = next(row for row in context["captures"] if str(row.get("label")) == cap)
            tbc = context["downloads"] / str(source["tbc_file"])
            report_out = (context["instance"] / "references" /
                          f"efm-provenance-{context['episode']}-{cap}-{uuid.uuid4().hex[:12]}.json")
            manifest_sha256 = hashlib.sha256(context["manifest"].read_bytes()).hexdigest()
            plan = {
                "schema": "ld-pipeline-workflow-plan-v1",
                "job_id": f"verify-efm-{context['episode'].lower()}-{cap.lower()}-{uuid.uuid4().hex[:12]}",
                "episode": context["episode"], "series": context["series"], "identity": context["identity"],
                "episode_title": str(context["manifest_data"].get("title") or context["episode"]),
                "profile": str(context["profile"]), "manifest_path": str(context["manifest"]),
                "instance_root": str(context["instance"]),
                "fingerprint_policy": self.values["fingerprint_policy"].get().strip() or "balanced",
                "decisions": [{"id": "efm-provenance-capture", "value": cap},
                              {"id": "efm-provenance-report", "value": str(report_out)},
                              {"id": "efm-provenance-manifest-sha256", "value": manifest_sha256}],
                "steps": [{"id": "verify-efm-source-and-output-identity", "stage": "verify-efm-capture",
                           "args": ["--capture", cap, "--orc-log", values["orc_log"].get().strip(),
                                    "--wav", values["wav"].get().strip(),
                                    "--efm-report", values["efm_report"].get().strip(),
                                    "--report-out", str(report_out),
                                    "--confirm-manifest-sha256", manifest_sha256],
                           "inputs": [str(tbc), values["orc_log"].get().strip(),
                                      values["wav"].get().strip(), values["efm_report"].get().strip()],
                           "outputs": [str(context["manifest"]), str(report_out)]}],
            }
            if not self._save_app_stage_plan(
                    plan, f"{context['series']}-{context['episode']}-verify-efm-{cap}-plan.json",
                    "EFM provenance verification"):
                return
            self.auto_run_after_preview = True
            self.status.set("The capture's log/source/WAV binding will be verified; this does not select the capture or choose an audio rate.")
            dialog.destroy()
            self._launch("preview")

        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, columnspan=4, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right", padx=5)
        ttk.Button(buttons, text="Build verification plan", command=make_plan).pack(side="right")

    def _save_guided_workbench_plan(self, plan: dict[str, Any], filename: str, scope: str) -> bool:
        if not self._save_app_stage_plan(plan, filename, scope):
            return False
        self.status.set(f"{scope.title()} plan saved. Previewing only; no media operation starts until Run is explicitly selected.")
        self._launch("preview")
        return True

    def _open_audio_workbench(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Workflow is running", "Wait for the current command before planning diagnostics.",
                                   parent=self.root)
            return
        try:
            context = self._efm_episode_context()
        except WorkflowAppError as exc:
            messagebox.showinfo("Audio diagnostics unavailable", str(exc), parent=self.root)
            return
        self.active_workbench = AudioDiagnosticWindow(
            self.root, context, self._save_guided_workbench_plan,
            fingerprint_policy=self.values["fingerprint_policy"].get().strip() or "balanced")
        self.active_workbench.show()

    def _open_cadence_workbench(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Workflow is running", "Wait for the current command before planning cadence work.",
                                   parent=self.root)
            return
        try:
            context = self._efm_episode_context()
        except WorkflowAppError as exc:
            messagebox.showinfo("Cadence workbench unavailable", str(exc), parent=self.root)
            return
        self.active_workbench = CadenceWorkbenchWindow(
            self.root, context, self._save_guided_workbench_plan,
            fingerprint_policy=self.values["fingerprint_policy"].get().strip() or "balanced")
        self.active_workbench.show()

    def _open_delivery_workbench(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Workflow is running", "Wait for the current command before planning delivery.",
                                   parent=self.root)
            return
        try:
            context = self._efm_episode_context()
        except WorkflowAppError as exc:
            messagebox.showinfo("Delivery planning unavailable", str(exc), parent=self.root)
            return
        self.active_workbench = DeliveryPlanWindow(
            self.root, context, self._save_guided_workbench_plan,
            fingerprint_policy=self.values["fingerprint_policy"].get().strip() or "balanced")
        self.active_workbench.show()

    def _select_efm_capture(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Workflow is running", "Wait for the current command before selecting an EFM capture.", parent=self.root)
            return
        try:
            context = self._efm_episode_context()
        except WorkflowAppError as exc:
            messagebox.showinfo("EFM selection unavailable", str(exc), parent=self.root)
            return
        audio = context["manifest_data"].get("audio") or {}
        provenance = audio.get("efm_provenance") or {}
        verified = []
        for row in context["captures"]:
            label = str(row.get("label", ""))
            history = provenance.get(label)
            if not isinstance(history, list) or not history:
                continue
            entry = history[-1]
            try:
                report_path = context["instance"] / Path(str(entry["report_path"]))
                if report_path.is_symlink():
                    continue
                report_path.resolve(strict=True).relative_to((context["instance"] / "references").resolve(strict=True))
                report_data = json.loads(report_path.read_text(encoding="utf-8-sig"))
                if (report_data.get("status") != "VERIFIED" or
                        report_data.get("identity") != context["identity"] or
                        report_data.get("capture") != label or
                        hashlib.sha256(report_path.read_bytes()).hexdigest() != entry.get("report_sha256")):
                    continue
                wav_rel = Path(str(report_data.get("wav_relative_path", "")))
                report_rel = Path(str(report_data.get("efm_report_relative_path", "")))
                if (wav_rel.is_absolute() or report_rel.is_absolute() or
                        any(part in ("..", "") for part in (*wav_rel.parts, *report_rel.parts))):
                    continue
                wav_path = (context["audio_root"] / wav_rel).resolve(strict=True)
                efm_report_path = (context["audio_root"] / report_rel).resolve(strict=True)
                if (not efm_report_path.is_file() or efm_report_path.stat().st_size != report_data.get("efm_report_size_bytes") or
                        hashlib.sha256(efm_report_path.read_bytes()).hexdigest() != report_data.get("efm_report_sha256")):
                    continue
            except (OSError, ValueError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            quality = report_data.get("quality_summary") or {}
            verified.append((label, report_path, entry["report_sha256"], quality,
                             efm_report_path, wav_path))
        labels = [row[0] for row in verified]
        if len(labels) != len(context["captures"]):
            missing = sorted(set(str(row.get("label")) for row in context["captures"]) - set(labels))
            messagebox.showwarning(
                "EFM provenance incomplete",
                "Verify the source/log/WAV/report binding for every capture before selection. Missing: " +
                ", ".join(missing), parent=self.root)
            return

        dialog = tk.Toplevel(self.root)
        dialog.title(f"Select native EFM capture — {context['identity']}")
        dialog.geometry("860x430")
        dialog.transient(self.root)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Human capture selection — {context['identity']}",
                  font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(body, text="Reports are provenance-verified, not automatically ranked. Compare the error-correction/audio evidence and choose explicitly.",
                  wraplength=820).pack(anchor="w", pady=(4, 8))
        table = ttk.Treeview(body, columns=("capture", "assessment", "postc2", "c1", "c2", "missing", "concealed", "muted"),
                             show="headings", height=7)
        labels_by_column = {"capture": "Capture", "assessment": "EFM grade", "postc2": "Post-C2 loss %",
                            "c1": "C1 errors %", "c2": "C2 errors %", "missing": "Missing sections",
                            "concealed": "Concealed", "muted": "Muted"}
        for key, heading in labels_by_column.items():
            table.heading(key, text=heading)
            table.column(key, width=110 if key != "capture" else 70, anchor="center")
        table.pack(fill="x", pady=(0, 8))
        for label, _path, _digest, quality, _efm_report, _wav in verified:
            table.insert("", "end", values=(label, quality.get("assessment", "unknown"),
                        quality.get("post_c2_data_loss_pct", "unknown"),
                        quality.get("c1_uncorrectable_pct", "unknown"),
                        quality.get("c2_uncorrectable_pct", "unknown"),
                        quality.get("missing_sections", "unknown"),
                        quality.get("concealed_samples", "unknown"),
                        quality.get("muted_samples", "unknown")))
        capture = tk.StringVar(value="")
        ttk.Label(body, text="Selected capture").pack(anchor="w")
        selector = ttk.Combobox(body, textvariable=capture, values=labels, state="readonly", width=14)
        selector.pack(anchor="w", pady=(2, 6))
        note = tk.StringVar()
        ttk.Label(body, text="Selection note (required; explain the evidence used)").pack(anchor="w")
        ttk.Entry(body, textvariable=note).pack(fill="x", pady=(2, 6))

        def make_plan():
            selected = capture.get().strip()
            found = next((row for row in verified if row[0] == selected), None)
            if not found:
                messagebox.showerror("Capture required", "Choose one provenance-verified capture.", parent=dialog)
                return
            if len(note.get().strip()) < 8:
                messagebox.showerror("Selection note required", "Explain the comparison in at least 8 characters.", parent=dialog)
                return
            label, report_path, digest, quality, _efm_report, selected_wav = found
            selected_capture = next(row for row in context["captures"] if str(row.get("label")) == label)
            selected_tbc = context["downloads"] / str(selected_capture["tbc_file"])
            manifest_sha256 = hashlib.sha256(context["manifest"].read_bytes()).hexdigest()
            plan = {
                "schema": "ld-pipeline-workflow-plan-v1",
                "job_id": f"select-efm-{context['episode'].lower()}-{label.lower()}-{uuid.uuid4().hex[:12]}",
                "episode": context["episode"], "series": context["series"], "identity": context["identity"],
                "episode_title": str(context["manifest_data"].get("title") or context["episode"]),
                "profile": str(context["profile"]), "manifest_path": str(context["manifest"]),
                "instance_root": str(context["instance"]),
                "fingerprint_policy": self.values["fingerprint_policy"].get().strip() or "balanced",
                "decisions": [{"id": "selected-efm-capture", "value": label},
                              {"id": "efm-selection-note", "value": note.get().strip()},
                              {"id": "efm-quality-summary", "value": quality},
                              {"id": "efm-selection-manifest-sha256", "value": manifest_sha256}],
                "steps": [
                    {"id": "record-human-efm-capture-selection", "stage": "record-efm-capture",
                     "args": ["--capture", label, "--provenance-report", str(report_path),
                              "--confirm-provenance-sha256", digest,
                              "--confirm-manifest-sha256", manifest_sha256,
                              "--selection-note", note.get().strip(), "--apply"],
                     "inputs": [str(row[1]) for row in verified] +
                               [str(row[4]) for row in verified] + [str(selected_wav)] +
                               [str(selected_tbc)] +
                               [str(context["manifest_data"].get("files", {}).get("stack_project", {}).get("name", ""))],
                     "outputs": [str(context["manifest"]) ]},
                    {"id": "review-recorded-efm-capture", "kind": "review",
                     "for_stage": "record-efm-capture",
                     "prompt": (f"Review the selected capture {label}, the provenance report, selection rationale, "
                                "recorded Orc source order/pad count, and updated manifest before continuing. "
                                "No encode or remux is included."),
                     "inputs": [str(context["manifest"]), str(report_path)], "outputs": []},
                ],
            }
            # Resolve the stack project using the active machine profile, not a guessed root.
            try:
                from ldcommon import Ld, load_episode
                prior = {key: os.environ.get(key) for key in (
                    "LDP_PROFILE", "LDP_SERIES", "LDP_EPISODE", "LDP_IDENTITY", "LDP_INSTANCE_ROOT", "LDP_MANIFEST_DIR")}
                try:
                    os.environ.update({"LDP_PROFILE": str(context["profile"]), "LDP_SERIES": context["series"],
                                       "LDP_EPISODE": context["episode"], "LDP_IDENTITY": context["identity"],
                                       "LDP_INSTANCE_ROOT": str(context["instance"]),
                                       "LDP_MANIFEST_DIR": str(context["instance"] / "manifest")})
                    ld = Ld(context["profile"])
                    ep = load_episode(context["identity"], ld, series=context["series"])
                    plan["steps"][0]["inputs"][-1] = str(ep.path("stack_project").resolve(strict=True))
                finally:
                    for key, value in prior.items():
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value
            except (PipelineError, OSError) as exc:
                messagebox.showerror("Cannot resolve Orc project", str(exc), parent=dialog)
                return
            if not self._save_app_stage_plan(
                    plan, f"{context['series']}-{context['episode']}-record-efm-capture-plan.json",
                    "human-selected EFM capture"):
                return
            self.auto_run_after_preview = True
            self.status.set("Previewing the human EFM capture-selection plan; manifest mutation remains backed up and a separate review gate follows.")
            dialog.destroy()
            self._launch("preview")

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right", padx=5)
        ttk.Button(buttons, text="Build selection plan", command=make_plan).pack(side="right")

    def _classify_pad_candidates(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Workflow is running", "Open PAD classification after the current command finishes.",
                                   parent=self.root)
            return
        if self.pad_classification_window and self.pad_classification_window.window:
            self.pad_classification_window.window.lift()
            return
        episode, series, profile_value, manifest_value, instance_value = self._current_episode_context()
        if not all((episode, profile_value, manifest_value, instance_value)):
            messagebox.showwarning(
                "PAD classification unavailable",
                "Resolve/open an episode plan with a device workspace, profile, and manifest first.",
                parent=self.root)
            return
        profile = Path(profile_value).expanduser()
        manifest = Path(manifest_value).expanduser()
        instance = Path(instance_value).expanduser()
        if not profile.is_file() or not manifest.is_file() or not instance.is_dir():
            messagebox.showerror("PAD classification unavailable",
                                 "The active profile, manifest, or device workspace is missing.",
                                 parent=self.root)
            return

        def released(cache: PreviewCache) -> None:
            cache.cleanup()
            self.pad_review_caches.discard(cache)
            self.pad_classification_window = None

        reviewer = PadClassificationReviewWindow(
            self.root, episode, series, profile, manifest, instance,
            on_report=self._save_pad_classification_plan, on_close=released)
        self.pad_classification_window = reviewer
        reviewer.show()
        if reviewer.window is None:
            self.pad_classification_window = None
        elif reviewer.cache:
            self.pad_review_caches.add(reviewer.cache)

    def _save_pad_classification_plan(self, report_path: Path, report_sha256: str,
                                      report: dict) -> bool:
        """Save the human-reviewed manifest update as an immutable workflow plan."""
        try:
            episode, series, profile, manifest, instance = self._current_episode_context()
            identity = f"{series}:{episode}"
            if report.get("identity") != identity or report.get("manifest_path") != str(Path(manifest).resolve()):
                raise WorkflowAppError("PAD classification report identity/path no longer matches the active plan")
            manifest_path = Path(manifest).resolve(strict=True)
            instance_root = Path(instance).resolve(strict=True)
            plans = instance_root / "plans"
            committed = report_path.with_name(report_path.stem + "-committed.json")
            evidence = report.get("evidence") or {}
            inputs = [report_path,
                      Path(evidence["pad_audit"]),
                      Path(evidence["repair_frames"]),
                      Path(evidence["fullres_report"])]
            counts = {kind: sum(row.get("classification") == kind for row in report["candidates"])
                      for kind in ("cut", "rife")}
            note = str(report.get("review_note", "")).strip()
            plan = {
                "schema": "ld-pipeline-workflow-plan-v1",
                "job_id": f"pad-classify-{episode.lower()}-{uuid.uuid4().hex[:12]}",
                "episode": episode, "series": series, "identity": identity,
                "episode_title": str(json.loads(manifest_path.read_text(encoding="utf-8-sig")).get("title") or episode),
                "profile": str(Path(profile).resolve(strict=True)),
                "manifest_path": str(manifest_path),
                "instance_root": str(instance_root),
                "fingerprint_policy": self.values["fingerprint_policy"].get().strip() or "balanced",
                "decisions": [
                    {"id": "pad-classification-report-sha256", "value": report_sha256},
                    {"id": "pad-classification-counts", "value": counts},
                    {"id": "pad-auto-motion-count", "value": len(report.get("auto_classified_rife_frames", []))},
                    {"id": "pad-human-review-count", "value": len(report.get("human_review_frames", []))},
                    {"id": "pad-review-note", "value": note},
                    {"id": "final-encode-remux", "value": "not included; no encode/remux approval"},
                ],
                "steps": [
                    {"id": "record-reviewed-pad-classifications",
                     "stage": "record-pad-classifications",
                     "args": ["--series", series, "--decision-file", str(report_path),
                              "--confirm-review", identity,
                              "--confirm-decision-sha256", report_sha256,
                              "--review-note", note, "--apply"],
                     "inputs": [str(path.resolve(strict=True)) for path in inputs],
                     "outputs": [str(manifest_path), str(committed)]},
                ],
            }
            if not self._save_app_stage_plan(
                    plan, f"{series}-{episode}-pad-classification-plan.json",
                    "PAD cut approval and motion-baseline classification"):
                return False
        except (WorkflowAppError, OSError, KeyError, TypeError, ValueError,
                json.JSONDecodeError) as exc:
            messagebox.showerror("PAD decisions not planned", str(exc), parent=self.root)
            return False
        self.auto_start_rife_after_pad_record = True
        self.auto_run_after_preview = True
        self.status.set("The immutable PAD-record plan is saved. Previewing the manifest update; after it succeeds, RIFE will run from a fresh plan using approved cuts and motion-baseline non-cuts.")
        self._launch("preview")
        return True

    def _auto_start_rife_replacements(self) -> None:
        """Build a fresh, fingerprinted RIFE-only plan from committed human decisions."""
        if self.process and self.process.poll() is None:
            return
        try:
            episode, series, profile_value, manifest_value, instance_value = self._current_episode_context()
            if not all((episode, profile_value, manifest_value, instance_value)):
                raise WorkflowAppError("committed PAD review has no active device episode workspace")
            manifest_path = Path(manifest_value).resolve(strict=True)
            instance_root = Path(instance_value).resolve(strict=True)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            review = (manifest.get("pads") or {}).get("pad_classification_review") or {}
            decision_sha = str(review.get("decision_sha256") or "")
            committed_path = None
            for candidate in sorted((instance_root / "plans").glob("*-committed.json")):
                try:
                    row = json.loads(candidate.read_text(encoding="utf-8-sig"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if row.get("status") == "COMMITTED" and row.get("identity") == f"{series}:{episode}" and row.get("manifest_sha256_after") == hashlib.sha256(manifest_path.read_bytes()).hexdigest() and row.get("decision_sha256") == decision_sha:
                    committed_path = candidate
                    committed_report = row
                    break
            if not committed_path:
                raise WorkflowAppError("cannot find the committed PAD decision matching the current manifest")
            report_evidence = committed_report.get("evidence") or {}
            audit_path = Path(report_evidence["pad_audit"]).resolve(strict=True)
            repair_path = Path(report_evidence["repair_frames"]).resolve(strict=True)
            fullres_path = Path(report_evidence["fullres_report"]).resolve(strict=True)
            candidate_frames = sorted({int(value) for value in repair_path.read_text(encoding="utf-8-sig").split()})
            cut_frames = {int(frame) for frame in review.get("cut_frames", [])}
            rife_frames = {int(frame) for frame in review.get("rife_frames", [])}
            if cut_frames | rife_frames != set(candidate_frames) or cut_frames & rife_frames:
                raise WorkflowAppError("committed PAD decision does not cover the exact repair candidate set")
            from ldcommon import Ld, load_episode
            env_keys = ("LDP_PROFILE", "LDP_SERIES", "LDP_EPISODE", "LDP_IDENTITY",
                        "LDP_INSTANCE_ROOT", "LDP_MANIFEST_DIR", "LDP_INSTANCE_LOGS",
                        "LDP_CUT_PAD_IMAGES", "LDP_INSTANCE_MODE")
            prior_env = {key: os.environ.get(key) for key in env_keys}
            try:
                os.environ["LDP_PROFILE"] = str(Path(profile_value).resolve(strict=True))
                os.environ["LDP_SERIES"] = series
                os.environ["LDP_EPISODE"] = episode
                os.environ["LDP_IDENTITY"] = f"{series}:{episode}"
                os.environ["LDP_INSTANCE_ROOT"] = str(instance_root)
                os.environ["LDP_MANIFEST_DIR"] = str(instance_root / "manifest")
                os.environ["LDP_INSTANCE_LOGS"] = str(instance_root / "logs")
                os.environ["LDP_CUT_PAD_IMAGES"] = str(instance_root / "cut-pad-images")
                os.environ["LDP_INSTANCE_MODE"] = "shared-release"
                ld = Ld(profile_value)
                ep = load_episode(f"{series}:{episode}", ld, series=series)
                probe_path = ld.probe().resolve(strict=True)
                avs = ep.path("avs_encode").resolve(strict=True)
            finally:
                for key, value in prior_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            label = str(manifest.get("pad_report_label") or episode)
            run_id = uuid.uuid4().hex
            work = probe_path / "rife-padframes-local" / label / run_id
            args = ["--audit", str(audit_path), "--repair-frames", str(repair_path),
                    "--fullres", str(fullres_path), "--run-id", run_id]
            for frame in review.get("hold_frames", []):
                args.extend(("--hold-cut", str(int(frame))))
            for frame, image in sorted((review.get("cut_images") or {}).items(), key=lambda item: int(item[0])):
                args.extend(("--cut-pad-image", f"{int(frame)}={image}"))
            for frame in review.get("accepted_residual_frames", []):
                args.extend(("--accept-residual", str(int(frame))))
            image_inputs = [Path(path).resolve(strict=True) for path in (review.get("cut_images") or {}).values()]
            avs = Path(manifest["files"]["avs_encode"]["name"])
            # Resolve through the selected profile via the same manifest accessor used by the stage.
            outputs = [probe_path / "rife-pad-replacements-local.tsv",
                       work / "rife-cpu-tta.log"]
            for frame in candidate_frames:
                outputs.extend((work / "neighbors" / f"prev_{frame:06d}.png",
                                work / "neighbors" / f"next_{frame:06d}.png",
                                work / "interpolated" / f"pad_{frame:06d}.png"))
            plan = {
                "schema": "ld-pipeline-workflow-plan-v1",
                "job_id": f"rife-{episode.lower()}-{run_id[:12]}",
                "episode": episode, "series": series, "identity": f"{series}:{episode}",
                "episode_title": str(manifest.get("title") or episode),
                "profile": str(Path(profile_value).resolve(strict=True)),
                "manifest_path": str(manifest_path),
                "instance_root": str(instance_root),
                "fingerprint_policy": self.values["fingerprint_policy"].get().strip() or "balanced",
                "decisions": [
                    {"id": "pad-decision-sha256", "value": decision_sha},
                    {"id": "rife-run-id", "value": run_id},
                    {"id": "rife-candidate-count", "value": len(rife_frames)},
                    {"id": "final-encode-remux", "value": "not included; no encode/remux approval"},
                ],
                "steps": [{
                    "id": "generate-reviewed-rife-replacements", "stage": "rife-replacements",
                    "args": args,
                    "inputs": [str(path.resolve(strict=True)) for path in
                               (manifest_path, audit_path, repair_path, fullres_path, avs, committed_path, *image_inputs)],
                    "outputs": [str(path.resolve(strict=False)) for path in outputs],
                }],
            }
            if not self._save_app_stage_plan(
                    plan, f"{series}-{episode}-rife-replacements-plan.json", "human-reviewed RIFE replacement"):
                return
        except (WorkflowAppError, PipelineError, OSError, KeyError, TypeError, ValueError,
                json.JSONDecodeError) as exc:
            messagebox.showerror("RIFE plan not built", str(exc), parent=self.root)
            self.status.set(f"PAD decisions remain recorded; RIFE continuation stopped: {exc}")
            self.auto_start_rife_after_pad_record = False
            return
        self.auto_run_after_preview = True
        self.status.set("The RIFE-only plan is auto-saved. Previewing and running it; padfix, encode, and remux still need separate plans/approval.")
        self._launch("preview")

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
        self._reset_post_run_cleanup()
        self.pending_action = action
        self.active_command = list(command)
        self.active_process_action = action
        self.active_process_pid = None
        self.process_started_monotonic = time.monotonic()
        self.process_last_output_monotonic = self.process_started_monotonic
        self.process_last_status_second = -1
        self.process_output_tail.clear()
        self.pending_plan_hash = self._plan_digest() if action == "preview" else None
        self.pending_review_step = None
        self.waiting_review_step = None
        if not self.console_expanded:
            self._toggle_console()
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
            self.output_queue.put("__PROCESS_STARTED__" + str(self.process.pid))
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.process_last_output_monotonic = time.monotonic()
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
                if payload.get("url") == self.values["archive_url"].get().strip():
                    if payload.get("series") != self.values["series"].get().strip().upper():
                        self.resolving_episode = False
                        self.status.set("Selected series changed during Archive resolution; resolve the item again.")
                        self._update_mode_controls()
                        continue
                    self._finish_episode_resolution(payload["url"], payload["episode"],
                                                    payload["source"], payload["title"],
                                                    payload["series"])
                else:
                    self.resolving_episode = False
                self._update_mode_controls()
            elif line.startswith("__EPISODE_RESOLVE_ERROR__"):
                payload = json.loads(line.removeprefix("__EPISODE_RESOLVE_ERROR__"))
                self.resolving_episode = False
                if payload.get("url") == self.values["archive_url"].get().strip():
                    self.status.set(payload["error"])
                    messagebox.showerror("Series/episode not resolved", payload["error"], parent=self.root)
                self._update_mode_controls()
            elif line.startswith("__PROCESS_STARTED__"):
                try:
                    self.active_process_pid = int(line.removeprefix("__PROCESS_STARTED__"))
                except ValueError:
                    self.active_process_pid = None
                self.process_status_var.set(self._process_status_text())
                self.process_snapshot_button.configure(state="normal")
            elif line.startswith("__WORKFLOW_DONE__"):
                code = line.removeprefix("__WORKFLOW_DONE__")
                review_to_open = self.pending_review_step if code == "3" else None
                completed_action = self.pending_action
                auto_advance = (code == "0" and completed_action == "run" and
                                self.auto_advance_after_run)
                auto_run = (code == "0" and completed_action == "preview" and
                            self.auto_run_after_preview)
                auto_rife = (code == "0" and completed_action in ("run", "approve") and
                             getattr(self, "auto_start_rife_after_pad_record", False))
                if (self.pending_action == "preview" and code == "0" and
                        self.pending_plan_hash is not None and self._plan_digest() == self.pending_plan_hash):
                    self.previewed_plan_hash = self.pending_plan_hash
                self.auto_advance_after_run = False
                self.auto_run_after_preview = False
                if code == "4" or code not in ("0", "3"):
                    self.auto_start_rife_after_pad_record = False
                elif code == "0" and not (auto_run or auto_rife):
                    self.auto_start_rife_after_pad_record = False
                completion_plan = getattr(self, "plan", None) or {}
                self.post_run_cleanup_ready = cleanup_review_available(
                    code, completed_action, completion_plan)
                self.process = None
                if getattr(self, "process_status_var", None):
                    self.process_status_var.set("Workflow subprocess finished; inspect its state/log before continuing.")
                if hasattr(self, "process_snapshot_button"):
                    self.process_snapshot_button.configure(state="normal")
                self._set_running(False)
                if code == "4":
                    self.status.set("Review denied. This workflow job is terminal; reconcile the evidence and create a new plan.")
                else:
                    self.status.set(f"Workflow command finished (exit {code}). Review its output/state before continuing.")
                if code == "0" and self.pending_action in ("run", "resume", "approve"):
                    self._cleanup_pad_previews()
                self.pending_action = None
                self.pending_plan_hash = None
                self.pending_review_step = None
                if review_to_open:
                    self.waiting_review_step = review_to_open
                    step = next((row for row in (self.plan or {}).get("steps", [])
                                 if row.get("kind") == "review" and row.get("id") == review_to_open), None)
                    if step:
                        self.root.after_idle(lambda review_step=step: self._open_review_popup(review_step))
                elif auto_advance:
                    self.root.after_idle(self._auto_start_stack_reference)
                elif auto_run:
                    self.root.after_idle(lambda: self._launch("run", automatic=True))
                elif auto_rife:
                    self.auto_start_rife_after_pad_record = False
                    self.root.after_idle(self._auto_start_rife_replacements)
            else:
                self.process_last_output_monotonic = time.monotonic()
                self.process_output_tail.append(line.rstrip())
                review_match = re.search(r"REVIEW REQUIRED \[([^\]]+)\]", line)
                if review_match:
                    self.pending_review_step = review_match.group(1)
                self._append_output(line)
        self._update_process_monitor()
        self.root.after(100, self._drain_output)

    def _process_status_text(self) -> str:
        if self.process_started_monotonic is None:
            return "No active workflow subprocess."
        now = time.monotonic()
        elapsed = max(0.0, now - self.process_started_monotonic)
        last = self.process_last_output_monotonic or self.process_started_monotonic
        return process_status_text(
            self.active_process_action or "workflow", self.active_process_pid,
            elapsed, max(0.0, now - last),
            bool(self.process and self.process.poll() is None))[0]

    def _update_process_monitor(self) -> None:
        if not getattr(self, "process_status_var", None):
            return
        if not self.process or self.process.poll() is not None:
            return
        now = time.monotonic()
        second = int(now)
        if second == self.process_last_status_second:
            return
        self.process_last_status_second = second
        elapsed = max(0.0, now - (self.process_started_monotonic or now))
        last = self.process_last_output_monotonic or now
        text, stalled = process_status_text(
            self.active_process_action or "workflow", self.process.pid,
            elapsed, max(0.0, now - last), self.process.poll() is None)
        self.process_status_var.set(text)
        if stalled:
            self.status.set("No output for 20+ minutes. Preserve evidence and inspect manually; this app will not kill or retry the process.")
        if hasattr(self, "process_snapshot_button"):
            self.process_snapshot_button.configure(state="normal")

    def _save_process_diagnostics(self) -> None:
        plan = self.plan or {}
        instance = plan.get("instance_root")
        if instance:
            instance_root = Path(str(instance)).expanduser()
            if instance_root.is_symlink():
                messagebox.showerror("Diagnostics unavailable", "The active episode workspace may not be a symlink.", parent=self.root)
                return
            log_root = instance_root / "logs"
        else:
            profile_value = str(plan.get("profile") or self.values["profile"].get()).strip()
            if not profile_value:
                messagebox.showerror("Diagnostics unavailable", "The active plan has no profile/log root.", parent=self.root)
                return
            try:
                profile = json.loads(Path(profile_value).expanduser().read_text(encoding="utf-8-sig"))
                raw_logs = (profile.get("roots") or {}).get("logs")
                if not raw_logs:
                    raise ValueError("machine profile has no roots.logs")
                log_root = Path(os.path.expandvars(os.path.expanduser(str(raw_logs))))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                messagebox.showerror("Diagnostics unavailable", str(exc), parent=self.root)
                return
        try:
            if log_root.is_symlink():
                raise ValueError("active logs folder may not be a symlink")
            log_root.mkdir(parents=True, exist_ok=True)
            state_path, workflow_log = self._workflow_audit_paths()
            now = time.monotonic()
            started = self.process_started_monotonic
            last = self.process_last_output_monotonic
            elapsed = max(0.0, now - started) if started is not None else None
            idle = max(0.0, now - last) if last is not None else None
            snapshot = {
                "schema": "fromstuntokill-process-diagnostics-v1",
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "episode": plan.get("episode"), "series": plan.get("series", "DS9"),
                "identity": plan.get("identity"), "job_id": plan.get("job_id"),
                "action": self.active_process_action, "pid": self.active_process_pid,
                "process_alive": bool(self.process and self.process.poll() is None),
                "elapsed_seconds": elapsed, "seconds_since_console_output": idle,
                "stall_warning_threshold_seconds": STALL_WARNING_SECONDS,
                "automatic_termination_or_retry": False,
                "command": self.active_command or [],
                "workflow_state": str(state_path) if state_path else None,
                "workflow_log": str(workflow_log) if workflow_log else None,
                "recent_console_output": list(self.process_output_tail),
            }
            profile_value = str(plan.get("profile") or self.values["profile"].get()).strip()
            if profile_value:
                try:
                    profile_data = json.loads(Path(profile_value).expanduser().read_text(encoding="utf-8-sig"))
                    if (profile_data.get("shared") or {}).get("enabled") is True:
                        from dependency_lock import redact_shared_text, shared_redactions
                        snapshot_text = redact_shared_text(
                            json.dumps(snapshot, ensure_ascii=False),
                            shared_redactions(profile_value, profile_data))
                    else:
                        snapshot_text = json.dumps(snapshot, ensure_ascii=False)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
                    raise ValueError(f"cannot safely prepare diagnostic snapshot redactions: {exc}") from exc
            else:
                snapshot_text = json.dumps(snapshot, ensure_ascii=False)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            safe_identity = re.sub(r"[^A-Za-z0-9._-]+", "-", str(snapshot["identity"] or "active"))
            path = log_root / f"workflow-diagnostics-{safe_identity}-{stamp}-{uuid.uuid4().hex[:6]}.json"
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(json.loads(snapshot_text), ensure_ascii=False, indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            messagebox.showinfo("Process diagnostics saved", str(path), parent=self.root)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Diagnostics not saved", str(exc), parent=self.root)

    def _append_output(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        for button in (self.preview_button, self.run_button, self.resume_button, self.approve_button,
                       self.pad_review_button, self.pad_classify_button, self.episode_status_button,
                       self.stage_console_button,
                       self.prepare_retime_button, self.review_retime_button,
                       self.verify_efm_button, self.select_efm_button, self.workbench_button):
            button.configure(state=state)
        if hasattr(self, "post_run_cleanup_button"):
            self.post_run_cleanup_button.configure(
                state="normal" if (not running and self.post_run_cleanup_ready) else "disabled")
        self.verbose_logging_check.configure(state=state)
        if hasattr(self, "process_snapshot_button") and running:
            self.process_snapshot_button.configure(state="normal")
        if hasattr(self, "resolve_button"):
            can_resolve = (not running and self.form_editable and
                           bool(self.values["archive_url"].get().strip()) and not self.resolving_episode)
            self.resolve_button.configure(state="normal" if can_resolve else "disabled")

    def _on_close(self) -> None:
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno("Workflow is running",
                                       "The workflow command will continue in the background if the GUI closes. Close anyway?",
                                       parent=self.root):
                return
        if (self.opencode_help_audit and
                not Path(self.opencode_help_audit["done"]).is_file()):
            if not messagebox.askyesno(
                    "OpenCode help is running",
                    "The separate OpenCode terminal will continue if this GUI closes. Its audit bundle will be saved when it exits. Close anyway?",
                    parent=self.root):
                return
        self._cleanup_pad_previews()
        if self.episode_dashboard:
            self.episode_dashboard.close()
            self.episode_dashboard = None
        if self.active_workbench and self.active_workbench.window:
            self.active_workbench.window.destroy()
            self.active_workbench = None
        if self.post_run_cleanup_window:
            self.post_run_cleanup_window.close()
            self.post_run_cleanup_window = None
        if self.pad_classification_window:
            self.pad_classification_window.close()
            self.pad_classification_window = None
        self.root.destroy()

    def _cleanup_pad_previews(self) -> None:
        for reviewer in list(self.pad_review_windows):
            reviewer.close()
        self.pad_review_windows.clear()
        for cache in list(self.pad_review_caches):
            cache.cleanup()
        self.pad_review_caches.clear()


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
