#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
export LANG="${LANG:-C.UTF-8}"
export LC_ALL="${LC_ALL:-C.UTF-8}"

resolve_layout() {
  local candidate root
  for candidate in "$SCRIPT_DIR" "$SCRIPT_DIR/.." "$SCRIPT_DIR/../.."; do
    root="$(CDPATH='' cd -- "$candidate" 2>/dev/null && pwd)" || continue
    if [ -f "$root/app/core.py" ] && [ -f "$root/app/run.sh" ]; then
      RELEASE_ROOT="$root"
      APP_DIR="$root/app"
      return 0
    fi
    if [ -f "$root/core.py" ] && [ -f "$root/run.sh" ]; then
      RELEASE_ROOT="$root"
      APP_DIR="$root"
      return 0
    fi
  done
  return 1
}

if ! resolve_layout; then
  echo "[ProjectLing] 找不到 core.py/run.sh；请保持仓库或发行目录结构完整。" >&2
  exit 2
fi

if [ -d "$RELEASE_ROOT/aidebug/runner" ]; then
  AIDEBUG_DIR="$RELEASE_ROOT/aidebug"
else
  AIDEBUG_DIR="$APP_DIR/aidebug"
fi
ROOT_RUN="$RELEASE_ROOT/run.sh"
ROOT_ZSH="$RELEASE_ROOT/projectling.zsh"
if [ ! -f "$ROOT_ZSH" ]; then
  ROOT_ZSH="$APP_DIR/projectling.zsh"
fi

CHECK_ONLY=0
NO_ZSHRC=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --no-zshrc) NO_ZSHRC=1 ;;
    --help|-h)
      cat <<'EOF'
ProjectLing Termux installer

Usage:
  bash Termux/install.sh [--check] [--no-zshrc]

Options:
  --check       Validate the real Termux runtime without changing files.
  --no-zshrc    Do not install the standalone zsh bridge.
EOF
      exit 0
      ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

missing=""
for command_name in bash python zsh tmux; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    missing="$missing $command_name"
  fi
done
if [ "${PREFIX:-}" != "" ] && printf '%s' "$PREFIX" | grep -q '^/data/data/com.termux/'; then
  if ! command -v am >/dev/null 2>&1 && [ ! -x /system/bin/am ]; then
    missing="$missing am"
  fi
fi
if [ -n "$missing" ]; then
  echo "[ProjectLing] 缺少命令:$missing" >&2
  echo "请先执行：pkg install python zsh tmux" >&2
  exit 1
fi

test -f "$APP_DIR/core.py"
test -f "$APP_DIR/projectling.py"
test -f "$APP_DIR/tooling.py"
test -f "$APP_DIR/projectling.zsh"
test -x "$APP_DIR/run.sh" || test -f "$APP_DIR/run.sh"
test -f "$AIDEBUG_DIR/runner/aidebug_health.py"
test -f "$AIDEBUG_DIR/runner/projectling_auto.py"
test -f "$AIDEBUG_DIR/runner/motd_zshrc_smoke.py"
test -f "$APP_DIR/release/app-files.txt"
test -f "$APP_DIR/tests/aidebug-matrix-contract-smoke.py"
test -f "$APP_DIR/tests/release-package-smoke.py"
test -f "$APP_DIR/tests/termux-install-bridge-smoke.sh"
test -f "$APP_DIR/tests/termux-platform-smoke.sh"
test -f "$APP_DIR/tests/termux-readiness-smoke.py"
test -f "$APP_DIR/tests/windows-launcher-source-smoke.py"

python -B - "$APP_DIR" "$AIDEBUG_DIR" <<'PY'
from pathlib import Path
import sys

app = Path(sys.argv[1])
aidebug = Path(sys.argv[2])
files = [
    app / "__init__.py",
    app / "core.py",
    app / "projectling.py",
    app / "tooling.py",
    aidebug / "runner" / "aidebug_health.py",
    aidebug / "runner" / "motd_zshrc_smoke.py",
    aidebug / "runner" / "projectling_auto.py",
    aidebug / "runner" / "relay_model_matrix.py",
    aidebug / "runner" / "runtime_state_guard.py",
    aidebug / "runner" / "termux_verify.py",
    app / "tests" / "aidebug-matrix-contract-smoke.py",
    app / "tests" / "release-package-smoke.py",
    app / "tests" / "termux-readiness-smoke.py",
    app / "tests" / "windows-launcher-source-smoke.py",
]
for path in files:
    compile(path.read_bytes(), str(path), "exec")
print(f"[ProjectLing] Python syntax ok: {len(files)} files")
PY

bash -n "$APP_DIR/run.sh"
bash -n "$ROOT_RUN"
bash -n "$SCRIPT_DIR/run.sh"
bash -n "$APP_DIR/tests/termux-install-bridge-smoke.sh"
bash -n "$APP_DIR/tests/termux-platform-smoke.sh"
zsh -n "$APP_DIR/projectling.zsh"
zsh -n "$ROOT_ZSH"

printf '[ProjectLing] layout=%s app=%s aidebug=%s\n' "$RELEASE_ROOT" "$APP_DIR" "$AIDEBUG_DIR"
echo "[ProjectLing] Python、Bash、Zsh、tmux、Android 命令和 AIDEBUG 检查通过。"
if [ "$CHECK_ONLY" -eq 1 ]; then
  exit 0
