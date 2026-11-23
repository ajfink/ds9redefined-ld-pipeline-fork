#!/bin/sh
# FromStunToKill launcher for Linux (and macOS from a shell).  ./FromStunToKill.sh [setup|check|cli ...]
# If the file is not executable (some shares drop the flag), run it as:  sh FromStunToKill.sh
exec sh "$(dirname "$0")/tools/launch.sh" "$@"
