"""EFM audio stages: decode every capture's raw EFM with Orc/EFMSink, then select one capture.

``efm-decode``
    Ported behaviour of v38 ``decode_efm_captures.py`` + ``verify_efm_capture.py``. For each
    capture that has a raw EFM source (``.efm`` or ``.efm.7z``):

    1. verify the TBC/JSON/EFM against the inventoried sizes (and Archive MD5s when known);
       the EFM is hashed (MD5 + SHA-256) before and after the decode;
    2. a ``.efm.7z`` is listed, exactly one regular ``<name>.efm`` member is accepted, and it is
       streamed into ``work/efm-decodes/`` (kept; it is the decode input of record);
    3. a two-node Orc project (tbc_source with an explicit ``efm_path`` → EFMSink) is written
       under ``references/`` and run with Orc's own log file;
    4. the Orc log must name this TBC, this JSON, this WAV and this report and report success;
       the WAV must be 44.1 kHz/16-bit/stereo PCM; the EFMSink report must be non-empty;
    5. a provenance report (sources, decode input, project, log, WAV, report hashes and the
       parsed quality metrics) is written under ``reports/``; the manifest's
       ``audio.efm_provenance[label]`` history gains an entry when the step verifies.

    Every run writes into a new run folder (``audio/efm/<run>/``); nothing is overwritten or
    deleted. A capture whose latest recorded decode still verifies is kept unless
    ``redecode`` is set. The stage decodes captures in order and stops at the first failure
    (v38 behaviour); successful decodes from a failed run stay on disk but are not recorded.

``efm-select``
    Ported behaviour of v38 ``record_efm_capture_selection.py`` + the app's unique-Pareto rule.
    Every EFM-bearing capture must have a verified decode whose WAV/report still hash-match.
    If one capture is no worse on all six error-correction metrics and strictly better on one,
    it is selected automatically; otherwise ``tools.orc_efm.least_bad`` picks the capture with the
    least audible damage (owner decision 2026-11-21: no human EFM review). An operator may still
    override (params ``capture`` + ``note``, e.g. from the EFM capture review window). ``audio.pad_frames`` comes
    from the measured Orc alignment map in the saved stack project's source order.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import shutil
from pathlib import Path
from typing import Any

from fstk.core.errors import ArtifactError, GateError, ToolError, WorkflowError
from fstk.core.fsio import atomic_write_json, move_aside, read_json
from fstk.core.hashing import SCOPE_HEAD_TAIL, head_tail_sha256, sha256_file
from fstk.core.timeutil import utc_now_iso, utc_stamp
from fstk.tools.orc import project_source_labels
from fstk.tools.orc_efm import (
    LEAST_BAD_RULE,
    PARETO_METRICS,
    build_decode_project,
    check_audio_wav,
    decode_text,
    least_bad,
    parse_alignment_map,
    parse_efm_report,
    project_formats,
    render_table,
    report_path_for,
    sevenzip_efm_member,
    unique_pareto,
    verify_efm_log,
)
from fstk.tools.orc_project import bundled_template

from .base import ArtifactExists, Custom, SpaceNeed, Stage, StepOutput, StepPlan, ValueSet, register
from .intake import downloads_folder

LOG = logging.getLogger("fstk.efm")
GIB = 1024 ** 3
PROVENANCE_SCHEMA = "fstk-efm-provenance-v1"
SELECTION_SCHEMA = "fstk-efm-selection-v1"
PCM_BYTES_PER_SECOND = 44100 * 2 * 2
FIELD_RATES = {"NTSC": 60000 / 1001, "PAL": 50.0, "PAL-M": 60000 / 1001}


# ------------------------------------------------------------------------------ helpers
def efm_captures(manifest) -> list[dict]:
    return [c for c in manifest.captures if (c.get("files") or {}).get("efm")]


def _has_efm(ctx, manifest):
    n = len(efm_captures(manifest))
    return n > 0, f"{n} capture(s) with a raw EFM source" if n else "no capture has a raw EFM source"


def _safe_source(folder: Path, name: str, role: str) -> Path:
    rel = Path(str(name))
    if not name or rel.is_absolute() or len(rel.parts) != 1 or rel.parts[0] in (".", ".."):
        raise GateError(f"{role} must be one safe filename: {name!r}")
    p = folder / rel
    if p.is_symlink():
        raise GateError(f"{role} may not be a symlink: {p}")
    if not p.is_file():
        raise GateError(f"{role} is missing: {p}")
    return p


def _hash_md5_sha256(path: Path, on_chunk=None) -> tuple[str, str]:
    md5, sha = hashlib.md5(), hashlib.sha256()
    done = 0
    with path.open("rb") as stream:
        while block := stream.read(4 * 1024 * 1024):
            md5.update(block)
            sha.update(block)
            done += len(block)
            if on_chunk:
                on_chunk(done)
    return md5.hexdigest(), sha.hexdigest()


def _verify_source(path: Path, spec: dict, role: str, *, full_hash: bool) -> dict:
    """Size against the inventory; MD5 against Archive metadata when known."""
    size = path.stat().st_size
    if spec.get("bytes") is not None and size != spec["bytes"]:
        raise ArtifactError(f"{role} size {size} differs from the inventory ({spec['bytes']}): {path}")
    rec: dict[str, Any] = {"name": path.name, "size_bytes": size}
    want_md5 = str(spec.get("md5") or "").lower()
    if full_hash or want_md5:
        md5, sha = _hash_md5_sha256(path)
        if want_md5 and md5 != want_md5:
            raise ArtifactError(f"{role} MD5 differs from the Archive metadata: {path}")
        rec.update(md5=md5, sha256=sha, hash_scope="full", md5_matches_archive=bool(want_md5))
    else:
        digest, scope = head_tail_sha256(path)
        rec.update(sha256=digest, hash_scope=scope, md5_matches_archive=False)
    return rec


def _source_formats(ctx, manifest) -> tuple[tuple[str, str, str], str]:
    spec = manifest.artifact("stack_project")
    if spec:
        try:
            p = ctx.artifact_path("stack_project", manifest)
            if p.is_file():
                return project_formats(p.read_text(encoding="utf-8-sig")), f"stack project {p.name}"
        except GateError:
            pass
    return project_formats(bundled_template()), "bundled stack template"


def estimate_wav_bytes(json_path: Path, video_format: str) -> tuple[int, str]:
    """PCM size from the capture's own field count (ld-decode JSON), +2 % margin."""
    try:
        meta = json.loads(json_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise GateError(f"cannot read {json_path.name} to estimate the WAV size: {exc}") from None
    fields = (meta.get("videoParameters") or {}).get("numberOfSequentialFields")
    if not isinstance(fields, int) or fields <= 0:
        fields = len(meta.get("fields") or [])
    if fields <= 0:
        raise GateError(f"{json_path.name} has no field count; give planned_gib + estimate_basis")
    seconds = fields / FIELD_RATES[video_format]
    return int(seconds * PCM_BYTES_PER_SECOND * 1.02), f"{fields} fields / {FIELD_RATES[video_format]:.3f} Hz"


PROGRESS_NAME = "efm-decode-progress.json"


def _progress_path(ctx) -> Path:
    return ctx.root("reports") / PROGRESS_NAME


def _load_progress(ctx) -> dict[str, dict]:
    """Verified decodes finished by a run that was stopped before the step committed them."""
    path = _progress_path(ctx)
    try:
        data = read_json(path) if path.is_file() else {}
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError):
            LOG.warning("damaged decode progress file %s moved aside (%s); decodes start over", path, exc)
            move_aside(path)
        return {}
    return {k: v for k, v in (data.get("captures") or {}).items() if isinstance(v, dict)}


