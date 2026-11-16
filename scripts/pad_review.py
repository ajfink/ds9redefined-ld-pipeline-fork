#!/usr/bin/env python3
"""Temporary, read-only GUI previews for cut-PAD stills and RIFE replacements."""
from __future__ import annotations

import csv
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

sys_path = Path(__file__).resolve().parent.parent
import sys
if str(sys_path) not in sys.path:
    sys.path.insert(0, str(sys_path))
from ldcommon import Episode, Ld, PipelineError  # noqa: E402
from vspipe_util import run_source_ffmpeg  # noqa: E402

OWNER_FILE = ".fromstuntokill-pad-review-cache.json"


class PadReviewError(RuntimeError):
    """Review evidence is unavailable or cannot be safely previewed."""


@dataclass
class ReviewItem:
    kind: str
    frame: int | None
    title: str
    source: Path
    before: Path | None = None
    after: Path | None = None
    video: Path | None = None


class PreviewCache:
    """Unique app-owned temp directory; cleanup refuses paths without its marker."""
    def __init__(self) -> None:
        self.token = uuid.uuid4().hex
        self.path = Path(tempfile.mkdtemp(prefix="fromstuntokill-pad-review-"))
        self.path = self.path.resolve()
        marker = {"schema": "fromstuntokill-pad-review-cache-v1", "token": self.token}
        (self.path / OWNER_FILE).write_text(json.dumps(marker), encoding="utf-8")
        self.closed = False

    def file(self, name: str) -> Path:
        if self.closed or Path(name).name != name:
            raise PadReviewError("invalid preview cache filename")
        return self.path / name

    def cleanup(self) -> bool:
        if self.closed:
            return False
        root = Path(tempfile.gettempdir()).resolve()
        try:
            if self.path.parent.resolve() != root or not self.path.name.startswith("fromstuntokill-pad-review-"):
                return False
            if self.path.is_symlink():
                return False
            marker_path = self.path / OWNER_FILE
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if marker != {"schema": "fromstuntokill-pad-review-cache-v1", "token": self.token}:
                return False
            shutil.rmtree(self.path)
            self.closed = True
            return True
        except (OSError, ValueError, json.JSONDecodeError):
            return False


def _inside(path: Path, root: Path, what: str) -> Path:
    if path.is_symlink():
        raise PadReviewError(f"{what} is a symbolic link: {path}")
    resolved = path.expanduser().resolve(strict=True)
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise PadReviewError(f"{what} is outside its expected folder: {resolved}") from exc
    if not resolved.is_file():
        raise PadReviewError(f"{what} is not a regular file: {resolved}")
    return resolved


def _cut_image_items(episode: str, manifest: dict, instance_root: Path | None) -> list[ReviewItem]:
    roots = [Path(__file__).resolve().parent.parent / "Cut Pad Images" / episode]
    active = os.environ.get("LDP_CUT_PAD_IMAGES")
    if active:
        roots.append(Path(os.path.expandvars(os.path.expanduser(active))))
    if instance_root:
        roots.append(instance_root / "cut-pad-images")
    wanted = {int(x) for x in ((manifest.get("pads") or {}).get("cut_pad_frames") or [])}
    found: dict[Path, ReviewItem] = {}
    for directory in roots:
        if not directory.is_dir():
            continue
        for image in sorted(directory.glob("DVD-*.png"), key=lambda p: p.name.casefold()):
            if image.is_symlink() or not image.is_file():
                continue
            mapped_frame = None
            sidecar = image.with_suffix(".provenance.json")
            if sidecar.is_file():
                try:
                    provenance = json.loads(sidecar.read_text(encoding="utf-8"))
                    if str(provenance.get("episode", "")).casefold() == episode.casefold():
                        mapped_frame = int(provenance.get("output_pad_frame"))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    mapped_frame = None
            if wanted and mapped_frame is not None and mapped_frame not in wanted:
                continue
            frame_text = f"PAD frame {mapped_frame}" if mapped_frame is not None else "output-frame mapping not recorded in sidecar"
            found[image.resolve()] = ReviewItem(
                kind="cut", frame=mapped_frame,
                title=f"Cut-PAD still — {frame_text} — {image.name}", source=image)
    return list(found.values())


