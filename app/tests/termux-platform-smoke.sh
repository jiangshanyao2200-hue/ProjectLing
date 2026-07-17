#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

ROOT="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"

if [ -z "${PREFIX:-}" ] || ! printf '%s' "$PREFIX" | grep -q '^/data/data/com.termux/'; then
  echo "termux_platform_smoke=skip reason=not-termux"
  exit 0
fi

bash -n "$ROOT/run.sh" "$ROOT/Termux/install.sh" "$ROOT/Termux/run.sh" "$ROOT/aidebug/bin/aidebug"
zsh -n "$ROOT/projectling.zsh"
bash "$ROOT/Termux/install.sh" --check --no-zshrc
bash "$ROOT/tests/termux-install-bridge-smoke.sh"
python3 "$ROOT/tests/termux-readiness-smoke.py"
python3 "$ROOT/tests/windows-launcher-source-smoke.py"
python3 "$ROOT/tests/aidebug-matrix-contract-smoke.py"
bash "$ROOT/aidebug/bin/aidebug" verify-termux --profile local

echo "termux_platform_smoke=ok"
