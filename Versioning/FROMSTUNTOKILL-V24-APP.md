# FromStunToKill app branch - V24 dependency update progress

V24 extends the V23 dependency updater with live installation feedback. The update dialog now
streams installer output into its bottom message area while selected updates run. Each selected
dependency/tool displays **Queued...**, **Updating...**, then **Updated!**, **Failed**, or
**Stopped**. Unselected rows remain unchanged.

Closing the popup (Close or the window X) while an installer is in progress presents three
choices: **Stop updates and close**, **Continue updates and close**, or **Continue updates and
stay**. Stopping terminates the active installer process tree and prevents queued updates from
starting. Continuing in the background keeps the main workflow controls disabled until the
installer sequence finishes. During a version check or before the first installer starts, closing
the popup is immediate and cancels pending installations; after the update run completes, closing
is immediate as well.

Validation: **432 passed, 7 skipped** (`python -m unittest discover -s tests -q`), plus compileall,
warnings-as-errors imports, generated-runbook check, PowerShell parse check for `build_map.ps1`,
and `git diff --check`. No GUI display or real package update was run. No dependency/tool
installation or media processing was performed for this change. Production `main` and the W
shared bundle remain V15.
