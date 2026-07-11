#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
APP_DIR="$ROOT_DIR/app"

if [ ! -f "$APP_DIR/run.sh" ]; then
  printf 'PROJECT凌 核心缺失：%s\n' "$APP_DIR/run.sh" >&2
  exit 1
fi

projectling_migrate_file_if_missing() {
  local source="$1"
  local target="$2"
  [ -f "$source" ] || return 0
  [ ! -e "$target" ] || return 0
  mkdir -p "$(dirname -- "$target")"
  cp -a "$source" "$target"
}

projectling_migrate_dir_if_missing() {
  local source="$1"
  local target="$2"
  [ -d "$source" ] || return 0
  [ ! -e "$target" ] || return 0
  mkdir -p "$(dirname -- "$target")"
  cp -a "$source" "$target"
}

projectling_migrate_legacy_state() {
  local relative=""
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
    projectling_migrate_file_if_missing "$ROOT_DIR/$relative" "$APP_DIR/$relative"
  done
  projectling_migrate_dir_if_missing "$ROOT_DIR/context/persona" "$APP_DIR/context/persona"
  projectling_migrate_dir_if_missing "$ROOT_DIR/memory" "$APP_DIR/memory"
}

projectling_migrate_legacy_state

export PROJECTLING_HOME="$APP_DIR"
export AITERMUX_HOME="${AITERMUX_HOME:-$(CDPATH= cd -- "$ROOT_DIR/.." && pwd)}"
export AITERMUX_AIDEBUG_DIR="${AITERMUX_AIDEBUG_DIR:-$ROOT_DIR/aidebug}"

if [ "${1:-}" = "--compat-migrate-only" ]; then
  exit 0
fi

exec bash "$APP_DIR/run.sh" "$@"
