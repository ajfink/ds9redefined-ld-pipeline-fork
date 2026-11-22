"""``fstk`` command-line interface (scripting surface; the app is the primary UI).

Every command accepts ``--profile`` (defaults to ``FSTK_PROFILE`` or the per-user config path)
and ``--json`` for machine-readable output where it makes sense.
"""
from __future__ import annotations

import argparse
import getpass
import json
import socket
import sys
from pathlib import Path
from typing import Any

from fstk import __version__
from fstk.core.errors import FstkError


def _actor(args) -> str:
    return args.actor or f"{getpass.getuser()}@{socket.gethostname()}"


def _print(args, data: Any, text: str | None = None) -> None:
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, sort_keys=True, default=str))
    else:
        print(text if text is not None else json.dumps(data, indent=2, default=str))


def _profile(args):
    from fstk.config.profile import Profile

    return Profile.load(args.profile)


# ----------------------------------------------------------------------------- profile
def cmd_profile_import(args) -> int:
    from fstk.config.profile import Profile, convert_v38_profile, default_profile_path
    from fstk.core.fsio import read_json

    v38 = read_json(args.v38_profile)
    data, notes = convert_v38_profile(v38, data_root=args.data_root, media_root=args.media_root)
    profile = Profile.from_dict(data)
    out = Path(args.out) if args.out else (Path(args.profile) if args.profile else default_profile_path())
    if out.exists() and not args.force:
        raise FstkError(f"{out} exists; pass --force to replace it")
    profile.save(out)
    _print(args, {"written": str(out), "notes": notes},
           f"wrote {out}\n" + "\n".join(f"  note: {n}" for n in notes))
    return 0


def cmd_profile_init(args) -> int:
    import shutil as _sh

    from fstk.config.profile import KNOWN_TOOLS, SCHEMA, Profile, current_os, default_profile_path

    out = Path(args.out) if args.out else (Path(args.profile) if args.profile else default_profile_path())
    if out.exists() and not args.force:
        raise FstkError(f"{out} exists; pass --force to replace it")
    tools = {}
    for name, spec in KNOWN_TOOLS.items():
        found = next((_sh.which(n) for n in spec["path_names"] if _sh.which(n)), None)
        if found:
            tools[name] = found
    profile = Profile.from_dict({"schema": SCHEMA, "machine": args.machine or socket.gethostname(),
                                 "os": current_os(), "data_root": str(Path(args.data_root).absolute()),
                                 "media_root": str(Path(args.media_root).absolute()), "tools": tools,
                                 "limits": {"min_free_gb": args.min_free_gb}})
    profile.save(out)
    missing = [n for n, s in KNOWN_TOOLS.items() if s["required"] and n not in tools]
    _print(args, {"written": str(out), "tools": tools, "missing_required": missing},
           f"wrote {out}; found {', '.join(tools) or 'no tools'} on PATH"
           + (f"; set these by hand: {', '.join(missing)}" if missing else ""))
    return 0


def cmd_profile_show(args) -> int:
    p = _profile(args)
    _print(args, p.to_dict())
    return 0


# ----------------------------------------------------------------------------- manifest
def _workspace(args, episode: str):
    from fstk.core.identity import EpisodeId
    from fstk.workflow.engine import EpisodeWorkspace

    return EpisodeWorkspace(_profile(args), EpisodeId.parse(episode))


def cmd_manifest_import(args) -> int:
    from fstk.core.hashing import sha256_bytes
    from fstk.manifest.v38 import export_v38, import_v38, v38_equivalent

    raw = Path(args.v38_manifest).read_bytes()
    doc = json.loads(raw)
    manifest = import_v38(doc, source_sha256=sha256_bytes(raw))
    if not v38_equivalent(doc, export_v38(manifest)):
        raise FstkError("round-trip check failed; refusing to import (report this manifest)")
    ws = _workspace(args, str(manifest.episode))
    ws.store.create(manifest, by=_actor(args), reason=f"import from v38 {Path(args.v38_manifest).name}",
                    origin="v38")
    _print(args, {"episode": str(manifest.episode), "manifest": str(ws.store.path),
                  "values": len(manifest.set_keys())},
           f"imported {manifest.episode} → {ws.store.path} ({len(manifest.set_keys())} values; "
           "v38 records are historical until re-confirmed)")
    return 0


