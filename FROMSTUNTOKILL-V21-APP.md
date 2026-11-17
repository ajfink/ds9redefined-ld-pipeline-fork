# FromStunToKill app branch - V21 boundary-review integration

V21 clarifies the DVD-reference policy for episode opening and ending points. The cadence-workbench
review now calls for separate DVD visual/audio boundary measurements, mapping those points to LD
output frames and native-audio samples, preserving the DVD's measured A/V relationship, and
recomputing downstream trims, origin, endpoint, and retime evidence.

For a midpoint commercial-break fade, an LD trim is only a candidate when the matching DVD
interval is measured shorter. Internal LD picture/audio cuts are **not implemented**; the app
review asks the operator to keep the candidate unresolved and stop rather than altering the LD
master or overloading the DVD retime schedule.

Validation was **403 passed, 7 skipped (410 tests)** plus compile/import, generated-runbook,
PowerShell parser, and diff checks. No media processing or GUI smoke test was performed.
Production `main` and W remain V15; see `V21-RELEASE.md` for the internal-LD-edit limitation.
