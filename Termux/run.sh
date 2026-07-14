#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
CANONICAL_RUN="$SCRIPT_DIR/../../Termux/run.sh"
if [ -f "$SCRIPT_DIR/../../core.py" ] && [ -f "$CANONICAL_RUN" ]; then
  exec bash "$CANONICAL_RUN" "$@"
fi
if [ -f "$SCRIPT_DIR/app/core.py" ]; then
  RELEASE_ROOT="$SCRIPT_DIR"
elif [ -f "$SCRIPT_DIR/../app/core.py" ]; then
  RELEASE_ROOT="$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)"
else
  echo "[PROJECT凌] 找不到 app/core.py，请保持发行目录结构不变。" >&2
  exit 2
fi

export AITERMUX_HOME="${AITERMUX_HOME:-$RELEASE_ROOT}"
export PROJECTLING_HOME="$RELEASE_ROOT/app"
export PROJECTLING_RUNNER="$PROJECTLING_HOME/run.sh"
export AITERMUX_AIDEBUG_DIR="$PROJECTLING_HOME/aidebug"
export LANG="${LANG:-C.UTF-8}"
export LC_ALL="${LC_ALL:-C.UTF-8}"

exec bash "$PROJECTLING_RUNNER" "$@"
