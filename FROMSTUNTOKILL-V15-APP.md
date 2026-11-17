# FromStunToKill desktop app — V15 integration record

## Integration boundary

This branch is based on the published V15 production source. It retains the production
V15 `ldcommon`, series-aware initializer, shared-workspace, validator, `run_stage.py`, and
V15 retime helpers, with an optional Tk desktop app layered on top. The production checkout
was not modified. `WORKFLOW_VERSION` is 15.

The app workflow runner adds typed review/deny steps and concise/verbose console output to
the V15 series-aware plan/state contract. V15 stage names and gates are retained. The app's
concrete intake plans store GUI values in `_fromstuntokill_intake`; this lets the GUI support
DVD-backed intake with the exact selected DVD while preserving the stock V15 template's
no-DVD-only rule. Existing bare identities remain DS9.

## Integrated app behavior

- Archive URL mode hides the Series selector and resolves series from the selected catalog
  item or Archive metadata. Open local mode shows Series when filenames do not identify DS9
  or VOY. Both modes show canonical `SERIES:SxxExx` identity.
- Bootstrap reserves the stable device identity and creates `devices/<DEVICE>-<ID>/` before
  writing `local device config/machine.json` there. The profile is accepted inside the release
  only at that exact device-bound path and only when its ID matches a machine-local identity
  marker. Its work, probe, AVS, audio, subtitle, output, stack, download, and log roots are scoped
  to the ignored device directory. Other per-user profiles are not candidates and remain intact.
- The metadata-only Archive picker routes standalone DS9/VOY items through the full V15
  series-aware intake. It does not download media or bypass ListOnly/integrity/sidecar/space
  gates. TNG combined items and Emissary remain blocked from generic single-episode intake.
- Open local inventories decoded captures and records fingerprints without claiming Archive
  origin. Input validation rechecks the local inventory and its file fingerprints.
- The app exposes V15 retime-break candidate preparation as a report-only plan, then shows all
  candidate/pair statuses in a review table. No rows are preselected. Unique pairs require an
  explicit cut decision; no-silence candidates require an explicit merged-through decision and
  confirmation. Recording is a separate plan with a rationale, report digest, input gate, and
  explicit human review before the V15 backup/atomic writer runs. Offsets and steps remain null.
- Open local folder/file selection resolves and populates the selected local episode through the
  same identity path; a missing GUI import in this callback was fixed and covered by a regression.
- Shared-profile download templates constrain Archive downloads only; existing local-source
  folders may remain anywhere the operator selects, with the normal local inventory checks.
- Exact DVD and local capture paths may live outside the device profile's configured default
  roots; they are recorded in the machine-isolated device episode manifest for later validation.
- Saved plans remain immutable; the GUI suggests the next unused intake-plan filename rather
  than overwriting an earlier plan.
- Template instruction placeholders are cleared from GUI fields rather than displayed as
  selections; DVD mode and cut-PAD policy remain operator choices.
- Episode title is optional; an omitted local title becomes an explicit identity-based
  placeholder and a reminder to confirm it before downstream processing. Cut-PAD image policy
  is validated only for no-DVD intake and is recorded as not applicable for DVD-backed intake.
- The separate Save Plan action is removed. Preview automatically creates a new immutable plan
  under the verified device's `plans/` folder. Clicking Run on an unsaved intake saves and
  previews first; a second click is required to execute. Intake fields stay editable through
  preview; changes create a new plan on the next Preview/Run. Fields freeze after Run is confirmed.
- Doctor/preflight accepts the same verified device-local config path. Initializer doctor checks
  omit episode-manifest validation until initialization has created the manifest; device-rooted
  outputs are verified before the shared-root ACL is treated as a warning rather than a hard gate.
  Shared write-path checks permit only the active episode workspace or the current device's
  configured subroots; sibling-device, device-config, plan, episode-tree, and release-source
  writes remain blocked.
- Local initializer stages accept selected capture folders and DVDs outside the configured
  Archive-download/DVD roots and record those paths in the machine-isolated episode manifest.
- OpenCode review help is optional, audited, and disabled by default. The core workflow and
  candidate analysis work without AI; AI output cannot select measurements or approve a gate.
  The repair-source bundle excludes changelogs, version histories, and queues.
- `measure_ld_black.py` now computes float64 row means directly from the mapped descriptor
  array, avoiding a full-size float64 copy. This reduces a potential memory spike without
  changing the reduction precision; no wall-time or peak-RSS improvement is claimed until
  measured on target hardware.

## Validation and platform limits

- Full app-plus-V15 suite after the local-picker, device-profile bootstrap, placeholder, and
  intake validation fixes: **314 passed, 7 skipped (321 tests run)**.
- Python compile checks, generated-runbook check, PowerShell parsing, and GUI smoke tests
  passed. GUI smoke used mocked resolver callbacks; no live Archive request was made.
- Device-profile preflight: **67 pass, 1 conditional QTGMC warning, 0 fail**. The profile and
  generated directories are under the ignored device subtree; the older root-level profile was
  not overwritten.
- No media was downloaded or processed. No final encode or remux was run.
- Production's M5/macOS arm64 end-to-end evidence remains the only Apple Silicon workflow
  baseline. This integration was validated on Windows x64; it makes no fresh-Mac, Linux,
  Linux ARM64, or Windows ARM64 support claim. V15 adds no cross-platform performance
  benchmark or hardware-acceleration path.

See the production `V15-RELEASE.md` for the core release evidence. This file records only the
FromStunToKill app overlay and its additional tests.
