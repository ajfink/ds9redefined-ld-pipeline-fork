#!/usr/bin/env python3
"""Read-only episode gate dashboard for the FromStunToKill desktop app."""
from __future__ import annotations

import json
import hashlib
import os
import queue
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk
import tkinter as tk
from typing import Any
import uuid

ROOT = Path(__file__).resolve().parent.parent
VALIDATION_STAGES = (
    ("manifest", "Manifest"), ("inputs", "Source inputs"), ("stack", "Stack"),
    ("cadence", "Cadence"), ("audio", "Audio"), ("retime", "Retime"),
    ("encode", "Encode readiness"), ("padfix", "PAD repair"),
    ("deliverable", "Delivery QC"),
)
APP_ACTIONS = {
    "manifest": "Resolve identity and manifest inputs through the intake/local-source workflow.",
    "inputs": "Resolve source/input failures; verify and explicitly select EFM captures when applicable.",
    "stack": "Use Build next stack plan, then inspect Orc source/pair/frame evidence before approval.",
    "cadence": "Use the Cadence workbench; review TFM/motion/DI-map evidence and record only measured trims.",
    "audio": "Use Audio diagnostics or the EFM verify/select workflow; reports do not auto-select a rate.",
    "retime": "Prepare the report-only retime candidate plan; adjudicate each pairing and preserve LIMITs.",
    "encode": "Use the Delivery plan; validate first, then separately approve any final encode.",
    "padfix": "Review PAD cut candidates and still/hold safeguards before RIFE and padfix generation.",
    "deliverable": "Use the Delivery plan for QC-only or gated remux/QC; never infer delivery from an encode alone.",
}
CHECK_RE = re.compile(r"^\[(PASS|WARN|FAIL|SKIP|INFO)\]\s+([^:]+):\s*(.*)$")


def parse_gate_output(stage: str, return_code: int, output: str) -> dict[str, Any]:
    """Summarize validator output without collapsing WARN/LIMIT into PASS."""
    rows = []
    for line in output.splitlines():
        match = CHECK_RE.match(line.strip())
        if match:
            rows.append({"status": match.group(1), "name": match.group(2).strip(),
                         "detail": match.group(3).strip()})
    failures = sum(row["status"] == "FAIL" for row in rows)
    warnings = sum(row["status"] == "WARN" or "LIMIT" in row["detail"].upper() for row in rows)
    skips = sum(row["status"] == "SKIP" for row in rows)
    if failures or return_code not in (0, 1):
        status = "FAIL"
    elif warnings or skips:
        status = "WARN / LIMIT"
    elif return_code == 1 or not rows:
        status = "FAIL"
    else:
        status = "PASS"
    summary = (f"{sum(row['status'] == 'PASS' for row in rows)} pass, {warnings} warn/limit, "
               f"{skips} skipped, {failures} fail")
    if not rows:
        summary = next((line.strip() for line in reversed(output.splitlines()) if line.strip()),
                       f"validator exited {return_code} without gate rows")
    return {"stage": stage, "status": status, "return_code": return_code,
            "summary": summary, "rows": rows, "output": output}


def next_gate_summary(results: dict[str, dict[str, Any]]) -> str:
    for stage, label in VALIDATION_STAGES:
        result = results.get(stage)
        if result is None:
            return f"Next unverified gate: {label}. Suggested app action: {APP_ACTIONS[stage]}"
        if result["status"] == "FAIL":
            return (f"STOP at {label}: resolve its first failed check before later stages. "
                    f"Suggested app action: {APP_ACTIONS[stage]}")
        if result["status"] == "WARN / LIMIT":
            return (f"Review the warning/limit at {label}; it is not a PASS. "
                    f"Suggested app action: {APP_ACTIONS[stage]}")
    return ("All current validation gates passed without warnings or limits. "
            "If this is the delivery boundary, use the separately gated Delivery plan and inspect QC.")


