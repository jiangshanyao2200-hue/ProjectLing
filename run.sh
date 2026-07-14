#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
APP_DIR="$ROOT_DIR/app"

if [ ! -f "$APP_DIR/run.sh" ] || [ ! -f "$APP_DIR/core.py" ]; then
  printf 'ProjectLing shared core missing: %s\n' "$APP_DIR" >&2
  exit 1
fi

migrate_file_if_missing() {
  local source="$1"
  local target="$2"
  [ -f "$source" ] || return 0
  [ ! -e "$target" ] || return 0
  mkdir -p "$(dirname -- "$target")"
  cp -a "$source" "$target"
}

migrate_dir_if_missing() {
  local source="$1"
  local target="$2"
  [ -d "$source" ] || return 0
  [ ! -e "$target" ] || return 0
  mkdir -p "$(dirname -- "$target")"
  cp -a "$source" "$target"
}

for relative in \
  config/env \
  config/role.json \
  config/focus.json \
  config/context-budget.json \
  config/update-plan.json \
  context/entries.jsonl \
  context/entries.jsonl.lock \
  context/shared_context.txt \
  context/.legacy-context-migrated-v1; do
  migrate_file_if_missing "$ROOT_DIR/$relative" "$APP_DIR/$relative"
done
migrate_dir_if_missing "$ROOT_DIR/context/persona" "$APP_DIR/context/persona"
migrate_dir_if_missing "$ROOT_DIR/memory" "$APP_DIR/memory"

export PROJECTLING_HOME="$APP_DIR"
export PROJECTLING_DIR="$APP_DIR"
export AITERMUX_HOME="${AITERMUX_HOME:-$(CDPATH='' cd -- "$ROOT_DIR/.." && pwd)}"
export AITERMUX_AIDEBUG_DIR="${AITERMUX_AIDEBUG_DIR:-$ROOT_DIR/aidebug}"

if [ "${1:-}" = "--compat-migrate-only" ]; then
  exit 0
fi

exec bash "$APP_DIR/run.sh" "$@"
