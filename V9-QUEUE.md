# V9 planning queue

**Historical planning snapshot: V9. Current release is V10.** This file preserves the V9
device-log review and its inherited carry-overs; it is not workflow instructions or
authorization to change a gate, profile, active episode, or device data. Accepting a core
item requires implementation, validation, a workflow-version increment, release notes, and
the normal publication record. V9 work must not weaken existing quality, space, or ACL
gates.

## New proposals from device-log review

### 1. Make long Orc reference selection observable and resumable

**Evidence:** `W:\ld-pipeline\devices\LENOVOFLEX-530F8C94\episodes\S01E09\logs\S01E09-job-log.md`
records roughly 67 minutes per six-capture refresh. A harness timeout killed a later pass
after about 52 minutes; buffered output left the run log empty, making a healthy refresh
hard to distinguish from a stall. The operator's no-short-timeout/unbuffered-output practice
is already documented, but the selector does not checkpoint completed passes.

**Proposal:** add flushed live progress/heartbeats, an interruption record, and durable
per-pass checkpoints to `select_stack_reference.py`. Resume only when workflow version,
input order, source/project identities, and hashes match the checkpoint; otherwise refuse
resume and start a new uniquely named run. Never promote a partial project or silently
reuse a verdict for changed inputs.

**Acceptance:** tests interrupt between and during passes; a valid resume skips only
completed passes and reaches the same stability verdict as a clean run; mismatched inputs
are rejected; no partial project/manifest promotion occurs; logs show progress and the
resume point. Validate on a real long refresh before claiming the timeout/recovery problem
solved.

### 2. Make free-space decisions robust to inconsistent APFS readings

**Evidence:** `W:\ld-pipeline\devices\MAC-OPENCODE-4DD0B985\episodes\S01E11\logs\S01E11-run-log.md`
records initial APFS readings of 655 GiB used / 218 GiB free, followed by stable readings
near 690.9 GiB free that matched a manual accounting check. The operator remeasured and
still used the downloader's normal space gate; no reserve was lowered.

**Proposal:** improve the space diagnostic to identify the actual filesystem/volume,
report source and timestamp for each reading, and detect implausible or changing samples.
When readings disagree, ask for a fresh measurement or stop with an actionable diagnostic;
do not choose the most favorable value, infer volume identity from path spelling, or bypass
the existing gate. Keep per-stage output estimates separate from the configured reserve.

**Acceptance:** tests cover stable, changing, unavailable, and aliased-volume readings on
supported platforms; inconsistent measurements cannot produce a false PASS. Back-test
against recorded runs without lowering any device's current configured reserve.

### 3. Collect real stage-duration data before optimizing the workflow

**Evidence:** the reviewed episode records contain useful timestamps but predate structured timing
or use manual/direct helper calls, so they do not provide comparable active-time-per-stage
data. V9's automatic reports and manual `stage_clock.py` exist, but their use on real runs
still needs to be demonstrated.

**Proposal:** use V9 timing on subsequent episodes: retain each automatic stage report and
use `stage_clock.py` for manual stages, pausing for human gate waits. Review completeness
and report sample counts and per-stage distributions (at least median and range); label
platforms and distinguish active, gate, and human-wait time. Do not infer active duration
from wall-clock gaps or older untimed logs.

**Acceptance:** demonstrate complete timing records on multiple real episode runs, including
at least one manual gate pause; verify wait time is excluded and outcomes are recorded.
Publish a small, privacy-safe summary with sample counts before proposing stage-order,
hardware, or concurrency optimizations. Missing timing evidence is reported as missing,
not zero.

## Carry-over audit from the historical V6 queue

The original `V6-QUEUE.md` was removed from the working tree after V7. Its latest pre-removal
version was recovered from Git history (`dc0e85f^:V6-QUEUE.md`) and compared with V8 code,
docs, release notes, and the device records.

| Historical item | V9 disposition |
|---|---|
| Multi-device workspaces and run-log sharing | **Partially superseded/integrated.** V7/V8 replaced the proposed sibling-folder layout with README-relative `devices/<instance>/episodes/<episode>/` workspaces, local fallback, per-workspace logs, and scoped application write paths. **Still open:** administrator ACL enforcement (W release root is currently writable by the connected account), a same-episode single-writer/claim policy, and atomic publication/immutability for reviewed log packages. Do not treat concurrent logs as safe to publish atomically until tested. |
| Optional DVD-free audio route | **Integrated in V6.** Keep explicit and optional; no queue work identified. |
| Root `changelog.txt` | **Integrated in V6** and maintained in later releases. |
| Conditional post-download free-space reserve (35 GiB same-volume proposal) | **Still deferred.** No cross-device back-test establishes the proposed conditional floor. Existing stage checks preserve configured reserves; the V6 proposal must not be applied by assumption. Reconcile the recorded 25 GiB LenovoFlex versus 80 GiB authoring-profile settings separately, without overwriting machine profiles or lowering a reserve. |
| Linux x86_64 support | **Still untested/unsupported for production.** V7 added a dependency/environment candidate and doctor checks only, not end-to-end support. |
| Low-/no-cost model design goal | **Integrated in V6** as a design target, not a performance or reliability guarantee. |
| Remove hard-coded FFmpeg path | **Integrated in V6.** Profile-based resolution and documentation are present. |
| Hardware-aware `heavy_jobs` policy | **Still deferred.** Profile values/documentation exist, but the V6 plan explicitly required an execution-enforced concurrency policy and hardware-tier benchmarks. Do not claim the setting is a scheduler or start concurrent heavy jobs on that basis. |
| Linux ARM64 and Windows ARM64 | **Still untested/unsupported.** The V6 queue's research plans do not constitute platform validation. |
| Fresh macOS setup/toolchain | **Porting work integrated; fresh-machine end-to-end validation still open.** M5 S01E10 evidence does not establish a fresh-Mac installation and delivery pass. |

The old sibling-device-folder proposal is superseded by the README-relative workspace design;
its remaining ACL, concurrency, and publication requirements are retained above rather than
restoring the old layout. V6 items not listed as open above were confirmed integrated or
superseded and are not carried forward as new work.

## Dependencies and guardrails

- Administrator-owned W ACL enforcement is an external prerequisite for shared processing;
  application code must not try to change ACLs.
- Keep Linux/ARM64 support claims explicitly unvalidated until real-platform end-to-end
  acceptance evidence exists.
- Preserve device-local profiles, episode manifests, logs, and media during all queue work.
- The V9 timing-data proposal is evidence gathering, not permission to change stage order or
  concurrency before the data is reviewed.
