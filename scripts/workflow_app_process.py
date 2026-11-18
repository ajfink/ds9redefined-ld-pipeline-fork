#!/usr/bin/env python3
"""Process-control mixin for the Tk desktop workflow shell."""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox
from typing import Any

from dependency_lock import redact_shared_text, shared_redactions
from job_monitor import STALL_WARNING_SECONDS, process_status_text
from post_run_cleanup import cleanup_review_available
from workflow_process_control import (ProcessControlError, resume_process_tree,
                                      suspend_process_tree)

ROOT = Path(__file__).resolve().parent.parent


class WorkflowProcessMixin:
    def _start_process(self, command: list[str], action: str) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("Workflow is running", "Wait for the current workflow command to finish.", parent=self.root)
            return
        self._reset_post_run_cleanup()
        self.pending_action = action
        self.active_command = list(command)
        self.active_process_action = action
        self.active_process_pid = None
        self.process_paused = False
        self.process_pause_token = None
        self.process_started_monotonic = time.monotonic()
        self.process_last_output_monotonic = self.process_started_monotonic
        self.process_last_status_second = -1
        self.process_output_tail.clear()
        self.pending_plan_hash = self._plan_digest() if action == "preview" else None
        self.pending_review_step = None
        self.waiting_review_step = None
        self.auto_skipped_review_steps: set[str] = set()
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
                                            bufsize=1, creationflags=creationflags,
                                            start_new_session=(os.name != "nt"))
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
                self._set_running(True)
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
                completion_stages = [step.get("stage") for step in completion_plan.get("steps", [])
                                     if step.get("kind", "stage") == "stage"]
                completion_reviews = [step.get("id") for step in completion_plan.get("steps", [])
                                      if step.get("kind") == "review"]
                completion_decisions = {row.get("id") for row in completion_plan.get("decisions", [])
                                        if isinstance(row, dict)}
                auto_select_efm = (code == "0" and completed_action in ("run", "resume", "approve") and
                                   any(stage in completion_stages for stage in
                                       ("verify-efm-capture", "decode-efm-captures")))
                auto_decode_efm = (
                    code == "0" and completed_action == "approve" and
                    "render-stack" in completion_stages and
                    "review-stack-evidence" in completion_reviews)
                retime_report = next((Path(output)
                                      for step in completion_plan.get("steps", [])
                                      if step.get("stage") == "prepare-retime-breaks"
                                      for output in step.get("outputs", [])
                                      if str(output).casefold().endswith(".json")), None)
                retime_review_assets = next(
                    (Path(output) for step in completion_plan.get("steps", [])
                     if step.get("stage") == "retime-review-assets"
                     for output in step.get("outputs", [])), None)
                auto_review_retime = (code == "0" and completed_action in ("run", "resume", "approve") and
                                      retime_report is not None)
                auto_classify_pad = (code == "0" and completed_action in ("run", "resume", "approve") and
                                     "fullres-check" in completion_stages)
                auto_review_interpolated_pad = (
                    code == "0" and completed_action in ("run", "resume", "approve") and
                    "rife-replacements" in completion_stages)
                auto_encode_validate = (code == "0" and completed_action in ("run", "resume", "approve") and
                                        "build-padfix" in completion_stages)
                auto_delivery_workbench = (
                    code == "0" and completed_action in ("run", "resume", "approve") and
                    any(stage in completion_stages for stage in
                        ("encode-validate", "audio-promote-no-dvd")))
                auto_audio_workbench = (code == "0" and completed_action in ("run", "resume", "approve") and
                                        "record-efm-capture" in completion_stages)
                auto_review_audio_evidence = (
                    code == "0" and completed_action in ("run", "resume", "approve") and
                    "audio-evidence-suite" in completion_decisions)
                alignment_sample_dir = next(
                    (Path(output) for step in completion_plan.get("steps", [])
                     if step.get("stage") == "alignment-samples"
                     for output in step.get("outputs", [])), None)
                auto_review_alignment_samples = (
                    code == "0" and completed_action in ("run", "resume", "approve") and
                    alignment_sample_dir is not None)
                alignment_review_dir = next(
                    (Path(output) for step in completion_plan.get("steps", [])
                     if step.get("stage") == "alignment-review-assets"
                     for output in step.get("outputs", [])), None)
                auto_review_dvd_alignment = (
                    code == "0" and completed_action in ("run", "resume", "approve") and
                    alignment_review_dir is not None)
                alignment_review_decision = next(
                    (str(row.get("value")) for row in completion_plan.get("decisions", [])
                     if isinstance(row, dict) and row.get("id") == "alignment-visual-decision"), "")
                auto_continue_dvd_alignment_review = (
                    code == "0" and completed_action in ("run", "resume", "approve") and
                    "record-dvd-alignment-review" in completion_stages and
                    alignment_review_decision in ("APPROVED", "REJECTED"))
                cadence_review_dir = next(
                    (Path(output) for step in completion_plan.get("steps", [])
                     if step.get("stage") == "cadence-review-assets"
                     for output in step.get("outputs", [])), None)
                auto_open_cadence_review = (
                    code == "0" and completed_action in ("run", "resume", "approve") and
                    cadence_review_dir is not None)
                auto_continue_cadence_after_trim_review = (
                    code == "0" and completed_action in ("run", "resume", "approve") and
                    "record-video-trim-review" in completion_stages)
                cadence_review_decision = next(
                    (str(row.get("value")) for row in completion_plan.get("decisions", [])
                     if isinstance(row, dict) and row.get("id") == "cadence-visual-decision"), "")
                auto_continue_cadence_review = (
                    code == "0" and completed_action in ("run", "resume", "approve") and
                    "record-cadence-review" in completion_stages and
                    cadence_review_decision in ("APPROVED", "REJECTED"))
                auto_measure_break_edges = (code == "0" and completed_action in ("run", "resume", "approve") and
                                            "record-retime-breaks" in completion_stages)
                break_edge_report_complete = (code == "0" and
                                              "measure-break-edges" in completion_stages)
                auto_reference_review_skipped = (code == "0" and
                    "review-reference-selection" in getattr(self, "auto_skipped_review_steps", set()))
                auto_continue_stack = (
                    code == "0" and completed_action in ("run", "resume", "approve") and
                    ("record-orc-reference-measurements" in completion_stages or
                     "stack-reference" in completion_stages))
                self.post_run_cleanup_ready = cleanup_review_available(
                    code, completed_action, completion_plan)
                self.process = None
                self.process_paused = False
                self.process_pause_token = None
                if getattr(self, "process_status_var", None):
                    self.process_status_var.set("Workflow subprocess finished; inspect its state/log before continuing.")
                if hasattr(self, "process_snapshot_button"):
                    self.process_snapshot_button.configure(state="normal")
                self._set_running(False)
                if code == "4":
                    self.status.set("Review denied. This workflow job is terminal; reconcile the evidence and create a new plan.")
                elif auto_continue_stack or auto_reference_review_skipped:
                    self.status.set(
                        "Orc reference selection completed. Recording its verified measurements and starting the next stack step.")
                elif break_edge_report_complete:
                    self.status.set(
                        "Break-edge report completed. It records measurements only; retime offsets and steps remain unset pending adjudication.")
                else:
                    self.status.set(f"Workflow command finished (exit {code}). Review its output/state before continuing.")
                if code == "0" and self.pending_action in ("run", "resume", "approve"):
                    self._cleanup_pad_previews()
                self.pending_action = None
                self.pending_plan_hash = None
                self.pending_review_step = None
                self.auto_skipped_review_steps = set()
                if review_to_open:
                    self.waiting_review_step = review_to_open
                    step = next((row for row in (self.plan or {}).get("steps", [])
                                 if row.get("kind") == "review" and row.get("id") == review_to_open), None)
                    if step:
                        self.root.after_idle(lambda review_step=step: self._open_review_popup(review_step))
                elif auto_advance:
                    self.root.after_idle(self._auto_start_stack_reference)
                elif auto_continue_stack:
                    self.root.after_idle(self._auto_start_stack_reference)
                elif auto_decode_efm:
                    self.root.after_idle(self._auto_start_efm_decode)
                elif auto_run:
                    self.root.after_idle(lambda: self._launch("run"))
                elif auto_rife:
                    self.auto_start_rife_after_pad_record = False
                    self.root.after_idle(self._auto_start_rife_replacements)
                elif auto_select_efm:
                    self.root.after_idle(self._auto_select_efm_capture_if_ready)
                elif auto_review_retime:
                    if retime_review_assets is None:
                        self.root.after_idle(
                            lambda path=retime_report:
                            self._review_retime_break_candidates(path))
                    else:
                        self.root.after_idle(
                            lambda path=retime_report, assets=retime_review_assets:
                            self._review_retime_break_candidates(path, assets))
                elif auto_classify_pad:
                    self.root.after_idle(self._classify_pad_candidates)
                elif auto_review_interpolated_pad:
                    self.root.after_idle(lambda: self._review_pad_frames(auto_continue=True))
                elif auto_encode_validate:
                    self.root.after_idle(self._auto_start_encode_validation)
                elif auto_delivery_workbench:
                    self.root.after_idle(self._continue_to_audio_delivery)
                elif auto_audio_workbench:
                    self.root.after_idle(self._auto_start_audio_evidence)
                elif auto_review_audio_evidence:
                    self.root.after_idle(
                        lambda plan_snapshot=completion_plan:
                        self._review_audio_timing_evidence(plan_snapshot))
                elif auto_review_alignment_samples:
                    self.root.after_idle(
                        lambda path=alignment_sample_dir / "index.json":
                        self._review_alignment_samples(path))
                elif auto_review_dvd_alignment:
                    self.root.after_idle(
                        lambda path=alignment_review_dir / "index.json":
                        self._review_dvd_alignment(path))
                elif auto_continue_dvd_alignment_review:
                    self.root.after_idle(
                        lambda decision=alignment_review_decision:
                        self._after_dvd_alignment_review(decision))
                elif auto_continue_cadence_after_trim_review:
                    self.root.after_idle(self._auto_start_cadence_evidence)
                elif auto_open_cadence_review:
                    self.root.after_idle(
                        lambda path=cadence_review_dir / "index.json":
                        self._review_cadence_evidence(path))
                elif auto_continue_cadence_review:
                    if cadence_review_decision == "APPROVED":
                        self.root.after_idle(self._cadence_review_approved)
                    else:
                        self.root.after_idle(self._cadence_review_rejected)
                elif auto_measure_break_edges:
                    self.root.after_idle(self._auto_start_break_edge_measurement)
            else:
                self.process_last_output_monotonic = time.monotonic()
                self.process_output_tail.append(line.rstrip())
                review_match = re.search(r"REVIEW REQUIRED \[([^\]]+)\]", line)
                if review_match:
                    self.pending_review_step = review_match.group(1)
                    self._set_running(True)
                skipped_match = re.search(r"REPORT REVIEW AUTO-SKIPPED \[([^\]]+)\]", line)
                if skipped_match:
                    self.auto_skipped_review_steps.add(skipped_match.group(1))
                self._append_output(line)
        self._update_process_monitor()
        self.root.after(100, self._drain_output)

    def _process_status_text(self) -> str:
        if getattr(self, "process_paused", False):
            return "Workflow paused; the active step is suspended and will continue on Resume."
        started = getattr(self, "process_started_monotonic", None)
        if started is None:
            return "No active workflow subprocess."
        now = time.monotonic()
        elapsed = max(0.0, now - started)
        last = getattr(self, "process_last_output_monotonic", None) or started
        return process_status_text(
            getattr(self, "active_process_action", "workflow") or "workflow",
            getattr(self, "active_process_pid", None),
            elapsed, max(0.0, now - last),
            bool(getattr(self, "process", None) and self.process.poll() is None))[0]

    def _update_process_monitor(self) -> None:
        if not getattr(self, "process_status_var", None):
            return
        if not self.process or self.process.poll() is not None:
            return
        if getattr(self, "process_paused", False):
            self.process_status_var.set(self._process_status_text())
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

    def _pause_active_process(self) -> None:
        if (not self.process or self.process.poll() is not None or
                self.pending_action not in ("run", "resume", "approve")):
            return
        if self.process_paused:
            return
        try:
            token = suspend_process_tree(self.process)
        except ProcessControlError as exc:
            messagebox.showerror("Pause unavailable", str(exc), parent=self.root)
            return
        self.process_pause_token = token
        self.process_paused = True
        self.status.set("Workflow paused. Resume will continue the same active step; no stage was restarted.")
        self.process_status_var.set(self._process_status_text())
        self._set_running(True)

    def _resume_paused_process(self) -> bool:
        if not getattr(self, "process_paused", False):
            return False
        token = self.process_pause_token
        if token is None:
            messagebox.showerror("Resume unavailable", "The paused process handle is missing; preserve logs and inspect the workflow state.", parent=self.root)
            return True
        try:
            resume_process_tree(token)
        except ProcessControlError as exc:
            messagebox.showerror("Resume unavailable", str(exc), parent=self.root)
            return True
        self.process_pause_token = None
        self.process_paused = False
        self.status.set("Workflow resumed. The active step is continuing in place.")
        self.process_status_var.set(self._process_status_text())
        self._set_running(True)
        return True

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
        busy = running or getattr(self, "dependency_updates_busy", False)
        state = "disabled" if busy else "normal"
        for button in (self.run_button, self.approve_button,
                       self.pad_review_button, self.pad_classify_button, self.episode_status_button,
                       self.dependency_update_button,
                       self.stage_console_button, self.workbench_button,
                       self.open_profile_button, self.open_template_button,
                       self.open_plan_button):
            button.configure(state=state)
        paused = bool(getattr(self, "process_paused", False))
        running_process = bool(running and self.process and self.process.poll() is None)
        pausable_action = getattr(self, "pending_action", None) in ("run", "resume", "approve")
        can_pause = (running_process and pausable_action and not paused and
                     not getattr(self, "pending_review_step", None) and
                     bool(self.active_process_pid))
        self.pause_button.configure(state="normal" if can_pause and not getattr(self, "dependency_updates_busy", False)
                                    else "disabled")
        self.resume_button.configure(state=("normal" if paused or not busy else "disabled"))
        self.select_efm_button.configure(state=state)
        self.prepare_retime_button.configure(state=state)
        if hasattr(self, "post_run_cleanup_button"):
            self.post_run_cleanup_button.configure(
                state="normal" if (not busy and self.post_run_cleanup_ready) else "disabled")
        self.verbose_logging_check.configure(state=state)
        if hasattr(self, "process_snapshot_button") and running:
            self.process_snapshot_button.configure(state="normal")
        if hasattr(self, "resolve_button"):
            can_resolve = (not busy and self.form_editable and
                           bool(self.values["archive_url"].get().strip()) and not self.resolving_episode)
            self.resolve_button.configure(state="normal" if can_resolve else "disabled")
