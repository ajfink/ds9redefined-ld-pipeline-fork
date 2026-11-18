# FromStunToKill desktop app — V17 integration record

V17 updates the app-branch PAD experience so humans review cut-like/outlier candidates instead of
individually classifying every PAD. Candidates below the existing source motion-baseline P95 are
automatically recorded as non-cut/RIFE when metrics are valid, the frame is not a prior cut, and
the full-raster residual is within policy. P95+ candidates, missing metrics, prior cut-PADs, and
above-threshold residuals remain subject to human review. The V16 cut still/hold protections, exact
warning, immutable input-bound decision reports, and direct RIFE evidence checks remain active.

The GUI presents the P95 policy and which candidates need review. After the reviewed/automatic PAD
manifest update succeeds, it automatically creates, previews, and runs a unique RIFE-only plan.
Failures stop continuation. Validation: **371 passed, 7 skipped (378 tests)**, with compilation,
warnings-as-errors imports, generated-runbook check, seven PowerShell parsers, and diff check
passing. This app-branch release does not change production `main` or W, and does not claim
real-media threshold calibration or new platform support. See `V17-RELEASE.md` and the
carried-forward episode evidence notes in authoring execution-log section 55.