def _save_progress(ctx, captures: dict[str, dict]) -> None:
    atomic_write_json(_progress_path(ctx), {"schema": "fstk-efm-decode-progress-v1", "captures": captures})


def _partial_paths(item: dict) -> list[Path]:
    keys = ("wav", "report", "project", "orc_log", "extract_to")
    return [Path(item[k]) for k in keys if item.get(k)]


def _marker_path(item: dict) -> Path:
    return Path(str(item["project"]) + ".partial")


def _discard_partial(item: dict) -> None:
    """Remove what an unfinished decode of this capture left behind (never a finished, verified one)."""
    if Path(item["provenance"]).exists():
        return
    for p in _partial_paths(item):
        with contextlib.suppress(OSError):
            if p.is_file() or p.is_symlink():
                p.unlink()
    with contextlib.suppress(OSError):
        _marker_path(item).unlink()


def _sweep_dead_partials(ctx) -> None:
    """A run that was killed (crash, power loss) cannot clean up: its markers name the partial files."""
    import psutil

    for marker in ctx.root("references").glob("efm-decode-*.orcprj.partial"):
        try:
            data = read_json(marker)
            if data.get("pid") == os.getpid() or psutil.pid_exists(int(data.get("pid", 0))):
                continue
            for name in data.get("paths", []):
                with contextlib.suppress(OSError):
                    Path(name).unlink()
            marker.unlink()
        except (OSError, ValueError):
            continue