def cmd_manifest_show(args) -> int:
    from fstk.manifest.registry import field_def

    m = _workspace(args, args.episode).store.load()
    if args.json:
        _print(args, m.doc)
        return 0
    print(f"{m.episode}  {m.title}")
    print(f"captures: {', '.join(m.capture_labels) or '-'}")
    for key in m.set_keys():
        e = m.entry(key)
        fd = field_def(key)
        value = e["value"]
        shown = value if not isinstance(value, (dict, list)) or len(json.dumps(value)) < 70 else f"<{type(value).__name__}>"
        print(f"  {key:36} = {shown!s:30} {fd.unit:6} [{e.get('origin')}/{e.get('by')}]")
    unset = [f.key for f in __import__('fstk.manifest.registry', fromlist=['FIELDS']).FIELDS
             if f.kind == "measurement" and not m.is_set(f.key)]
    print(f"unmeasured: {len(unset)} measurement field(s)")
    return 0


def cmd_manifest_verify(args) -> int:
    problems = _workspace(args, args.episode).store.verify()
    _print(args, {"problems": problems}, "\n".join(problems) if problems else "manifest history is consistent")
    return 1 if problems else 0


def cmd_manifest_history(args) -> int:
    rows = _workspace(args, args.episode).store.journal()
    if args.json:
        _print(args, rows)
        return 0
    for r in rows:
        keys = ", ".join(c["key"] for c in r.get("changes", []))
        print(f"{r['utc']}  {r['op']:6} {r.get('stage') or '-':24} {r['by']:24} {keys}")
    return 0


# ----------------------------------------------------------------------------- stages/workflow
def cmd_stages(args) -> int:
    import fstk.stages  # noqa: F401
    from fstk.stages.base import REGISTRY

    rows = []
    for sid, cls in sorted(REGISTRY.items()):
        if sid.startswith("t-"):
            continue
        rows.append({"id": sid, "title": cls.title, "resource": cls.resource, "review": cls.review,
                     "requires": [r.name for r in cls.requires], "writes": list(cls.writes)})
    _print(args, rows, "\n".join(f"{r['id']:28} {r['title']}" for r in rows))
    return 0


def cmd_docs_stages(args) -> int:
    import fstk.stages  # noqa: F401
    from fstk.stages.base import REGISTRY

    lines = ["# Stage reference", "", "Generated by `fstk docs stages`; do not edit by hand.", ""]
    for sid, cls in sorted(REGISTRY.items()):
        if sid.startswith("t-"):
            continue
        lines += [f"## `{sid}` — {cls.title}", "", (cls.__doc__ or cls.description or "").strip(), "",
                  f"- resource: {cls.resource or '—'}", f"- human review: {cls.review or '—'}" + (f" ({cls.review_when})" if cls.review_when else ""),
                  f"- requires: {', '.join(r.name for r in cls.requires) or '—'}",
                  f"- writes: {', '.join(cls.writes) or '—'}",
                  f"- artifacts: {', '.join(cls.writes_artifacts) or '—'}", ""]
    out = Path(args.out)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}")
    return 0


def cmd_run(args) -> int:
    from fstk.workflow.engine import Engine, load_plan

    plan = load_plan(args.plan)

    def show(event: dict) -> None:
        if args.json:
            print(json.dumps(event, default=str), flush=True)
        elif event["kind"] == "progress":
            frac = event.get("fraction")
            print(f"  [{event['step']}] {'' if frac is None else f'{frac:5.0%} '}{event.get('message', '')}",
                  flush=True)
        else:
            extra = event.get("issues") or event.get("detail") or ""
            print(f"{event['kind']:22} {event.get('step', '')} {extra}", flush=True)

    engine = Engine(_profile(args), plan, actor=_actor(args), events=show)
    state = engine.run()
    bad = [sid for sid, st in state["steps"].items() if st["status"] not in ("passed", "awaiting-review")]
    return 1 if bad else 0


def cmd_status(args) -> int:
    ws = _workspace(args, args.episode)
    path = ws.job_dir(args.job) / "state.json"
    if not path.exists():
        raise FstkError(f"no state for job {args.job!r} at {path}")
    state = json.loads(path.read_text(encoding="utf-8"))
    if args.json:
        _print(args, state)
        return 0
    for sid, st in state["steps"].items():
        print(f"{sid:24} {st['status']:16} {'; '.join(st.get('issues') or [])}")
    return 0


