#!/usr/bin/env python3
"""Human visual review and immutable report creation for PAD cut/RIFE decisions."""
from __future__ import annotations

import csv
import getpass
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ldcommon import Ld, PipelineError, assert_writable_path, load_episode  # noqa: E402
from pad_review import PadReviewError, PreviewCache  # noqa: E402
from record_pad_classifications import (  # noqa: E402
    MAX_MEAN, MAX_RGB, RIFE_MISIDENTIFICATION_WARNING, SCHEMA,
)
from rife_padframes import extract_neighbors  # noqa: E402
from shared_workspace import validate_device_config_profile, validate_instance  # noqa: E402


class PadClassificationReviewError(RuntimeError):
    """PAD audit evidence or visual review inputs are unavailable."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_tsv(path: Path, required: tuple[str, ...], label: str) -> dict[int, dict[str, str]]:
    if path.is_symlink() or not path.is_file():
        raise PadClassificationReviewError(f"{label} is missing or unsafe: {path}")
    rows: dict[int, dict[str, str]] = {}
    try:
        with path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream, delimiter="\t")
            if not reader.fieldnames or not set(required).issubset(reader.fieldnames):
                raise PadClassificationReviewError(f"{label} is missing required columns {required}")
            for row in reader:
                try:
                    frame = int(row["frame"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise PadClassificationReviewError(f"{label} has an invalid frame value") from exc
                if frame < 0 or frame in rows:
                    raise PadClassificationReviewError(f"{label} has a negative or duplicate frame {frame}")
                rows[frame] = row
    except (OSError, csv.Error) as exc:
        raise PadClassificationReviewError(f"cannot read {label} {path}: {exc}") from exc
    return rows


def load_classification_evidence(episode: str, series: str, manifest_path: Path,
                                 probe: Path) -> tuple[dict, list[dict], dict]:
    """Load and hash the exact audit inputs; do not classify any candidate automatically."""
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PadClassificationReviewError(f"cannot read episode manifest: {exc}") from exc
    identity = f"{series.upper()}:{episode.upper()}"
    if (str(manifest.get("episode", "")).upper() != episode.upper() or
            str(manifest.get("series", "DS9")).upper() != series.upper() or
            str(manifest.get("identity", identity)).upper() != identity):
        raise PadClassificationReviewError(f"manifest identity does not match {identity}")

    paths = {
        "pad_audit": probe / f"{episode}-pad-audit.tsv",
        "repair_frames": probe / f"{episode}-pad-repair-frames.txt",
        "fullres_report": probe / f"{episode}-pad-fullres.tsv",
    }
    audit_rows = _read_tsv(paths["pad_audit"], ("frame", "is_predicted_pad", "time"), "PAD audit")
    fullres_rows = _read_tsv(paths["fullres_report"], ("frame", "rgb_max", "rgb_mean"), "full-raster report")
    try:
        candidate_list = [int(value) for value in paths["repair_frames"].read_text(encoding="utf-8-sig").split()]
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise PadClassificationReviewError(f"cannot parse PAD repair list: {exc}") from exc
    if len(candidate_list) != len(set(candidate_list)) or any(frame < 0 for frame in candidate_list):
        raise PadClassificationReviewError("PAD repair list contains duplicate or negative frame indices")
    video_frames = (manifest.get("video") or {}).get("frames")
    if type(video_frames) is not int or video_frames <= 0:
        raise PadClassificationReviewError("manifest video.frames must be measured before PAD review")
    if any(frame >= video_frames for frame in candidate_list):
        raise PadClassificationReviewError("PAD repair list contains a frame outside manifest video.frames")

    candidates = []
    for frame in sorted(candidate_list):
        audit = audit_rows.get(frame)
        raster = fullres_rows.get(frame)
        if not audit or audit.get("is_predicted_pad") != "1" or not raster:
            raise PadClassificationReviewError(f"repair frame {frame} lacks map-matched audit/full-raster evidence")
        try:
            rgb_max = int(raster["rgb_max"])
            rgb_mean = float(raster["rgb_mean"])
            time_s = float(audit["time"])
            motion_score = float(audit.get("motion_score") or "nan")
        except (KeyError, TypeError, ValueError) as exc:
            raise PadClassificationReviewError(f"candidate {frame} has malformed evidence") from exc
        if not (0 <= rgb_max <= 255 and 0 <= rgb_mean <= 255):
            raise PadClassificationReviewError(f"candidate {frame} has out-of-range RGB evidence")
        if not (time_s >= 0):
            raise PadClassificationReviewError(f"candidate {frame} has invalid time")
        candidates.append({"frame": frame, "time": time_s, "motion_score": motion_score,
                           "rgb_max": rgb_max, "rgb_mean": rgb_mean,
                           "residual_requires_acceptance": rgb_max > MAX_RGB or rgb_mean > MAX_MEAN})

    pad_data = manifest.get("pads") or {}
    cut_frames = pad_data.get("cut_pad_frames") or []
    if (not isinstance(cut_frames, list) or any(type(frame) is not int or frame < 0 for frame in cut_frames) or
            len(cut_frames) != len(set(cut_frames))):
        raise PadClassificationReviewError("manifest pads.cut_pad_frames must be a unique integer list")
    if not set(cut_frames).issubset(set(candidate_list)):
        raise PadClassificationReviewError("manifest cut-PAD frames are not present in the confirmed repair list")
    files = manifest.get("files") or {}
    dvd_available = manifest.get("dvd_reference_available")
    if dvd_available is None:
        dvd_available = bool(files.get("dvd"))
    report = {
        "schema": SCHEMA,
        "status": "HUMAN_REVIEWED",
        "identity": identity,
        "episode": episode.upper(),
        "series": series.upper(),
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "dvd_reference_available": bool(dvd_available),
        "cut_pad_images_choice": manifest.get("cut_pad_images_choice"),
        "original_cut_frames": sorted(cut_frames),
        "reviewed_by": getpass.getuser(),
        "reviewed_at_utc": datetime.now(timezone.utc).isoformat(),
        "rife_misidentification_warning": RIFE_MISIDENTIFICATION_WARNING,
    }
    evidence = {}
    for key, path in paths.items():
        evidence[key] = str(path.resolve(strict=True))
        evidence[key + "_sha256"] = sha256_file(path)
    report["evidence"] = evidence
    return manifest, candidates, report


class PadClassificationReviewWindow:
    """Review every repair candidate, with real left/right flanks, before committing decisions."""

    def __init__(self, parent: tk.Misc, episode: str, series: str, profile_path: Path,
                 manifest_path: Path, instance_root: Path,
                 on_report: Callable[[Path, str, dict], bool] | None = None,
                 on_close: Callable[[PreviewCache], None] | None = None):
        self.parent = parent
        self.episode = episode.upper()
        self.series = series.upper()
        self.profile_path = profile_path
        self.manifest_path = manifest_path
        self.instance_root = instance_root
        self.on_report = on_report
        self.on_close = on_close
        self.window: tk.Toplevel | None = None
        self.cache: PreviewCache | None = None
        self.photos: list[ImageTk.PhotoImage] = []
        self.items: list[dict] = []
        self.decisions: dict[int, dict] = {}
        self.index = 0
        self.manifest: dict = {}
        self.report_base: dict = {}
        self.probe: Path | None = None
        self.video: Path | None = None
        self.ld: Ld | None = None
        self.classification_var: tk.StringVar | None = None
        self.replacement_var: tk.StringVar | None = None
        self.image_path_var: tk.StringVar | None = None
        self.misclassified_var: tk.BooleanVar | None = None
        self.residual_var: tk.BooleanVar | None = None
        self.hold_confirm_var: tk.BooleanVar | None = None
        self.note_var: tk.StringVar | None = None
        self.caption_var: tk.StringVar | None = None
        self.position_var: tk.StringVar | None = None
        self.preview_frame: ttk.Frame | None = None
        self.misclassified_check: ttk.Checkbutton | None = None
        self.residual_check: ttk.Checkbutton | None = None
        self.hold_check: ttk.Checkbutton | None = None

    def show(self) -> None:
        try:
            env_keys = ("LDP_PROFILE", "LDP_SERIES", "LDP_EPISODE", "LDP_IDENTITY",
                        "LDP_INSTANCE_ROOT", "LDP_MANIFEST_DIR", "LDP_INSTANCE_LOGS",
                        "LDP_CUT_PAD_IMAGES", "LDP_INSTANCE_MODE")
            prior = {key: os.environ.get(key) for key in env_keys}
            try:
                self._set_episode_environment()
                self.ld = Ld(self.profile_path)
                profile = self.ld.profile
                validate_device_config_profile(self.profile_path, profile, ROOT)
                validate_instance(str(self.instance_root), self.profile_path, profile,
                                  f"{self.series}:{self.episode}")
                self.probe = self.ld.probe().resolve(strict=True)
                self.manifest, self.items, self.report_base = load_classification_evidence(
                    self.episode, self.series, self.manifest_path.resolve(strict=True), self.probe)
                from ldcommon import Episode
                ep = load_episode(f"{self.series}:{self.episode}", self.ld, series=self.series)
                self.video = ep.path("avs_encode").resolve(strict=True)
                if self.video.suffix.lower() not in (".avs", ".vpy"):
                    raise PadClassificationReviewError("PAD context preview requires source-derived .avs/.vpy")
                self.cache = PreviewCache()
            finally:
                self._restore_environment(prior)
        except (PadClassificationReviewError, PadReviewError, PipelineError, RuntimeError, OSError) as exc:
            messagebox.showerror("PAD classification unavailable", str(exc), parent=self.parent)
            return

        self.window = tk.Toplevel(self.parent)
        self.window.title(f"PAD cut/RIFE classification — {self.series}:{self.episode}")
        self.window.geometry("1240x830")
        self.window.minsize(900, 680)
        self.window.transient(self.parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(self.window, padding=10)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"Human visual classification — {self.series}:{self.episode}",
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(body, text=RIFE_MISIDENTIFICATION_WARNING, wraplength=1180,
                  foreground="#8b1a1a").pack(anchor="w", pady=(4, 8))
        self.caption_var = tk.StringVar()
        ttk.Label(body, textvariable=self.caption_var, wraplength=1180).pack(anchor="w", pady=(0, 6))
        self.preview_frame = ttk.Frame(body, padding=4)
        self.preview_frame.pack(fill="both", expand=True)

        controls = ttk.LabelFrame(body, text="Classification and evidence", padding=8)
        controls.pack(fill="x", pady=(8, 0))
        self.classification_var = tk.StringVar(value="")
        ttk.Label(controls, text="Human decision:").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(controls, text="Non-cut PAD → RIFE", variable=self.classification_var,
                        value="rife", command=self._save_current).grid(row=0, column=1, sticky="w")
        ttk.Radiobutton(controls, text="Cut PAD → still/hold", variable=self.classification_var,
                        value="cut", command=self._save_current).grid(row=0, column=2, sticky="w")

        self.misclassified_var = tk.BooleanVar(value=False)
        self.misclassified_check = ttk.Checkbutton(
            controls, text="I confirm the earlier cut classification was incorrect; I reviewed the warning above.",
            variable=self.misclassified_var, command=self._save_current)
        self.misclassified_check.grid(row=1, column=0, columnspan=3, sticky="w")

        ttk.Label(controls, text="Cut replacement:").grid(row=2, column=0, sticky="w", pady=(3, 0))
        self.replacement_var = tk.StringVar(value="")
        dvd_available = bool(self.report_base["dvd_reference_available"])
        replacement_values = ("image",) if dvd_available else ("image", "hold")
        self.replacement_box = ttk.Combobox(controls, textvariable=self.replacement_var,
                                            values=replacement_values, state="readonly", width=12)
        self.replacement_box.grid(row=2, column=1, sticky="w", pady=(3, 0))
        self.replacement_box.bind("<<ComboboxSelected>>", lambda _event: self._save_current())
        self.image_path_var = tk.StringVar(value="")
        self.image_entry = ttk.Entry(controls, textvariable=self.image_path_var, width=72)
        self.image_entry.grid(row=2, column=2, sticky="ew", pady=(3, 0))
        self.image_entry.bind("<FocusOut>", lambda _event: self._save_current())
        ttk.Button(controls, text="Choose still…", command=self._choose_image).grid(
            row=2, column=3, padx=(6, 0), pady=(3, 0))

        self.hold_confirm_var = tk.BooleanVar(value=False)
        self.hold_check = ttk.Checkbutton(
            controls, text="Confirm no-DVD previous-frame hold for this cut PAD.",
            variable=self.hold_confirm_var, command=self._save_current)
        self.hold_check.grid(row=3, column=0, columnspan=3, sticky="w")
        self.residual_var = tk.BooleanVar(value=False)
        self.residual_check = ttk.Checkbutton(
            controls, text="I reviewed and accept this above-threshold full-raster residual as a PAD.",
            variable=self.residual_var, command=self._save_current)
        self.residual_check.grid(row=4, column=0, columnspan=3, sticky="w")
        ttk.Label(controls, text="Review note:").grid(row=5, column=0, sticky="w", pady=(5, 0))
        self.note_var = tk.StringVar()
        ttk.Entry(controls, textvariable=self.note_var).grid(
            row=5, column=1, columnspan=3, sticky="ew", pady=(5, 0))
        controls.columnconfigure(2, weight=1)

        nav = ttk.Frame(body)
        nav.pack(fill="x", pady=(8, 0))
        ttk.Button(nav, text="← Previous", command=self.previous).pack(side="left")
        self.position_var = tk.StringVar()
        ttk.Label(nav, textvariable=self.position_var).pack(side="left", expand=True)
        ttk.Button(nav, text="Next →", command=self.next).pack(side="right")
        ttk.Button(nav, text="Save reviewed decisions", command=self.save_report).pack(side="right", padx=8)

        if not self.items:
            messagebox.showinfo("No PAD candidates", "The confirmed repair list is empty; no classification is needed.", parent=self.window)
            self.close()
            return
        self._load_current()

    def _set_episode_environment(self) -> None:
        os.environ["LDP_PROFILE"] = str(self.profile_path.resolve(strict=True))
        os.environ["LDP_SERIES"] = self.series
        os.environ["LDP_EPISODE"] = self.episode
        os.environ["LDP_IDENTITY"] = f"{self.series}:{self.episode}"
        os.environ["LDP_INSTANCE_ROOT"] = str(self.instance_root.resolve(strict=True))
        os.environ["LDP_MANIFEST_DIR"] = str(self.instance_root / "manifest")
        os.environ["LDP_INSTANCE_LOGS"] = str(self.instance_root / "logs")
        os.environ["LDP_CUT_PAD_IMAGES"] = str(self.instance_root / "cut-pad-images")
        try:
            workspace = json.loads((self.instance_root / "workspace.json").read_text(encoding="utf-8-sig"))
            mode = workspace.get("storage_mode")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PadClassificationReviewError(f"cannot verify device workspace metadata: {exc}") from exc
        if mode not in ("shared-release", "local-fallback"):
            raise PadClassificationReviewError(f"unsupported device workspace storage mode: {mode!r}")
        os.environ["LDP_INSTANCE_MODE"] = mode

    @staticmethod
    def _restore_environment(prior: dict[str, str | None]) -> None:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _save_current(self) -> None:
        if not self.items or self.index >= len(self.items):
            return
        row = self.items[self.index]
        frame = row["frame"]
        decision = dict(self.decisions.get(frame, {}))
        decision.update({
            "frame": frame,
            "classification": self.classification_var.get().strip(),
            "confirmed_misidentified_cut": bool(self.misclassified_var.get()),
            "rife_warning_acknowledged": bool(self.misclassified_var.get()),
            "cut_replacement": self.replacement_var.get().strip(),
            "cut_image_path": self.image_path_var.get().strip(),
            "confirmed_hold": bool(self.hold_confirm_var.get()),
            "accept_residual": bool(self.residual_var.get()),
        })
        self.decisions[frame] = decision
        self._update_choice_controls(frame)

    def _update_choice_controls(self, frame: int) -> None:
        if not self.items or self.index >= len(self.items):
            return
        row = self.items[self.index]
        is_cut = self.classification_var.get() == "cut"
        was_cut = frame in set(self.report_base.get("original_cut_frames", []))
        if not (was_cut and self.classification_var.get() == "rife"):
            self.misclassified_var.set(False)
        self.misclassified_check.configure(
            state=("normal" if was_cut and self.classification_var.get() == "rife" else "disabled"))
        self.hold_check.configure(
            state=("normal" if is_cut and not self.report_base["dvd_reference_available"] else "disabled"))
        self.residual_check.configure(
            state=("normal" if row["residual_requires_acceptance"] else "disabled"))
        self.replacement_box.configure(state=("readonly" if is_cut else "disabled"))
        self.image_entry.configure(state=("normal" if is_cut else "disabled"))

    def _load_current(self) -> None:
        if not self.items or not self.window or not self.cache or not self.preview_frame:
            return
        row = self.items[self.index]
        frame = row["frame"]
        decision = self.decisions.get(frame, {})
        old_cuts = set(self.report_base["original_cut_frames"])
        self.classification_var.set(decision.get("classification", ""))
        self.misclassified_var.set(bool(decision.get("confirmed_misidentified_cut", False)))
        self.replacement_var.set(decision.get("cut_replacement", ""))
        self.image_path_var.set(decision.get("cut_image_path", ""))
        self.hold_confirm_var.set(bool(decision.get("confirmed_hold", False)))
        self.residual_var.set(bool(decision.get("accept_residual", False)))
        self._update_choice_controls(frame)
        self.position_var.set(f"Candidate {self.index + 1} / {len(self.items)} — frame {frame}")
        score = row["motion_score"]
        score_text = f"{score:.6f}" if score == score else "unavailable"
        self.caption_var.set(
            f"Frame {frame} at {row['time']:.3f}s; across-flank score={score_text}; "
            f"full-raster rgb_max={row['rgb_max']}, rgb_mean={row['rgb_mean']:.9f}. "
            "Use the two real flanks, not a black-frame metric, to decide whether a cut lies between them.")
        for child in self.preview_frame.winfo_children():
            child.destroy()
        self.photos.clear()
        try:
            assert self.ld is not None and self.video is not None
            assert self.probe is not None
            env_keys = ("LDP_PROFILE", "LDP_SERIES", "LDP_EPISODE", "LDP_IDENTITY",
                        "LDP_INSTANCE_ROOT", "LDP_MANIFEST_DIR", "LDP_INSTANCE_LOGS",
                        "LDP_CUT_PAD_IMAGES", "LDP_INSTANCE_MODE")
            prior = {key: os.environ.get(key) for key in env_keys}
            try:
                self._set_episode_environment()
                prev, nxt = extract_neighbors(self.ld, self.video, frame, self.cache.path)
            finally:
                self._restore_environment(prior)
            for title, source in ((f"Source flank −1 ({frame - 1})", prev),
                                  (f"Source flank +1 ({frame + 1})", nxt)):
                with Image.open(source) as image:
                    image = image.convert("RGB")
                    image.thumbnail((560, 430), Image.Resampling.LANCZOS)
                    photo = ImageTk.PhotoImage(image)
                self.photos.append(photo)
                column = ttk.Frame(self.preview_frame)
                column.pack(side="left", fill="both", expand=True, padx=6)
                ttk.Label(column, text=title, anchor="center").pack(fill="x", pady=4)
                ttk.Label(column, image=photo, anchor="center").pack(fill="both", expand=True)
        except (OSError, PipelineError, PadReviewError) as exc:
            ttk.Label(self.preview_frame, text=f"Could not preview source flanks:\n{exc}",
                      wraplength=900).pack(expand=True)

    def previous(self) -> None:
        self._save_current()
        if self.items:
            self.index = max(0, self.index - 1)
            self._load_current()

    def next(self) -> None:
        self._save_current()
        if self.items:
            self.index = min(len(self.items) - 1, self.index + 1)
            self._load_current()

    def _choose_image(self) -> None:
        if not self.classification_var or self.classification_var.get() != "cut":
            return
        path = filedialog.askopenfilename(
            parent=self.window, title="Choose a reviewed/provenanced cut-PAD still",
            initialdir=str(self.instance_root / "cut-pad-images"),
            filetypes=(("PNG stills", "*.png"),))
        if path:
            self.image_path_var.set(path)
            self._save_current()

    def save_report(self) -> None:
        self._save_current()
        note = self.note_var.get().strip()
        if len(note) < 8:
            messagebox.showerror("Review note required", "Enter a specific review note of at least 8 characters.", parent=self.window)
            return
        missing = [item["frame"] for item in self.items
                   if self.decisions.get(item["frame"], {}).get("classification") not in ("cut", "rife")]
        if missing:
            messagebox.showerror("Classify every candidate", f"No human decision for frames: {missing[:20]}", parent=self.window)
            return
        old_cuts = set(self.report_base["original_cut_frames"])
        invalid = []
        for item in self.items:
            row = self.decisions[item["frame"]]
            frame = item["frame"]
            if item["residual_requires_acceptance"] and row.get("accept_residual") is not True:
                invalid.append(f"{frame}: residual not accepted")
            if row.get("classification") == "rife" and frame in old_cuts and (
                    row.get("confirmed_misidentified_cut") is not True or
                    row.get("rife_warning_acknowledged") is not True):
                invalid.append(f"{frame}: prior cut correction warning not acknowledged")
            if row.get("classification") == "cut":
                replacement = row.get("cut_replacement")
                if replacement == "image" and not row.get("cut_image_path"):
                    invalid.append(f"{frame}: reviewed still not selected")
                elif replacement == "hold" and (
                        self.report_base["dvd_reference_available"] or
                        row.get("confirmed_hold") is not True):
                    invalid.append(f"{frame}: hold is invalid or unconfirmed")
                elif replacement not in ("image", "hold"):
                    invalid.append(f"{frame}: cut replacement policy not selected")
                elif (self.report_base["dvd_reference_available"] and replacement != "image"):
                    invalid.append(f"{frame}: DVD-backed cut needs a reviewed still")
        if invalid:
            messagebox.showerror("PAD decisions incomplete", "\n".join(invalid[:20]), parent=self.window)
            return
        report = dict(self.report_base)
        report["review_note"] = note
        report["candidates"] = [self.decisions[item["frame"]] for item in self.items]
        if any(row.get("classification") == "rife" and row["frame"] in set(report["original_cut_frames"])
               for row in report["candidates"]):
            if report["rife_misidentification_warning"] != RIFE_MISIDENTIFICATION_WARNING:
                messagebox.showerror("Warning mismatch", "Required RIFE warning is missing or changed.", parent=self.window)
                return
        if not messagebox.askyesno(
                "Save and record human PAD decisions",
                "This creates an immutable decision report and a workflow plan that updates the manifest only after you run it.\n\n"
                "The RIFE stage remains protected by its manifest cut-PAD gate.", parent=self.window):
            return
        try:
            plans = self.instance_root / "plans"
            plans.mkdir(parents=True, exist_ok=True)
            assert self.ld is not None
            path = plans / f"{self.episode}-pad-classification-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}.json"
            assert_writable_path(path, self.ld)
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            digest = sha256_file(path)
        except (OSError, PipelineError) as exc:
            messagebox.showerror("Decision report not saved", str(exc), parent=self.window)
            return
        if self.on_report and not self.on_report(path, digest, report):
            return
        self.close()

    def close(self) -> None:
        if self.window:
            self.window.destroy()
            self.window = None
        if self.cache:
            if self.on_close:
                self.on_close(self.cache)
            else:
                self.cache.cleanup()
            self.cache = None