def _record_still_valid(ctx, record: dict) -> tuple[bool, str, dict | None]:
    """Latest provenance entry → (still valid?, why, provenance report)."""
    try:
        path = Path(record["report_path"])
        if not path.is_absolute():
            path = ctx.profile.episode_state_dir(ctx.episode) / path
        if not path.is_file() or sha256_file(path) != record.get("report_sha256"):
            return False, f"provenance report missing or changed: {path}", None
        prov = read_json(path)
        wav = ctx.root("audio") / prov["wav"]["relative_path"]
        rep = ctx.root("audio") / prov["efm_report"]["relative_path"]
        if not wav.is_file() or wav.stat().st_size != prov["wav"]["size_bytes"]:
            return False, f"WAV missing or resized: {wav}", prov
        if sha256_file(wav) != prov["wav"]["sha256"]:
            return False, f"WAV changed since its decode was verified: {wav}", prov
        if not rep.is_file() or sha256_file(rep) != prov["efm_report"]["sha256"]:
            return False, f"EFMSink report missing or changed: {rep}", prov
        return True, "verified", prov
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return False, f"provenance entry unreadable: {exc}", None


def _rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


# ------------------------------------------------------------------------------ efm-decode
@register
class EfmDecode(Stage):
    id = "efm-decode"
    title = "Decode EFM audio (Orc EFMSink)"
    description = ("Per-capture Orc/EFMSink decode with log path-binding, PCM format and report checks; "
                   "records verified provenance and quality metrics.")
    requires = (Custom("efm-sources", _has_efm),)
    writes = ("audio.efm_provenance", "audio.efm_provenance_required", "audio.efm_decode_failures")
    resource = "compute"

    def plan(self, ctx, params):
        m = ctx.manifest
        folder = downloads_folder(ctx, m)
        caps = efm_captures(m)
        wanted = params.get("captures")
        if wanted:
            unknown = sorted(set(wanted) - {c["label"] for c in caps})
            if unknown:
                raise GateError(f"no raw EFM source for capture(s) {unknown}")
            caps = [c for c in caps if c["label"] in wanted]
        (video, source, amplitude), formats_from = _source_formats(ctx, m)
        if source != "Composite":
            raise GateError("automatic EFM decoding requires composite TBC sources (v38 rule); "
                            f"{formats_from} declares {source}")
        existing = m.get("audio.efm_provenance") or {}
        run = f"{utc_stamp()[:15].lower()}-{secrets.token_hex(3)}"
        audio_dir = ctx.root("audio") / "efm" / run
        todo, kept, inputs, outputs = [], {}, [], []
        adopted: dict[str, dict] = {}
        saved = {} if params.get("redecode") else _load_progress(ctx)
        wav_bytes, basis = 0, []
        for cap in caps:
            label = cap["label"]
            history = existing.get(label) or []
            if history and not params.get("redecode"):
                ok, why, _ = _record_still_valid(ctx, history[-1])
                if ok:
                    kept[label] = why
                    continue
            if label in saved:
                ok, why, _ = _record_still_valid(ctx, saved[label])
                if ok:
                    adopted[label] = saved[label]      # finished before an earlier run was stopped
                    continue
            files = cap["files"]
            tbc = _safe_source(folder, files["tbc"]["name"], f"{label} TBC")
            meta = _safe_source(folder, files["json"]["name"], f"{label} TBC JSON")
            efm = _safe_source(folder, files["efm"]["name"], f"{label} EFM")
            name = efm.name.casefold()
            if not (name.endswith(".efm") or name.endswith(".efm.7z")):
                raise GateError(f"unsupported raw EFM filename: {efm.name}")
            wav = audio_dir / f"{label}-efm.wav"
            item = {"label": label, "tbc": str(tbc), "json": str(meta), "efm": str(efm),
                    "specs": {k: files[k] for k in ("tbc", "json", "efm")},
                    "wav": str(wav), "report": str(report_path_for(wav)),
                    "project": str(ctx.root("references") / f"efm-decode-{label}-{run}.orcprj"),
                    "orc_log": str(ctx.root("logs") / f"efm-decode-{label}-{run}.log"),
                    "provenance": str(ctx.root("reports") / f"efm-provenance-{label}-{run}.json"),
                    "extract_to": (str(ctx.root("work") / "efm-decodes" / f"{label}-{run}.efm")
                                   if name.endswith(".efm.7z") else None)}
            if "planned_gib" not in params:
                size, why = estimate_wav_bytes(meta, video)
                wav_bytes += size
                basis.append(f"{label}: {why}")
            todo.append(item)
            inputs += [tbc, meta, efm]
            outputs.append(Path(item["provenance"]))    # VERIFIED, or FAILED with the reason
        space = None
        if todo:
            if "planned_gib" in params:
                space = SpaceNeed("audio", float(params["planned_gib"]), str(params.get("estimate_basis") or ""))
                if not space.basis:
                    raise GateError("planned_gib needs an estimate_basis")
            else:
                space = SpaceNeed("audio", wav_bytes / GIB,
                                  "44.1 kHz/16-bit stereo PCM for each capture's own field count (+2 %): "
                                  + "; ".join(basis))
        summary = (f"decode {len(todo)} capture(s) [{', '.join(t['label'] for t in todo)}]" if todo
                   else "nothing to decode") + (f"; keeping verified {sorted(kept)}" if kept else "")
        if adopted:
            summary += f"; resuming finished decode(s) {sorted(adopted)} from an interrupted run"
        return StepPlan(self.id, params, inputs=inputs, outputs=outputs, space=space,
                        input_scope={str(p): SCOPE_HEAD_TAIL for p in inputs}, summary=summary,
                        data={"run": run, "todo": todo, "kept": kept, "adopted": adopted, "formats": [video, source, amplitude],
                              "formats_from": formats_from, "audio_dir": str(audio_dir)})

    # -- one capture
    def _decode_one(self, ctx, plan, item: dict, position: tuple[int, int] = (1, 1)) -> dict:
        label = item["label"]
        number, count = position
        tag = f"{number} of {count}" if count > 1 else ""
        lead = f"[{tag}] " if tag else ""
        video, source, amplitude = plan.data["formats"]
        tbc, meta, efm = Path(item["tbc"]), Path(item["json"]), Path(item["efm"])
        wav, report = Path(item["wav"]), Path(item["report"])
        project, orc_log, prov_path = Path(item["project"]), Path(item["orc_log"]), Path(item["provenance"])
        for p in (wav, report, project, orc_log, prov_path):
            if p.exists() or p.is_symlink():
                raise ArtifactError(f"refusing to overwrite existing EFM decode artifact: {p}")
        specs = item["specs"]
        ctx.report((number - 1) / count, f"{lead}{label}: verifying sources")
        src_tbc = _verify_source(tbc, specs["tbc"], f"{label} TBC", full_hash=False)
        src_json = _verify_source(meta, specs["json"], f"{label} TBC JSON", full_hash=True)
        src_efm = _verify_source(efm, specs["efm"], f"{label} EFM", full_hash=True)

        decode_input, decoded_root = efm, "downloads"
        if item["extract_to"]:
            target = Path(item["extract_to"])
            zipper = ctx.tool("sevenzip")
            member, size = sevenzip_efm_member(zipper.list_slt(efm), efm.name[:-3])
            target.parent.mkdir(parents=True, exist_ok=True)
            reserve = ctx.profile.limits.min_free_gb * GIB
            free = shutil.disk_usage(target.parent).free
            if free < size + reserve:
                raise GateError(f"not enough work space to unpack {efm.name}: need {size / GIB:.2f} GiB plus "
                                f"{reserve / GIB:.1f} GiB reserve, free {free / GIB:.2f} GiB")
            ctx.report((number - 1) / count, f"{lead}{label}: extracting {member}")
            zipper.extract_member(efm, member, size, target)
            decode_input, decoded_root = target, "work"
        input_sha = (src_efm["sha256"] if decode_input == efm else sha256_file(decode_input))

        text = build_decode_project(episode=ctx.episode.code, capture=label, tbc=tbc.resolve(),
                                    efm=decode_input.resolve(), wav=wav.resolve(strict=False),
                                    video_format=video, source_format=source, amplitude_unit=amplitude)
        project.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(_marker_path(item), {"pid": os.getpid(), "paths": [str(p) for p in _partial_paths(item)]})
        with project.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        wav.parent.mkdir(parents=True, exist_ok=True)
        orc_log.parent.mkdir(parents=True, exist_ok=True)
        ctx.report((number - 1) / count, f"{lead}{label}: Orc EFMSink decode")
        from fstk.tools.progress_lines import LineProgress

        res = ctx.tool("orc").decode_efm(project, orc_log, log_name=f"orc-efm-{label}", cancel=ctx.cancel,
                                         on_line=LineProgress(ctx.report, base=(number - 1) / count, span=1 / count,
                                                              label=f"{lead}{label}: Orc EFM decode"))
        ctx.check_cancel()

        wav_info = check_audio_wav(wav)
        if report.is_symlink() or not report.is_file() or report.stat().st_size <= 0:
            raise ArtifactError(f"EFMSink decoding report is missing or empty: {report} ({res.describe()})")
        if orc_log.is_symlink() or not orc_log.is_file():
            raise ArtifactError(f"Orc log is missing: {orc_log} ({res.describe()})")
        checks = verify_efm_log(decode_text(orc_log.read_bytes()), tbc=tbc, metadata=meta, wav=wav, report=report)
        if sha256_file(decode_input) != input_sha:
            raise ArtifactError(f"{label}: raw EFM input changed during the decode")
        report_text = decode_text(report.read_bytes())
        quality = parse_efm_report(report_text)
        audio_root = ctx.root("audio")
        prov = {
            "schema": PROVENANCE_SCHEMA, "status": "VERIFIED", "episode": str(ctx.episode), "capture": label,
            "run": plan.data["run"], "created_utc": utc_now_iso(), "actor": ctx.actor,
            "sources": {"tbc": src_tbc, "json": src_json, "efm": src_efm},
            "decoded_efm": {"root": decoded_root,
                            "relative_path": (decode_input.name if decoded_root == "downloads" else
                                              _rel(decode_input, ctx.root("work"))),
                            "sha256": input_sha, "size_bytes": decode_input.stat().st_size},
            "formats": {"video_format": video, "source_format": source, "amplitude_unit": amplitude,
                        "from": plan.data["formats_from"]},
            "orc_project": {"name": project.name, "sha256": sha256_file(project)},
            "orc_version": plan.data.get("orc_version", "unknown"),
            "orc_log": {"name": orc_log.name, "sha256": sha256_file(orc_log), "exit": res.returncode},
            "path_checks": checks,
            "wav": {"relative_path": _rel(wav, audio_root), "size_bytes": wav.stat().st_size,
                    "sha256": sha256_file(wav), **wav_info},
            "efm_report": {"relative_path": _rel(report, audio_root), "size_bytes": report.stat().st_size,
                           "sha256": sha256_file(report)},
            "quality": quality,
            "selection_note": "provenance only; this report does not rank or select the audio capture",
        }
        atomic_write_json(prov_path, prov)
        with contextlib.suppress(OSError):
            _marker_path(item).unlink()
        return {"report_path": _rel(prov_path, ctx.profile.episode_state_dir(ctx.episode)),
                "report_sha256": sha256_file(prov_path), "wav_sha256": prov["wav"]["sha256"],
                "verified_utc": prov["created_utc"], "run": plan.data["run"]}

    def run(self, ctx, plan):
        """Decode every capture; one failure does not stop the others (owner: best-quality outcome —
        every successful decode is a candidate). Failures are recorded and excluded from the
        automatic choice; the step fails only if nothing decoded."""
        todo = plan.data["todo"]
        if todo:
            orc = ctx.tool("orc")
            plan.data["orc_version"] = orc.identity() if hasattr(orc, "identity") else {"version": orc.version()}
        _sweep_dead_partials(ctx)
        merged = {k: list(v) for k, v in (ctx.manifest.get("audio.efm_provenance") or {}).items()}
        failures = dict(ctx.manifest.get("audio.efm_decode_failures") or {})
        added, failed = {}, {}
        progress = _load_progress(ctx)
        for label, entry in (plan.data.get("adopted") or {}).items():
            merged.setdefault(label, []).append(entry)
            added[label] = entry
            failures.pop(label, None)
        for n, item in enumerate(todo):
            ctx.check_cancel()
            ctx.report(n / max(1, len(todo)), f"[{n + 1} of {len(todo)}] {item['label']}: starting")
            try:
                entry = self._decode_one(ctx, plan, item, (n + 1, len(todo)))
            except (ArtifactError, GateError, ToolError) as exc:
                if ctx.cancel.is_set():       # stopped by the operator or a closing app: not a failed capture
                    _discard_partial(item)
                    raise WorkflowError("cancelled by operator") from exc
                _discard_partial(item)
                failed[item["label"]] = {"run": plan.data["run"], "reason": str(exc)[:2000],
                                         "efm_bytes": item["specs"]["efm"].get("bytes"), "utc": utc_now_iso()}
                prov = Path(item["provenance"])
                if not prov.exists():
                    atomic_write_json(prov, {"schema": PROVENANCE_SCHEMA, "status": "FAILED", "episode": str(ctx.episode),
                                             "capture": item["label"], "run": plan.data["run"],
                                             "created_utc": utc_now_iso(), "reason": str(exc),
                                             "orc_version": plan.data.get("orc_version", "unknown")})
                continue
            except BaseException:
                _discard_partial(item)
                raise
            progress[item["label"]] = entry
            _save_progress(ctx, progress)       # kept even if the run is stopped before the step commits
            merged.setdefault(item["label"], []).append(entry)
            added[item["label"]] = entry
            failures.pop(item["label"], None)
        if todo and not added:
            raise ArtifactError("no capture decoded: " + "; ".join(f"{k}: {v['reason'][:300]}" for k, v in failed.items()))
        failures.update(failed)
        out = StepOutput()
        out.data["added"] = added
        out.data["failed"] = failed
        if added:
            out.measure("audio.efm_provenance", merged,
                        note=f"verified EFMSink decode(s) {sorted(added)} in run {plan.data['run']}"
                             + (f"; failed {sorted(failed)}" if failed else ""))
            out.measure("audio.efm_provenance_required", True)
        if failed or (ctx.manifest.get("audio.efm_decode_failures") or {}) != failures:
            out.measure("audio.efm_decode_failures", failures,
                        note=f"{sorted(failed)} failed in run {plan.data['run']}" if failed else "cleared by a decode")
        out.notes += [f"{k} failed: {v['reason'][:200]}" for k, v in failed.items()]
        return out

    def verify(self, ctx, plan, output):
        issues = []
        for label, entry in output.data["added"].items():
            ok, why, _ = _record_still_valid(ctx, entry)
            if not ok:
                issues.append(f"{label}: {why}")
        return issues


