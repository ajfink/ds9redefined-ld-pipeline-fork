#!/bin/sh
# FromStunToKill launcher for macOS: double-click in Finder (opens Terminal), or run from a shell.
# If Finder says it cannot be opened because it is not executable, run:  sh FromStunToKill.command
# If macOS blocks it as "from an unidentified developer": right-click the file, choose Open once.
exec sh "$(dirname "$0")/tools/launch.sh" "$@"