fi

chmod u+x "$APP_DIR/run.sh" "$ROOT_RUN" "$SCRIPT_DIR/run.sh" "$AIDEBUG_DIR/bin/aidebug" 2>/dev/null || true

# Unified public releases keep compatibility wrappers at the release root.
# Migrate legacy flat state before creating a fresh app/config/env, otherwise
# the default file would mask the user's previous Settings on the first launch.
if grep -Fq -- '--compat-migrate-only' "$ROOT_RUN" 2>/dev/null; then
  bash "$ROOT_RUN" --compat-migrate-only
fi

mkdir -p "$APP_DIR/config" "$AIDEBUG_DIR/logs" "$AIDEBUG_DIR/notes" "$AIDEBUG_DIR/state" "$AIDEBUG_DIR/tmp"
if [ ! -f "$APP_DIR/config/env" ]; then
  umask 077
  if [ -f "$APP_DIR/config/example/env" ]; then
    cp "$APP_DIR/config/example/env" "$APP_DIR/config/env"
  else
    : >"$APP_DIR/config/env"
  fi
  chmod 600 "$APP_DIR/config/env" 2>/dev/null || true
  echo "[ProjectLing] 已创建本机 config/env。"
fi

install_zsh_bridge() {
  local zshrc_path="$1"
  local marker_begin="# >>> PROJECTLING ZSH BRIDGE >>>"
  local marker_end="# <<< PROJECTLING ZSH BRIDGE <<<"
  local legacy_termux="# PROJECTLING_TERMUX_ENTRY"
  local legacy_release="# PROJECTLING_RELEASE_ENTRY"
  local tmp_path="${zshrc_path}.projectling.$$.$RANDOM"
  local new_path="${tmp_path}.new"
  local original_mode=""

  if ! awk \
    -v marker_begin="$marker_begin" \
    -v marker_end="$marker_end" \
    -v legacy_termux="$legacy_termux" \
    -v legacy_release="$legacy_release" '
      function emit(line) {
        if (line ~ /^[[:space:]]*$/) {
          pending_blanks = pending_blanks line ORS
          return
        }
        if (pending_blanks != "") {
          printf "%s", pending_blanks
          pending_blanks = ""
        }
        print line
      }
      $0 == marker_begin {
        if (in_block) malformed = 1
        in_block = 1
        next
      }
      $0 == marker_end {
        if (!in_block) malformed = 1
        in_block = 0
        next
      }
      in_block { next }
      $0 == legacy_termux || $0 == legacy_release {
        in_legacy = 1
        next
      }
      in_legacy {
        if ($0 ~ /^export (AITERMUX_HOME|PROJECTLING_HOME|PROJECTLING_DIR|PROJECTLING_RUNNER|AITERMUX_AIDEBUG_DIR)=/) {
          next
        }
        if ($0 ~ /^source[[:space:]]+.*projectling[.]zsh.*$/) {
          in_legacy = 0
          next
        }
        in_legacy = 0
      }
      { emit($0) }
      END {
        if (in_block || malformed) exit 42
      }
    ' "$zshrc_path" >"$tmp_path"; then
    rm -f "$tmp_path" "$new_path"
    echo "[ProjectLing] $zshrc_path 中的 ProjectLing bridge 标记不完整；未改写文件。" >&2
    return 1
  fi

  {
    cat "$tmp_path"
    [ ! -s "$tmp_path" ] || printf '\n'
    printf '%s\n' "$marker_begin"
    printf 'export PROJECTLING_HOME=%q\n' "$APP_DIR"
    printf 'export PROJECTLING_DIR=%q\n' "$APP_DIR"
    printf 'export PROJECTLING_RUNNER=%q\n' "$ROOT_RUN"
    printf 'export AITERMUX_AIDEBUG_DIR=%q\n' "$AIDEBUG_DIR"
    printf 'source %q\n' "$ROOT_ZSH"
    printf '%s\n' "$marker_end"
  } >"$new_path"

  if cmp -s "$new_path" "$zshrc_path"; then
    rm -f "$tmp_path" "$new_path"
    echo "[ProjectLing] zsh 接入已是最新。"
    return 0
  fi

  original_mode="$(stat -c '%a' "$zshrc_path" 2>/dev/null || true)"
  if [ -n "$original_mode" ]; then
    chmod "$original_mode" "$new_path" 2>/dev/null || true
  fi
  mv -f "$new_path" "$zshrc_path"
  rm -f "$tmp_path"
  echo "[ProjectLing] 已更新 $zshrc_path。"
}

if [ "$NO_ZSHRC" -eq 0 ]; then
  ZSHRC_PATH="${ZDOTDIR:-$HOME}/.zshrc"
  mkdir -p "$(dirname -- "$ZSHRC_PATH")"
  [ -f "$ZSHRC_PATH" ] || : >"$ZSHRC_PATH"
  install_zsh_bridge "$ZSHRC_PATH"
fi

echo "[ProjectLing] Termux 初始化完成。"
echo "启动：bash \"$SCRIPT_DIR/run.sh\""