def discover_review_items(episode: str, manifest_path: Path, profile_path: Path,
                         instance_root: Path | None = None) -> tuple[list[ReviewItem], Path, Path]:
    """Read the manifest and replacement report; return available review evidence."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PadReviewError(f"cannot read episode manifest: {exc}") from exc
    if str(manifest.get("episode", "")).casefold() != episode.casefold():
        raise PadReviewError("selected manifest episode does not match the loaded plan")
    try:
        ld = Ld(profile_path)
        ep = Episode(episode, manifest, ld)
        probe = ld.probe().resolve(strict=True)
    except (PipelineError, OSError) as exc:
        raise PadReviewError(str(exc)) from exc
    label = str(manifest.get("pad_report_label") or episode)
    items = _cut_image_items(episode, manifest, instance_root)
    report = probe / "rife-pad-replacements-local.tsv"
    if report.is_file():
        try:
            with report.open(newline="", encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream, delimiter="\t"))
        except (OSError, csv.Error) as exc:
            raise PadReviewError(f"cannot read RIFE replacement report: {exc}") from exc
        try:
            video = ep.path("avs_encode").resolve(strict=True)
        except (PipelineError, OSError):
            video = None
        for row in rows:
            if str(row.get("episode", "")).casefold() != label.casefold():
                continue
            method = str(row.get("method", ""))
            if not method.startswith("RIFE-"):
                continue
            try:
                frame = int(row["frame"])
            except (KeyError, TypeError, ValueError):
                continue
            png_value = Path(str(row.get("png", "")))
            source = png_value if png_value.is_absolute() else probe / png_value
            try:
                replacement = _inside(source, probe, "RIFE replacement image")
            except (OSError, PadReviewError):
                continue
            neighbor_dir = probe / "rife-padframes-local" / label / "neighbors"
            before = neighbor_dir / f"prev_{frame:06d}.png"
            after = neighbor_dir / f"next_{frame:06d}.png"
            if video is None:
                continue
            try:
                before = _inside(before, probe, "preceding RIFE neighbor")
                after = _inside(after, probe, "following RIFE neighbor")
            except (OSError, PadReviewError):
                continue
            items.append(ReviewItem("rife", frame,
                                    f"RIFE replacement — output frame {frame}", replacement,
                                    before=before, after=after, video=video))
    if not items:
        raise PadReviewError(
            "no cut-PAD stills or generated RIFE replacement images were found. "
            "Cut stills belong under Cut Pad Images/<episode>; RIFE images are created by rife_padframes.py.")
    return items, probe, report


def _extract_outer_frames(ld: Ld, video: Path, frame: int, cache: PreviewCache,
                          episode: str, instance_root: Path | None) -> tuple[Path, Path]:
    """Extract source frames -2 and +2 at exact source frame indices."""
    if frame < 2:
        raise PadReviewError(f"cannot show two preceding context frames for output frame {frame}")
    first, last = cache.file(f"context_{frame:06d}_01.png"), cache.file(f"context_{frame:06d}_02.png")
    pattern = cache.file(f"context_{frame:06d}_%02d.png")
    command = [ld.ffmpeg(), "-y", "-hide_banner", "-v", "error"]
    vs_args = None
    if video.suffix.casefold() == ".avs":
        avs_range = cache.file(f"context_{frame:06d}.avs")
        avs_range.write_text(
            f"src = Import({json.dumps(str(video))})\nsrc.Trim({frame - 2}, {frame + 2})\n",
            encoding="utf-8")
        command += ["-f", "avisynth", "-i", str(avs_range)]
        video = avs_range
    else:
        command += ["-f", "yuv4mpegpipe", "-i", "-"]
        vs_args = ["-s", str(frame - 2), "-e", str(frame + 2)]
    command += ["-vf", "select='eq(n,0)+eq(n,4)',format=rgb24",
                "-fps_mode", "passthrough", "-frames:v", "2", "-start_number", "1", str(pattern)]
    env_keys = ("LDP_PROFILE", "LDP_EPISODE", "LDP_INSTANCE_ROOT")
    prior_env = {key: os.environ.get(key) for key in env_keys}
    os.environ["LDP_PROFILE"] = str(ld.profile_path)
    os.environ["LDP_EPISODE"] = episode
    if instance_root:
        os.environ["LDP_INSTANCE_ROOT"] = str(instance_root)
    try:
        result = run_source_ffmpeg(ld, command, video, text=False, vs_args=vs_args)
    finally:
        for key, value in prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if result.returncode or not first.is_file() or not last.is_file():
        detail = result.stderr.decode("utf-8", "replace")[-1000:]
        raise PadReviewError(f"could not extract outer context frames for PAD {frame}: {detail}")
    return first, last


def _temporary_copy(source: Path, cache: PreviewCache, name: str) -> Path:
    target = cache.file(name)
    shutil.copyfile(source, target)
    return target


class PadReviewWindow:
    def __init__(self, parent: tk.Misc, episode: str, profile_path: Path,
                 manifest_path: Path, instance_root: Path | None = None,
                 on_close: Callable[[PreviewCache], None] | None = None):
        self.parent = parent
        self.episode = episode
        self.profile_path = profile_path
        self.manifest_path = manifest_path
        self.instance_root = instance_root
        self.on_close = on_close
        self.window: tk.Toplevel | None = None
        self.cache: PreviewCache | None = None
        self.photos: list[ImageTk.PhotoImage] = []
        self.index = 0
        self.items: list[ReviewItem] = []
        self.all_items: list[ReviewItem] = []
        self.filter_value: tk.StringVar | None = None
        self.probe: Path | None = None
        self.ld: Ld | None = None

    def show(self) -> None:
        try:
            self.all_items, self.probe, _report = discover_review_items(
                self.episode, self.manifest_path, self.profile_path, self.instance_root)
            self.ld = Ld(self.profile_path)
            self.cache = PreviewCache()
        except (PadReviewError, PipelineError, OSError) as exc:
            messagebox.showerror("PAD review unavailable", str(exc), parent=self.parent)
            return
        self.window = tk.Toplevel(self.parent)
        self.window.title(f"PAD frame review — {self.episode}")
        self.window.geometry("1320x760")
        self.window.minsize(760, 520)
        self.window.transient(self.parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Left>", lambda _e: self.previous())
        self.window.bind("<Right>", lambda _e: self.next())
        self.caption = tk.StringVar()
        ttk.Label(self.window, textvariable=self.caption, anchor="center", wraplength=1260).pack(fill="x", padx=12, pady=8)
        self.image_frame = ttk.Frame(self.window, padding=8)
        self.image_frame.pack(fill="both", expand=True)
        controls = ttk.Frame(self.window, padding=8)
        controls.pack(fill="x")
        available_kinds = {item.kind for item in self.all_items}
        choices = ["All review items"]
        if "cut" in available_kinds:
            choices.append("Cut-PAD stills")
        if "rife" in available_kinds:
            choices.append("RIFE 5-frame sequences")
        self.filter_value = tk.StringVar(value=choices[0])
        ttk.Label(controls, text="Show:").pack(side="left", padx=(4, 2))
        selector = ttk.Combobox(controls, textvariable=self.filter_value, values=choices,
                                state="readonly", width=26)
        selector.pack(side="left", padx=(0, 8))
        selector.bind("<<ComboboxSelected>>", self._filter_changed)
        self.back_button = ttk.Button(controls, text="← Back", command=self.previous)
        self.back_button.pack(side="left", padx=10)
        self.position = tk.StringVar()
        ttk.Label(controls, textvariable=self.position).pack(side="left", expand=True)
        self.forward_button = ttk.Button(controls, text="Forward →", command=self.next)
        self.forward_button.pack(side="right", padx=10)
        ttk.Label(controls, text="Close this window to discard temporary preview copies.").pack(side="right", padx=12)
        self._apply_filter()
        self.window.after(50, self.render)

    def _filter_changed(self, _event=None) -> None:
        self._apply_filter()
        self.render()

    def _apply_filter(self) -> None:
        selected = self.filter_value.get() if self.filter_value else "All review items"
        if selected == "Cut-PAD stills":
            self.items = [item for item in self.all_items if item.kind == "cut"]
        elif selected == "RIFE 5-frame sequences":
            self.items = [item for item in self.all_items if item.kind == "rife"]
        else:
            self.items = list(self.all_items)
        self.index = 0

    def previous(self) -> None:
        if self.items:
            self.index = (self.index - 1) % len(self.items)
            self.render()

    def next(self) -> None:
        if self.items:
            self.index = (self.index + 1) % len(self.items)
            self.render()

    def render(self) -> None:
        if not self.window or not self.cache or not self.ld or not self.probe or not self.items:
            return
        item = self.items[self.index]
        for child in self.image_frame.winfo_children():
            child.destroy()
        self.photos = []
        try:
            if item.kind == "rife" and item.before and item.after and item.video:
                before2, after2 = _extract_outer_frames(
                    self.ld, item.video, int(item.frame), self.cache, self.episode, self.instance_root)
                sources = [before2, item.before, item.source, item.after, after2]
                captions = [f"Source −2 ({item.frame - 2})", f"Source −1 ({item.frame - 1})",
                            f"RIFE replacement ({item.frame})", f"Source +1 ({item.frame + 1})",
                            f"Source +2 ({item.frame + 2})"]
                self.caption.set(item.title + " — five-frame context")
                max_size = (245, 560)
            else:
                sources = [item.source]
                captions = [item.title]
                self.caption.set(item.title)
                max_size = (1180, 600)
            for index, source in enumerate(sources):
                copy = _temporary_copy(source, self.cache, f"preview_{self.index:04d}_{index:02d}.png")
                with Image.open(copy) as image:
                    image = image.convert("RGB")
                    image.thumbnail(max_size, Image.Resampling.LANCZOS)
                    photo = ImageTk.PhotoImage(image)
                self.photos.append(photo)
                column = ttk.Frame(self.image_frame)
                column.pack(side="left", fill="both", expand=True, padx=3)
                ttk.Label(column, text=captions[index], anchor="center", wraplength=max_size[0]).pack(fill="x", pady=(0, 5))
                ttk.Label(column, image=photo, anchor="center").pack(fill="both", expand=True)
        except (OSError, PadReviewError, PipelineError) as exc:
            ttk.Label(self.image_frame, text=f"Could not create this preview:\n{exc}", wraplength=900).pack(expand=True)
        self.position.set(f"{self.index + 1} / {len(self.items)}")
        self.back_button.configure(state="normal" if len(self.items) > 1 else "disabled")
        self.forward_button.configure(state="normal" if len(self.items) > 1 else "disabled")

    def close(self) -> None:
        if self.window:
            self.window.destroy()
            self.window = None
        if self.cache:
            if self.on_close:
                self.on_close(self.cache)
            else:
                self.cache.cleanup()
