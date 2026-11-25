"""Intake stages: Archive download and capture inventory.

``archive-download`` (v38 ``download_archive.ps1`` in Python): live item metadata decides the
file set; the space gate is checked against bytes still missing; every file is size+MD5 verified;
nothing is deleted or overwritten. The catalog snapshot is navigation only.

``capture-inventory`` (v38 ``local_source.py`` + the inventory half of ``init_episode`` /
``init_local_episode``): pairs each TBC with its JSON sidecar (and optional EFM), derives unique
capture labels, confirms the folder's episode/series identity matches this workspace, cross-checks
an existing ``download-manifest.csv`` (sizes + Archive id), and records the capture inventory in
the manifest's ``sources``. Large files are fingerprinted head/tail (v38 "balanced" policy).
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

from fstk.core.errors import ArtifactError, GateError
from fstk.core.fsio import atomic_write_json, read_json
from fstk.core.hashing import SCOPE_HEAD_TAIL
from fstk.core.timeutil import utc_now_iso
from fstk.tools.archive_http import read_download_manifest
from fstk.workspace.local_source import LocalSourceError, build_local_source_report, inspect_local_source

from .base import Custom, SpaceNeed, Stage, StepOutput, StepPlan, register

GIB = 1024 ** 3


def downloads_folder(ctx, manifest) -> Path:
    override = manifest.doc["sources"].get("downloads_override")
    return Path(override) if override else ctx.profile.downloads_dir(ctx.episode)


def _has_archive_item(ctx, manifest):
    item = manifest.doc["sources"].get("archive_item")
    return bool(item), f"archive item {item}" if item else "sources.archive_item is not set (local-source episode?)"


def _folder_present(ctx, manifest):
    try:
        folder = downloads_folder(ctx, manifest)
    except Exception as exc:
        return False, str(exc)
    return folder.is_dir(), str(folder) if folder.is_dir() else f"capture folder missing: {folder}"


@register
class ArchiveDownload(Stage):
    id = "archive-download"
    carry_over = False      # always planned again: it resumes and re-checks partial downloads
    title = "Download captures from the Internet Archive"
    description = "Live metadata, space gate, resumable size+MD5-verified download of TBC/JSON/EFM files."
    requires = (Custom("archive-item", _has_archive_item),)
    writes_sources = ("archive_item_verified", "download")
    resource = "network"

    def plan(self, ctx, params):
        m = ctx.manifest
        ident = m.doc["sources"]["archive_item"]
        client = ctx.tool("archive")
        sel = client.selection(ident)
        dest = downloads_folder(ctx, m)
        missing = 0
        for f in sel.files:
            p = dest / f.name
            have = p.stat().st_size if p.is_file() else 0
            if have > f.size:
                raise GateError(f"existing file larger than Archive metadata (left untouched): {p}")
            missing += f.size - have
        catalog_labels = (m.doc["sources"].get("catalog") or {}).get("capture_labels")
        if catalog_labels and sorted(catalog_labels) != sel.capture_labels:
            ctx.report(None, f"note: live captures {sel.capture_labels} differ from catalog {catalog_labels}")
        space = None
        if missing > 0:
            space = SpaceNeed("downloads_root", missing / GIB, "live Archive metadata sizes minus bytes present",
                              target=dest)
        manifest_path = dest / "download-manifest.csv"
        return StepPlan(self.id, params, outputs=[manifest_path], space=space,
                        summary=f"{len(sel.files)} files, {sel.total_bytes / GIB:.1f} GiB "
                                f"({missing / GIB:.1f} GiB to fetch) → {dest}",
                        data={"identifier": ident, "dest": str(dest), "manifest": str(manifest_path),
                              "selection": [{"name": f.name, "size": f.size, "md5": f.md5} for f in sel.files],
                              "title": sel.title, "total": sel.total_bytes})

    def run(self, ctx, plan):
        from fstk.tools.archive_http import RemoteFile, Selection

        d = plan.data
        client = ctx.tool("archive")
        sel = Selection(d["identifier"], d["title"], tuple(RemoteFile(**f) for f in d["selection"]))
        done: dict[str, int] = {}

        def progress(name: str, have: int, size: int) -> None:
            done[name] = have
            ctx.report(sum(done.values()) / max(1, d["total"]), f"{name} {have / GIB:.2f}/{size / GIB:.2f} GiB")

        manifest = client.download_item(sel, Path(d["dest"]), source_url=f"https://archive.org/details/{d['identifier']}",
                                        progress=progress, cancel=ctx.cancel)
        out = StepOutput()
        out.sources_update = {"archive_item_verified": True,
                              "download": {"manifest": str(manifest), "verified_utc": utc_now_iso(),
                                           "files": len(sel.files), "bytes": sel.total_bytes,
                                           "archive_title": d["title"]}}
        return out

    def verify(self, ctx, plan, output):
        rows = read_download_manifest(Path(plan.data["manifest"]))
        want = {(f["name"], f["size"], f["md5"]) for f in plan.data["selection"]}
        got = {(r["Name"], int(r["Bytes"]), r["MD5"].lower()) for r in rows}
        issues = [] if want == got else ["download-manifest.csv does not match the live selection"]
        dest = Path(plan.data["dest"])
        for name, size, _ in want:
            p = dest / name
            if not p.is_file() or p.stat().st_size != size:
                issues.append(f"{name} missing or wrong size after download")
        return issues


@register
class CaptureInventory(Stage):
    id = "capture-inventory"
    title = "Inventory captures"
    description = "Pair TBC/JSON/EFM files, derive capture labels, confirm identity, record the inventory."
    requires = (Custom("capture-folder", _folder_present),)
    writes_sources = ("captures", "source_provenance")
    resource = "prep"

    def plan(self, ctx, params):
        m = ctx.manifest
        folder = downloads_folder(ctx, m)
        try:
            scan = inspect_local_source(folder)
        except LocalSourceError as exc:
            raise GateError(f"capture folder is not a usable capture set: {exc}") from None
        if (scan["series"], scan["episode"]) != (ctx.episode.series, ctx.episode.code):
            raise GateError(f"capture folder identifies {scan['series']}:{scan['episode']}, "
                            f"but this workspace is {ctx.episode}")
        files = [folder / c[f"{k}_file"] for c in scan["captures"] for k in ("tbc", "json", "efm")
                 if c.get(f"{k}_file")]
        report = ctx.root("reports") / "capture-inventory.json"
        return StepPlan(self.id, params, inputs=files, outputs=[report],
                        input_scope={str(p): SCOPE_HEAD_TAIL for p in files},
                        summary=f"{len(scan['captures'])} captures in {folder}",
                        data={"folder": str(folder), "scan": scan, "report": str(report)})

    def run(self, ctx, plan):
        m = ctx.manifest
        scan = plan.data["scan"]
        folder = Path(plan.data["folder"])
        inventory = build_local_source_report(scan, policy="balanced")
        dl_manifest = folder / "download-manifest.csv"
        archive_item = m.doc["sources"].get("archive_item")
        md5_by_name: dict[str, str] = {}
        mode = "local-inventory"
        if dl_manifest.is_file():
            rows = read_download_manifest(dl_manifest)
            ids = {r["ArchiveId"] for r in rows}
            if archive_item and ids != {archive_item}:
                raise ArtifactError(f"download-manifest.csv names {sorted(ids)}, workspace expects {archive_item}")
            for r in rows:
                p = folder / PurePosixPath(r["Name"]).name
                if p.is_file() and p.stat().st_size != int(r["Bytes"]):
                    raise ArtifactError(f"{p.name} size differs from download-manifest.csv")
                md5_by_name[r["Name"]] = r["MD5"].lower()
            mode = "archive-download"
        captures = []
        for cap in inventory["captures"]:
            files = {}
            for kind, rec in cap["files"].items():
                entry = {"name": rec["name"], "bytes": rec["size_bytes"]}
                if rec["name"] in md5_by_name:
                    entry["md5"] = md5_by_name[rec["name"]]
                files[kind] = entry
            captures.append({"label": cap["label"], "files": files})
        atomic_write_json(plan.data["report"], {**inventory, "fstk_mode": mode,
                                                "download_manifest_cross_checked": bool(md5_by_name)})
        out = StepOutput(report=Path(plan.data["report"]))
        out.sources_update = {"captures": captures, "source_provenance": {
            "mode": mode, "source_dir": str(folder), "inventory_report": plan.data["report"],
            "archive_id_hint": scan.get("archive_id_hint", ""), "fingerprint_policy": "balanced",
            "created_utc": inventory["created_utc"]}}
        return out

    def verify(self, ctx, plan, output):
        report = read_json(plan.data["report"])
        labels = [c["label"] for c in report["captures"]]
        issues = []
        if not labels or len(set(labels)) != len(labels):
            issues.append("capture labels missing or not unique")
        for cap in report["captures"]:
            if "tbc" not in cap["files"] or "json" not in cap["files"]:
                issues.append(f"{cap['label']}: TBC/JSON pair incomplete")
        return issues
