"""Error hierarchy.

Every failure that an operator can act on is an ``FstkError`` with a message that says
what is wrong and, where possible, what to do. Code never catches these to substitute a
default value: an unknown measurement stays unknown (invariant 1, docs/REFACTOR-PLAN.md).
"""
from __future__ import annotations


class FstkError(RuntimeError):
    """Base class for actionable pipeline failures."""


class ConfigError(FstkError):
    """Machine profile or job configuration is missing or invalid."""


class IdentityError(FstkError):
    """An episode/series identity could not be parsed or conflicts."""


class ManifestError(FstkError):
    """Manifest content, schema, or write-protocol failure."""


class ConcurrentModificationError(ManifestError):
    """The manifest changed on disk after it was read (lost-update protection)."""


class ToolError(FstkError):
    """An external tool is missing, misconfigured, or failed."""


class ArtifactError(FstkError):
    """A produced artifact failed validation (regardless of the tool's exit code)."""


class GateError(FstkError):
    """A prerequisite gate (measurement, approval, space) is not satisfied."""


class SpaceError(GateError):
    """Projected free space would fall below the configured reserve."""


class WorkflowError(FstkError):
    """Plan, state, or execution-engine failure."""


class PreflightError(WorkflowError):
    """A plan was not started because a tool it needs is missing or broken."""
