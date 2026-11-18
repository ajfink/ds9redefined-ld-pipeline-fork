# FromStunToKill app branch - V19 integration record

V19 adds workflow progress to `scripts/run_workflow.py`. Each plan step, including human review,
updates an ASCII bar with plan completion, the active step, elapsed time, and child-output idle
time. Heartbeats continue during long or silent child stages. The value is workflow plan-step
completion; it is not an estimate of percent complete inside a stage.

The display is informational only. Existing output streaming, logging, review gates, and
fail-closed retry policy remain unchanged. Production `main` and W remain V15.

Validation was **393 passed, 7 skipped (400 tests)**, plus compile/import, generated-runbook,
PowerShell parser, and diff checks. No media was processed and no GUI smoke test was performed.
