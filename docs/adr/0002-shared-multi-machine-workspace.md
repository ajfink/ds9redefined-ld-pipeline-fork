# ADR 0002 — Shared multi-machine workspace (Legion, Flex, Mac)

Date: 2026-11-21 · Status: proposed (built while the owner reviews; defaults chosen are listed
under "Decisions to confirm")

## Context
v38 keeps one folder per device inside the shared release tree
(`<release>/devices/<DEVICE>-<8 hex>/episodes/<code>/`), a machine-local identity file that pins
the device folder, and one rule: **a device writes only inside its own folder**. Large media stays
on each machine's own disks ("do not process large captures from SMB"). Machines share only
explicit publications (`publish-reference`, `publish-cut-pad`). There is no way to move an
episode from one machine to another; the S01E11/S01E13 audit shows what happens instead (another
episode's EFM logs ended up in a reconstructed workspace).

fstk keeps device state in a machine-local `data_root` (ADR 0001 §5): the journaled manifest store
relies on file locks and atomic renames, which are not reliable across Windows and macOS clients
on one SMB share.

## Decision
1. **State stays local; the share carries publications.** `profile.shared_root` (any folder all
   machines can reach, e.g. `W:\fstk-shared`, `/Volumes/fstk-shared`; never the code tree, never
   `data_root`) holds `devices/<DEVICE>-<ID>/`. Each device writes only its own folder: device
   marker, and per episode a `status.json` plus immutable content-addressed snapshots of the
   small state (manifest, journal, approvals, reports, references). Peers are read-only.
2. **Stable device identity**, v38-style: an 8-hex id kept machine-locally next to the profile
   (`device.json`), mirrored by a marker in the shared device folder. A folder whose marker names
   another machine or id is refused, never reused.
3. **Portable paths.** Snapshots replace machine paths with tokens: `{data_root}`, `{media_root}`,
   `{root:<name>}` for media-root overrides, `{source:<name>}` for source roots, and
   `{volume:<name>}` for named volumes declared in each profile (`volumes`), e.g.
   `"flex-e-hdd": "\\\\LENOVOFLEX\\e hdd"` on Windows and `"/Volumes/e hdd"` on the Mac. Whole-path
   string values in the manifest, journal and approvals are tokenised; a receiving device expands
   them with its own profile; tokens it cannot expand are listed. Reports and references travel
   byte for byte so approvals bound to them keep matching (the share is private to the owner's
   machines; process logs do not travel).
4. **Explicit handoff.** `handoff` freezes the local workspace (the engine refuses to run it),
   publishes a snapshot plus a media inventory (logical root, name, size, head/tail hash) and names
   the receiving device. `adopt` on that device verifies every snapshot file's hash, expands the
   paths, creates a local workspace with a fresh journal whose first entry records the source
   device and manifest hash (the source journal is kept beside it verbatim), and reports which
   media artifacts are present, missing or different locally. `--fork` adopts from a peer that did
   not hand off (both stay active; both are visible). `reclaim` unfreezes a handed-off workspace
   only while no peer has adopted it.
5. **Visibility, not locks.** Every engine run publishes the episode's status (best effort; a
   failed publication never fails a job). The picker and `fstk share status` show which devices
   hold an episode and in what state. Creating an episode that a peer already has active is
   refused unless the operator chooses to adopt, fork or run in parallel.
6. **Machine-local resources stay local**: the space ledger, process logs, job state and media.
   Approvals travel (re-checked against local files after adoption; missing evidence makes them
   stale, never silently approved).

## Decisions to confirm
- Space ledger per machine (chosen) rather than one shared ledger.
- Job state does not travel; the receiving device plans the remaining stages itself.
- Snapshots are never deleted automatically (they are small); old ones accumulate per manifest hash.
- No automatic media transfer: adopt lists what to copy (sizes and hashes) or re-make.

## Consequences
- Each machine needs `shared_root` and, for shared capture disks, matching `volumes` names.
- v38's `devices/` tree is not touched; importing v38 device workspaces is separate work.
