> **NOT FOR LIVE WORKFLOW USE.** This is a proposal backlog only. Do not read, cite, or apply
> queue entries as episode instructions, measurements, approvals, stage arguments, or gates.
> Active jobs must follow the current README, canonical runbook, and released tools. A queue
> proposal becomes operative only after explicit implementation and release.

# V11 planning queue

**Current released version is V11.** This file is a backlog of proposals, not current behavior.
V11's accepted queue-use boundary and `init_episode` regression fix are described in
`V11-RELEASE.md`; the remaining items below are not operative until separately implemented
and released. They do not change the dependency doctor, locks, readiness gates, or installation
behavior. No package is installed, updated, downgraded, or fetched automatically by a proposal.

## V11 accepted changes

- Queue files are explicitly marked non-operative. README and live runbooks direct active
  workflows to current released instructions and scripts; queue proposals are not
  measurements, approvals, stage inputs, or gates.
- Fixed the shared-mode `init_episode.py` crash by passing the Archive-derived `code` to
  workspace resolution rather than undeclared `args.episode`. Added AST regression tests.

The dependency policy and LD/DVD comparator below remain proposals. They do not alter the
released V11 behavior unless separately implemented and approved in a later version.

## 1. Accept installed target-or-newer dependency versions without prompting

### Requested behavior

When a required tool or dependency is already installed at its declared target version or
newer, the routine dependency check should pass without asking the operator to confirm,
checking upstream for updates, or suggesting an update. Version checks remain local and
read-only. Existing capability, import, and functional smoke checks still run; an installed
version is not proof that a required feature works.

### Implementation scope

- Inventory every version check in `dependency_lock.py`, `bootstrap_deps.py`, `doctor.py`,
  Windows/Mac preflight, and the setup/runbook prompts. Replace exact-equality mismatch
  handling with an explicit `installed >= target` rule for comparable versions.
- Use the correct version semantics for each ecosystem (for example PEP 440 for Python
  packages); never compare version strings lexicographically. Treat malformed, unknown, or
  incomparable versions as **unverified**, not as newer.
- Keep platform-specific targets and dependency relationships. A newer dependency does not
  excuse a missing import, missing Orc/FFmpeg capability, failed smoke test, or an existing
  stage/quality gate.
- Report both target and installed versions, and identify when a newer installed version
  was accepted. Do not make a network request to learn the latest version, and do not invoke
  pip, WinGet, Homebrew, or another installer from a check/doctor path.
- Preserve the explicit `bootstrap_deps.py --install` opt-in for a missing or older package;
  this queue item only removes confirmation/update-check friction for an already-installed
  target-or-newer version. Do not change device profiles or silently rewrite lockfiles.

### Acceptance tests

- For every supported Python lock target, exact target versions pass and newer comparable
  versions pass with a clear `newer-than-target accepted` report and no prompt.
- Older, missing, malformed, prerelease, and incomparable versions follow an explicit,
  tested policy; they are never accidentally accepted because of string ordering.
- Native tool tests cover target/newer version output plus required feature/smoke checks.
- Mock subprocess/network boundaries prove version checks neither query update services nor
  launch installers. Installation remains possible only through the existing explicit
  `--install` command.
- Run the full unit suite, compilation, preflight/parser checks, generated-runbook checks,
  and doctor tests on supported hosts. Do not infer macOS/Linux production support from a
  dependency comparison passing.

## 2. Ship a reusable report-only LD/DVD audio comparator

### Evidence and current prototype

The V8-era device guidance calls for comparing LD and DVD audio levels, spectrum, and
content on every relevant episode. The only reusable attempt so far is the untracked
`scripts/ld_dvd_audio_compare.py` prototype, described in
`Z:\work\logs\S01E12-helper-ld-dvd-audio-compare.md`. Its E08 regression report is
`Z:\work\probe\S01E08-ld-dvd-audio-compare.json`; the original E08 one-off script/report
remain historical evidence and must not be copied into the bundle as the implementation.

The helper is report-only: it selects no rate, approves nothing, edits no manifest, and
writes no audio. It should remain a diagnostic, not silently become an automatic quality or
rate-selection gate.

The running-instance v2 label/schema correction reported in the S01E12 helper log is not
present in the current Z prototype: the local untracked script still has schema v1 and the
same-start band labels. Reconcile and test that patch before considering the helper adopted;
do not treat its report as released V11 behavior.

### Implementation scope

- Review and adopt the parameterized helper into the repository with tests. Resolve the
  selected `files.ld_native` or `files.ld_corrected` and DVD from the episode manifest by
  default; permit explicit WAV/DVD/selector overrides for isolated A/B diagnostics. Refuse
  an existing report and preserve shared-workspace path redaction.
- Decode DVD audio sequentially (never `-ss`) and select the configured/manifest DVD stream
  or a clearly reported default. Keep the report caveat that LD Dolby Surround and DVD AC-3
  are different mixes: envelope lag is corroboration, not an `asetrate` measurement.
- Report second-by-second envelopes and spectral-band summaries. **Audit the prototype's
  band labels:** its existing `ld_band_mean` and `dvd_band_mean` are averaged from each
  source's same-start seconds even when the refined content lag is about 50 seconds. Keep
  those values explicitly labeled as unshifted whole-source summaries, or add separately
  named lag-aligned band statistics; never label same-start aggregates as aligned. Version
  the report schema if fields or semantics change.
- Keep memory use bounded for full episode audio and preserve the currently documented E08
  coarse/refined envelope-lag and level/band results as a regression fixture without adding
  E08 media or absolute local paths to the repository.

### Acceptance tests

- Regression test reproduces the documented E08 lag/correlation, first-400-second envelope
  difference, and legacy band summaries from a sanitized numeric fixture.
- Synthetic shifted signals verify lag sign and aligned-window selection; synthetic bands
  verify that the aligned summaries use the same content seconds while legacy whole-source
  fields retain their original labels.
- Tests cover missing manifest WAV/DVD entries, invalid audio format, selector errors,
  existing-report refusal, report-only behavior, and shared-path redaction.
- Run the full test, compile, runbook-generation, warning, and diff checks. No actual episode
  audio/DVD processing is part of unit acceptance; real use remains optional and report-only.

## Guardrails

- V11 remains authoritative until a later proposal is accepted and released.
- A newer version is not automatically considered functionally compatible: existing
  capability, import, readiness, and media-quality gates remain binding.
- No update lookup or package mutation is part of a diagnostic/check operation.
- The V10 release and dependency behavior remain authoritative until a tested V11 release.
- Do not include the one-off E08 script/report, DVD, WAV/FLAC, or other media in the source
  repository; only sanitized regression numbers/fixtures may be committed.