def cmd_doctor(args) -> int:
    from fstk.tools.doctor import run_doctor

    report = run_doctor(_profile(args) if not args.no_profile else None)
    if args.json:
        _print(args, report)
    else:
        for row in report["checks"]:
            print(f"[{row['status']:4}] {row['name']:28} {row['detail']}")
    return 0 if report["ok"] else 1


# ----------------------------------------------------------------------------- catalog / episodes
def _catalog(args):
    from fstk.catalog.index import CatalogIndex

    path = None
    try:
        path = getattr(_profile(args), "catalog_path", None)
    except FstkError:
        path = None
    return CatalogIndex.load(path or None)


def _entry_row(e, status=None) -> dict:
    row = {"id": f"{e.series}:{e.code}", "title": e.title, "kind": e.kind, "captures": len(e.capture_labels),
           "raw_efm": e.raw_efm_count, "gib": round((e.selected_bytes or 0) / 1024 ** 3, 1),
           "selectable": e.selectable, "reason": e.reason, "warnings": list(e.warnings)}
    if status is not None:
        row["local"] = status.state
    return row


def cmd_catalog_search(args) -> int:
    from fstk.catalog.index import local_status

    index = _catalog(args)
    try:
        prof = _profile(args)
    except FstkError:
        prof = None
    hits = index.search(" ".join(args.query))
    rows = [_entry_row(e, local_status(e, prof) if prof else None) for e in hits]
    _print(args, rows, "\n".join(
        f"{r['id']:11} {r['title'][:38]:38} {r['captures']:>2} caps {r['gib']:>7.1f} GiB "
        f"{'' if r['selectable'] else '[lookup-only]'} {r.get('local', '')}" for r in rows) or "no match")
    return 0 if rows else 1


def cmd_catalog_coverage(args) -> int:
    index = _catalog(args)
    cov = index.coverage()
    data = {"summary": index.summary(),
            "series": {s: {"seasons": {k: len(v) for k, v in c.seasons.items()}, "note": c.note} for s, c in cov.items()}}
    _print(args, data, "\n".join(
        [f"snapshot {data['summary']['generated_at_utc']} ({data['summary']['upload_count']} uploads)"] +
        [f"{s}: " + ", ".join(f"S{k}: {n}" for k, n in sorted(v['seasons'].items())) + f"  — {v['note']}"
         for s, v in data["series"].items()]))
    return 0


def cmd_episode_create(args) -> int:
    from fstk.core.identity import EpisodeId
    from fstk.workspace.episodes import create_from_catalog, create_from_local

    prof = _profile(args)
    if args.local:
        store = create_from_local(prof, args.local, actor=_actor(args), allow_parallel=args.parallel)
    else:
        ep = EpisodeId.parse(args.catalog)
        index = _catalog(args)
        entry = index.find(ep.series, ep.season, ep.number)
        if entry is None:
            raise FstkError(f"{ep} is not in the catalog snapshot")
        store = create_from_catalog(prof, entry, actor=_actor(args), catalog_summary=index.summary(),
                                    allow_parallel=args.parallel)
    m = store.load()
    _print(args, {"episode": str(m.episode), "title": m.title, "manifest": str(store.path)},
           f"created {m.episode} ({m.title or 'untitled'}) → {store.path}")
    return 0


# ----------------------------------------------------------------------------- share
def _tree(args):
    from fstk.workspace.shared import SharedTree

    return SharedTree(_profile(args))


def cmd_share_device(args) -> int:
    tree = _tree(args)
    _print(args, {"device": tree.device, "folder": str(tree.folder), "peers": tree.peers()},
           f"this device: {tree.device}\nshared folder: {tree.folder}\npeers: {', '.join(tree.peers()) or 'none'}")
    return 0


def cmd_share_status(args) -> int:
    from fstk.core.identity import EpisodeId

    tree = _tree(args)
    if args.episode:
        rows = {EpisodeId.parse(args.episode).canonical: tree.presence(EpisodeId.parse(args.episode))}
    else:
        rows = tree.all_presence()
    data = {ep: [{"device": p.device, "state": p.state, "updated_utc": p.updated_utc, "mine": p.mine,
                  "snapshot": p.snapshot, "awaiting_reviews": p.detail.get("awaiting_reviews", [])} for p in ps]
            for ep, ps in rows.items()}
    lines = []
    for ep, ps in sorted(rows.items()):
        lines.append(ep)
        lines += [f"  {'*' if p.mine else ' '} {p.describe()}" for p in ps] or ["  (no device has published it)"]
    _print(args, data, "\n".join(lines) or "no episode has been published to the shared folder yet")
    return 0


