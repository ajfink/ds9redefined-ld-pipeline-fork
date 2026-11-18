> **NOT FOR LIVE WORKFLOW USE.** This release record is historical planning/evidence only.
> Follow the released README, canonical runbook, manifests, and helpers for live work.

# V16 implementation record — device-local resilience and evidence integrity

## Scope

- Treat release-root ACL diagnostics as advisory; preserve application guards so bundle writes
  remain limited to the active device's own workspace. If that workspace is inaccessible, use
  an isolated local fallback and a reviewed no-overwrite resync.
- Bind EFM comparison reports to the episode/capture output path and declared capture inventory.
- Validate retime interval objects, measured step/gap consistency, and alignment coverage of the
  measured delta anchor before retime.
- Resolve PAD black scans from the manifest deliverable; verify TFM run completeness from the
  FFmpeg session log, not the decision log.
- Supervise long Orc stack rendering through an allowlisted runner with an explicit space
  estimate, a separate file log, process-group cleanup, and a bounded stale-progress watchdog.
- Use local subprocess working directories in shared mode, preserve script-owned logs, and make
  shared-workspace QC produce a per-run device closeout/retention record.

## Acceptance

- Synthetic tests cover stale/cross-episode EFM reports, malformed merged-interval schema,
  interval/recorded-gap consistency, range-edge alignment refusal, deliverable-key resolution,
  TFM false-failure prevention, local fallback sync conflicts/redaction, QC closeout, and
  subprocess heartbeat/watchdog behavior.
- Full unit suite, compilation/import, generated runbooks/plain-text freshness, PowerShell
  parsing, and whitespace checks pass before release.
- No media download, processing, encoding, or remuxing is required or authorized for this
  implementation.

This record is not an independent operator instruction source.
