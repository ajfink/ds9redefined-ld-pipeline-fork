# ADR 0007 — Main window: one Run button, menus, a "needs you" banner, Progress as the run's home

Date: 2026-11-25 · Status: accepted (owner: "Implement them all", after the UI assessment in the project doc
`UI-ASSESSMENT.md`; DS9 palette supplied by the owner the same day)

## Context
The main window had grown fourteen equal-weight buttons in two bars, four lines repeating the run's status (one of
them naming the wrong step on S02E02), internal ids for steps and jobs, two developer tables as tabs (Manifest, Jobs),
an always-open log pane and empty states that still showed their controls.

## Decision
1. **Menus** File (Settings, Notifications, Quit) · Episode (Find or add, Choose DVD, EFM capture choice, Delivery A/V
   check, Hand off, Remove) · Run (intake, delivery, Pause/Resume, Cancel, a plan file — advanced) · Help (log, logs
   folder, debug bundle). Every command is one `QAction` shared by menus, buttons and right-click menus.
2. **One primary Run button** beside the episode title: it offers intake until intake has produced the stack reference,
   then delivery; its arrow offers the other and a plan file. A bell toggles desktop notifications.
3. **Progress tab** owns the run: one headline (most serious problem first: failed, blocked, interrupted, out of date),
   the overall bar with its figures beside it, Pause/Cancel shown only while running, a run picker (replaces the Jobs
   tab), steps by short name (id in the tooltip), one status vocabulary (`STATUS_WORDS`), a bar only on the running
   step, durations also for runs that ended before the app started, "not started" for steps a stopped run never began,
   row icons plus a right-click menu (restart, full message, copy, report, tool logs, opening pack) and double-click for
   the full message.
4. **Banner** — the one place that says what needs doing next, with buttons: review waiting → *Review now*; a step
   stopped the run → *Show details* and *Restart from here* (or *Run again*); the app was updated → *Restart the app*.
   Messages about what just happened use the same banner.
5. **Reviews tab** is the front door: count and colour only when something waits, one sentence when nothing does, and
   links to the EFM capture and delivery windows. **Details** replaces Manifest: grouped, searchable, empty fields
   hidden by default, readable names with the key in the tooltip, "Set by" with stage and time.
6. **Episode list** shows the title and where each episode stands; **log** is a one-line strip that opens on demand
   (remembered) or on a failure, with warnings/errors in colour and an "Only problems" filter.
7. **Look** (`fstk.ui.theme`): light base with the DS9 palette as accents — plum menu bar and headings, periwinkle
   progress fill, slate selection, maroon primary button, grey secondary text. Status colours stay semantic (green /
   red / amber) and follow a dark system theme (`fstk.ui.colours`).

## Consequences
- No function was removed: every former button is a menu action (and most also in a right-click menu); the Jobs table's
  information is the run picker; the Manifest is Details with "Show empty fields".
- Tests address actions and widgets by the same attribute names (`dvd_button`, `remove_button`, … are now actions).
