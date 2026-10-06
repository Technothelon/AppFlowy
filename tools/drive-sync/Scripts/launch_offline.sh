#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
set -euo pipefail
scripts_directory=$(cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$scripts_directory/launch_offline.py"