def cmd_share_publish(args) -> int:
    from fstk.core.identity import EpisodeId

    doc = _tree(args).publish(EpisodeId.parse(args.episode))
    _print(args, doc, f"published {doc['episode']} snapshot {doc['snapshot']} ({doc['state']})")
    return 0


def cmd_share_handoff(args) -> int:
    from fstk.core.identity import EpisodeId

    h = _tree(args).handoff(EpisodeId.parse(args.episode), to=args.to, actor=_actor(args), note=args.note)
    missing = [r["artifact"] for r in h["media"] if not r["present"]]
    _print(args, h, f"handed {args.episode} to {h['to']} (snapshot {h['snapshot']}); this device will not run it "
                    f"any more. Media to make available there: {len(h['media'])} artifact(s)"
                    + (f"; not on this device either: {', '.join(missing)}" if missing else ""))
    return 0


def cmd_share_adopt(args) -> int:
    from fstk.core.identity import EpisodeId
    from fstk.workspace.shared import media_line

    rep = _tree(args).adopt(EpisodeId.parse(args.episode), source=args.source, actor=_actor(args), fork=args.fork)
    lines = [f"{'forked' if args.fork else 'adopted'} {rep['episode']} from {args.source} "
             f"(snapshot {rep['from']['snapshot']})", "media:"] + [f"  {media_line(r)}" for r in rep["media"]]
    if rep["unmapped_tokens"]:
        lines.append("paths this profile cannot place (add them to roots/sources/volumes): "
                     + ", ".join(rep["unmapped_tokens"]))
    _print(args, rep, "\n".join(lines))
    return 0


def cmd_share_reclaim(args) -> int:
    from fstk.core.identity import EpisodeId

    _tree(args).reclaim(EpisodeId.parse(args.episode), actor=_actor(args))
    _print(args, {"reclaimed": args.episode}, f"{args.episode} is active on this device again")
    return 0


def cmd_plan_intake(args) -> int:
    from fstk.core.fsio import atomic_write_json
    from fstk.workflow.plans import expects_efm, intake_plan

    ws = _workspace(args, args.episode)
    m = ws.store.load()
    download = bool(m.doc["sources"].get("archive_item")) and not args.no_download
    plan = intake_plan(m.episode, download=download, include_stack_reference=not args.no_stack,
                       include_efm=expects_efm(m) and not args.no_efm)
    out = Path(args.out) if args.out else ws.state_dir / "plans" / f"{plan['job']}.plan.json"
    atomic_write_json(out, plan)
    _print(args, {"plan": str(out), "steps": [s["stage"] for s in plan["steps"]]},
           f"wrote {out}: " + " → ".join(s["stage"] for s in plan["steps"]))
    return 0