# ------------------------------------------------------------------------------ efm-select
def _comparison(ctx, manifest) -> tuple[dict[str, dict], list[str], dict[str, dict]]:
    """Latest verified decode per EFM capture → (quality by label, problems, provenance by label)."""
    provenance = manifest.get("audio.efm_provenance") or {}
    quality, problems, provs = {}, [], {}
    for cap in efm_captures(manifest):
        label = cap["label"]
        history = provenance.get(label) or []
        failure = (manifest.get("audio.efm_decode_failures") or {}).get(label)
        if failure and failure.get("efm_bytes") == cap["files"]["efm"].get("bytes"):
            continue   # this capture's current source cannot be decoded: not a candidate
        if not history:
            problems.append(f"{label}: no verified decode")
            continue
        ok, why, prov = _record_still_valid(ctx, history[-1])
        if not ok or prov is None:
            problems.append(f"{label}: {why}")
            continue
        src = prov["sources"]
        for kind in ("tbc", "json", "efm"):
            spec = cap["files"][kind]
            if src[kind]["name"] != spec["name"] or src[kind]["size_bytes"] != spec.get("bytes", src[kind]["size_bytes"]):
                problems.append(f"{label}: decode was made from a different {kind} than the inventory")
        quality[label] = prov["quality"]
        provs[label] = prov
    return quality, problems, provs


