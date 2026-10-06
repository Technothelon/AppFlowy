#!/usr/bin/env bash
set -euo pipefail
# /tmp is bound to the isolated profile's Temp directory by the launcher.
# Use its short sandbox path to stay below the Unix socket pathname limit.
exec /usr/bin/dbus-daemon "$@" --address="unix:path=/tmp/appflowy-session-bus"