# ----------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fstk", description="FromStunToKill LaserDisc pipeline")
    p.add_argument("--version", action="version", version=f"fstk {__version__}")
    p.add_argument("--profile", help="machine profile path (default: FSTK_PROFILE or per-user config)")
    p.add_argument("--actor", help="name recorded in manifests/approvals (default user@host)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    prof = sub.add_parser("profile").add_subparsers(dest="sub", required=True)
    pi = prof.add_parser("import", help="convert a v38 machine.json")
    pi.add_argument("v38_profile")
    pi.add_argument("--data-root", required=True)
    pi.add_argument("--media-root")
    pi.add_argument("--out")
    pi.add_argument("--force", action="store_true")
    pi.set_defaults(func=cmd_profile_import)
    pn = prof.add_parser("init", help="create a new profile (tools discovered on PATH)")
    pn.add_argument("--data-root", required=True)
    pn.add_argument("--media-root", required=True)
    pn.add_argument("--machine")
    pn.add_argument("--min-free-gb", type=float, default=80.0)
    pn.add_argument("--out")
    pn.add_argument("--force", action="store_true")
    pn.set_defaults(func=cmd_profile_init)
    prof.add_parser("show").set_defaults(func=cmd_profile_show)

    man = sub.add_parser("manifest").add_subparsers(dest="sub", required=True)
    mi = man.add_parser("import", help="import a v38 episode manifest (never modifies the source)")
    mi.add_argument("v38_manifest")
    mi.set_defaults(func=cmd_manifest_import)
    for name, func in (("show", cmd_manifest_show), ("verify", cmd_manifest_verify),
                       ("history", cmd_manifest_history)):
        sp = man.add_parser(name)
        sp.add_argument("episode")
        sp.set_defaults(func=func)

    cat = sub.add_parser("catalog", help="DS9/VOY/TNG Archive catalog").add_subparsers(dest="sub", required=True)
    cs = cat.add_parser("search", help="natural request or title words, e.g. 'DS9 season 2 episode 18'")
    cs.add_argument("query", nargs="*")
    cs.set_defaults(func=cmd_catalog_search)
    cat.add_parser("coverage").set_defaults(func=cmd_catalog_coverage)

    epi = sub.add_parser("episode").add_subparsers(dest="sub", required=True)
    ec = epi.add_parser("create", help="create an episode workspace")
    g = ec.add_mutually_exclusive_group(required=True)
    g.add_argument("--catalog", metavar="SERIES:SxxExx", help="from the Archive catalog")
    g.add_argument("--local", metavar="FOLDER", help="from a local capture folder")
    ec.add_argument("--parallel", action="store_true",
                    help="create it even though another device is actively working on it")
    ec.set_defaults(func=cmd_episode_create)

    sh = sub.add_parser("share", help="shared multi-machine workspace (ADR 0002)").add_subparsers(
        dest="sub", required=True)
    sh.add_parser("device", help="this machine's device identity and shared folder").set_defaults(
        func=cmd_share_device)
    ss = sh.add_parser("status", help="which devices hold an episode (or every published episode)")
    ss.add_argument("episode", nargs="?")
    ss.set_defaults(func=cmd_share_status)
    sp = sh.add_parser("publish", help="publish this device's snapshot + status for an episode")
    sp.add_argument("episode")
    sp.set_defaults(func=cmd_share_publish)
    sho = sh.add_parser("handoff", help="freeze an episode here and hand it to another device")
    sho.add_argument("episode")
    sho.add_argument("--to", required=True, metavar="DEVICE")
    sho.add_argument("--note", default="")
    sho.set_defaults(func=cmd_share_handoff)
    sa = sh.add_parser("adopt", help="continue an episode another device handed to this one")
    sa.add_argument("episode")
    sa.add_argument("--from", dest="source", required=True, metavar="DEVICE")
    sa.add_argument("--fork", action="store_true", help="copy it although it was not handed off (both stay active)")
    sa.set_defaults(func=cmd_share_adopt)
    sr = sh.add_parser("reclaim", help="take back a handoff the other device has not adopted yet")
    sr.add_argument("episode")
    sr.set_defaults(func=cmd_share_reclaim)

    pl = sub.add_parser("plan").add_subparsers(dest="sub", required=True)
    pi2 = pl.add_parser("intake", help="write an intake plan (download → inventory → stack reference)")
    pi2.add_argument("episode")
    pi2.add_argument("--out")
    pi2.add_argument("--no-download", action="store_true")
    pi2.add_argument("--no-stack", action="store_true")
    pi2.add_argument("--no-efm", action="store_true", help="leave out EFM decode/selection")
    pi2.set_defaults(func=cmd_plan_intake)

    sub.add_parser("stages", help="list registered stages").set_defaults(func=cmd_stages)
    docs = sub.add_parser("docs").add_subparsers(dest="sub", required=True)
    ds = docs.add_parser("stages", help="write the generated stage reference")
    ds.add_argument("--out", default="docs/stage-reference.md")
    ds.set_defaults(func=cmd_docs_stages)

    r = sub.add_parser("run", help="run (or resume) a workflow plan")
    r.add_argument("plan")
    r.set_defaults(func=cmd_run)
    st = sub.add_parser("status", help="show a job's step states")
    st.add_argument("episode")
    st.add_argument("job")
    st.set_defaults(func=cmd_status)
    d = sub.add_parser("doctor", help="read-only readiness report")
    d.add_argument("--no-profile", action="store_true", help="check the Python environment only")
    d.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except FstkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
