> **NOT FOR LIVE WORKFLOW USE.** This is a status/evidence register only. Do not read, cite,
> or apply entries as episode instructions, measurements, approvals, stage arguments, or gates.
> Recover missing evidence from authentic sources; never reconstruct it from memory or neighboring episodes.

# V16 evidence register — reconstructed Mac episode gaps

This file tracks unresolved evidence only. It is not a work plan and does not authorize media
operations. The underlying files remain in their device episode workspaces; the Git-ignored
`devices/` tree is intentionally not included in the app-branch commit.

| Episode | Preserved state | Open evidence gap | Boundary |
|---|---|---|---|
| S01E11 | Reconstructed manifest/QC and original recovered log bytes preserved. | The five recovered `efm-decode-{L1,M1,P1,P2,P3}.log` files identify S01E10, not S01E11; the matching cross-folder copies are byte-identical. B2 exclusion rationale is missing; B8 remains a sub-resolution `LIMIT`, and verification rows lack numeric per-gap measurements. | Logs are invalid for S01E11 capture selection. QC remains 13 pass, 1 warning, 0 fail. Do not claim selection rationale or full retime verification. |
| S01E12 | Existing pre-upscale output is present (4,929,991,550 bytes); `S01E12-qc-report.md` records 14 pass, 0 warn, 0 skip, 0 fail, and authoring execution-log section 55 records encode/remux/QC. | The V16 release did not independently replay or audit the referenced detailed stage logs; those log files were not found under the checked `Z:\work\logs` path. | Do not describe this V16 implementation/test run as a media revalidation. Preserve the output and QC report; treat section 55/report as the existing delivery record, not newly generated evidence. |
| S01E13 | Reconstructed retime revalidation and QC evidence retained. | Recovered EFM logs identify S01E10 and duplicate S01E11-folder bytes. The S01E13 download folder was deleted; authentic reports are unavailable in the reconstruction. | P3's surviving filename/QC are not authentic EFM comparison evidence. QC remains 14 pass, 0 warning, 0 fail. Do not claim the capture comparison was verified. |
| S01E15 | Latest render attempt evidence retained. | No completion evidence; the cause of the observed Orc hang is unproven. | Progress heartbeat is advisory only; do not declare completion or automatically kill/retry. |

For additional source details, consult the preserved S01E11 and S01E13 quarantine notes under
their device `episodes/<episode>/logs/` folders. Those notes are not changed by this app release.
