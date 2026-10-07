#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
BACKUP_DIR="$PROJECT_DIR/backups/daily"
TODAY="$(date +%Y%m%d)"

mkdir -p "$BACKUP_DIR"
"$PROJECT_DIR/financer" backup --output "$BACKUP_DIR/financer-$TODAY.sqlite3"

# Prune only automated snapshots; manual backups remain untouched.
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'financer-[0-9]*.sqlite3' -mtime +30 -delete
