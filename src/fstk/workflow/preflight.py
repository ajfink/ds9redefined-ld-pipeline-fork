"""Check, before a plan starts, that every tool its steps will use is there and runs.

Legion 2026-11-23: a delivery plan started with a vspipe that already exited 1 (``vspipe --version``), rendered the
stack master for 7 hours, and then failed at the cadence step because of that vspipe. A step only resolves its tools
when it starts, so the engine now asks first: which tools do the plan's stages use, does each resolve, and does each
executable answer its version flag with exit 0. Any problem stops the plan before the first step, with what to do.
"""
from __future__ import annotations

import inspect
import re
from collections.abc import Iterable, Mapping

from fstk.stages.base import get_stage

#: adapter name used by stages (``ctx.tool(name)``) → the profile tools it needs, and how to ask each if it runs
ADAPTERS: dict[str, tuple[str, ...]] = {
    "ffmpeg": ("ffmpeg", "ffprobe"),
    "vapoursynth": ("vspipe",),
    "orc": ("orc",),
    "rife": ("rife_engine", "rife_model_dir"),
    "sevenzip": ("sevenzip",),
}
VERSION_ARGS: dict[str, list[str]] = {"ffmpeg": ["-version"], "ffprobe": ["-version"], "vspipe": ["--version"]}
_TOOL_CALL = re.compile(r"""\.tool\(\s*["']([a-z0-9_]+)["']\s*\)""")
_CACHE: dict[str, frozenset[str]] = {}


def stage_tools(stage_id: str) -> frozenset[str]:
    """Adapters a stage uses: its declared ``tools`` if it has them, otherwise every ``ctx.tool("…")`` in its
    module (conservative: a module's stages share helpers)."""
    if stage_id in _CACHE:
        return _CACHE[stage_id]
    stage = get_stage(stage_id)
    declared = getattr(stage, "tools", None)
    if declared is not None:
        found = frozenset(declared)
    else:
        try:
            src = inspect.getsource(inspect.getmodule(type(stage)))
        except (OSError, TypeError):
            src = ""
        found = frozenset(_TOOL_CALL.findall(src))
    _CACHE[stage_id] = found
    return found


def plan_tools(plan: Mapping) -> set[str]:
    out: set[str] = set()
    for step in plan.get("steps", []):
        out |= stage_tools(step["stage"])
    return out


def tool_problems(profile, adapters: Iterable[str], *, skip: Iterable[str] = (), runner=None,
                  warnings: list[str] | None = None) -> list[str]:
    """Problems with the tools behind ``adapters`` (none = ready). ``skip``: adapters replaced in tests.

    A tool that is set up but missing or broken is a problem (the step using it would fail). A tool that is not
    set up at all and not on PATH goes to ``warnings`` only: some stages use a tool for some inputs only (7-Zip for
    zipped captures), and such a step still says exactly what is missing if it does need it."""
    from fstk.tools import process

    run = runner or (lambda argv: process.run(argv, capture_stdout=True, timeout=60).returncode)
    skipped = set(skip)
    problems: list[str] = []
    seen: set[str] = set()
    for adapter in sorted(set(adapters) - skipped):
        for name in ADAPTERS.get(adapter, ()):
            if name in seen:
                continue
            seen.add(name)
            try:
                path = profile.tool(name)
            except Exception as exc:
                configured = bool((getattr(profile, "tools", None) or {}).get(name))
                (problems if configured or warnings is None else warnings).append(str(exc))
                continue
            args = VERSION_ARGS.get(name)
            if args is None:
                continue
            try:
                code = run([str(path), *args])
            except Exception as exc:
                problems.append(f"{name} at {path} does not start ({exc.__class__.__name__}: {exc})")
                continue
            if code != 0:
                problems.append(f"{name} at {path} does not work (`{name} {' '.join(args)}` exits {code})")
    if "vapoursynth" in set(adapters) - skipped:
        problems += vapoursynth_problems()
    return problems


def vapoursynth_problems(probe=None) -> list[str]:
    """VapourSynth plugins the generated scripts load, tested by loading them (Legion 2026-11-23: a new
    environment lacked `mvu`, and the video-trim step failed after the cadence step had run)."""
    from fstk.tools import requirements

    rows = (probe or requirements.probe_vapoursynth)()
    return [f"VapourSynth {label}: {note}" for label, status, note in rows if status == "FAIL"]


def describe(problems: list[str]) -> str:
    return ("Not started: a tool this plan needs is missing or broken, and it would only fail hours in.\n"
            + "\n".join(f"• {p}" for p in problems)
            + "\nQuit and start the app again with the FromStunToKill launcher (it checks and repairs the tools), "
              "or run `FromStunToKill setup`.")
