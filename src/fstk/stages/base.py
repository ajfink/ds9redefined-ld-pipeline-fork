"""Stage framework.

A stage is a class that *declares* its contract and implements three steps:

    plan(ctx, params)            → StepPlan     (pure-ish: resolves paths, estimates space)
    run(ctx, plan)               → StepOutput   (does the work; proposes measurements)
    verify(ctx, plan, output)    → list[str]    (artifact validation; empty list = pass)

Stages never write the manifest themselves. They *propose* values through
``StepOutput.measurements`` (each with evidence fingerprints); the engine commits them in
one journaled transaction **only after verify passes**. A failed or unverified run leaves
the manifest untouched.

Declared contract (used by the engine, the docs generator, and the UI):

* ``requires``      – gates that must pass before ``plan`` (values set, artifacts present,
                      approvals current, operator choices);
* ``writes``        – manifest keys the stage may propose (anything else is refused);
* ``resource``      – scheduling class (network / prep / compute / gpu) or None;
* ``review``        – if set, downstream stages are blocked until a human approves this
                      stage's evidence (see ``fstk.evidence.approvals``).
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from fstk.config.profile import Profile
from fstk.core.errors import GateError, ManifestError, WorkflowError
from fstk.core.hashing import SCOPE_FULL, SCOPE_HEAD_TAIL, SCOPE_JSON, Fingerprint
from fstk.core.identity import EpisodeId
from fstk.manifest.model import Manifest
from fstk.manifest.registry import field_def
from fstk.manifest.store import ManifestStore

RESOURCES = ("network", "prep", "compute", "gpu")


# ------------------------------------------------------------------------------ context
@dataclass
class StageContext:
    profile: Profile
    episode: EpisodeId
    store: ManifestStore
    actor: str
    cancel: threading.Event = field(default_factory=threading.Event)
    progress: Callable[[str, float | None], None] | None = None
    log_dir: Path | None = None
    tool_overrides: dict[str, Any] = field(default_factory=dict)

    @property
    def manifest(self) -> Manifest:
        return self.store.load()

    def root(self, name: str) -> Path:
        return self.profile.root(name, self.episode)

    def artifact_path(self, key: str, manifest: Manifest | None = None) -> Path:
        m = manifest or self.manifest
        spec = m.artifact(key)
        if not spec:
            raise GateError(f"{self.episode}: artifact {key!r} is not declared in the manifest")
        if "path" in spec:
            return Path(spec["path"])
        return self.root(spec["root"]) / spec["name"]

    def tool(self, name: str):
        """Tool adapters, overridable in tests (``tool_overrides``)."""
        if name in self.tool_overrides:
            return self.tool_overrides[name]
        if name == "ffmpeg":
            from fstk.tools.ffmpeg import FFmpeg

            return FFmpeg.from_profile(self.profile, log_dir=self.log_dir)
        if name == "orc":
            from fstk.tools.orc import Orc

            return Orc(self.profile.tool("orc"), log_dir=self.log_dir)
        if name == "archive":
            from fstk.tools.archive_http import ArchiveClient

            return ArchiveClient()
        if name == "sevenzip":
            from fstk.tools.sevenzip import SevenZip

            return SevenZip(self.profile.tool("sevenzip"))
        if name == "vapoursynth":
            from fstk.tools.vapoursynth import VapourSynth

            return VapourSynth.from_profile(self.profile, log_dir=self.log_dir)
        raise WorkflowError(f"unknown tool adapter {name!r}")

    def report(self, fraction: float | None = None, message: str = "") -> None:
        from fstk.tools.process import PAUSE

        PAUSE.wait_while_paused(self.cancel)
        if self.progress:
            self.progress(message, fraction)

    def check_cancel(self) -> None:
        from fstk.tools.process import PAUSE

        PAUSE.wait_while_paused(self.cancel)
        if self.cancel.is_set():
            raise WorkflowError("cancelled by operator")


# ------------------------------------------------------------------------------ gates
@dataclass(frozen=True)
class GateResult:
    ok: bool
    name: str
    detail: str


class Requirement:
    name: str = "requirement"

    def check(self, ctx: StageContext, manifest: Manifest) -> GateResult:  # pragma: no cover
        raise NotImplementedError

    def describe(self) -> str:
        return self.name


@dataclass(frozen=True)
class ValueSet(Requirement):
    key: str

    def __post_init__(self) -> None:
        field_def(self.key)

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"value:{self.key}"

    def check(self, ctx, manifest):
        ok = manifest.is_set(self.key)
        return GateResult(ok, self.name, "set" if ok else f"{self.key} is not measured/recorded yet")


@dataclass(frozen=True)
class ValueIs(Requirement):
    key: str
    expected: Any

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"value:{self.key}=={self.expected!r}"

    def check(self, ctx, manifest):
        actual = manifest.get(self.key)
        ok = manifest.is_set(self.key) and actual == self.expected
        return GateResult(ok, self.name, "ok" if ok else f"{self.key} is {actual!r}")


@dataclass(frozen=True)
class ArtifactExists(Requirement):
    key: str

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"artifact:{self.key}"

    def check(self, ctx, manifest):
        try:
            p = ctx.artifact_path(self.key, manifest)
        except GateError as exc:
            return GateResult(False, self.name, str(exc))
        ok = p.is_file() and p.stat().st_size > 0
        return GateResult(ok, self.name, str(p) if ok else f"missing or empty: {p}")


@dataclass(frozen=True)
class CapturesAtLeast(Requirement):
    count: int

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"captures>={self.count}"

    def check(self, ctx, manifest):
        n = len(manifest.capture_labels)
        return GateResult(n >= self.count, self.name, f"{n} capture(s) inventoried")


@dataclass(frozen=True)
class Custom(Requirement):
    label: str
    func: Callable[[StageContext, Manifest], tuple[bool, str]]

    @property
    def name(self) -> str:  # type: ignore[override]
        return self.label

    def check(self, ctx, manifest):
        ok, detail = self.func(ctx, manifest)
        return GateResult(bool(ok), self.name, detail)


# ------------------------------------------------------------------------------ plan/output
@dataclass
class SpaceNeed:
    root: str
    planned_gib: float
    basis: str  # how the estimate was derived (must be defensible, never a prior episode's size)
    target: Path | None = None  # explicit folder (overrides the logical root), e.g. a download dir


@dataclass
class StepPlan:
    stage: str
    params: dict[str, Any]
    inputs: list[Path] = field(default_factory=list)
    outputs: list[Path] = field(default_factory=list)
    space: SpaceNeed | None = None
    summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    #: Fingerprint scope per input (large captures use head-tail)
    input_scope: dict[str, str] = field(default_factory=dict)
    #: Per-output fingerprint scope; outputs not listed use head+tail when ≥ LARGE_OUTPUT_BYTES.
    output_scope: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        lines = [f"{self.stage}: {self.summary}".rstrip(": ")]
        lines += [f"  in : {p}" for p in self.inputs]
        lines += [f"  out: {p}" for p in self.outputs]
        if self.space:
            lines.append(f"  space: {self.space.planned_gib:.2f} GiB on '{self.space.root}' ({self.space.basis})")
        return "\n".join(lines)


@dataclass
class Measurement:
    key: str
    value: Any
    evidence: list[dict] = field(default_factory=list)
    note: str | None = None


@dataclass
class StepOutput:
    measurements: list[Measurement] = field(default_factory=list)
    artifacts: dict[str, dict[str, str]] = field(default_factory=dict)  # key -> {root,name}|{path}
    report: Path | None = None
    notes: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    #: Keys merged into manifest ``sources`` (only for stages declaring ``writes_sources``).
    sources_update: dict[str, Any] = field(default_factory=dict)
    #: For stages with a ``review``: False means every automatic check passed and the result is
    #: committed without waiting for a human (e.g. an auto-accepted DVD alignment).
    needs_review: bool = True

    def measure(self, key: str, value: Any, *, evidence: Iterable[Fingerprint | dict] = (),
                note: str | None = None) -> None:
        field_def(key).validate(value)
        ev = [e.to_dict() if isinstance(e, Fingerprint) else dict(e) for e in evidence]
        self.measurements.append(Measurement(key, value, ev, note))


# ------------------------------------------------------------------------------ stage base
class Stage:
    id: ClassVar[str] = ""
    title: ClassVar[str] = ""
    description: ClassVar[str] = ""
    requires: ClassVar[tuple[Requirement, ...]] = ()
    writes: ClassVar[tuple[str, ...]] = ()
    writes_artifacts: ClassVar[tuple[str, ...]] = ()
    resource: ClassVar[str | None] = None
    review: ClassVar[str | None] = None  # review id; None = no human review required
    #: When the review applies, if not always (shown in the stage reference).
    review_when: ClassVar[str] = ""
    writes_sources: ClassVar[tuple[str, ...]] = ()  # manifest ``sources`` keys this stage may set

    def __init_subclass__(cls, **kw) -> None:
        super().__init_subclass__(**kw)
        if not cls.id:
            return
        if cls.resource is not None and cls.resource not in RESOURCES:
            raise WorkflowError(f"stage {cls.id}: unknown resource {cls.resource!r}")
        for key in cls.writes:
            field_def(key)

    # -- gates
    def gates(self, ctx: StageContext, manifest: Manifest | None = None) -> list[GateResult]:
        m = manifest or ctx.manifest
        return [req.check(ctx, m) for req in self.requires]

    def assert_gates(self, ctx: StageContext, manifest: Manifest | None = None) -> None:
        failed = [g for g in self.gates(ctx, manifest) if not g.ok]
        if failed:
            raise GateError(f"{self.id}: prerequisites not met: " +
                            "; ".join(f"{g.name} ({g.detail})" for g in failed))

    # -- lifecycle (override)
    def plan(self, ctx: StageContext, params: dict[str, Any]) -> StepPlan:
        return StepPlan(self.id, dict(params))

    def run(self, ctx: StageContext, plan: StepPlan) -> StepOutput:  # pragma: no cover
        raise NotImplementedError

    def verify(self, ctx: StageContext, plan: StepPlan, output: StepOutput) -> list[str]:
        return []

    # -- engine helpers
    def check_writes(self, output: StepOutput) -> None:
        illegal = [m.key for m in output.measurements if m.key not in self.writes]
        if illegal:
            raise ManifestError(f"stage {self.id} proposed undeclared manifest key(s) {illegal}")
        bad_art = [k for k in output.artifacts if k not in self.writes_artifacts]
        if bad_art:
            raise ManifestError(f"stage {self.id} proposed undeclared artifact key(s) {bad_art}")
        bad_src = [k for k in output.sources_update if k not in self.writes_sources]
        if bad_src:
            raise ManifestError(f"stage {self.id} proposed undeclared sources key(s) {bad_src}")


#: Stage outputs at least this large (stack masters, encodes) are fingerprinted by size + first and
#: last 8 MiB instead of a full hash, so resuming a job does not re-read tens of GB (owner decision
#: 2026-11-21). Inputs already worked this way where stages asked for it.
LARGE_OUTPUT_BYTES = 1024 ** 3


def output_scope_for(path: Path, explicit: dict[str, str]) -> str:
    if str(path) in explicit:
        return explicit[str(path)]
    size = Path(path).stat().st_size
    if size >= LARGE_OUTPUT_BYTES:
        return SCOPE_HEAD_TAIL
    return SCOPE_JSON if Path(path).suffix.lower() == ".json" else SCOPE_FULL


def input_scope_for(path: Path, explicit: dict[str, str]) -> str:
    """Step inputs: full hashes, except JSON reports, which are compared by content (time stamps ignored)."""
    if str(path) in explicit:
        return explicit[str(path)]
    return SCOPE_JSON if Path(path).suffix.lower() == ".json" else SCOPE_FULL


def fingerprint_paths(paths: Iterable[Path], scopes: dict[str, str] | None = None) -> list[dict]:
    scopes = scopes or {}
    return [Fingerprint.of(p, scope=scopes.get(str(p), SCOPE_FULL)).to_dict() for p in paths]


# ------------------------------------------------------------------------------ registry
REGISTRY: dict[str, type[Stage]] = {}


def register(cls: type[Stage]) -> type[Stage]:
    if not cls.id:
        raise WorkflowError(f"{cls.__name__} has no id")
    if cls.id in REGISTRY and REGISTRY[cls.id] is not cls:
        raise WorkflowError(f"duplicate stage id {cls.id!r}")
    REGISTRY[cls.id] = cls
    return cls


def get_stage(stage_id: str) -> Stage:
    try:
        return REGISTRY[stage_id]()
    except KeyError:
        raise WorkflowError(f"unknown stage {stage_id!r}; known: {', '.join(sorted(REGISTRY))}") from None


@dataclass(frozen=True)
class ApprovalCurrent(Requirement):
    """A named human review must be APPROVED and still bound to unchanged evidence."""

    review: str

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"approval:{self.review}"

    def check(self, ctx, manifest):
        from fstk.evidence.approvals import ApprovalBook

        book = ApprovalBook(ctx.profile.episode_state_dir(ctx.episode) / "approvals")
        status = book.status(self.review, manifest=manifest)
        return GateResult(status.satisfied, self.name, f"{status.state} {'; '.join(status.reasons)}".strip())
