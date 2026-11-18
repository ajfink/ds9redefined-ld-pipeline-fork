# FromStunToKill desktop app — V18 integration record

V18 adds a read-only episode status dashboard, report-only audio plans, cadence preparation plans,
explicit delivery workflow plans, and a non-terminating subprocess monitor. These features reuse
the existing allowlisted validators and stages rather than implementing alternate media logic.

The delivery workbench detects whether the current artifacts call for QC-only, remux+QC, or a full
encode/remux/QC path. It refuses output collisions, adds separate review gates before final encode
and remux, and does not execute a plan while it is being built or previewed. Long-running process
warnings are advisory and diagnostic snapshots are written as unique files under the active log
folder.

Release validation was **389 passed, 7 skipped (396 tests)** plus compile/import, runbook, PowerShell,
and diff checks. No real-media workflow, final encode/remux, Mac runtime, or real workspace restore
was run. Production `main` and W remain V15; open reconstructed evidence is carried forward in
authoring execution-log section 55.
