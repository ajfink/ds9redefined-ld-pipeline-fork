# The frozen v38 reference

Owner decision, 2026-11-21: v38 is frozen as fstk's behavioural reference.

| Item | Value |
|---|---|
| Repository | `Z:\ld-pipeline`, branch `fromstuntokill-v15-app` |
| Worktree | `Z:\fromstuntokill-v15-app` (`WORKFLOW_VERSION` = 38) |
| Commit | `035ecc6` — "Centre-referenced audio origin; fix Windows tail trim (findings 0001/0002)" |
| Working-tree snapshot | `.fstk-local/v38-now-20261121.tgz`, 5,183,947 bytes, sha256 `8a010d58b51ff636301bf9e7e91f6271ed0fc216f02703018dab0124719a7dac` |
| Device evidence | `Z:\ld-pipeline\devices\<DEVICE>\episodes\<EPISODE>` (read-only) |

What "reference" means:

- Characterization tests and real-evidence validators (`tools/validate_v38_*.py`) compare fstk
  with this commit's behaviour and with the device evidence it produced.
- A difference is either a **bug in fstk**, or a **deliberate improvement** that is recorded in a
  finding (`docs/findings/`) or ADR with the measured reason. Owner instruction: accepted v38
  values are not assumed to be ideal; where fstk can determine a better value automatically, it
  does, and the finding records how the two compare.
- `docs/v38-inventory.md` is generated from this tree with `tools/v38_inventory.py`.