def efm_comparison_rows(ctx, manifest) -> list[dict]:
    quality, _problems, _ = _comparison(ctx, manifest)
    return [{"capture": label, **q} for label, q in quality.items()]


@register
class EfmSelect(Stage):
    id = "efm-select"
    title = "Select the EFM audio capture"
    description = ("Chosen automatically: the unique Pareto-dominant verified decode, otherwise the least-bad "
                   "by audible damage (fewest silenced samples first). An operator may override with a note. "
                   "Records audio.capture, audio.pad_frames and the decision.")
    requires = (ValueSet("audio.efm_provenance"), ValueSet("stack.reference"), ValueSet("stack.alignment_map"),
                ArtifactExists("stack_project"))
    writes = ("audio.capture", "audio.pad_frames", "audio.capture_selection", "audio.capture_selection_history")
    writes_artifacts = ("ld_native",)
    resource = "prep"

    def plan(self, ctx, params):
        m = ctx.manifest
        quality, problems, provs = _comparison(ctx, m)
        if problems:
            raise GateError("every capture with a raw EFM source needs a current verified decode before one is "
                            "selected: " + "; ".join(problems))
        project = ctx.artifact_path("stack_project", m)
        order = project_source_labels(project)
        if sorted(order) != sorted(m.capture_labels) or order[0] != m.get("stack.reference"):
            raise GateError(f"stack project source order {order} does not match the inventory/reference")
        pads = parse_alignment_map(m.require("stack.alignment_map"), order)
        rows = [{"capture": label, **q} for label, q in quality.items()]
        auto, rule = unique_pareto(quality), "unique-pareto"
        if auto is None:
            auto, rule = least_bad(quality), LEAST_BAD_RULE
        chosen, note, how = params.get("capture"), str(params.get("note") or "").strip(), "operator"
        if chosen:
            if chosen not in quality:
                raise GateError(f"capture {chosen!r} has no verified EFM decode")
            if len(note) < 8:
                raise GateError("an operator's capture choice needs a specific note (at least 8 characters)")
        elif auto:
            chosen, note, how = auto.capture, auto.note, rule
        else:
            missing = sorted({k for q in quality.values() for k in PARETO_METRICS if q.get(k) is None})
            raise GateError(f"EFM reports are missing metrics {missing}; re-decode those captures.\n"
                            + render_table(rows))
        report = ctx.root("reports") / "efm-selection.json"
        return StepPlan(self.id, params, inputs=[project], outputs=[report],
                        summary=f"{chosen} ({how}), pad_frames={pads[chosen]}",
                        data={"chosen": chosen, "note": note, "how": how, "pads": pads, "order": order,
                              "rows": rows, "report": str(report), "auto": auto.capture if auto else None,
                              "provenance": {k: {"wav": v["wav"]["relative_path"], "run": v["run"]}
                                             for k, v in provs.items()}})

    def run(self, ctx, plan):
        m = ctx.manifest
        d = plan.data
        chosen = d["chosen"]
        latest = (m.get("audio.efm_provenance") or {})[chosen][-1]
        selection = {"schema": SELECTION_SCHEMA, "capture": chosen, "pad_frames": d["pads"][chosen],
                     "method": d["how"], "automatic_candidate": d["auto"],
                     "verified_provenance_report": latest["report_path"],
                     "verified_provenance_sha256": latest["report_sha256"],
                     "alignment_map": m.get("stack.alignment_map"), "orc_source_order": d["order"],
                     "selection_note": d["note"], "selected_by": ctx.actor, "selected_utc": utc_now_iso()}
        atomic_write_json(d["report"], {**selection, "comparison": d["rows"],
                                        "comparison_table": render_table(d["rows"])})
        out = StepOutput(report=Path(d["report"]))
        note = d["note"] if d["how"] != "operator" else f"operator {ctx.actor}: {d['note']}"
        out.measure("audio.capture", chosen, note=note)
        out.measure("audio.pad_frames", d["pads"][chosen])
        previous = m.get("audio.capture_selection")
        if isinstance(previous, dict) and previous:
            history = [*(m.get("audio.capture_selection_history") or []), previous]
            out.measure("audio.capture_selection_history", history, note="previous selection superseded")
        out.measure("audio.capture_selection", selection, note=note)
        out.artifacts["ld_native"] = {"root": "audio", "name": d["provenance"][chosen]["wav"]}
        return out

    def verify(self, ctx, plan, output):
        rep = read_json(plan.data["report"])
        return [] if rep.get("capture") == plan.data["chosen"] else ["selection report does not name the choice"]

