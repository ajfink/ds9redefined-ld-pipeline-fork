# FromStunToKill app branch - V22 A/V alignment priority

V22 keeps the LD's internal picture/audio timeline; the existing piecewise retime continues to
map DVD 5.1/SRT onto it. The goal is verified final A/V alignment, not matching the duration of
every DVD/LD black interval. The cadence-workbench review now presents this policy for DVD-backed
episodes and does not suggest cutting LD at internal commercial breaks.

V21's DVD-referenced opening/ending alignment remains in force to remove measured LD-only front
matter/tail and establish a common program origin. Picture and audio fade points are measured
separately, mapped to LD frame/sample domains, and the DVD's measured relative A/V offset is
preserved. No media transforms or manifest schema were added in V22.

Validation was **403 passed, 7 skipped (410 tests)** plus compile/import, generated-runbook,
PowerShell parser, and diff checks. No real-media run or GUI smoke test was performed. Production
`main` and W remain V15.
