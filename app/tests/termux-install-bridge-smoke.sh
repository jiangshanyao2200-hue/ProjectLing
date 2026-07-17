#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

ROOT="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
CASE_ROOT="$(mktemp -d "${TMPDIR:-/data/data/com.termux/files/usr/tmp}/projectling-install-bridge.XXXXXX")"
trap 'rm -rf "$CASE_ROOT"' EXIT

assert_line() {
  local path="$1"
  local expected="$2"
  grep -Fqx -- "$expected" "$path"
}

assert_single_bridge() {
  local path="$1"
  test "$(grep -Fxc -- '# >>> PROJECTLING ZSH BRIDGE >>>' "$path")" -eq 1
  test "$(grep -Fxc -- '# <<< PROJECTLING ZSH BRIDGE <<<' "$path")" -eq 1
  if grep -Fq -- '# PROJECTLING_TERMUX_ENTRY' "$path"; then
    return 1
  fi
  if grep -Fq -- '# PROJECTLING_RELEASE_ENTRY' "$path"; then
    return 1
  fi
}

shell_quote() {
  printf '%q' "$1"
}

# Legacy standalone blocks must be replaced without deleting adjacent user lines.
flat_home="$CASE_ROOT/flat-home"
mkdir -p "$flat_home"
{
  printf '# user-prelude\n'
  printf '# PROJECTLING_RELEASE_ENTRY\n'
  printf 'export AITERMUX_HOME=/old/projectling\n'
  printf 'export PROJECTLING_HOME=/old/projectling/app\n'
  printf 'export PROJECTLING_RUNNER=/old/projectling/app/run.sh\n'
  printf 'export AITERMUX_AIDEBUG_DIR=/old/projectling/app/aidebug\n'
  printf 'source /old/projectling/app/projectling.zsh\n'
  printf '# user-suffix\n'
} >"$flat_home/.zshrc"

HOME="$flat_home" ZDOTDIR="$flat_home" bash "$ROOT/Termux/install.sh" >/dev/null
assert_single_bridge "$flat_home/.zshrc"
assert_line "$flat_home/.zshrc" '# user-prelude'
assert_line "$flat_home/.zshrc" '# user-suffix'
assert_line "$flat_home/.zshrc" "export PROJECTLING_HOME=$(shell_quote "$ROOT")"
assert_line "$flat_home/.zshrc" "export PROJECTLING_DIR=$(shell_quote "$ROOT")"
assert_line "$flat_home/.zshrc" "export PROJECTLING_RUNNER=$(shell_quote "$ROOT/run.sh")"
assert_line "$flat_home/.zshrc" "source $(shell_quote "$ROOT/projectling.zsh")"
if grep -Fq -- '/old/projectling' "$flat_home/.zshrc"; then
  exit 1
fi

flat_hash_before="$(sha256sum "$flat_home/.zshrc" | awk '{print $1}')"
HOME="$flat_home" ZDOTDIR="$flat_home" bash "$ROOT/Termux/install.sh" >/dev/null
flat_hash_after="$(sha256sum "$flat_home/.zshrc" | awk '{print $1}')"
test "$flat_hash_before" = "$flat_hash_after"

# Build a compact unified public fixture. The compatibility root wrapper must
# migrate flat Settings before the installer creates app/config/env.
unified_root="$CASE_ROOT/unified-release"
mkdir -p \
  "$unified_root/app/aidebug/bin" \
  "$unified_root/app/aidebug/runner" \
  "$unified_root/app/config/example" \
  "$unified_root/app/release" \
  "$unified_root/app/tests" \
  "$unified_root/Termux" \
  "$unified_root/config"

for relative in __init__.py core.py projectling.py tooling.py projectling.zsh run.sh; do
  cp -a "$ROOT/$relative" "$unified_root/app/$relative"
done
for relative in \
  aidebug_health.py \
  motd_zshrc_smoke.py \
  projectling_auto.py \
  relay_model_matrix.py \
  runtime_state_guard.py \
  termux_verify.py; do
  cp -a "$ROOT/aidebug/runner/$relative" "$unified_root/app/aidebug/runner/$relative"
done
cp -a "$ROOT/aidebug/bin/aidebug" "$unified_root/app/aidebug/bin/aidebug"
cp -a "$ROOT/config/example/env" "$unified_root/app/config/example/env"
cp -a "$ROOT/release/app-files.txt" "$unified_root/app/release/app-files.txt"
for relative in \
  aidebug-matrix-contract-smoke.py \
  release-package-smoke.py \
  termux-install-bridge-smoke.sh \
  termux-platform-smoke.sh \
  termux-readiness-smoke.py \
  windows-launcher-source-smoke.py; do
  cp -a "$ROOT/tests/$relative" "$unified_root/app/tests/$relative"
