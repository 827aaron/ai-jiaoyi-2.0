#!/bin/bash
# Daily snapshot of the decision log and module status into data/backups/<date>/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DEST="data/backups/$(date +%Y-%m-%d)"
mkdir -p "$DEST"

paths=()
for p in data/decision_log data/module_status.json data/positions.json; do
  [ -e "$p" ] && paths+=("$p")
done
[ "${#paths[@]}" -gt 0 ] || { echo "nothing to back up"; exit 0; }

tar -czf "$DEST/data-$(date +%H%M%S).tar.gz" "${paths[@]}"

# Keep 30 daily folders.
ls -1d data/backups/*/ 2>/dev/null | sort -r | tail -n +31 | xargs -I{} rm -rf {}
