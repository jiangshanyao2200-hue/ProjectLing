if [[ -n "${ZSH_VERSION:-}" ]]; then
  typeset -g PROJECTLING_HOME="${PROJECTLING_HOME:-${AITERMUX_HOME:-$HOME/AItermux}/projectling}"
  typeset -g PROJECTLING_RUNNER="${PROJECTLING_RUNNER:-$PROJECTLING_HOME/run.sh}"
  typeset -g PROJECTLING_ZSH_SOURCE="${${(%):-%N}:A}"
  typeset -g PROJECTLING_HOOK_VERSION="2026.07.15.3"
  typeset -g PROJECTLING_PENDING_COMMAND_FILE="${PROJECTLING_PENDING_COMMAND_FILE:-$PROJECTLING_HOME/config/pending-command.json}"
  typeset -g PROJECTLING_DISPATCH_KIND=""
  typeset -g PROJECTLING_TTY_DEV="/dev/tty"
  typeset -g PROJECTLING_MAX_INLINE_CHARS="${PROJECTLING_MAX_INLINE_CHARS:-4000}"
  if (( ! ${+PROJECTLING_EDIT_HISTORY} )); then
    typeset -ga PROJECTLING_EDIT_HISTORY=()
  fi
  typeset -g PROJECTLING_EDIT_BROWSE_POS=0
  typeset -g PROJECTLING_EDIT_DRAFT=""

  projectling_run_on_tty() {
    local rc=0
    local saved_stty=''
    [[ -x "$PROJECTLING_RUNNER" ]] || {
      print -u2 -- "projectling 未安装：$PROJECTLING_RUNNER"
      return 1
    }

    if [[ -t 0 || -t 1 ]] && : <"$PROJECTLING_TTY_DEV" >"$PROJECTLING_TTY_DEV" 2>/dev/null; then
      saved_stty="$(stty -g <"$PROJECTLING_TTY_DEV" 2>/dev/null || true)"
      if [[ -n "$saved_stty" ]]; then
        stty -echoctl intr '^[' <"$PROJECTLING_TTY_DEV" >/dev/null 2>&1 || true
      fi
      {
        printf '\033[5 q' >"$PROJECTLING_TTY_DEV" 2>/dev/null || true
        "$PROJECTLING_RUNNER" "$@" <"$PROJECTLING_TTY_DEV" >"$PROJECTLING_TTY_DEV"
        rc=$?
      } always {
        printf '\033[0 q' >"$PROJECTLING_TTY_DEV" 2>/dev/null || true
        if [[ -n "$saved_stty" ]]; then
          stty "$saved_stty" <"$PROJECTLING_TTY_DEV" >/dev/null 2>&1 || true
        fi
      }
      return $rc
    else
      "$PROJECTLING_RUNNER" "$@"
    fi
  }

  projectling_trim_text() {
    local text="${1:-}"
    text="${text#"${text%%[![:space:]]*}"}"
    text="${text%"${text##*[![:space:]]}"}"
    printf '%s' "$text"
  }

  projectling_settings() {
    projectling_run_local_command settings "${1:-root}"
  }

  projectling_status() {
    projectling_run_on_tty status "$@"
  }

  projectling_reload() {
    local source_path="${PROJECTLING_ZSH_SOURCE:-$PROJECTLING_HOME/projectling.zsh}"
    if [[ ! -f "$source_path" ]]; then
      source_path="$PROJECTLING_HOME/projectling.zsh"
    fi
    [[ -f "$source_path" ]] || {
      print -u2 -- "projectling hook 不存在：$source_path"
      return 1
    }
    source "$source_path" || return $?
    print -- "ProjectLing Zsh 已重载 · hook $PROJECTLING_HOOK_VERSION"
  }

  projectling_update() {
    local repo_dir="$PROJECTLING_HOME"
    local current_remote=''
    if [[ ! -d "$repo_dir/.git" && -d "${repo_dir:h}/.git" ]]; then
      repo_dir="${repo_dir:h}"
    fi
    if [[ -d "$repo_dir/.git" ]] && command -v git >/dev/null 2>&1; then
      current_remote="$(git -C "$repo_dir" remote get-url origin 2>/dev/null || true)"
      if [[ "${(L)current_remote}" == *projectling-private* ]]; then
        print -u2 -- "当前是 ProjectLing 私有开发工作树；Kit2 更新器固定跟随公开仓，本次已停止，未改写 remote 或工作树。"
        return 2
      fi
    fi
    if ! command -v aitermux-cli-install >/dev/null 2>&1; then
      print -u2 -- "未找到 aitermux-cli-install；请从 AITermux MOTD 设置页更新 ProjectLing。"
      return 1
    fi
    aitermux-cli-install update-projectling || return $?
    projectling_reload
  }

  projectling_run_local_command() {
    local kind="$1"
    local extra="${2:-}"
    case "$kind" in
      settings)
        local settings_tab
        settings_tab="$(projectling_trim_text "$extra")"
        settings_tab="${(L)settings_tab}"
        case "$settings_tab" in
          ''|root)
            projectling_run_on_tty shell-settings
            ;;
          api|main|main_api|main-api|planner|executor|executor_api|executor-api|support|gpt|codex|openai|deepseek|gemini|grok|xai|gemini_params|gemini-params|persona|role|system|settings|websearch|web_search)
            projectling_run_on_tty shell-settings --tab "$settings_tab"
            ;;
          web-search)
            projectling_run_on_tty shell-settings --tab websearch
            ;;
          *)
            print -u2 -- "未知 Settings 页面：$settings_tab（可用 main / executor / api / gpt / gemini / grok / deepseek / role / system / websearch）"
            return 2
            ;;
        esac
        ;;
      status)
        if [[ -n "$extra" ]]; then
          projectling_run_on_tty status ${(z)extra}
        else
          projectling_run_on_tty status
        fi
        ;;
      reload)
        projectling_reload
        ;;
      update)
        projectling_update
        ;;
      help)
        projectling_run_on_tty help
        ;;
      confirm)
        projectling_run_on_tty confirm-command "${extra:-y}"
        ;;
      deny)
        projectling_run_on_tty deny-command
        ;;
      codexurl)
        projectling_run_on_tty codexurl
        ;;
      models)
        if [[ -n "$extra" ]]; then
          projectling_run_on_tty list-models ${(z)extra}
        else
          projectling_run_on_tty list-models
        fi
        ;;
      api-test)
        if [[ -n "$extra" ]]; then
          projectling_run_on_tty api-test ${(z)extra}
        else
          projectling_run_on_tty api-test
        fi
        ;;
      model)
        local model_text
        model_text="$(projectling_trim_text "$extra")"
        model_text="${model_text%%[[:space:]]*}"
        if [[ -n "$model_text" ]]; then
          projectling_run_on_tty model "$model_text"
        else
          projectling_run_on_tty model
        fi
        ;;
      *)
        return 1
        ;;
    esac
  }

  projectling_has_pending_command() {
    [[ -x "$PROJECTLING_RUNNER" ]] || return 1
    [[ -f "$PROJECTLING_PENDING_COMMAND_FILE" ]] || return 1
    "$PROJECTLING_RUNNER" has-pending-command >/dev/null 2>&1
  }

  projectling_menu_residue_input() {
    local trimmed="${1:-}"
    trimmed="${trimmed#"${trimmed%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    case "$trimmed" in
      <->)
        return 0
        ;;
    esac
    return 1
  }

  projectling_special_command_kind() {
    local trimmed="${1:-}"
    local lowered=''

    trimmed="${trimmed#"${trimmed%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    lowered="${(L)trimmed}"

    case "$trimmed" in
      /settings\ *)
        printf '%s' "settings:${trimmed#/settings }"
        return 0
        ;;
      /settings)
        printf '%s' "settings"
        return 0
        ;;
      /role)
        printf '%s' "settings:role"
        return 0
        ;;
      /deepseek)
        printf '%s' "settings:deepseek"
        return 0
        ;;
      /gemini)
        printf '%s' "settings:gemini"
        return 0
        ;;
      /gpt|/codex|/openai)
        printf '%s' "settings:gpt"
        return 0
        ;;
      /grok|/xai)
        printf '%s' "settings:grok"
        return 0
        ;;
      /websearch|/web-search)
        printf '%s' "settings:websearch"
        return 0
        ;;
      /status\ *)
        printf '%s' "status:${trimmed#/status }"
        return 0
        ;;
      /status)
        printf '%s' "status"
        return 0
        ;;
      /reload|/projectling-reload)
        printf '%s' "reload"
        return 0
        ;;
      /update|/projectling-update)
        printf '%s' "update"
        return 0
        ;;
      /model\ *)
        printf '%s' "model:${trimmed#/model }"
        return 0
        ;;
      /mode\ *)
        local mode_extra="${trimmed#/mode }"
        mode_extra="${mode_extra#"${mode_extra%%[![:space:]]*}"}"
        if [[ -n "$mode_extra" ]]; then
          printf '%s' "model:${mode_extra}"
        else
          printf '%s' "model:next"
        fi
        return 0
        ;;
      /model)
        printf '%s' "model"
        return 0
        ;;
      /mode)
        printf '%s' "model:next"
        return 0
        ;;
      /models\ *)
        printf '%s' "models:${trimmed#/models }"
        return 0
        ;;
      /model-list\ *)
        printf '%s' "models:${trimmed#/model-list }"
        return 0
        ;;
      /list-models\ *)
        printf '%s' "models:${trimmed#/list-models }"
        return 0
        ;;
      /models|/model-list|/list-models)
        printf '%s' "models"
        return 0
        ;;
      /api-test\ *)
        printf '%s' "api-test:${trimmed#/api-test }"
        return 0
        ;;
      /apitest\ *)
        printf '%s' "api-test:${trimmed#/apitest }"
        return 0
        ;;
      /api-test|/apitest)
        printf '%s' "api-test"
        return 0
        ;;
      /model?*)
        printf '%s' "model:${trimmed#/model}"
        return 0
        ;;
      /mode?*)
        printf '%s' "model:${trimmed#/mode}"
        return 0
        ;;
      /send\ *)
        printf '%s' "send:${trimmed#/send }"
        return 0
        ;;
      /send)
        printf '%s' "send:"
        return 0
        ;;
      /send?*)
        printf '%s' "send:${trimmed#/send}"
        return 0
        ;;
      /codexurl\ *)
        printf '%s' "codexurl:${trimmed#/codexurl }"
        return 0
        ;;
      /codexurl)
        printf '%s' "codexurl"
        return 0
        ;;
      /help\ *)
        printf '%s' "help:${trimmed#/help }"
        return 0
        ;;
      /help)
        printf '%s' "help"
        return 0
        ;;
    esac

    case "$lowered" in
      y|yes|n|no)
        projectling_has_pending_command || return 1
        case "$lowered" in
          y|yes)
          printf '%s' "confirm:y"
          return 0
          ;;
          n|no)
          printf '%s' "deny"
          return 0
          ;;
        esac
        ;;
    esac

    return 1
  }

  projectling_dispatch_input() {
    local raw_input="$1"
    local mode="${2:-command_not_found}"
    [[ -n "${raw_input// }" ]] || return 0
    [[ -x "$PROJECTLING_RUNNER" ]] || {
      print -u2 -- "zsh: command not found: ${raw_input%%[[:space:]]*}"
      return 0
    }
    projectling_run_on_tty shell-dispatch --mode "$mode" --cwd "$PWD" --raw "$raw_input"
    return 0
  }

  projectling_detect_text_signal() {
    local raw_input="$1"
    # 这里是 Enter 热路径，尽量只走 zsh 自身的多字节模式匹配，不再起 grep 子进程。
    if [[ "$raw_input" == *[一-龥ぁ-んァ-ヴー々〆ヶ，。！？：；、】【（）「」『』、]* ]]; then
      return 0
    fi
    return 1
  }

  projectling_has_shell_syntax() {
    local raw_input="$1"
    case "$raw_input" in
      *$'\n'*|*';'*|*'|'*|*'&&'*|*'||'*|*'`'*|*'$('*|*'${'*|*'>'*|*'<'*)
        return 0
        ;;
    esac
    return 1
  }

  projectling_first_word() {
    local raw_input="$1"
    raw_input="${raw_input#"${raw_input%%[![:space:]]*}"}"
    printf '%s' "${raw_input%%[[:space:]]*}"
  }

  projectling_word_is_command() {
    local word="$1"
    [[ -n "$word" ]] || return 1
    whence -w -- "$word" >/dev/null 2>&1
  }

  projectling_classify_buffer() {
    local raw_input="$1"
    local trimmed="$raw_input"
    local first_word=''
    local lowered=''
    local special_kind=''
    local max_chars="${PROJECTLING_MAX_INLINE_CHARS:-4000}"

    PROJECTLING_DISPATCH_KIND=""
    trimmed="${trimmed#"${trimmed%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    [[ -n "$trimmed" ]] || return 1
    if (( ${#trimmed} > max_chars )); then
      return 1
    fi
    lowered="${(L)trimmed}"
    special_kind="$(projectling_special_command_kind "$trimmed")" || special_kind=''
    if [[ -n "$special_kind" ]]; then
      PROJECTLING_DISPATCH_KIND="$special_kind"
      return 0
    fi

    case "$trimmed" in
      \#*)
        return 1
        ;;
    esac

    first_word="$(projectling_first_word "$trimmed")"

    case "$first_word" in
      ./*|../*|/*|~/*|.|source|builtin|command|exec|noglob|nocorrect|time)
        return 1
        ;;
      [[:alpha:]_][[:alnum:]_]*=*)
        return 1
        ;;
    esac

    if projectling_word_is_command "$first_word"; then
      return 1
    fi

    if projectling_has_shell_syntax "$trimmed"; then
      return 1
    fi

    if projectling_detect_text_signal "$trimmed"; then
      PROJECTLING_DISPATCH_KIND="chat"
      return 0
    fi

    if [[ "$trimmed" == *[[:space:]]* ]]; then
      PROJECTLING_DISPATCH_KIND="chat"
      return 0
    fi

    case "$lowered" in
      hello|hi|hey|yo|thanks|thankyou|thank-you|pls|please)
        PROJECTLING_DISPATCH_KIND="chat"
        return 0
        ;;
    esac

    return 1
  }

  projectling_run_inline_action() {
    local raw_input="$1"
    local mode="$2"
    local local_mode="$mode"
    local local_arg=''
    if [[ "$local_mode" == settings:* ]]; then
      local_arg="${local_mode#settings:}"
      local_mode="settings"
    fi
    if [[ "$local_mode" == status:* ]]; then
      local_arg="${local_mode#status:}"
      local_mode="status"
    fi
    if [[ "$local_mode" == model:* ]]; then
      local_arg="${local_mode#model:}"
      local_mode="model"
    fi
    if [[ "$local_mode" == codexurl:* ]]; then
      local_arg="${local_mode#codexurl:}"
      local_mode="codexurl"
    fi
    if [[ "$local_mode" == models:* ]]; then
      local_arg="${local_mode#models:}"
      local_mode="models"
    fi
    if [[ "$local_mode" == api-test:* ]]; then
      local_arg="${local_mode#api-test:}"
      local_mode="api-test"
    fi
    if [[ "$local_mode" == help:* ]]; then
      local_arg="${local_mode#help:}"
      local_mode="help"
    fi
    if [[ "$local_mode" == send:* ]]; then
      local_arg="${local_mode#send:}"
      local_mode="send"
    fi
    if [[ "$local_mode" == confirm:* ]]; then
      local_arg="${local_mode#confirm:}"
      local_mode="confirm"
    fi
    zle -I
    print
    BUFFER=''
    CURSOR=0
    if [[ "$local_mode" == "settings" || "$local_mode" == "status" || "$local_mode" == "reload" || "$local_mode" == "update" || "$local_mode" == "model" || "$local_mode" == "models" || "$local_mode" == "api-test" || "$local_mode" == "codexurl" || "$local_mode" == "help" || "$local_mode" == "confirm" || "$local_mode" == "deny" ]]; then
      projectling_run_local_command "$local_mode" "$local_arg"
    elif [[ "$local_mode" == "send" ]]; then
      projectling_dispatch_input "$local_arg" "send"
    else
      projectling_dispatch_input "$raw_input" "$mode"
    fi
    return 0
  }

  projectling_record_edit_history() {
    local raw_input="$1"
    [[ -n "${raw_input// }" ]] || return 0
    if (( ${#PROJECTLING_EDIT_HISTORY[@]} > 0 )) && [[ "${PROJECTLING_EDIT_HISTORY[-1]}" == "$raw_input" ]]; then
      PROJECTLING_EDIT_BROWSE_POS=0
      PROJECTLING_EDIT_DRAFT=''
      return 0
    fi
    PROJECTLING_EDIT_HISTORY+=("$raw_input")
    if (( ${#PROJECTLING_EDIT_HISTORY[@]} > 30 )); then
      PROJECTLING_EDIT_HISTORY=("${PROJECTLING_EDIT_HISTORY[@]: -30}")
    fi
    PROJECTLING_EDIT_BROWSE_POS=0
    PROJECTLING_EDIT_DRAFT=''
    return 0
  }

  projectling-edit-prev() {
    local count=${#PROJECTLING_EDIT_HISTORY[@]}
    (( count > 0 )) || return 0
    if (( PROJECTLING_EDIT_BROWSE_POS == 0 )); then
      PROJECTLING_EDIT_DRAFT="$BUFFER"
      PROJECTLING_EDIT_BROWSE_POS=$count
    elif (( PROJECTLING_EDIT_BROWSE_POS > 1 )); then
      PROJECTLING_EDIT_BROWSE_POS=$(( PROJECTLING_EDIT_BROWSE_POS - 1 ))
    fi
    BUFFER="${PROJECTLING_EDIT_HISTORY[$PROJECTLING_EDIT_BROWSE_POS]}"
    CURSOR=${#BUFFER}
    zle redisplay
  }

  projectling-edit-next() {
    local count=${#PROJECTLING_EDIT_HISTORY[@]}
    (( PROJECTLING_EDIT_BROWSE_POS > 0 )) || return 0
    (( PROJECTLING_EDIT_BROWSE_POS < count )) || return 0
    PROJECTLING_EDIT_BROWSE_POS=$(( PROJECTLING_EDIT_BROWSE_POS + 1 ))
    BUFFER="${PROJECTLING_EDIT_HISTORY[$PROJECTLING_EDIT_BROWSE_POS]}"
    CURSOR=${#BUFFER}
    zle redisplay
  }

  command_not_found_handler() {
    local raw_input="$*"
    local special_kind=''
    local max_chars="${PROJECTLING_MAX_INLINE_CHARS:-4000}"
    if (( ${#raw_input} > max_chars )); then
      print -u2 -- "projectling: input too long for inline dispatch (${#raw_input}/${max_chars}); not sending to AI."
      return 127
    fi
    special_kind="$(projectling_special_command_kind "$raw_input")" || special_kind=''
    if [[ -n "$special_kind" ]]; then
      if [[ "$special_kind" == settings:* ]]; then
        projectling_run_local_command "settings" "${special_kind#settings:}"
      elif [[ "$special_kind" == status:* ]]; then
        projectling_run_local_command "status" "${special_kind#status:}"
      elif [[ "$special_kind" == models:* ]]; then
        projectling_run_local_command "models" "${special_kind#models:}"
      elif [[ "$special_kind" == api-test:* ]]; then
        projectling_run_local_command "api-test" "${special_kind#api-test:}"
      elif [[ "$special_kind" == model:* ]]; then
        projectling_run_local_command "model" "${special_kind#model:}"
      elif [[ "$special_kind" == codexurl:* ]]; then
        projectling_run_local_command "codexurl" "${special_kind#codexurl:}"
      elif [[ "$special_kind" == help:* ]]; then
        projectling_run_local_command "help" "${special_kind#help:}"
      elif [[ "$special_kind" == send:* ]]; then
        projectling_dispatch_input "${special_kind#send:}" "send"
      elif [[ "$special_kind" == confirm:* ]]; then
        projectling_run_local_command "confirm" "${special_kind#confirm:}"
      else
        projectling_run_local_command "$special_kind"
      fi
      return 0
    fi
    if projectling_menu_residue_input "$raw_input"; then
      return 0
    fi
    projectling_record_edit_history "$raw_input"
    projectling_dispatch_input "$raw_input" "command_not_found"
    return $?
  }

  if [[ -o interactive ]]; then
    if (( ! ${+widgets[projectling-orig-accept-line]} )); then
      zle -A accept-line projectling-orig-accept-line
    fi
      projectling-accept-line() {
        local raw_input="$BUFFER"
        if projectling_classify_buffer "$raw_input"; then
          projectling_record_edit_history "$raw_input"
          projectling_run_inline_action "$raw_input" "$PROJECTLING_DISPATCH_KIND"
          return 0
        fi
        projectling_record_edit_history "$raw_input"
        zle projectling-orig-accept-line
      }
      zle -N projectling-accept-line
      zle -N projectling-edit-prev
      zle -N projectling-edit-next
      bindkey '^M' projectling-accept-line
      bindkey '^J' projectling-accept-line
      bindkey '^[[2~' projectling-edit-prev
      bindkey '^[[4~' projectling-edit-next
      bindkey '^[OF' projectling-edit-next
      bindkey '^[[F' projectling-edit-next
  fi
fi