def save_gate_evidence(log_root: Path, identity: str, result: dict[str, Any]) -> tuple[Path, Path]:
    """Write an immutable per-gate text log and JSON summary after an explicit check."""
    if log_root.is_symlink():
        raise ValueError(f"gate evidence log root may not be a symlink: {log_root}")
    log_root.mkdir(parents=True, exist_ok=True)
    token = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:6]
    safe_identity = re.sub(r"[^A-Za-z0-9._-]+", "-", identity)
    stem = f"episode-gate-{safe_identity}-{result['stage']}-{token}"
    log_path = log_root / f"{stem}.log"
    report_path = log_root / f"{stem}.json"
    output = str(result.get("output", ""))
    with log_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(output)
        if output and not output.endswith("\n"):
            stream.write("\n")
    report = {key: result.get(key) for key in ("stage", "status", "return_code", "summary")}
    report.update({"schema": "fromstuntokill-episode-gate-report-v1",
                   "identity": identity, "created_utc": datetime.now(timezone.utc).isoformat(),
                   "log_name": log_path.name,
                   "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest()})
    with report_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return log_path, report_path


def _validator_environment(profile: Path, episode: str, series: str,
                           instance_root: Path | None, manifest_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({"LDP_PROFILE": str(profile), "LDP_SERIES": series,
                "LDP_EPISODE": episode, "LDP_IDENTITY": f"{series}:{episode}"})
    if instance_root:
        env.update({"LDP_INSTANCE_ROOT": str(instance_root),
                    "LDP_MANIFEST_DIR": str(instance_root / "manifest"),
                    "LDP_INSTANCE_LOGS": str(instance_root / "logs"),
                    "LDP_CUT_PAD_IMAGES": str(instance_root / "cut-pad-images")})
        try:
            workspace = json.loads((instance_root / "workspace.json").read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read active workspace metadata: {exc}") from exc
        mode = workspace.get("storage_mode")
        if mode not in ("shared-release", "local-fallback"):
            raise ValueError(f"unsupported workspace storage mode: {mode!r}")
        env["LDP_INSTANCE_MODE"] = mode
    else:
        for key in ("LDP_INSTANCE_ROOT", "LDP_MANIFEST_DIR", "LDP_INSTANCE_LOGS",
                    "LDP_CUT_PAD_IMAGES", "LDP_INSTANCE_MODE"):
            env.pop(key, None)
        env["LDP_MANIFEST_DIR"] = str(manifest_path.parent)
    return env


class EpisodeGateDashboard:
    """Run the selected existing read-only validator and save unique gate evidence."""

    def __init__(self, parent: tk.Misc, episode: str, series: str, profile: Path,
                 manifest: Path, instance_root: Path | None = None,
                 log_root: Path | None = None):
        self.parent = parent
        self.episode = episode.upper()
        self.series = series.upper()
        self.identity = f"{self.series}:{self.episode}"
        raw_profile = profile.expanduser()
        raw_manifest = manifest.expanduser()
        raw_instance = instance_root.expanduser() if instance_root else None
        if raw_profile.is_symlink() or raw_manifest.is_symlink() or (raw_instance and raw_instance.is_symlink()):
            raise ValueError("episode dashboard paths may not be symbolic links")
        self.profile = raw_profile.resolve(strict=True)
        self.manifest = raw_manifest.resolve(strict=True)
        self.instance_root = raw_instance.resolve(strict=True) if raw_instance else None
        raw_log_root = log_root.expanduser() if log_root else (
            self.instance_root / "logs" if self.instance_root else None)
        if raw_log_root and raw_log_root.is_symlink():
            raise ValueError("episode dashboard log root may not be a symlink")
        self.log_root = raw_log_root
        self.window: tk.Toplevel | None = None
        self.queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.running = False
        self.results: dict[str, dict[str, Any]] = {}
        self.stage_var: tk.StringVar | None = None
        self.summary_var: tk.StringVar | None = None
        self.status_var: tk.StringVar | None = None
        self.details: tk.Text | None = None
        self.tree: ttk.Treeview | None = None
        self.check_button: ttk.Button | None = None
        self.check_all_button: ttk.Button | None = None

    def show(self) -> None:
        self.window = tk.Toplevel(self.parent)
        self.window.title(f"Episode status — {self.identity}")
        self.window.geometry("920x610")
        self.window.minsize(760, 500)
        self.window.transient(self.parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(self.window, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Read-only gate dashboard — {self.identity}",
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(body, text=f"Manifest: {self.manifest}", wraplength=880).pack(anchor="w", pady=(3, 2))
        ttk.Label(body, text="Checks run the existing validator only; they do not write media or manifests. "
                            "Each check saves a unique gate log/report under the episode logs folder. "
                            "The audio gate may hash large TBC/WAV files.",
                  wraplength=880).pack(anchor="w", pady=(0, 8))

        main = ttk.Frame(body)
        main.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(main, columns=("status", "summary"), show="tree headings", height=9)
        self.tree.heading("#0", text="Gate")
        self.tree.heading("status", text="Status")
        self.tree.heading("summary", text="Summary")
        self.tree.column("#0", width=150, stretch=False)
        self.tree.column("status", width=110, stretch=False, anchor="center")
        self.tree.column("summary", width=550, stretch=True)
        scroll = ttk.Scrollbar(main, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for stage, label in VALIDATION_STAGES:
            self.tree.insert("", "end", iid=stage, text=label,
                             values=("NOT CHECKED", "Run this gate to refresh status"))
        self.tree.bind("<<TreeviewSelect>>", self._show_selected_details)

        self.summary_var = tk.StringVar(value=next_gate_summary(self.results))
        ttk.Label(body, textvariable=self.summary_var, wraplength=880,
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 4))
        self.status_var = tk.StringVar(value="Select one gate or explicitly run the full read-only sweep.")
        ttk.Label(body, textvariable=self.status_var, wraplength=880).pack(anchor="w", pady=(0, 5))
        detail_frame = ttk.LabelFrame(body, text="Selected gate details", padding=5)
        detail_frame.pack(fill="both", expand=True)
        self.details = tk.Text(detail_frame, wrap="word", height=8, state="disabled")
        detail_scroll = ttk.Scrollbar(detail_frame, orient="vertical", command=self.details.yview)
        self.details.configure(yscrollcommand=detail_scroll.set)
        self.details.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(8, 0))
        self.check_button = ttk.Button(buttons, text="Check selected gate", command=self.check_selected)
        self.check_button.pack(side="left")
        self.check_all_button = ttk.Button(buttons, text="Check all gates…", command=self.check_all)
        self.check_all_button.pack(side="left", padx=6)
        ttk.Button(buttons, text="Close", command=self.close).pack(side="right")
        self.window.after(100, self._drain)

    def _selected_stage(self) -> str | None:
        if not self.tree:
            return None
        selected = self.tree.selection()
        return selected[0] if selected else None

    def check_selected(self) -> None:
        stage = self._selected_stage()
        if not stage:
            messagebox.showinfo("Select a gate", "Select one gate in the table first.", parent=self.window)
            return
        if stage == "audio" and not messagebox.askyesno(
                "Audio gate may hash large files",
                "The audio validator rechecks source/WAV provenance hashes and may read large files. Run it now?",
                parent=self.window):
            return
        self._start([stage])

    def check_all(self) -> None:
        if not messagebox.askyesno(
                "Run all read-only gates",
                "This runs manifest through delivery validation and saves unique logs/reports under "
                "the episode logs folder. It does not alter media or manifests, but the audio gate "
                "may hash large source/WAV files. Continue?",
                parent=self.window):
            return
        self._start([stage for stage, _label in VALIDATION_STAGES])

    def _start(self, stages: list[str]) -> None:
        if self.running or not self.window:
            return
        self.running = True
        self._set_buttons("disabled")
        self.status_var.set("Running read-only validators; only unique episode gate logs/reports are written.")
        threading.Thread(target=self._run_checks, args=(stages,), daemon=True).start()

    def _run_checks(self, stages: list[str]) -> None:
        try:
            env = _validator_environment(self.profile, self.episode, self.series,
                                         self.instance_root, self.manifest)
            profile_data = json.loads(self.profile.read_text(encoding="utf-8-sig"))
            if (profile_data.get("shared") or {}).get("enabled") is True:
                from dependency_lock import redact_shared_text, shared_redactions
                redactions = shared_redactions(self.profile, profile_data)
            else:
                redactions = []
            for stage in stages:
                self.queue.put(("stage-start", stage))
                command = [sys.executable, str(ROOT / "scripts" / "validate_episode.py"),
                           "--episode", self.episode, "--stage", stage]
                try:
                    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                                            text=True, encoding="utf-8", errors="replace")
                    output = (result.stdout or "") + (("\n[stderr]\n" + result.stderr)
                                                       if result.stderr else "")
                    if redactions:
                        output = redact_shared_text(output, redactions)
                    parsed = parse_gate_output(stage, result.returncode, output)
                except OSError as exc:
                    parsed = parse_gate_output(stage, 2, f"Could not start validator: {exc}")
                self.queue.put(("stage-result", parsed))
                if parsed["status"] == "FAIL" and stage == "manifest":
                    break
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self.queue.put(("error", str(exc)))
        finally:
            self.queue.put(("finished", None))

    def _drain(self) -> None:
        if not self.window:
            return
        while True:
            try:
                event, value = self.queue.get_nowait()
            except queue.Empty:
                break
            if event == "stage-start":
                label = dict(VALIDATION_STAGES)[value]
                self.status_var.set(f"Checking {label}…")
                self.tree.item(value, values=("RUNNING", "Read-only validator active"))
            elif event == "stage-result":
                self.results[value["stage"]] = value
                saved = ""
                if self.log_root:
                    try:
                        _log_path, report_path = save_gate_evidence(
                            self.log_root, self.identity, value)
                        saved = f" Evidence: {report_path.name}."
                    except (OSError, ValueError) as exc:
                        saved = f" Could not save gate evidence: {exc}."
                label = dict(VALIDATION_STAGES)[value["stage"]]
                summary = value["summary"]
                first_issue = next((row for row in value["rows"]
                                    if row["status"] in ("FAIL", "WARN", "SKIP") or
                                    "LIMIT" in row["detail"].upper()), None)
                if first_issue:
                    summary += f" — {first_issue['name']}: {first_issue['detail']}"
                self.tree.item(value["stage"], text=label,
                               values=(value["status"], summary))
                if self._selected_stage() == value["stage"]:
                    self._set_details(value["output"])
                self.summary_var.set(next_gate_summary(self.results))
                self.status_var.set(f"{label}: {value['status']}.{saved}")
            elif event == "error":
                self.status_var.set(value)
            elif event == "finished":
                self.running = False
                self._set_buttons("normal")
                self.status_var.set("Validation sweep finished. WARN/LIMIT is not a PASS; inspect the details.")
        self.window.after(100, self._drain)

    def _show_selected_details(self, _event=None) -> None:
        stage = self._selected_stage()
        if stage and stage in self.results:
            self._set_details(self.results[stage]["output"])

    def _set_details(self, text: str) -> None:
        if not self.details:
            return
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("end", text)
        self.details.configure(state="disabled")

    def _set_buttons(self, state: str) -> None:
        for button in (self.check_button, self.check_all_button):
            if button:
                button.configure(state=state)

    def close(self) -> None:
        if self.window:
            if self.running and not messagebox.askyesno(
                    "Validation is running",
                    "The read-only validator subprocess will continue if this dashboard closes. Close anyway?",
                    parent=self.window):
                return
            window = self.window
            self.window = None
            window.destroy()