done
cp -a "$ROOT/release/combined/run.sh" "$unified_root/run.sh"
cp -a "$ROOT/release/combined/projectling.zsh" "$unified_root/projectling.zsh"
cp -a "$ROOT/release/termux/install.sh" "$unified_root/Termux/install.sh"
cp -a "$ROOT/release/termux/run.sh" "$unified_root/Termux/run.sh"
chmod u+x \
  "$unified_root/app/run.sh" \
  "$unified_root/app/aidebug/bin/aidebug" \
  "$unified_root/run.sh" \
  "$unified_root/Termux/install.sh" \
  "$unified_root/Termux/run.sh"
printf 'PROJECTLING_TEST_MARKER=legacy-flat\n' >"$unified_root/config/env"

unified_home="$CASE_ROOT/unified-home"
mkdir -p "$unified_home"
{
  printf '# unified-user-prelude\n'
  printf '# >>> PROJECTLING ZSH BRIDGE >>>\n'
  printf 'export PROJECTLING_HOME=/stale/app\n'
  printf 'export PROJECTLING_DIR=/stale/app\n'
  printf 'export PROJECTLING_RUNNER=/stale/run.sh\n'
  printf 'export AITERMUX_AIDEBUG_DIR=/stale/aidebug\n'
  printf 'source /stale/projectling.zsh\n'
  printf '# <<< PROJECTLING ZSH BRIDGE <<<\n'
  printf '# unified-user-suffix\n'
} >"$unified_home/.zshrc"

HOME="$unified_home" ZDOTDIR="$unified_home" bash "$unified_root/Termux/install.sh" >/dev/null
grep -Fqx -- 'PROJECTLING_TEST_MARKER=legacy-flat' "$unified_root/app/config/env"
assert_single_bridge "$unified_home/.zshrc"
assert_line "$unified_home/.zshrc" '# unified-user-prelude'
assert_line "$unified_home/.zshrc" '# unified-user-suffix'
assert_line "$unified_home/.zshrc" "export PROJECTLING_HOME=$(shell_quote "$unified_root/app")"
assert_line "$unified_home/.zshrc" "export PROJECTLING_DIR=$(shell_quote "$unified_root/app")"
assert_line "$unified_home/.zshrc" "export PROJECTLING_RUNNER=$(shell_quote "$unified_root/run.sh")"
assert_line "$unified_home/.zshrc" "source $(shell_quote "$unified_root/projectling.zsh")"
if grep -Fq -- '/stale/' "$unified_home/.zshrc"; then
  exit 1
fi

# The installed aidebug entry is a symlink. It must resolve its real script
# location instead of treating ~/.local/bin as the ProjectLing root.
aidebug_link="$unified_home/.local/bin/aidebug"
mkdir -p "$(dirname "$aidebug_link")"
ln -s "$unified_root/app/aidebug/bin/aidebug" "$aidebug_link"
test -L "$aidebug_link"
aidebug_status="$({
  env -u AITERMUX_HOME -u AITERMUX_AIDEBUG_DIR -u PROJECTLING_DIR \
    HOME="$unified_home" "$aidebug_link" status
} 2>&1)"
grep -Fqx -- "aidebug_dir=$unified_root/app/aidebug" <<<"$aidebug_status"
grep -Fqx -- "logs_dir=$unified_root/app/aidebug/logs" <<<"$aidebug_status"

# Reinstalling from a moved path must rewrite the managed block exactly once.
moved_root="$CASE_ROOT/moved release"
cp -a "$unified_root" "$moved_root"
HOME="$unified_home" ZDOTDIR="$unified_home" bash "$moved_root/Termux/install.sh" >/dev/null
assert_single_bridge "$unified_home/.zshrc"
assert_line "$unified_home/.zshrc" "export PROJECTLING_HOME=$(shell_quote "$moved_root/app")"
assert_line "$unified_home/.zshrc" "export PROJECTLING_RUNNER=$(shell_quote "$moved_root/run.sh")"
assert_line "$unified_home/.zshrc" "source $(shell_quote "$moved_root/projectling.zsh")"
if grep -Fq -- "$unified_root/app" "$unified_home/.zshrc"; then
  exit 1
fi

moved_hash_before="$(sha256sum "$unified_home/.zshrc" | awk '{print $1}')"
HOME="$unified_home" ZDOTDIR="$unified_home" bash "$moved_root/Termux/install.sh" >/dev/null
moved_hash_after="$(sha256sum "$unified_home/.zshrc" | awk '{print $1}')"
test "$moved_hash_before" = "$moved_hash_after"

printf 'termux_install_bridge_smoke=ok\n'
