if [[ -n "${ZSH_VERSION:-}" ]]; then
  typeset _projectling_bridge_source="${(%):-%N}"
  typeset _projectling_bridge_root="${_projectling_bridge_source:A:h}"

  export PROJECTLING_HOME="$_projectling_bridge_root/app"
  export AITERMUX_HOME="${AITERMUX_HOME:-${_projectling_bridge_root:h}}"
  export AITERMUX_AIDEBUG_DIR="${AITERMUX_AIDEBUG_DIR:-$_projectling_bridge_root/aidebug}"

  if [[ -x "$_projectling_bridge_root/run.sh" ]]; then
    bash "$_projectling_bridge_root/run.sh" --compat-migrate-only >/dev/null 2>&1 || true
  fi

  if [[ -f "$PROJECTLING_HOME/projectling.zsh" ]]; then
    source "$PROJECTLING_HOME/projectling.zsh"
  else
    print -u2 -- "PROJECT凌 核心缺失：$PROJECTLING_HOME/projectling.zsh"
  fi

  unset _projectling_bridge_source _projectling_bridge_root
fi
