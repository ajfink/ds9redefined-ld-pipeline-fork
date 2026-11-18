#!/usr/bin/env python3
"""Evidence table for explicitly reviewing V15 retime-break candidates."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
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
                                           list[str], str], bool],
                 alignment_assets_path: Path | None = None,
                 alignment_assets_sha256: str | None = None,
                 dvd_audio_assets_path: Path | None = None):
        self.parent = parent
        self.report_path = report_path.expanduser()
        self.expected_identity = expected_identity
        self.on_create_plan = on_create_plan
        self.alignment_assets_path = alignment_assets_path
        self.alignment_assets_sha256 = alignment_assets_sha256
        self.dvd_audio_assets_path = dvd_audio_assets_path
        self.window: tk.Toplevel | None = None
        self.rows: dict[str, dict[str, Any]] = {}
        self.row_ids: dict[str, str] = {}
        self.decisions: dict[str, str] = {}
        self.report: dict[str, Any] = {}
        self.report_sha256 = ""
        self.tree: ttk.Treeview | None = None
        self.detail: tk.Text | None = None
        self.review_note: tk.Text | None = None
        self.create_button: ttk.Button | None = None
        self.visual_label: ttk.Label | None = None
        self.visual_note: ttk.Label | None = None
        self._sample_photo = None
        self._sample_frames: list[Any] = []
        self._sample_index = 0
        self._sample_after: str | None = None
        self.alignment_assets: dict[str, Any] = {}
        self.samples_by_candidate: dict[str, dict[str, Any]] = {}
        self.dvd_audio_samples: dict[str, dict[str, Any]] = {}
        self.audio_choice: tk.StringVar | None = None
        self._load_report()
        if self.alignment_assets_path:
            self._load_alignment_assets()
        if self.dvd_audio_assets_path:
            self._load_dvd_audio_assets()

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

    def _load_alignment_assets(self) -> None:
        path = self.alignment_assets_path
        if path is None or path.is_symlink() or not path.is_file():
            raise RetimeBreakReviewError("approved alignment's paired visual evidence is missing or unsafe")
        try:
            payload = path.read_bytes()
            if (self.alignment_assets_sha256 and
                    hashlib.sha256(payload).hexdigest() != self.alignment_assets_sha256):
                raise RetimeBreakReviewError("approved alignment asset index changed after review")
            value = json.loads(payload.decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RetimeBreakReviewError(f"cannot read paired alignment evidence: {exc}") from exc
        inputs = self.report.get("inputs") or {}
        asset_root = path.parent.resolve(strict=True)
        if (value.get("schema") != "dvd-alignment-review-assets-v1" or
                value.get("identity") != self.expected_identity or
                value.get("candidate_is_approval") is not False or
                value.get("alignment_sha256") != inputs.get("alignment_sha256")):
            raise RetimeBreakReviewError("paired-frame assets do not match the reviewed alignment report")
        contact = value.get("contact_sheet")
        if contact:
            contact_path = Path(str(contact))
            try:
                contact_path.resolve(strict=True).relative_to(asset_root)
            except (OSError, ValueError) as exc:
                raise RetimeBreakReviewError("alignment contact sheet is outside its review asset folder") from exc
            if (contact_path.is_symlink() or not contact_path.is_file() or
                    hashlib.sha256(contact_path.read_bytes()).hexdigest() != value.get("contact_sheet_sha256")):
                raise RetimeBreakReviewError("alignment contact sheet changed or is missing")
        self.alignment_assets = value
        points = value.get("review_points") or []
        for label, candidate in self.rows.items():
            interval = candidate.get("ld_black") or []
            if (len(interval) != 2 or not points or
                    candidate.get("pair_status") == "alignment-uncovered" or
                    candidate.get("projected_dvd_interval_s") is None):
                continue
            midpoint = (float(interval[0]) + float(interval[1])) / 2
            closest = min(points, key=lambda row: abs(float(row.get("master_time_s", 0)) - midpoint))
            clip = Path(str(closest.get("playback_path") or ""))
            try:
                clip.resolve(strict=True).relative_to(asset_root)
            except (OSError, ValueError):
                continue
            if (closest.get("asset_status") == "READY" and not clip.is_symlink() and clip.is_file() and
                    hashlib.sha256(clip.read_bytes()).hexdigest() == closest.get("playback_sha256")):
                self.samples_by_candidate[label] = {
                    **closest,
                    "distance_s": abs(float(closest.get("master_time_s", 0)) - midpoint),
                }

    def _load_dvd_audio_assets(self) -> None:
        path = self.dvd_audio_assets_path
        if path is None or path.is_symlink() or not path.is_file():
            raise RetimeBreakReviewError("DVD audio audition index is missing or unsafe")
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RetimeBreakReviewError(f"cannot read DVD audio audition assets: {exc}") from exc
        if (value.get("schema") != "retime-dvd-audio-review-v1" or
                value.get("identity") != self.expected_identity or
                value.get("candidate_is_approval") is not False or
                value.get("candidate_report_sha256") != self.report_sha256 or
                value.get("manifest_sha256") != self.report.get("manifest_sha256")):
            raise RetimeBreakReviewError("DVD audio audition assets do not match this candidate report")
        dvd_path = Path(str(value.get("dvd_source") or ""))
        report_inputs = self.report.get("inputs") or {}
        try:
            dvd_stat = dvd_path.stat()
            dvd_matches = (
                dvd_path.is_file() and
                dvd_path.name == report_inputs.get("dvd_name") and
                dvd_stat.st_size == report_inputs.get("dvd_size_bytes") and
                dvd_stat.st_mtime_ns == report_inputs.get("dvd_mtime_ns") and
                hashlib.sha256(dvd_path.read_bytes()).hexdigest() == report_inputs.get("dvd_sha256") ==
                value.get("dvd_source_sha256"))
        except OSError:
            dvd_matches = False
        if not dvd_matches:
            raise RetimeBreakReviewError("DVD audio excerpts do not match the reviewed source")
        self.dvd_audio_samples = {}
        audio_root = path.parent.resolve(strict=True)
        for row in value.get("samples", []):
            if row.get("status") != "READY":
                continue
            sample = Path(str(row.get("path") or ""))
            if sample.is_symlink() or not sample.is_file():
                continue
            try:
                sample = sample.resolve(strict=True)
                sample.relative_to(audio_root)
            except (OSError, ValueError):
                continue
            if hashlib.sha256(sample.read_bytes()).hexdigest() == row.get("sha256"):
                self.dvd_audio_samples[str(row.get("id"))] = {**row, "path": str(sample)}

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
        dialog.geometry("1180x820")
        dialog.minsize(900, 680)
        dialog.transient(self.parent)
        dialog.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Report-only candidates for {self.expected_identity}",
                  font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(body, text=f"Candidate evidence fingerprint: {self.report_sha256[:16]}…",
                  wraplength=1140, justify="left").pack(anchor="w", pady=(4, 6))
        parameters = self.report.get("parameters") or {}
        inputs = self.report.get("inputs") or {}
        summary = ttk.LabelFrame(body, text="Detection parameters and bound input evidence", padding=5)
        summary.pack(fill="x", pady=(0, 8))
        summary_text = (
            f"LD luma threshold {parameters.get('ld_luma_threshold', 'unavailable')}  |  "
            f"DVD silence {parameters.get('dvd_silence_noise_db', 'unavailable')} dB / "
            f"{parameters.get('dvd_silence_minimum_duration_s', 'unavailable')} s  |  "
            f"pair tolerance {parameters.get('pair_tolerance_s', 'unavailable')} s  |  "
            f"alignment spread limit {parameters.get('max_alignment_offset_spread_frames', 'unavailable')} frames\n"
            f"Alignment SHA-256: {str(inputs.get('alignment_sha256') or 'unavailable')[:16]}…  |  "
            f"DVD source: {inputs.get('dvd_name', 'unavailable')}")
        ttk.Label(summary, text=summary_text, wraplength=1140,
                  justify="left").pack(anchor="w")
        ttk.Label(body,
                  text="No candidate is preselected. Unique pairs may be recorded only after review. "
                       "A no-silence candidate may be marked merged-through only with an explicit human decision.",
                   wraplength=1140, justify="left").pack(anchor="w", pady=(0, 8))

        table_frame = ttk.Frame(body)
        table_frame.pack(fill="both", expand=True)
        columns = ("label", "status", "ld_black", "dvd_pairs", "decision")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings",
                                 selectmode="browse", height=6)
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
            self.row_ids[label] = f"candidate-{index}"
        if not self.rows:
            ttk.Label(
                body, text="No boundary candidates were measured for this parameter set. "
                          "The report is retained; no manifest values changed.",
                wraplength=1140).pack(anchor="w", pady=(0, 5))

        visual = ttk.LabelFrame(body, text="Paired scene / DVD silence evidence", padding=5)
        visual.pack(fill="x", pady=(7, 0))
        self.visual_label = ttk.Label(visual, text="Select a candidate to show nearby paired frames.")
        self.visual_label.pack(anchor="center")
        self.visual_note = ttk.Label(
            visual, text=("The displayed clip is the nearest sampled alignment context, not an extrapolated "
                          "boundary. DVD silence intervals above are independent audio-detector evidence."),
            wraplength=1140)
        self.visual_note.pack(anchor="w")
        visual_buttons = ttk.Frame(visual)
        visual_buttons.pack(fill="x")
        ttk.Button(visual_buttons, text="Play paired sample",
                   command=self._play_alignment_sample).pack(side="left")
        ttk.Button(visual_buttons, text="Stop",
                   command=self._stop_alignment_sample).pack(side="left", padx=5)
        self.audio_choice = tk.StringVar(value="")
        self.audio_box = ttk.Combobox(visual_buttons, textvariable=self.audio_choice,
                                      values=(), state="readonly", width=16)
        self.audio_box.pack(side="left", padx=(12, 4))
        ttk.Button(visual_buttons, text="Audition DVD audio",
                   command=self._play_dvd_audio_sample).pack(side="left")

        self.detail = tk.Text(body, height=7, wrap="word", state="disabled")
        self.detail.pack(fill="x", pady=(8, 4))
        self.tree.bind("<<TreeviewSelect>>", self._show_selected_detail)
        decisions = ttk.Frame(body)
        decisions.pack(fill="x", pady=(0, 6))
        ttk.Button(decisions, text="Mark as unique cut", command=self._mark_cut).pack(side="left")
        ttk.Button(decisions, text="Select all unambiguous pairs",
                   command=self._select_all_unique).pack(side="left", padx=6)
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
        row = self.rows[label]
        pairs = row.get("pair_candidates") or []
        pair_text = "; ".join(
            f"{candidate.get('id', '?')} {self._interval([candidate.get('start_s'), candidate.get('end_s')])} "
            f"(gap {float(candidate.get('distance_s', 0)):.3f}s)"
            for candidate in pairs) or "No DVD audio-silence interval matched."
        spread = row.get("offset_spread_frames")
        spread_text = f"{float(spread):.1f} frames" if isinstance(spread, (int, float)) else "unavailable"
        details = (
            f"{label} — {row.get('pair_status', 'unknown')}\n"
            f"LD/master black interval: {self._interval(row.get('ld_black'))}; "
            f"projected DVD interval: {self._interval(row.get('projected_dvd_interval_s'))}\n"
            f"Alignment offset spread across this interval: {spread_text}\n"
            f"DVD audio-silence evidence: {pair_text}\n"
            "Candidate only. No retime offset, step, or gap is implied.")
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", details)
        self.detail.configure(state="disabled")
        self._show_alignment_sample(label)
        if self.audio_choice and self.tree:
            available = [str(pair.get("id")) for pair in row.get("pair_candidates", [])
                         if str(pair.get("id")) in self.dvd_audio_samples]
            projected_id = f"LD-{label}"
            if projected_id in self.dvd_audio_samples:
                available.insert(0, projected_id)
            self.audio_box.configure(values=available)
            self.audio_choice.set(available[0] if available else "")

    def _show_alignment_sample(self, label: str) -> None:
        if not self.visual_label or not self.visual_note:
            return
        sample = self.samples_by_candidate.get(label)
        if not sample:
            self.visual_label.configure(image="", text="No nearby verified paired-frame sample.")
            self.visual_note.configure(
                text="The candidate is outside the reviewed sample points; no frame was extrapolated.")
            return
        try:
            from PIL import Image, ImageTk
            path = Path(str(sample["playback_path"]))
            with Image.open(path) as image:
                frame = image.convert("RGB")
                frame.thumbnail((720, 270), Image.Resampling.LANCZOS)
                self._sample_photo = ImageTk.PhotoImage(frame)
            self.visual_label.configure(image=self._sample_photo, text="")
            self.visual_note.configure(
                text=(f"Nearest sampled pair is {sample['distance_s']:.3f}s from this candidate's LD-black "
                      f"midpoint: master f{sample['master_frame']} ↔ DVD f{sample['dvd_frame']}, "
                      f"local offset {sample['offset_frames']:+d}. The separate DVD silence interval "
                      "remains the audio evidence; the clip does not infer audio origin."))
        except (OSError, ImportError) as exc:
            self.visual_label.configure(image="", text=f"Paired sample unavailable: {exc}")

    def _play_alignment_sample(self) -> None:
        label = self._selected_label()
        sample = self.samples_by_candidate.get(label or "")
        if not sample or not self.window or not self.visual_label:
            return
        try:
            from PIL import Image, ImageSequence, ImageTk
            path = Path(str(sample["playback_path"]))
            if hashlib.sha256(path.read_bytes()).hexdigest() != sample.get("playback_sha256"):
                raise OSError("paired sample changed after verification")
            with Image.open(path) as image:
                self._sample_frames = [
                    ImageTk.PhotoImage(frame.convert("RGB").copy())
                    for frame in ImageSequence.Iterator(image)
                ]
        except (OSError, ImportError) as exc:
            messagebox.showerror("Paired sample unavailable", str(exc), parent=self.window)
            return
        self._stop_alignment_sample()
        self._sample_index = 0
        self._tick_alignment_sample()

    def _tick_alignment_sample(self) -> None:
        if not self.window or not self.visual_label or not self._sample_frames:
            return
        self.visual_label.configure(image=self._sample_frames[self._sample_index], text="")
        self._sample_index = (self._sample_index + 1) % len(self._sample_frames)
        self._sample_after = self.window.after(100, self._tick_alignment_sample)

    def _stop_alignment_sample(self) -> None:
        if self.window and self._sample_after:
            try:
                self.window.after_cancel(self._sample_after)
            except tk.TclError:
                pass
        self._sample_after = None
        self._sample_frames = []

    def _play_dvd_audio_sample(self) -> None:
        if not self.window or not self.audio_choice:
            return
        sample = self.dvd_audio_samples.get(self.audio_choice.get().strip())
        if not sample:
            messagebox.showinfo(
                "DVD audio excerpt unavailable",
                "No verified DVD silence excerpt is available for this pairing.",
                parent=self.window)
            return
        path = Path(str(sample["path"]))
        try:
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sample.get("sha256"):
                raise OSError("DVD audio excerpt changed after verification; rebuild candidate assets.")
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("DVD audio audition unavailable", str(exc), parent=self.window)

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

    def _select_all_unique(self) -> None:
        """Offer a one-click batch choice only for globally unique, non-colliding pairs."""
        eligible = [
            label for label, row in self.rows.items()
            if row.get("pair_status") == "unique-pair" and
            len(row.get("pair_candidates") or []) == 1
        ]
        if not eligible:
            messagebox.showinfo(
                "No unambiguous pairs",
                "No candidate has exactly one non-colliding DVD-silence match. "
                "Ambiguous, transition, uncovered, and unmatched rows stay for explicit review.",
                parent=self.window)
            return
        if not messagebox.askyesno(
                "Select unambiguous pairs",
                f"Select {len(eligible)} unique-pair candidate(s) for backup-safe recording? "
                "Ambiguous and colliding candidates will not be selected.",
                parent=self.window):
            return
        for label in eligible:
            self.decisions[label] = "record unique pair"
            if self.tree and label in self.row_ids:
                values = list(self.tree.item(self.row_ids[label], "values"))
                values[-1] = "record unique pair"
                self.tree.item(self.row_ids[label], values=values)
        if self.create_button:
            self.create_button.configure(state="normal")

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
        self._stop_alignment_sample()
        if self.window:
            self.window.destroy()
            self.window = None
