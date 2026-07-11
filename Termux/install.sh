#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/app/core.py" ]; then
  RELEASE_ROOT="$SCRIPT_DIR"
elif [ -f "$SCRIPT_DIR/../app/core.py" ]; then
  RELEASE_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
else
  echo "[PROJECT凌] 找不到 app/core.py，请保持发行目录结构不变。" >&2
  exit 2
fi

APP_DIR="$RELEASE_ROOT/app"
export LANG="${LANG:-C.UTF-8}"
export LC_ALL="${LC_ALL:-C.UTF-8}"
CHECK_ONLY=0
NO_ZSHRC=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --no-zshrc) NO_ZSHRC=1 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

missing=""
for command_name in python zsh tmux; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    missing="$missing $command_name"
  fi
done
if [ -n "$missing" ]; then
  echo "[PROJECT凌] 缺少命令:$missing" >&2
  echo "请先执行：pkg install python zsh tmux" >&2
  exit 1
fi

python -m py_compile "$APP_DIR/core.py" "$APP_DIR/projectling.py" "$APP_DIR/tooling.py"
echo "[PROJECT凌] Python、zsh、tmux 与核心文件检查通过。"
if [ "$CHECK_ONLY" -eq 1 ]; then
  exit 0
fi

chmod +x "$APP_DIR/run.sh" "$APP_DIR/aidebug/bin/aidebug" "$SCRIPT_DIR/run.sh" 2>/dev/null || true
mkdir -p "$APP_DIR/config"
if [ ! -f "$APP_DIR/config/env" ]; then
  cp "$APP_DIR/config/example/env" "$APP_DIR/config/env"
  chmod 600 "$APP_DIR/config/env" 2>/dev/null || true
  echo "[PROJECT凌] 已创建 app/config/env。"
fi

if [ "$NO_ZSHRC" -eq 0 ]; then
  ZSHRC_PATH="${ZDOTDIR:-$HOME}/.zshrc"
  MARKER="# PROJECTLING_RELEASE_ENTRY"
  if ! grep -Fq "$MARKER" "$ZSHRC_PATH" 2>/dev/null; then
    {
      printf '\n%s\n' "$MARKER"
      printf 'export AITERMUX_HOME=%q\n' "$RELEASE_ROOT"
      printf 'export PROJECTLING_HOME=%q\n' "$APP_DIR"
      printf 'export PROJECTLING_RUNNER=%q\n' "$APP_DIR/run.sh"
      printf 'export AITERMUX_AIDEBUG_DIR=%q\n' "$APP_DIR/aidebug"
      printf 'source %q\n' "$APP_DIR/projectling.zsh"
    } >>"$ZSHRC_PATH"
    echo "[PROJECT凌] 已写入 $ZSHRC_PATH。"
  else
    echo "[PROJECT凌] zsh 接入已存在。"
  fi
fi

echo "[PROJECT凌] 初始化完成。"
echo "启动：bash \"$SCRIPT_DIR/run.sh\""
