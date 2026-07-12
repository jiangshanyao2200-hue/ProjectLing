from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from html import unescape
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import shutil
import signal
import sys
import tempfile
import threading
import time
from typing import Any, Sequence
import unicodedata
from types import SimpleNamespace


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_stdio()

# --- Sibling Runtime Bridge -------------------------------------------------
#
# `core.py` 和 `projectling.py` 被刻意保持在同一目录下，不拆包。
# 为了让外部入口、单测式导入、以及从其它 cwd 调用时都能稳定找到
# 同目录的 `projectling.py`，这里显式把当前目录压进 `sys.path` 前部。
PROJECTLING_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECTLING_DIR))

from projectling import (
    ChatResult,
    DeepSeekClient,
    DeepSeekAPIError,
    LauncherRole,
    PersonaBundle,
    load_context_budget,
    is_role_locked,
    DEEPSEEK_FAST_MODEL,
    DEEPSEEK_PRECISE_MODEL,
    ProjectLingConfig,
    ProjectLingEngine,
    PromptBundle,
    ToolContext,
    ToolRegistry,
    GEMINI_FAST_MODEL,
    GEMINI_PRECISE_MODEL,
    GEMINI_DEFAULT_BASE_URL,
    _api_provider_value,
    _collab_mode_value,
    _format_remaining_text,
    _remaining_seconds_for_role,
    build_roll_sequence,
    confirm_pending_command,
    load_config,
    load_external_context,
    load_role_context,
    load_roster,
    persona_path_for_role,
    reject_pending_command,
    render_animation_frame,
    render_motd_card,
    reroll_active_role,
    resolve_active_role,
    resolve_current_role,
    resolve_persona_bundle,
    save_env_config,
    set_role_locked,
    scrub_volatile_memory_entries,
    select_current_role_by_name,
    select_liaison_role_by_name,
    show_pending_command,
)
from tooling import (
    _execute_apply_patch_tool,
    _execute_command_tool,
    _execute_contextmanage_tool,
    _execute_update_plan_tool,
    context_entries_status,
    ensure_memory_layout,
    memory_status,
)


# --- Static Model Choices ---------------------------------------------------
#
# DeepSeek 保留保守默认项；Gemini 通过 OpenAI-compatible /models 动态发现。
#
# Maintenance zones:
# - ANSI/Markdown rendering and stream sanitizing live before ShellStreamPrinter.
# - Settings and command help live around `_render_*settings*` and `_run_*settings*`.
# - Tool receipt UI lives in `_tool_*` and `_render_*_receipt` helpers.
# - CLI routing starts at `dispatch_shell_input`, parser setup, then `_cmd_*`.
# Keep behavior changes in the narrowest zone so this large file stays navigable.
MODEL_CHOICES: list[tuple[str, str]] = [
    ("deepseek-v4-flash", "V4 Flash"),
    ("deepseek-v4-pro", "V4 Pro"),
    (GEMINI_FAST_MODEL, "Gemini Flash"),
    (GEMINI_PRECISE_MODEL, "Gemini Pro"),
]

DEEPSEEK_SETTINGS_MODEL_CHOICES: tuple[tuple[str, str], ...] = (
    (DEEPSEEK_PRECISE_MODEL, "V4 Pro · 主星/精确"),
    (DEEPSEEK_FAST_MODEL, "V4 Flash · 执行/快速"),
)

COLLAB_MODE_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("rapid", "快速模式", "轻量主星 + 轻量执行星"),
    ("standard", "标准模式", "强主星 + 快执行星"),
    ("precise", "精确模式", "强主星 + 强执行星"),
)
COLLAB_MODE_ORDER = tuple(mode for mode, _label, _desc in COLLAB_MODE_CHOICES)
COLLAB_MODE_ALIASES = {
    "1": "rapid",
    "fast": "rapid",
    "quick": "rapid",
    "迅速": "rapid",
    "快速": "rapid",
    "2": "standard",
    "normal": "standard",
    "std": "standard",
    "标准": "standard",
    "3": "precise",
    "accurate": "precise",
    "exact": "precise",
    "精确": "precise",
    "精准": "precise",
}
COLLAB_MODE_CYCLE_ALIASES = {"next", "cycle", "toggle", "切换", "下一个", "轮换"}
COLLAB_MODE_STATUS_ALIASES = {"status", "current", "show", "当前", "状态"}

SHELL_DISPATCH_MODES = {"chat", "command_not_found", "send"}
LEGACY_RUNTIME_FILES = ("shell_history.json",)
LEGACY_ROOT_RUNTIME_FILES = ("pending-command.json", "update-plan.json")
THINKING_PREVIEW_MAX_LINES = 10
THINKING_PREVIEW_EDGE_LINES = 5
THINKING_FOLD_DELAY_SECONDS = 0.35
THINKING_RENDER_INTERVAL_SECONDS = 0.2
WORKING_ANIMATION_INTERVAL_SECONDS = 0.55
TYPEWRITER_BULK_CHARS = 1400
TOOL_PREVIEW_HEAD_LINES = 2
TOOL_PREVIEW_TAIL_LINES = 3

# --- ANSI + Markdown Rendering ---------------------------------------------
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_ITALIC = "\033[3m"
ANSI_UNDERLINE = "\033[4m"
ANSI_CYAN = "\033[38;2;0;255;229m"
ANSI_MAGENTA = "\033[38;2;255;92;218m"
ANSI_WHITE = "\033[97m"
ANSI_GOLD = "\033[38;2;255;220;120m"
ANSI_QUOTE = "\033[38;2;182;194;224m"
ANSI_RULE = "\033[38;2;96;108;138m"
ANSI_LINK = "\033[38;2;120;220;255m"
ANSI_VIOLET = "\033[1;38;2;170;120;255m"
ANSI_SOFT_PINK = "\033[38;2;255;178;214m"
ANSI_SOFT_RED = "\033[38;2;255;120;152m"
ANSI_SOFT_BLUE = "\033[38;2;150;218;255m"
ANSI_MUTED_BLUE = "\033[38;2;148;178;196m"
ANSI_MUTED_TEXT = "\033[38;2;184;194;210m"
ANSI_SOFT_GREEN = "\033[38;2;142;220;184m"
ANSI_BADGE_BG = "\033[48;2;26;38;45m"
ANSI_CTX_BG = "\033[48;2;22;43;47m"
ANSI_CTX_FG = "\033[38;2;191;232;224m"
ANSI_BG_INVERT = "\033[47m"
ANSI_FG_INVERT = "\033[30m"

ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
PATHLIKE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_.-])(?:~(?:/[^\s\"'<>|;&]+)?|\$PREFIX(?:/[^\s\"'<>|;&]+)?|/[^\s\"'<>|;&]+)")
TOOL_OMISSION_RE = re.compile(r"^\.\.\.\s+\+\d+\s+lines$")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")
MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\(([^)\n]+)\)")
MARKDOWN_AUTO_LINK_RE = re.compile(r"<(https?://[^>\n]+)>")
MARKDOWN_BARE_URL_RE = re.compile(r"(?<![<(/])\b(https?://[^\s)>]+)")
MARKDOWN_REFERENCE_LINK_RE = re.compile(r"!\[([^\]\n]*)\]\[([^\]\n]*)\]|\[([^\]\n]+)\]\[([^\]\n]*)\]")
MARKDOWN_REFERENCE_DEF_RE = re.compile(r'^\s*\[([^\]\n]+)\]:\s*<?(\S+?)>?(?:\s+(?:"[^"]*"|\'[^\']*\'|\([^)]+\)))?\s*$')
MARKDOWN_FOOTNOTE_REF_RE = re.compile(r"\[\^([^\]\n]+)\]")
MARKDOWN_FOOTNOTE_DEF_RE = re.compile(r"^\s*\[\^([^\]\n]+)\]:\s*(.*)$")
MARKDOWN_CODE_RE = re.compile(r"`([^`\n]+)`")
MARKDOWN_STREAM_BLOCK_RE = re.compile(
    r"(?m)^\s*(?:#{1,6}\s+|[-+*]\s+(?:\[[ xX]\]\s+)?|\d+\.\s+(?:\[[ xX]\]\s+)?|>\s*|```+|~~~+|\|)"
)
MARKDOWN_BOLD_ITALIC_RE = re.compile(r"(\*\*\*|___)(.+?)\1")
MARKDOWN_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1")
MARKDOWN_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
MARKDOWN_ITALIC_UNDERSCORE_RE = re.compile(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)")
MARKDOWN_STRIKE_RE = re.compile(r"~~(.+?)~~")
MARKDOWN_HIGHLIGHT_RE = re.compile(r"==(.+?)==")
MARKDOWN_HTML_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
MARKDOWN_SETEXT_H1_RE = re.compile(r"^\s*=+\s*$")
MARKDOWN_SETEXT_H2_RE = re.compile(r"^\s*-+\s*$")
MARKDOWN_TABLE_ALIGN_RE = re.compile(r"^:?-{3,}:?$")
MARKDOWN_HTML_ANCHOR_RE = re.compile(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE)
MARKDOWN_HTML_IMG_RE = re.compile(
    r'<img\s+[^>]*src=["\']([^"\']+)["\'][^>]*alt=["\']([^"\']*)["\'][^>]*>|<img\s+[^>]*alt=["\']([^"\']*)["\'][^>]*src=["\']([^"\']+)["\'][^>]*>',
    re.IGNORECASE,
)
MARKDOWN_HTML_STRONG_RE = re.compile(r"<(?:strong|b)>(.*?)</(?:strong|b)>", re.IGNORECASE)
MARKDOWN_HTML_EM_RE = re.compile(r"<(?:em|i)>(.*?)</(?:em|i)>", re.IGNORECASE)
MARKDOWN_HTML_DEL_RE = re.compile(r"<(?:del|s|strike)>(.*?)</(?:del|s|strike)>", re.IGNORECASE)
MARKDOWN_HTML_CODE_RE = re.compile(r"<(?:code|kbd)>(.*?)</(?:code|kbd)>", re.IGNORECASE)
MARKDOWN_HTML_MARK_RE = re.compile(r"<mark>(.*?)</mark>", re.IGNORECASE)
MARKDOWN_HTML_UNDERLINE_RE = re.compile(r"<u>(.*?)</u>", re.IGNORECASE)
MARKDOWN_HTML_STRIP_RE = re.compile(r"</?(?:details|summary|div|span|p|section|article|main|small|sub|sup|ul|ol|li|table|thead|tbody|tr|td|th|blockquote|center|font)[^>]*>", re.IGNORECASE)
STREAM_SENTENCE_ENDINGS = "。！？.!?；;：:"
ESCAPED_MARKDOWN_TOKENS = {
    r"\*": "\uFFF0",
    r"\_": "\uFFF1",
    r"\`": "\uFFF2",
    r"\[": "\uFFF3",
    r"\]": "\uFFF4",
    r"\(": "\uFFF5",
    r"\)": "\uFFF6",
    r"\~": "\uFFF7",
    r"\#": "\uFFF8",
    r"\+": "\uFFF9",
    r"\-": "\uFFFA",
    r"\!": "\uFFFB",
    r"\>": "\uFFFC",
}
EXPLORE_SEARCH_COMMANDS = {"find", "grep", "rg"}
EXPLORE_READ_COMMANDS = {"cat", "file", "head", "readlink", "sed", "stat", "tail", "wc"}
EXPLORE_LIST_COMMANDS: set[str] = set()
GROUPABLE_BASH_COMMANDS = {
    "date",
    "df",
    "du",
    "echo",
    "env",
    "free",
    "git",
    "id",
    "ip",
    "netstat",
    "printenv",
    "ps",
    "pwd",
    "ss",
    "test",
    "uname",
    "whoami",
    "which",
}
GROUPABLE_GIT_READONLY_SUBCOMMANDS = {"branch", "describe", "diff", "log", "remote", "rev-parse", "show", "status", "tag"}
STATUS_SUCCESS_TEXT = {"ok": "Succeed", "empty": "Succeed", "stopped": "Succeed"}
FIND_MUTATING_TOKENS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fdelete"}


def _supports_tty_control() -> bool:
    term = os.environ.get("TERM", "")
    return bool(sys.stdout.isatty() and term and term.lower() != "dumb")


def _style_heading(text: str, kind: str = "deepseek") -> str:
    if not _supports_tty_control():
        return text
    color = ANSI_CYAN if kind == "deepseek" else ANSI_MAGENTA if kind == "thinking" else ANSI_WHITE
    return f"{ANSI_BOLD}{color}{text}{ANSI_RESET}"


def _style_status(text: str, kind: str) -> str:
    if not _supports_tty_control():
        return text
    color = ANSI_MAGENTA if kind == "thinking" else ANSI_WHITE
    return f"{ANSI_DIM}{color}{text}{ANSI_RESET}"


def _style_context_text(text: str) -> str:
    if not _supports_tty_control():
        return text
    return f"{ANSI_CTX_BG}{ANSI_CTX_FG}{ANSI_BOLD} {text} {ANSI_RESET}"


def _style_badge(text: str, *, color: str = ANSI_MUTED_TEXT, background: str = ANSI_BADGE_BG) -> str:
    if not _supports_tty_control():
        return f"[{text}]"
    return f"{background}{color}{ANSI_BOLD} {text} {ANSI_RESET}"


def _style_thought_text(text: str) -> str:
    if not _supports_tty_control():
        return text
    return f"{ANSI_DIM}{ANSI_RULE}{text}{ANSI_RESET}"


def _format_thought_summary(elapsed_seconds: float | None) -> str:
    if elapsed_seconds is None:
        return "◌ 思考"
    seconds = max(0.0, float(elapsed_seconds))
    if seconds < 0.1:
        seconds = 0.1
    if seconds < 10:
        return f"◌ 思考 · {seconds:.1f} 秒"
    return f"◌ 思考 · {seconds:.0f} 秒"


def _context_budget_percent(state: dict[str, Any] | None) -> int:
    if not state:
        return 100
    try:
        raw_percent = state.get("percent")
        if raw_percent is None or raw_percent == "":
            return 100
        return max(0, min(100, int(raw_percent)))
    except (TypeError, ValueError):
        return 100


def _payload_percent(payload: dict[str, Any], default: int = 100) -> int:
    for key in ("context_budget_percent", "percent"):
        value = payload.get(key)
        if value is None or value == "":
            continue
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            continue
    return max(0, min(100, int(default)))


def _context_budget_bar(percent: int, *, width: int = 8) -> str:
    width = max(1, int(width))
    percent = max(0, min(100, int(percent)))
    filled = round(width * percent / 100)
    filled = max(0, min(width, filled))
    return f"[{'█' * filled}{'░' * (width - filled)}]"


def _context_budget_line(state: dict[str, Any] | None) -> str:
    percent = _context_budget_percent(state)
    return _style_context_text(f"CTK{percent}%")


def _strip_context_percent_marker_text(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return raw
    marker_line = re.compile(
        r"^[ \t]*(?:<!--\s*)?(?:PROJECTLING_CONTEXT_PERCENT|PROJECTLING_CTX|context_percent|next_context_percent)\s*[:=]\s*\d{1,3}\s*(?:-->)?[ \t]*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    stripped = marker_line.sub("", raw)
    inline_marker = re.compile(
        r"(?:<!--\s*)?(?:PROJECTLING_CONTEXT_PERCENT|PROJECTLING_CTX|context_percent|next_context_percent)\s*[:=]\s*\d{1,3}\s*(?:-->)?",
        flags=re.IGNORECASE,
    )
    stripped = inline_marker.sub("", stripped)
    return re.sub(r"\n{3,}", "\n\n", stripped).strip()


def _speaker_label_for_bundle(bundle: PersonaBundle) -> str:
    liaison_sources = {"speaker_handoff", "executor_handoff", "persona_link_mission", "liaison_tool"}
    return "执行星" if bundle.source in liaison_sources else "主星"


def _identity_part_key(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip()).casefold()


def _dedupe_identity_parts(parts: Sequence[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in parts:
        part = str(value or "").strip()
        if not part:
            continue
        key = _identity_part_key(part)
        if key and key not in seen:
            result.append(part)
            seen.add(key)
    return result


def _role_identity_name(role: LauncherRole) -> str:
    return " · ".join(_dedupe_identity_parts((role.name_zh, role.name_en)))


def _normalize_identity_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = re.split(r"\s*[／/]\s*|\s+·\s+", text)
    return " · ".join(_dedupe_identity_parts(parts))


def _speaker_identity_text(
    role: LauncherRole,
    persona_bundle: PersonaBundle | None = None,
    *,
    context_budget: dict[str, Any] | None = None,
    role_label: str = "",
    actor_name: Any = "",
) -> str:
    bundle = persona_bundle or PersonaBundle(main=role)
    label = role_label.strip() or _speaker_label_for_bundle(bundle)
    name = _normalize_identity_name(actor_name) or _role_identity_name(bundle.main)
    parts = [part for part in (name, label) if part]
    if context_budget is not None:
        parts.append(f"CTK{_context_budget_percent(context_budget)}%")
    return " · ".join(parts)


def _format_role_heading(role: LauncherRole, persona_bundle: PersonaBundle | None = None) -> str:
    bundle = persona_bundle or PersonaBundle(main=role)
    speaker_label = _speaker_label_for_bundle(bundle)
    mode_badge = ""
    speaker = bundle.main
    if not _supports_tty_control():
        suffix = f" · {mode_badge}" if mode_badge else ""
        return f"● {_speaker_identity_text(role, bundle)}{suffix}"
    dot = f"{ANSI_DIM}{ANSI_WHITE} · {ANSI_RESET}"
    label = f"{ANSI_DIM}{ANSI_WHITE}{speaker_label}{ANSI_RESET}"
    if speaker_label == "执行星":
        role_color = ANSI_VIOLET if mode_badge else ANSI_CYAN
        name = f"{role_color}{speaker.name_zh}{ANSI_RESET}"
        name_en = f"{ANSI_ITALIC}{role_color}{speaker.name_en}{ANSI_RESET}"
    else:
        name = f"{ANSI_BOLD}{ANSI_GOLD}{speaker.name_zh}{ANSI_RESET}"
        name_en = f"{ANSI_BOLD}{ANSI_ITALIC}{ANSI_GOLD}{speaker.name_en}{ANSI_RESET}"
    badge = f" {_style_badge(mode_badge, color=ANSI_CTX_FG)}" if mode_badge else ""
    styled_names: list[tuple[str, str]] = []
    for raw_name, styled_name in ((speaker.name_zh, name), (speaker.name_en, name_en)):
        key = _identity_part_key(raw_name)
        if key and all(existing_key != key for existing_key, _existing_name in styled_names):
            styled_names.append((key, styled_name))
    heading_parts = [styled_name for _key, styled_name in styled_names] + [label]
    return f"● {dot.join(heading_parts)}{badge}"


def _role_from_roster_payload(payload: dict[str, Any], *keys: str) -> LauncherRole | None:
    names = [str(payload.get(key) or "").strip() for key in keys]
    names = [name for name in names if name]
    if not names:
        return None
    try:
        roster = load_roster(load_config())
    except Exception:
        return None
    expanded_names: set[str] = set()
    for name in names:
        expanded_names.add(name)
        for part in re.split(r"[/·|]", name):
            part = part.strip()
            if part:
                expanded_names.add(part)
    normalized = {name.lower() for name in expanded_names}
    for role in roster:
        if role.name_en.lower() in normalized or role.name_zh.lower() in normalized:
            return role
    return None


def _role_from_roster_payload_priority(payload: dict[str, Any], *keys: str) -> LauncherRole | None:
    for key in keys:
        role = _role_from_roster_payload(payload, key)
        if role is not None:
            return role
    return None


def _persona_from_handoff_payload(payload: dict[str, Any]) -> tuple[LauncherRole, PersonaBundle] | None:
    tool_name = str(payload.get("tool") or "")
    action_name = str(payload.get("action") or payload.get("speaker_mode") or "").strip().lower()
    if tool_name == "persona_link" and action_name != "switch":
        return None
    if tool_name == "link" and action_name != "switch":
        return None
    if tool_name not in {"persona_handoff", "persona_link", "link"}:
        return None
    if str(payload.get("status") or "") != "ok":
        return None
    target = str(payload.get("target") or payload.get("speaker_mode") or "").strip().lower()
    speaker = _role_from_roster_payload(payload, "speaker_name_en", "speaker_name_zh", "speaker_name")
    main_role = _role_from_roster_payload(payload, "main_name_en", "main_name_zh", "main_name")
    liaison_role = _role_from_roster_payload(payload, "liaison_name_en", "liaison_name_zh", "liaison_name")
    if target == "liaison" and speaker is not None:
        return speaker, PersonaBundle(main=speaker, liaison=main_role, source="speaker_handoff")
    if target == "main" and (main_role is not None or speaker is not None):
        active = main_role or speaker
        return active, PersonaBundle(main=active, liaison=liaison_role, source="selected" if liaison_role else "solo")
    return None


def _split_shell_words(text: str) -> list[str]:
    command = str(text or "").strip()
    if not command:
        return []
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _shorten_path_token(token: str) -> str:
    text = str(token or "")
    if _display_width(text) <= 52 or "/" not in text:
        return text
    if text.startswith("~/"):
        root = "~/"
        parts = [part for part in text[2:].split("/") if part]
    elif text.startswith("$PREFIX/"):
        root = "$PREFIX/"
        parts = [part for part in text[len("$PREFIX/") :].split("/") if part]
    elif text.startswith("/"):
        root = "/"
        parts = [part for part in text[1:].split("/") if part]
    else:
        return _middle_truncate_display(text, 52)
    if len(parts) <= 3:
        return _middle_truncate_display(text, 52)
    for head_count, tail_count in ((2, 2), (1, 2), (1, 1)):
        head = "/".join(parts[:head_count])
        tail = "/".join(parts[-tail_count:])
        candidate = f"{root}{head}/…/{tail}".replace("//", "/")
        if _display_width(candidate) <= 52:
            return candidate
    return _middle_truncate_display(text, 52)


def _style_tool_omission(text: str) -> str:
    if not _supports_tty_control() or not TOOL_OMISSION_RE.match(str(text or "").strip()):
        return text
    return f"{ANSI_BOLD}{ANSI_SOFT_RED}{text}{ANSI_RESET}"


def _style_tool_line(text: str, color: str = ANSI_WHITE, *, bold: bool = False, dim: bool = False) -> str:
    if not _supports_tty_control():
        return text
    style = ""
    if bold:
        style += ANSI_BOLD
    if dim:
        style += ANSI_DIM
    style += color
    return f"{style}{text}{ANSI_RESET}"


def _cleanup_legacy_runtime(config: ProjectLingConfig) -> None:
    for name in LEGACY_RUNTIME_FILES:
        target = config.runtime_dir / name
        try:
            if target.is_file():
                target.unlink()
        except OSError:
            continue
    for name in LEGACY_ROOT_RUNTIME_FILES:
        target = config.root_dir / name
        try:
            if target.is_file() and target.parent != config.runtime_dir:
                target.unlink()
        except OSError:
            continue
    try:
        env_text = config.env_file_path.read_text(encoding="utf-8")
    except OSError:
        env_text = ""
    if re.search(r"(?m)^DEEPSEEK_(MODEL|ENABLE_THINKING)=", env_text):
        save_env_config({"DEEPSEEK_MODEL": None, "DEEPSEEK_ENABLE_THINKING": None}, path=config.env_file_path)


def _normalize_status_label(text: str | None, fallback: str) -> str:
    cleaned = str(text or "").strip().strip(". ")
    if not cleaned:
        return fallback
    return " ".join(part.capitalize() for part in cleaned.replace("_", " ").split())


def _indent_block(text: str) -> str:
    body = (text or "").strip() or "我没有得到有效回复。"
    lines = [f"  {line.rstrip()}" if line.strip() else "" for line in body.splitlines()]
    return "\n".join(lines) if lines else "  我没有得到有效回复。"


def _collapse_blank_lines(text: str) -> str:
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text


def _repair_unordered_list_markers_in_line(line: str) -> str:
    if not re.match(r"^\s*[-+*•]\s+", line):
        return line
    markers = list(re.finditer(r"[-+*•]\s+", line))
    if len(markers) <= 1:
        return line

    indent = re.match(r"^\s*", line).group(0)
    out: list[str] = []
    last = 0
    for marker in markers[1:]:
        out.append(line[last:marker.start()].rstrip())
        out.append("\n")
        out.append(indent)
        last = marker.start()
    out.append(line[last:])
    return "".join(out)


def _repair_ordered_list_markers_in_line(line: str) -> str:
    markers = list(re.finditer(r"(?<!\d)(\d{1,3})\.\s+", line))
    if len(markers) <= 1:
        return line

    sequence_start = None
    for index, marker in enumerate(markers):
        number = int(marker.group(1))
        if number == 1 or re.match(r"^\s*$", line[: marker.start()]):
            if index + 1 < len(markers) and int(markers[index + 1].group(1)) == number + 1:
                sequence_start = index
                break
    if sequence_start is None:
        return line

    indent = re.match(r"^\s*", line).group(0)
    out: list[str] = []
    last = 0
    expected = int(markers[sequence_start].group(1))
    for marker in markers[sequence_start:]:
        number = int(marker.group(1))
        if number != expected:
            expected = number + 1
            continue
        prefix = line[last:marker.start()].rstrip()
        if prefix:
            out.append(prefix)
            out.append("\n")
            out.append(indent)
        elif last == 0 and marker.start() > 0:
            out.append(line[last:marker.start()])
        elif out and not out[-1].endswith("\n"):
            out.append("\n")
            out.append(indent)
        last = marker.start()
        expected = number + 1
    out.append(line[last:])
    return "".join(out)


def _repair_collapsed_table_rows_in_line(line: str) -> str:
    if "||" not in line or "|" not in line:
        return line
    repaired = re.sub(r"\|\s*\|", "|\n|", line)
    parts = repaired.splitlines()
    if len(parts) < 2:
        return line
    has_separator = any(_split_table_separator_candidate(part) for part in parts)
    return repaired if has_separator else line


def _split_table_separator_candidate(line: str) -> bool:
    cells = [cell.strip().replace(" ", "") for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells)


def _repair_markdown_list_boundaries(text: str) -> str:
    repaired = re.sub(r"([^\s\n])(\s*-\s+\*\*)", r"\1\n\2", str(text or ""))
    repaired_lines: list[str] = []
    fence_marker: str | None = None
    for line in repaired.split("\n"):
        stripped = line.lstrip()
        fence_match = re.match(r"^(```+|~~~+)", stripped)
        if fence_match:
            marker = fence_match.group(1)[:3]
            fence_marker = None if fence_marker == marker else marker
            repaired_lines.append(line)
            continue

        if fence_marker is not None:
            repaired_lines.append(line)
            continue

        line = _repair_collapsed_table_rows_in_line(line)
        for part in line.split("\n"):
            part = _repair_unordered_list_markers_in_line(part)
            part = _repair_ordered_list_markers_in_line(part)
            repaired_lines.extend(part.split("\n"))
    return "\n".join(repaired_lines)


def _restore_escaped_markdown(text: str) -> str:
    restored = text
    for raw, token in ESCAPED_MARKDOWN_TOKENS.items():
        restored = restored.replace(token, raw[1:])
    return restored


def _tokenize_ansi(text: str) -> list[str]:
    parts: list[str] = []
    last = 0
    for match in ANSI_PATTERN.finditer(text):
        if match.start() > last:
            parts.append(text[last:match.start()])
        parts.append(match.group(0))
        last = match.end()
    if last < len(text):
        parts.append(text[last:])
    return parts


def _strip_ansi(text: str) -> str:
    return ANSI_PATTERN.sub("", text)


def _display_width(text: str) -> int:
    width = 0
    for char in _strip_ansi(text):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def _truncate_display(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    out: list[str] = []
    used = 0
    for char in text:
        char_width = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1)
        if used + char_width > max_width:
            break
        out.append(char)
        used += char_width
    return "".join(out)


def _truncate_display_ellipsis(text: str, max_width: int) -> str:
    if _display_width(text) <= max_width:
        return text
    if max_width <= 1:
        return _truncate_display(text, max_width)
    return f"{_truncate_display(text, max_width - 1)}…"


def _middle_truncate_display(text: str, max_width: int, *, head_ratio: float = 0.55) -> str:
    if max_width <= 0:
        return ""
    if _display_width(text) <= max_width:
        return text
    ellipsis = "…"
    ellipsis_width = _display_width(ellipsis)
    if max_width <= ellipsis_width:
        return ellipsis[:max_width]
    budget = max_width - ellipsis_width
    head_width = max(1, int(budget * head_ratio))
    tail_width = max(1, budget - head_width)
    while head_width + tail_width > budget:
        tail_width = max(1, tail_width - 1)
    while head_width + tail_width < budget:
        head_width += 1
    head = _truncate_display(text, head_width)
    tail = _truncate_display(text[::-1], tail_width)[::-1]
    return f"{head}{ellipsis}{tail}"


def _char_display_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def _wrap_ansi_display(text: str, max_width: int) -> list[str]:
    if max_width <= 0:
        return [""]

    lines: list[str] = []
    current: list[str] = []
    used = 0

    for token in _tokenize_ansi(text):
        if not token:
            continue
        if ANSI_PATTERN.fullmatch(token):
            current.append(token)
            continue

        for char in token:
            if char == "\n":
                lines.append("".join(current).rstrip())
                current = []
                used = 0
                continue

            char_width = _char_display_width(char)
            if used > 0 and used + char_width > max_width:
                lines.append("".join(current).rstrip())
                current = []
                used = 0

            current.append(char)
            used += char_width

    if current or not lines:
        lines.append("".join(current).rstrip())

    return lines


def _pad_display(text: str, width: int) -> str:
    plain = _truncate_display(text, width)
    return plain + (" " * max(0, width - _display_width(plain)))


def _terminal_render_width(default: int = 80) -> int:
    return max(24, shutil.get_terminal_size((default, 24)).columns - 2)


def _compact_render_width(default: int = 80) -> int:
    columns = shutil.get_terminal_size((default, 24)).columns
    if columns <= 0:
        columns = default
    if columns >= 24:
        columns -= 2
    return max(16, min(120, columns))


def _print_fit(text: Any, *, width: int | None = None) -> None:
    render_width = width if width is not None else _compact_render_width()
    print(_truncate_display_ellipsis(str(text), render_width))


def _print_fit_wrapped(text: Any, *, width: int | None = None, max_lines: int = 4) -> None:
    render_width = width if width is not None else _compact_render_width()
    lines = _wrap_ansi_display(str(text), render_width)
    for line in lines[:max(1, max_lines)]:
        _print_fit(line, width=render_width)
    if len(lines) > max_lines:
        _print_fit("...", width=render_width)


def _print_next_action_check(parts: Sequence[str], *, width: int | None = None) -> None:
    render_width = width if width is not None else _compact_render_width()
    clean_parts = [str(part).strip() for part in parts if str(part).strip()]
    if render_width >= 48:
        _print_fit(f"下一步：检查 {'、'.join(clean_parts)}。", width=render_width)
        return
    _print_fit("下一步：检查", width=render_width)
    for part in clean_parts:
        _print_fit(f"- {part}", width=render_width)


def _print_setting_saved(label: str, value: Any | None = None, *, width: int | None = None) -> None:
    render_width = width if width is not None else _compact_render_width()
    _print_fit(f"{label} 已保存", width=render_width)
    if value is not None:
        _print_setting_pair("值", value, width=render_width)


def _print_setting_cleared(label: str, *, width: int | None = None) -> None:
    render_width = width if width is not None else _compact_render_width()
    _print_fit(f"{label} 已清除", width=render_width)
    _print_fit("使用自动值。", width=render_width)


def _print_setting_rejected(reason: str, *, width: int | None = None, keep_label: str = "", keep_value: Any | None = None) -> None:
    render_width = width if width is not None else _compact_render_width()
    _print_fit("未保存，保持原样", width=render_width)
    if reason:
        _print_fit_wrapped(reason, width=render_width, max_lines=4)
    if keep_label and keep_value is not None:
        _print_setting_pair(keep_label, keep_value, width=render_width)


def _print_setting_unchanged(reason: str = "未输入内容。", *, width: int | None = None) -> None:
    render_width = width if width is not None else _compact_render_width()
    _print_fit("未输入，保持原样", width=render_width)
    if reason:
        _print_fit_wrapped(reason, width=render_width, max_lines=2)


def _print_setting_pair(label: str, value: Any, *, width: int | None = None) -> None:
    render_width = width if width is not None else _compact_render_width()
    value_text = str(value)
    if render_width >= 42:
        prefix = f"{label} "
        value_width = max(4, render_width - _display_width(prefix) - 2)
        _print_fit(f"{prefix}[{_middle_truncate_display(value_text, value_width)}]", width=render_width)
        return
    _print_fit(label, width=render_width)
    _print_fit(f"  {_middle_truncate_display(value_text, max(4, render_width - 2))}", width=render_width)


def _print_setting_option(index: str | int, label: str, value: Any | None = None, *, width: int | None = None) -> None:
    render_width = width if width is not None else _compact_render_width()
    prefix = f"› {index}  {label}" if str(index) != "0" else f"‹ {index}  {label}"
    if value is None or not str(value).strip():
        _print_fit(prefix, width=render_width)
    else:
        _print_setting_pair(prefix, value, width=render_width)


def _print_setting_section(title: str, *, width: int | None = None) -> None:
    render_width = width if width is not None else _compact_render_width()
    print("")
    _print_fit(f"{title}", width=render_width)


def _print_indexed_model(index: int, model_id: str, marker: str = "", *, width: int | None = None) -> None:
    render_width = width if width is not None else _compact_render_width()
    prefix = f"{index:02d}. "
    suffix = f" {marker.strip()}" if marker.strip() else ""
    model_width = max(4, render_width - _display_width(prefix) - _display_width(suffix))
    _print_fit(f"{prefix}{_middle_truncate_display(model_id, model_width)}{suffix}", width=render_width)


def _relay_model_tags(model_id: str) -> list[str]:
    model = str(model_id or "").strip().lower()
    tokens = [token for token in re.split(r"[^a-z0-9]+", model) if token]
    tags: list[str] = []
    if "claude" in tokens:
        tags.append("claude")
    if "pro" in tokens:
        tags.append("pro")
    if "flash" in tokens:
        tags.append("flash")
    if "thinking" in tokens or "think" in tokens:
        tags.append("think")
    if "image" in tokens or "vision" in tokens:
        tags.append("image")
    if "agent" in tokens:
        tags.append("agent")
    if "lite" in tokens:
        tags.append("lite")
    return tags or ["unknown"]


def _model_list_marker_parts(model_id: str, planner_model: str, executor_model: str, *, width: int) -> list[str]:
    normalized_id = str(model_id or "").strip()
    parts: list[str] = []
    if normalized_id == planner_model:
        parts.append("主星")
    if normalized_id == executor_model:
        parts.append("执行星")
    if width >= 32:
        for tag in _relay_model_tags(normalized_id):
            if tag not in parts:
                parts.append(tag)
    elif width >= 24 and not parts:
        parts.append(_relay_model_tags(normalized_id)[0])
    return parts


def _print_model_taxonomy_hint(ids: Sequence[str], *, width: int) -> None:
    if width < 30:
        return
    tag_order = ("pro", "flash", "think", "image", "agent", "claude", "lite", "unknown")
    counts = {tag: 0 for tag in tag_order}
    for model_id in ids:
        for tag in _relay_model_tags(model_id):
            counts[tag] = counts.get(tag, 0) + 1
    summary_parts = [f"{tag}:{counts[tag]}" for tag in tag_order if counts.get(tag)]
    if summary_parts:
        line = "分类"
        for part in summary_parts:
            candidate = f"{line} {part}" if line != "分类" else f"分类 {part}"
            if _display_width(candidate) <= width:
                line = candidate
                continue
            if line != "分类":
                _print_fit(line, width=width)
            line = f"分类 {part}"
            if _display_width(line) > width:
                _print_fit(line, width=width)
                line = "分类"
        if line != "分类":
            _print_fit(line, width=width)
        if width >= 48:
            _print_fit_wrapped("提示 pro适合主星 flash适合执行星 image/agent/claude需按任务确认", width=width, max_lines=2)


def _api_test_model_safety(model_id: str) -> dict[str, Any]:
    tags = _relay_model_tags(model_id)
    risky_tags = [tag for tag in ("image", "agent", "claude", "unknown") if tag in tags]
    if not risky_tags:
        return {"tags": tags, "risk": "normal", "hint": ""}
    risk = risky_tags[0]
    hints = {
        "image": "该模型偏图像任务；api-test仅验证文本连通，执行星优先使用flash。",
        "agent": "该模型偏agent任务；确认工具/权限/输出格式后再设为执行星。",
        "claude": "该模型是Claude中转；确认工具调用、流式和用量字段兼容后再设为执行星。",
        "unknown": "未知模型类别；建议先用诊断覆盖测试，确认文本聊天和工具兼容。",
    }
    return {"tags": tags, "risk": risk, "hint": hints.get(risk, hints["unknown"])}


def _model_role_safety_hint(model_id: str, role_label: str) -> str:
    tags = _relay_model_tags(model_id)
    risky_tags = [tag for tag in ("image", "agent", "claude", "unknown") if tag in tags]
    if not risky_tags:
        return ""
    risk = risky_tags[0]
    if risk == "image":
        return f"{role_label}模型偏图像任务；保存已完成，请确认文本和工具兼容。"
    if risk == "agent":
        return f"{role_label}模型偏agent任务；保存已完成，请确认权限、工具和输出格式。"
    if risk == "claude":
        return f"{role_label}模型是Claude中转；保存已完成，请确认工具、流式和用量字段兼容。"
    return f"{role_label}模型类别未知；保存已完成，建议先用 api-test 诊断覆盖验证。"


def _print_model_role_safety_hint(model_id: str, role_label: str, *, width: int | None = None) -> None:
    hint = _model_role_safety_hint(model_id, role_label)
    if not hint:
        return
    render_width = width if width is not None else _compact_render_width()
    tags = _relay_model_tags(model_id)
    risk = next((tag for tag in ("image", "agent", "claude", "unknown") if tag in tags), "unknown")
    short_labels = {
        "image": "提示：图像模型",
        "agent": "提示：agent 模型",
        "claude": "提示：Claude 中转",
        "unknown": "提示：未知模型",
    }
    _print_fit(short_labels.get(risk, short_labels["unknown"]), width=render_width)
    _print_fit_wrapped(hint, width=render_width, max_lines=3)


def _print_deepseek_model_control_notice(*, width: int | None = None) -> None:
    render_width = width if width is not None else _compact_render_width()
    _print_fit("DeepSeek 模型", width=render_width)
    _print_fit("可在 API 设置中选择", width=render_width)
    if render_width < 20:
        _print_fit("rapid=执行", width=render_width)
        _print_fit("std=主+执", width=render_width)
    else:
        _print_fit("rapid=执行星模型", width=render_width)
        _print_fit("standard=主星+执行星", width=render_width)
        _print_fit("precise=主星模型", width=render_width)


def _model_list_title(provider_label: str, status: str | int, *, width: int | None = None) -> str:
    render_width = width if width is not None else _compact_render_width()
    full = f"{provider_label} models · {status}"
    if _display_width(full) <= render_width:
        return full
    return f"{provider_label} · {status}"


def _normalize_reference_key(key: str) -> str:
    return " ".join(key.strip().lower().split())


def _footnote_marker(label: str) -> str:
    if label.isdigit():
        return label.translate(str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹"))
    return f"〔{label.strip()}〕"


def _style_span(text: str, style: str, *, base_style: str = "") -> str:
    if not text:
        return ""
    return f"{style}{text}{ANSI_RESET}{base_style}"


def _render_inline_markdown_plain(text: str) -> str:
    if not text:
        return text
    rendered = unescape(str(text))
    for raw, token in ESCAPED_MARKDOWN_TOKENS.items():
        rendered = rendered.replace(raw, token)

    rendered = MARKDOWN_HTML_BREAK_RE.sub(" ", rendered)
    rendered = MARKDOWN_HTML_ANCHOR_RE.sub(lambda match: f"{match.group(2)} ({match.group(1)})", rendered)
    rendered = MARKDOWN_HTML_IMG_RE.sub(
        lambda match: f"▣ {(match.group(2) or match.group(3) or 'image').strip()}",
        rendered,
    )
    rendered = MARKDOWN_IMAGE_RE.sub(lambda match: f"▣ {(match.group(1) or 'image').strip()}", rendered)
    rendered = MARKDOWN_LINK_RE.sub(lambda match: f"{match.group(1)} ({match.group(2)})", rendered)
    rendered = MARKDOWN_AUTO_LINK_RE.sub(lambda match: match.group(1), rendered)
    rendered = MARKDOWN_BARE_URL_RE.sub(lambda match: match.group(1), rendered)
    rendered = MARKDOWN_CODE_RE.sub(lambda match: match.group(1), rendered)
    rendered = MARKDOWN_HTML_CODE_RE.sub(lambda match: match.group(1), rendered)
    for pattern in (
        MARKDOWN_HTML_STRONG_RE,
        MARKDOWN_HTML_EM_RE,
        MARKDOWN_HTML_DEL_RE,
        MARKDOWN_HTML_MARK_RE,
        MARKDOWN_HTML_UNDERLINE_RE,
    ):
        rendered = pattern.sub(lambda match: match.group(1), rendered)
    rendered = MARKDOWN_BOLD_ITALIC_RE.sub(lambda match: match.group(2), rendered)
    rendered = MARKDOWN_BOLD_RE.sub(lambda match: match.group(2), rendered)
    rendered = MARKDOWN_ITALIC_STAR_RE.sub(lambda match: match.group(1), rendered)
    rendered = MARKDOWN_ITALIC_UNDERSCORE_RE.sub(lambda match: match.group(1), rendered)
    rendered = MARKDOWN_STRIKE_RE.sub(lambda match: match.group(1), rendered)
    rendered = MARKDOWN_HIGHLIGHT_RE.sub(lambda match: match.group(1), rendered)
    rendered = MARKDOWN_HTML_STRIP_RE.sub("", rendered)
    return _restore_escaped_markdown(rendered)


def _render_inline_markdown(text: str, *, base_style: str = "", tty: bool) -> str:
    if not text:
        return text
    if not tty:
        return _render_inline_markdown_plain(text)

    rendered = unescape(text)
    for raw, token in ESCAPED_MARKDOWN_TOKENS.items():
        rendered = rendered.replace(raw, token)

    placeholder_index = 0
    placeholders: dict[str, str] = {}

    def stash(value: str) -> str:
        nonlocal placeholder_index
        key = f"\uFFFDU{placeholder_index}\uFFFE"
        placeholder_index += 1
        placeholders[key] = value
        return key

    def image_repl(match: re.Match[str]) -> str:
        alt = match.group(1).strip() or "image"
        target = match.group(2).strip()
        if " " in target:
            target = target.split(" ", 1)[0]
        return stash(
            _style_span("▣ ", f"{ANSI_BOLD}{ANSI_MAGENTA}", base_style=base_style)
            + _style_span(alt, f"{ANSI_BOLD}{ANSI_WHITE}", base_style=base_style)
            + _style_span(f" <{target}>", f"{ANSI_DIM}{ANSI_WHITE}", base_style=base_style)
        )

    def link_repl(match: re.Match[str]) -> str:
        label = match.group(1).strip()
        target = match.group(2).strip()
        if not label or not target:
            return match.group(0)
        if " " in target:
            target = target.split(" ", 1)[0]
        return stash(
            _style_span(label, f"{ANSI_UNDERLINE}{ANSI_LINK}", base_style=base_style)
            + _style_span(f" <{target}>", f"{ANSI_DIM}{ANSI_WHITE}", base_style=base_style)
        )

    def auto_link_repl(match: re.Match[str]) -> str:
        target = match.group(1).strip()
        return stash(_style_span(target, f"{ANSI_UNDERLINE}{ANSI_LINK}", base_style=base_style))

    rendered = MARKDOWN_CODE_RE.sub(
        lambda match: stash(
            _style_span(match.group(1), f"{ANSI_BOLD}{ANSI_GOLD}", base_style=base_style)
        ),
        rendered,
    )
    rendered = MARKDOWN_BARE_URL_RE.sub(
        lambda match: stash(
            _style_span(match.group(1), f"{ANSI_UNDERLINE}{ANSI_LINK}", base_style=base_style)
        ),
        rendered,
    )
    rendered = MARKDOWN_AUTO_LINK_RE.sub(auto_link_repl, rendered)
    rendered = MARKDOWN_IMAGE_RE.sub(image_repl, rendered)
    rendered = MARKDOWN_LINK_RE.sub(link_repl, rendered)
    rendered = MARKDOWN_HIGHLIGHT_RE.sub(
        lambda match: _style_span(match.group(1), f"{ANSI_BOLD}{ANSI_GOLD}", base_style=base_style),
        rendered,
    )
    rendered = MARKDOWN_BOLD_ITALIC_RE.sub(
        lambda match: _style_span(match.group(2), f"{ANSI_BOLD}{ANSI_ITALIC}", base_style=base_style),
        rendered,
    )
    rendered = MARKDOWN_BOLD_RE.sub(
        lambda match: _style_span(match.group(2), ANSI_BOLD, base_style=base_style),
        rendered,
    )
    rendered = MARKDOWN_ITALIC_STAR_RE.sub(
        lambda match: _style_span(match.group(1), ANSI_ITALIC, base_style=base_style),
        rendered,
    )
    rendered = MARKDOWN_ITALIC_UNDERSCORE_RE.sub(
        lambda match: _style_span(match.group(1), ANSI_ITALIC, base_style=base_style),
        rendered,
    )
    rendered = MARKDOWN_STRIKE_RE.sub(
        lambda match: _style_span(match.group(1), ANSI_DIM, base_style=base_style),
        rendered,
    )

    rendered = MARKDOWN_HTML_ANCHOR_RE.sub(
        lambda match: stash(
            _style_span(match.group(2).strip() or match.group(1).strip(), f"{ANSI_UNDERLINE}{ANSI_LINK}", base_style=base_style)
            + _style_span(f" <{match.group(1).strip()}>", f"{ANSI_DIM}{ANSI_WHITE}", base_style=base_style)
        ),
        rendered,
    )
    rendered = MARKDOWN_HTML_IMG_RE.sub(
        lambda match: stash(
            _style_span("▣ ", f"{ANSI_BOLD}{ANSI_MAGENTA}", base_style=base_style)
            + _style_span(
                (match.group(2) or match.group(3) or "image").strip() or "image",
                f"{ANSI_BOLD}{ANSI_WHITE}",
                base_style=base_style,
            )
            + _style_span(
                f" <{(match.group(1) or match.group(4) or '').strip()}>",
                f"{ANSI_DIM}{ANSI_WHITE}",
                base_style=base_style,
            )
        ),
        rendered,
    )
    rendered = MARKDOWN_HTML_STRONG_RE.sub(
        lambda match: stash(_style_span(match.group(1), ANSI_BOLD, base_style=base_style)),
        rendered,
    )
    rendered = MARKDOWN_HTML_EM_RE.sub(
        lambda match: stash(_style_span(match.group(1), ANSI_ITALIC, base_style=base_style)),
        rendered,
    )
    rendered = MARKDOWN_HTML_DEL_RE.sub(
        lambda match: stash(_style_span(match.group(1), ANSI_DIM, base_style=base_style)),
        rendered,
    )
    rendered = MARKDOWN_HTML_CODE_RE.sub(
        lambda match: stash(
            _style_span(match.group(1), f"{ANSI_BOLD}{ANSI_GOLD}", base_style=base_style)
        ),
        rendered,
    )
    rendered = MARKDOWN_HTML_MARK_RE.sub(
        lambda match: stash(
            _style_span(match.group(1), f"{ANSI_BOLD}{ANSI_GOLD}", base_style=base_style)
        ),
        rendered,
    )
    rendered = MARKDOWN_HTML_UNDERLINE_RE.sub(
        lambda match: stash(_style_span(match.group(1), ANSI_UNDERLINE, base_style=base_style)),
        rendered,
    )
    rendered = MARKDOWN_HTML_STRIP_RE.sub("", rendered)
    rendered = _restore_escaped_markdown(rendered)
    for key, value in placeholders.items():
        rendered = rendered.replace(key, value)

    if base_style:
        return f"{base_style}{rendered}{ANSI_RESET}"
    return rendered


class MarkdownAnsiRenderer:
    def __init__(self, *, tty: bool) -> None:
        self.tty = tty
        self.render_width = _terminal_render_width()
        self._reset_render_state()

    def _reset_render_state(self) -> None:
        self.code_fence_marker = None
        self.reference_links = {}
        self.footnotes = {}
        self.referenced_footnotes = []
        self.emitted_footnotes: set[str] = set()

    def _resolve_reference_link(self, label: str, reference: str, *, image: bool) -> str:
        ref_key = _normalize_reference_key(reference or label)
        target = self.reference_links.get(ref_key, "").strip()
        if not target:
            if image:
                return f"▣ {label.strip() or 'image'}"
            return label.strip() or reference.strip()
        if image:
            return f"![{label}]({target})"
        return f"[{label}]({target})"

    def _preprocess_reference_style(self, text: str) -> str:
        def repl(match: re.Match[str]) -> str:
            if match.group(1) is not None:
                return self._resolve_reference_link(match.group(1), match.group(2), image=True)
            return self._resolve_reference_link(match.group(3), match.group(4), image=False)

        return MARKDOWN_REFERENCE_LINK_RE.sub(repl, text)

    def _preprocess_footnotes(self, text: str) -> str:
        def repl(match: re.Match[str]) -> str:
            label = match.group(1).strip()
            key = _normalize_reference_key(label)
            if key and key not in self.referenced_footnotes:
                self.referenced_footnotes.append(key)
            return _style_span(_footnote_marker(label), f"{ANSI_BOLD}{ANSI_MAGENTA}")

        return MARKDOWN_FOOTNOTE_REF_RE.sub(repl, text)

    def _preprocess_inline_text(self, text: str) -> str:
        return self._preprocess_footnotes(self._preprocess_reference_style(text))

    def _extract_reference_blocks(self, lines: list[str]) -> list[str]:
        cleaned_lines: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            ref_match = MARKDOWN_REFERENCE_DEF_RE.match(line)
            if ref_match:
                self.reference_links[_normalize_reference_key(ref_match.group(1))] = ref_match.group(2).strip()
                index += 1
                continue

            footnote_match = MARKDOWN_FOOTNOTE_DEF_RE.match(line)
            if footnote_match:
                key = _normalize_reference_key(footnote_match.group(1))
                parts = [footnote_match.group(2).rstrip()]
                index += 1
                while index < len(lines):
                    continuation = lines[index]
                    if continuation.startswith("    "):
                        parts.append(continuation[4:].rstrip())
                        index += 1
                        continue
                    if continuation.startswith("\t"):
                        parts.append(continuation[1:].rstrip())
                        index += 1
                        continue
                    if (
                        not continuation.strip()
                        and index + 1 < len(lines)
                        and (lines[index + 1].startswith("    ") or lines[index + 1].startswith("\t"))
                    ):
                        parts.append("")
                        index += 1
                        continue
                    break
                self.footnotes[key] = "\n".join(parts).strip()
                continue

            cleaned_lines.append(line)
            index += 1
        return cleaned_lines

    @staticmethod
    def _split_table_row(line: str) -> list[str]:
        text = line.strip()
        if text.startswith("|"):
            text = text[1:]
        if text.endswith("|"):
            text = text[:-1]
        cells = re.split(r"(?<!\\)\|", text)
        return [unescape(cell.replace(r"\|", "|").strip()) for cell in cells]

    def _is_table_separator(self, line: str) -> bool:
        cells = self._split_table_row(line)
        if not cells:
            return False
        return all(MARKDOWN_TABLE_ALIGN_RE.fullmatch(cell.replace(" ", "")) for cell in cells)

    def _render_table_lines(self, rows: list[list[str]]) -> list[str]:
        headers = rows[0]
        body_rows = rows[1:]
        col_count = max(len(row) for row in rows)
        headers = headers + [""] * (col_count - len(headers))
        body_rows = [row + [""] * (col_count - len(row)) for row in body_rows]

        if self.render_width < 72 or col_count > 3:
            rendered: list[str] = []
            if not body_rows:
                header_text = "▥ " + " · ".join(headers)
                rendered.append(_style_span(header_text, f"{ANSI_BOLD}{ANSI_CYAN}") if self.tty else header_text)
                return rendered
            for row_index, row in enumerate(body_rows, start=1):
                if row_index > 1:
                    rendered.append("")
                row_label = f"▥ Row {row_index}"
                rendered.append(_style_span(row_label, f"{ANSI_BOLD}{ANSI_CYAN}") if self.tty else row_label)
                for header, value in zip(headers, row):
                    label = header.strip() or f"Field {len(rendered)}"
                    prefix = f"{label}: "
                    rendered.append(
                        f"{_style_span(prefix, f'{ANSI_BOLD}{ANSI_CYAN}') if self.tty else prefix}"
                        f"{_render_inline_markdown(value or '—', tty=self.tty)}"
                    )
            return rendered

        max_cell_width = max(8, (self.render_width - (col_count - 1) * 3 - 4) // col_count)
        widths: list[int] = []
        for index in range(col_count):
            candidates = [headers[index], *[row[index] for row in body_rows]]
            widths.append(
                min(max(_display_width(cell) for cell in candidates if cell is not None), max_cell_width)
            )

        def render_table_row(cells: list[str], *, header: bool) -> str:
            parts: list[str] = []
            for idx, cell in enumerate(cells):
                plain = _truncate_display_ellipsis(cell or "—", widths[idx])
                styled = _render_inline_markdown(
                    self._preprocess_inline_text(plain),
                    base_style=f"{ANSI_BOLD}{ANSI_CYAN}" if header else "",
                    tty=self.tty,
                )
                parts.append(f" {styled}{' ' * max(0, widths[idx] - _display_width(plain))} ")
            return "│".join(parts).rstrip()

        rule = _style_span("╌╌", f"{ANSI_DIM}{ANSI_RULE}") if self.tty else "╌╌"
        rendered = [render_table_row(headers, header=True), rule]
        rendered.extend(render_table_row(row, header=False) for row in body_rows)
        return rendered

    def _render_line(self, line: str) -> str:
        stripped = MARKDOWN_HTML_BREAK_RE.sub("", line).strip()
        preprocessed_line = self._preprocess_inline_text(line)

        fence_match = re.match(r"^\s*(```+|~~~+)\s*(.*)$", stripped)
        if fence_match:
            marker = fence_match.group(1)[:3]
            if self.code_fence_marker:
                self.code_fence_marker = None
                return ""
            self.code_fence_marker = marker
            language = fence_match.group(2).strip()
            if not language:
                return _style_span("▌ code", f"{ANSI_DIM}{ANSI_MAGENTA}") if self.tty else "▌ code"
            code_label = f"▌ {language}"
            return _style_span(code_label, f"{ANSI_DIM}{ANSI_MAGENTA}") if self.tty else code_label

        if self.code_fence_marker:
            return _render_inline_markdown(preprocessed_line, base_style=f"{ANSI_BOLD}{ANSI_GOLD}" if self.tty else "", tty=self.tty)

        if not stripped:
            return ""

        heading = re.match(r"^(#{1,6})\s+(.*)$", preprocessed_line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            prefix = "■ " if level <= 2 else "▸ "
            base = (f"{ANSI_BOLD}{ANSI_CYAN}" if level <= 2 else f"{ANSI_BOLD}{ANSI_WHITE}") if self.tty else ""
            return _render_inline_markdown(f"{prefix}{title}", base_style=base, tty=self.tty)

        if re.fullmatch(r"\s*(?:[-*_]\s*){3,}\s*", line):
            rule = "╌╌"
            return _style_span(rule, f"{ANSI_DIM}{ANSI_RULE}") if self.tty else rule

        quote = re.match(r"^(\s*)((?:>\s*)+)(.*)$", preprocessed_line)
        if quote:
            indent = quote.group(1)
            depth = quote.group(2).count(">")
            body = _render_inline_markdown(quote.group(3), base_style=f"{ANSI_DIM}{ANSI_QUOTE}" if self.tty else "", tty=self.tty)
            marker = "│ " * max(1, depth)
            return f"{indent}{_style_span(marker, f'{ANSI_BOLD}{ANSI_QUOTE}') if self.tty else marker}{body}"

        task = re.match(r"^(\s*)[-+*]\s+\[([ xX])\]\s+(.*)$", preprocessed_line)
        if task:
            indent = task.group(1)
            level = max(0, len(indent.expandtabs(2)) // 2)
            marker = "☑ " if task.group(2).lower() == "x" else "☐ "
            body = _render_inline_markdown(task.group(3), tty=self.tty)
            bullet_style = f"{ANSI_BOLD}{ANSI_CYAN}" if level <= 1 else f"{ANSI_BOLD}{ANSI_WHITE}"
            return f"{indent}{_style_span(marker, bullet_style) if self.tty else marker}{body}"

        unordered = re.match(r"^(\s*)[-+*]\s+(.*)$", preprocessed_line)
        if unordered:
            indent = unordered.group(1)
            level = max(0, len(indent.expandtabs(2)) // 2)
            bullet = "• " if level <= 1 else "◦ " if level == 2 else "▪ "
            body = _render_inline_markdown(unordered.group(2), tty=self.tty)
            bullet_style = f"{ANSI_BOLD}{ANSI_CYAN}" if level <= 1 else f"{ANSI_BOLD}{ANSI_WHITE}"
            return f"{indent}{_style_span(bullet, bullet_style) if self.tty else bullet}{body}"

        ordered_task = re.match(r"^(\s*)(\d+)\.\s+\[([ xX])\]\s+(.*)$", preprocessed_line)
        if ordered_task:
            indent = ordered_task.group(1)
            number = ordered_task.group(2)
            marker = "☑ " if ordered_task.group(3).lower() == "x" else "☐ "
            body = _render_inline_markdown(ordered_task.group(4), tty=self.tty)
            number_text = f"{number}. "
            if self.tty:
                return f"{indent}{_style_span(number_text, f'{ANSI_BOLD}{ANSI_CYAN}')}{_style_span(marker, f'{ANSI_BOLD}{ANSI_CYAN}')}{body}"
            return f"{indent}{number_text}{marker}{body}"

        ordered = re.match(r"^(\s*)(\d+)\.\s+(.*)$", preprocessed_line)
        if ordered:
            indent = ordered.group(1)
            number = ordered.group(2)
            body = _render_inline_markdown(ordered.group(3), tty=self.tty)
            number_text = f"{number}. "
            return f"{indent}{_style_span(number_text, f'{ANSI_BOLD}{ANSI_CYAN}') if self.tty else number_text}{body}"

        return _render_inline_markdown(preprocessed_line, tty=self.tty)

    def _render_block(self, text: str) -> str:
        if not text:
            return ""
        normalized = MARKDOWN_HTML_BREAK_RE.sub("\n", text)
        chunks = normalized.splitlines(keepends=True)
        lines = [chunk[:-1] if chunk.endswith("\n") else chunk for chunk in chunks]
        lines = self._extract_reference_blocks(lines)
        rendered_lines: list[str] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            next_line = lines[index + 1] if index + 1 < len(lines) else None

            if (
                line.strip()
                and next_line is not None
                and MARKDOWN_SETEXT_H1_RE.fullmatch(next_line.strip())
            ):
                rendered_lines.append(
                    _render_inline_markdown(self._preprocess_inline_text(f"■ {line.strip()}"), base_style=f"{ANSI_BOLD}{ANSI_CYAN}", tty=True)
                )
                index += 2
                continue

            if (
                line.strip()
                and next_line is not None
                and MARKDOWN_SETEXT_H2_RE.fullmatch(next_line.strip())
                and "|" not in line
            ):
                rendered_lines.append(
                    _render_inline_markdown(self._preprocess_inline_text(f"▸ {line.strip()}"), base_style=f"{ANSI_BOLD}{ANSI_WHITE}", tty=True)
                )
                index += 2
                continue

            if (
                "|" in line
                and next_line is not None
                and self._is_table_separator(next_line)
            ):
                table_rows = [self._split_table_row(line)]
                index += 2
                while index < len(lines):
                    current = lines[index]
                    if not current.strip() or "|" not in current:
                        break
                    table_rows.append(self._split_table_row(current))
                    index += 1
                rendered_lines.extend(self._render_table_lines(table_rows))
                continue

            rendered_lines.append(self._render_line(line))
            index += 1

        visible_footnotes = [
            key
            for key in self.referenced_footnotes
            if key in self.footnotes and key not in self.emitted_footnotes
        ]
        if visible_footnotes:
            if rendered_lines and rendered_lines[-1] != "":
                rendered_lines.append("")
            rendered_lines.append(_style_span("Footnotes", f"{ANSI_BOLD}{ANSI_CYAN}"))
            for key in visible_footnotes:
                marker = _footnote_marker(key)
                body = self.footnotes[key].replace("\n", " / ")
                rendered_lines.append(
                    f"{_style_span(marker + ' ', f'{ANSI_BOLD}{ANSI_MAGENTA}')}"
                    f"{_render_inline_markdown(self._preprocess_inline_text(body), tty=True)}"
                )
                self.emitted_footnotes.add(key)

        return "\n".join(rendered_lines)

    def render(self, text: str) -> str:
        return self._render_block(_repair_markdown_list_boundaries(text))


def _find_safe_stream_split(text: str, *, force: bool) -> int:
    if not text:
        return 0
    if force:
        return len(text)

    safe_index = 0
    code_fence_open = False
    inline_code_open = False
    bold_open = False
    strike_open = False
    bracket_depth = 0
    paren_depth = 0
    index = 0
    length = len(text)

    while index < length:
        if text.startswith("```", index) or text.startswith("~~~", index):
            code_fence_open = not code_fence_open
            index += 3
            continue

        char = text[index]
        if char == "\\":
            index += 2
            continue

        if not code_fence_open and char == "`":
            inline_code_open = not inline_code_open
            index += 1
            continue

        if not code_fence_open and not inline_code_open and text.startswith("**", index):
            bold_open = not bold_open
            index += 2
            continue

        if not code_fence_open and not inline_code_open and text.startswith("~~", index):
            strike_open = not strike_open
            index += 2
            continue

        if not code_fence_open and not inline_code_open:
            if char == "[":
                bracket_depth += 1
            elif char == "]":
                bracket_depth = max(0, bracket_depth - 1)
            elif char == "(":
                paren_depth += 1
            elif char == ")":
                paren_depth = max(0, paren_depth - 1)

        if char == "\n":
            if not code_fence_open and not inline_code_open and not bold_open and not strike_open and bracket_depth == 0 and paren_depth == 0:
                safe_index = index + 1
        elif not code_fence_open and not inline_code_open and not bold_open and not strike_open and bracket_depth == 0 and paren_depth == 0:
            if char in STREAM_SENTENCE_ENDINGS:
                safe_index = index + 1
            elif char in "，、," and index + 1 >= 24:
                safe_index = index + 1
            elif char.isspace() and index + 1 >= 40:
                safe_index = index + 1
            elif index + 1 == length and length >= 64:
                safe_index = index + 1

        index += 1

    if safe_index > 0:
        return safe_index

    if (
        not code_fence_open
        and not inline_code_open
        and not bold_open
        and not strike_open
        and bracket_depth == 0
        and paren_depth == 0
        and length >= 80
    ):
        for index in range(length - 1, max(0, length - 32), -1):
            if text[index].isspace():
                return index + 1
        return max(1, length - 16)

    return 0


def _stream_has_block_markdown(text: str) -> bool:
    return bool(MARKDOWN_STREAM_BLOCK_RE.search(text or ""))


def _stream_fence_is_open(text: str) -> bool:
    marker: str | None = None
    for raw_line in str(text or "").splitlines():
        stripped = raw_line.strip()
        match = re.match(r"^(```+|~~~+)", stripped)
        if not match:
            continue
        current = match.group(1)[:3]
        if marker == current:
            marker = None
        elif marker is None:
            marker = current
    return marker is not None


def _markdown_stream_flush_index(text: str) -> int:
    if not text:
        return 0
    if not _stream_has_block_markdown(text):
        return len(text)

    best = 0
    search_from = 0
    while True:
        boundary = text.find("\n\n", search_from)
        if boundary < 0:
            break
        end = boundary + 2
        if not _stream_fence_is_open(text[:end]):
            best = end
        search_from = end
    return best


class StreamingTextSanitizer:
    def __init__(self) -> None:
        self.pending = ""
        self.leading_stage_checked = False
        self.emitted_visible = False

    def _strip_leading_stage_direction(self, *, force: bool) -> None:
        if self.leading_stage_checked or self.emitted_visible:
            return

        stripped = self.pending.lstrip()
        leading_ws = self.pending[: len(self.pending) - len(stripped)]
        if not stripped:
            self.pending = leading_ws
            return

        opener = stripped[0]
        if opener not in {"（", "("}:
            self.leading_stage_checked = True
            return

        closer = "）" if opener == "（" else ")"
        closing_index = stripped.find(closer)
        if closing_index < 0:
            if not force and len(stripped) <= 48 and "\n" not in stripped:
                return
            self.leading_stage_checked = True
            return

        candidate = stripped[1:closing_index]
        if 0 < len(candidate) <= 24 and "\n" not in candidate:
            remainder = stripped[closing_index + 1 :].lstrip("，,。.!！？、；;:： \t")
            self.pending = leading_ws + remainder
        self.leading_stage_checked = True

    def _normalize_pending(self, *, force: bool) -> None:
        self._strip_leading_stage_direction(force=force)
        self.pending = self.pending.replace("\r", "")
        self.pending = _collapse_blank_lines(self.pending)

    def push(self, text: str) -> str:
        if not text:
            return ""
        self.pending += text
        self._normalize_pending(force=False)

        split_index = _find_safe_stream_split(self.pending, force=False)
        if split_index <= 0:
            return ""

        ready = self.pending[:split_index]
        self.pending = self.pending[split_index:]
        if ready.strip():
            self.emitted_visible = True
        return ready

    def finish(self) -> str:
        self._normalize_pending(force=True)
        ready = self.pending
        self.pending = ""
        if ready.strip():
            self.emitted_visible = True
        return ready


def _prompt_line(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print("")
        return ""


def _prompt_menu_choice(exit_hint: str = "0 返回") -> str:
    width = _compact_render_width()
    print("")
    _print_fit(f"选择 · {exit_hint}", width=width)
    return _prompt_line("› ").strip()


def _render_assistant_block(
    text: str,
    role: LauncherRole | None = None,
    *,
    persona_bundle: PersonaBundle | None = None,
) -> str:
    if role is None:
        return f"\n{_style_heading('● PROJECT凌', 'deepseek')}\n{_indent_block(text)}\n"
    return f"\n{_format_role_heading(role, persona_bundle)}\n\n{_indent_block(text)}\n"


def _pick_model_interactive(current_model: str) -> str | None:
    print("")
    for index, (model_name, desc) in enumerate(MODEL_CHOICES, start=1):
        marker = "  当前" if model_name == current_model else ""
        print(f"{index}. {model_name}  {desc}{marker}")
    print("3. 自定义输入")
    picked = _prompt_line("选择模型 > ").strip()
    if not picked:
        return None
    if picked in {"1", "2"}:
        return MODEL_CHOICES[int(picked) - 1][0]
    if picked == "3":
        custom = _prompt_line("输入模型名 > ").strip()
        return custom or None
    print("无效输入，保持原样。")
    return None


def _unique_model_ids(ids: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for model_id in ids:
        normalized = str(model_id or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


def _configured_role_model(config: ProjectLingConfig, role: str) -> str:
    provider = _api_provider_value(getattr(config, "api_provider", ""))
    if provider == "gemini":
        if role == "planner":
            return str(getattr(config, "gemini_planner_model", GEMINI_PRECISE_MODEL) or GEMINI_PRECISE_MODEL)
        return str(getattr(config, "gemini_executor_model", GEMINI_FAST_MODEL) or GEMINI_FAST_MODEL)
    if role == "planner":
        return str(getattr(config, "deepseek_planner_model", DEEPSEEK_PRECISE_MODEL) or DEEPSEEK_PRECISE_MODEL)
    return str(getattr(config, "deepseek_executor_model", DEEPSEEK_FAST_MODEL) or DEEPSEEK_FAST_MODEL)


def _model_update_key(config: ProjectLingConfig, role: str) -> str:
    provider = _api_provider_value(getattr(config, "api_provider", ""))
    if provider == "gemini":
        return "GEMINI_PLANNER_MODEL" if role == "planner" else "GEMINI_EXECUTOR_MODEL"
    return "DEEPSEEK_PLANNER_MODEL" if role == "planner" else "DEEPSEEK_EXECUTOR_MODEL"


def _role_label(role: str) -> str:
    return "主星" if role == "planner" else "执行星"


def _pick_model_from_list(
    *,
    title: str,
    ids: Sequence[str],
    current_model: str,
    planner_model: str,
    executor_model: str,
) -> str | None:
    width = _compact_render_width()
    model_ids = _unique_model_ids(ids)
    if current_model and current_model not in model_ids:
        model_ids.insert(0, current_model)
    if not model_ids:
        _print_fit("没有可选模型。", width=width)
        return None
    _print_fit(title, width=width)
    _print_fit("输入编号选择；0 或空输入保持原样。", width=width)
    for index, model_id in enumerate(model_ids, start=1):
        marker = " / ".join(_model_list_marker_parts(model_id, planner_model, executor_model, width=width))
        if model_id == current_model and "当前" not in marker:
            marker = (f"{marker} / 当前" if marker else "当前").strip(" /")
        _print_indexed_model(index, model_id, marker, width=width)
    raw_choice = _prompt_line("选择模型 > ").strip()
    if not raw_choice or raw_choice == "0":
        return None
    try:
        choice = int(raw_choice)
    except ValueError:
        _print_setting_rejected("请输入列表编号。", keep_label="保留", keep_value=current_model, width=width)
        return None
    if choice < 1 or choice > len(model_ids):
        _print_setting_rejected("编号不在列表范围内。", keep_label="保留", keep_value=current_model, width=width)
        return None
    return model_ids[choice - 1]


def _pick_provider_model_interactive(config: ProjectLingConfig, role: str) -> str | None:
    width = _compact_render_width()
    provider = _api_provider_value(getattr(config, "api_provider", ""))
    role_text = _role_label(role)
    current_model = _configured_role_model(config, role)
    planner_model = _configured_role_model(config, "planner")
    executor_model = _configured_role_model(config, "executor")
    print("")
    if provider == "gemini":
        _print_fit(f"Gemini {role_text}模型", width=width)
        if not getattr(config, "api_key", None):
            _print_fit("未设置 GEMINI_API_KEY。", width=width)
            _print_fit("下一步：先写入 Gemini API Key。", width=width)
            return None
        _print_fit("正在从当前 relay 拉取模型列表...", width=width)
        try:
            payload = DeepSeekClient(config).list_models()
        except Exception as exc:
            _print_fit("模型列表拉取失败。", width=width)
            _print_fit_wrapped(exc, width=width)
            _print_fit("下一步：检查 API Key、Base URL、模型列表接口和网络。", width=width)
            return None
        ids = _extract_model_ids(payload)
        if not ids:
            _print_fit("模型列表为空或响应结构不含 data[].id。", width=width)
            return None
        _print_model_taxonomy_hint(ids, width=width)
        return _pick_model_from_list(
            title=f"选择 Gemini {role_text}模型",
            ids=ids,
            current_model=current_model,
            planner_model=planner_model,
            executor_model=executor_model,
        )

    ids = [model_id for model_id, _desc in DEEPSEEK_SETTINGS_MODEL_CHOICES]
    _print_fit(f"选择 DeepSeek {role_text}模型", width=width)
    for model_id, desc in DEEPSEEK_SETTINGS_MODEL_CHOICES:
        _print_fit(f"- {model_id} · {desc}", width=width)
    return _pick_model_from_list(
        title="DeepSeek 可选模型",
        ids=ids,
        current_model=current_model,
        planner_model=planner_model,
        executor_model=executor_model,
    )


def _save_config_value(config: ProjectLingConfig, updates: dict[str, str | None]) -> ProjectLingConfig:
    save_env_config(updates, path=config.env_file_path)
    return load_config()


def _collab_mode_input_value(raw: str | None) -> str | None:
    value = str(raw or "").strip().lower()
    if not value:
        return None
    value = COLLAB_MODE_ALIASES.get(value, value)
    if value in COLLAB_MODE_ORDER:
        return value
    return None


def _collab_mode_next_value(current: str | None) -> str:
    normalized = _collab_mode_value(current)
    try:
        current_index = COLLAB_MODE_ORDER.index(normalized)
    except ValueError:
        current_index = 0
    return COLLAB_MODE_ORDER[(current_index + 1) % len(COLLAB_MODE_ORDER)]


def _collab_mode_models(mode: str, config: ProjectLingConfig | None = None) -> tuple[str, str]:
    active_config = config or load_config()
    engine = ProjectLingEngine(active_config)
    return engine._planner_model_for_mode(mode), engine._executor_model_for_mode(mode)


def _provider_label(config: ProjectLingConfig) -> str:
    provider = _api_provider_value(getattr(config, "api_provider", ""))
    return "Gemini" if provider == "gemini" else "DeepSeek"


def _collab_mode_models_legacy(mode: str) -> tuple[str, str]:
    normalized = _collab_mode_value(mode)
    planner = "deepseek-v4-pro" if normalized == "precise" else "deepseek-v4-flash"
    executor = "deepseek-v4-pro" if normalized == "precise" else "deepseek-v4-flash"
    return planner, executor


def _collab_mode_detail(mode: str, config: ProjectLingConfig | None = None) -> str:
    normalized = _collab_mode_value(mode)
    label = next((name for value, name, _desc in COLLAB_MODE_CHOICES if value == normalized), normalized)
    desc = next((desc for value, _name, desc in COLLAB_MODE_CHOICES if value == normalized), "")
    if config is not None:
        planner, executor = _collab_mode_models(normalized, config)
        desc = f"{desc} · {planner} -> {executor}"
    return f"{label} · {desc}".strip(" ·")


def _render_model_mode_menu(current: ProjectLingConfig) -> None:
    current_mode = _collab_mode_value(current.collab_mode)
    print("")
    print("协作模式")
    print(f"Provider：{_provider_label(current)}")
    print(f"当前：{_collab_mode_detail(current_mode, current)}")
    for index, (mode, label, desc) in enumerate(COLLAB_MODE_CHOICES, start=1):
        marker = "  当前" if mode == current_mode else ""
        planner, executor = _collab_mode_models(mode, current)
        print(f"{index}. {label}  {desc} · {planner} -> {executor}{marker}")
    print("0. 返回")


def _apply_collab_mode(config: ProjectLingConfig, raw_mode: str | None) -> int:
    raw_value = str(raw_mode or "").strip().lower()
    current_mode = _collab_mode_value(config.collab_mode)
    if raw_value in COLLAB_MODE_STATUS_ALIASES:
        print(f"当前协作模式：{_collab_mode_detail(current_mode, config)}")
        return 0
    if raw_value in COLLAB_MODE_CYCLE_ALIASES:
        mode = _collab_mode_next_value(current_mode)
    else:
        mode = _collab_mode_input_value(raw_mode)
    if mode is None:
        print("无效模式。可用：快速 / 标准 / 精确，输入 1 / 2 / 3，或输入 next 轮换。")
        return 1
    _save_config_value(config, {"PROJECTLING_COLLAB_MODE": mode})
    if mode == current_mode:
        print(f"协作模式保持：{_collab_mode_detail(mode, config)}")
    else:
        print(f"协作模式已更新：{_collab_mode_detail(mode, config)}")
    return 0


def _run_model_mode_ui(mode_arg: str = "") -> int:
    if str(mode_arg or "").strip():
        return _apply_collab_mode(load_config(), mode_arg)

    while True:
        current = load_config()
        _render_model_mode_menu(current)
        choice = _prompt_menu_choice("0 返回")
        if choice == "0" or not choice:
            return 0
        if _apply_collab_mode(current, choice) == 0:
            return 0


def _prompt_optional_text(prompt: str) -> str | None:
    value = _prompt_line(prompt)
    if not value.strip():
        return None
    return value.strip()


def _api_provider_input_value(raw: str | None) -> str | None:
    value = str(raw or "").strip().lower()
    if not value:
        return None
    aliases = {
        "ds": "deepseek",
        "deepseek-v4": "deepseek",
        "gemini-openai": "gemini",
        "gemini-relay": "gemini",
    }
    value = aliases.get(value, value)
    return value if value in {"deepseek", "gemini"} else None


def _choose_provider_interactive(current: ProjectLingConfig) -> str | None:
    width = _compact_render_width()
    current_provider = _api_provider_value(getattr(current, "api_provider", ""))
    print("")
    _print_fit("选择服务商", width=width)
    _print_setting_option(1, "Gemini 中转站", "推荐" if current_provider == "gemini" else "", width=width)
    _print_setting_option(2, "DeepSeek", "当前" if current_provider == "deepseek" else "", width=width)
    _print_setting_option(0, "保持不变", width=width)
    raw = _prompt_menu_choice("空输入取消").lower()
    if not raw or raw == "0":
        return None
    aliases = {
        "1": "gemini",
        "g": "gemini",
        "gemini": "gemini",
        "2": "deepseek",
        "d": "deepseek",
        "ds": "deepseek",
        "deepseek": "deepseek",
        "deepseek-v4": "deepseek",
    }
    provider = aliases.get(raw)
    if provider is None:
        _print_setting_rejected("请选择 1 或 2。", keep_label="保留", keep_value=current_provider, width=width)
    return provider


def _choose_base_url_interactive(current: ProjectLingConfig) -> str | None:
    width = _compact_render_width()
    provider = _api_provider_value(getattr(current, "api_provider", ""))
    current_url = str(current.base_url or "").strip()
    print("")
    _print_fit("选择中转站", width=width)
    if provider == "gemini":
        _print_setting_option(1, "New API 中转站", GEMINI_DEFAULT_BASE_URL, width=width)
        _print_setting_option(2, "保持当前", current_url or "未设置", width=width)
        _print_setting_option(3, "手动输入", "高级", width=width)
        raw = _prompt_menu_choice("空输入保持原样")
        if not raw or raw == "0" or raw == "2":
            return None
        if raw == "1":
            return GEMINI_DEFAULT_BASE_URL
        if raw == "3":
            custom = _prompt_line("输入 Base URL > ").strip()
            return custom or None
        if raw.startswith(("http://", "https://")):
            return raw
        _print_setting_rejected("请选择列表编号。", keep_label="保留", keep_value=current_url, width=width)
        return None

    default_url = "https://api.deepseek.com"
    _print_setting_option(1, "DeepSeek 默认", default_url, width=width)
    _print_setting_option(2, "保持当前", current_url or "未设置", width=width)
    _print_setting_option(3, "手动输入", "高级", width=width)
    raw = _prompt_menu_choice("空输入保持原样")
    if not raw or raw == "0" or raw == "2":
        return None
    if raw == "1":
        return default_url
    if raw == "3":
        custom = _prompt_line("输入 Base URL > ").strip()
        return custom or None
    if raw.startswith(("http://", "https://")):
        return raw
    _print_setting_rejected("请选择列表编号。", keep_label="保留", keep_value=current_url, width=width)
    return None


def _choose_role_ttl_hours(current: ProjectLingConfig) -> int | None:
    width = _compact_render_width()
    choices = (1, 4, 8, 12, 24, 48)
    print("")
    _print_fit("角色停留时长", width=width)
    for index, hours in enumerate(choices, start=1):
        marker = "当前" if int(current.role_ttl_hours or 0) == hours else ""
        _print_setting_option(index, f"{hours} 小时", marker, width=width)
    _print_setting_option(0, "保持不变", width=width)
    raw = _prompt_menu_choice("空输入保持原样")
    if not raw or raw == "0":
        return None
    if raw.isdigit():
        value = int(raw)
        if 1 <= value <= len(choices):
            return choices[value - 1]
        if 1 <= value <= 48:
            return value
    _print_setting_rejected("请选择 1-6。", keep_label="保留", keep_value=f"{current.role_ttl_hours}h", width=width)
    return None


def _prompt_float(prompt: str, *, min_value: float, max_value: float, allow_empty_clear: bool = False) -> float | None | str:
    raw = _prompt_line(prompt).strip()
    if not raw:
        return "" if allow_empty_clear else None
    try:
        value = float(raw)
    except ValueError:
        _print_setting_rejected("请输入数字。")
        return None
    if value < min_value or value > max_value:
        _print_setting_rejected(f"需要在 {min_value:g} - {max_value:g} 之间。")
        return None
    return value


def _prompt_int(prompt: str, *, min_value: int, allow_empty_clear: bool = False) -> int | None | str:
    raw = _prompt_line(prompt)
    if not raw.strip():
        return "" if allow_empty_clear else None
    try:
        value = int(raw.strip())
    except ValueError:
        _print_setting_rejected("请输入整数。")
        return None
    if value < min_value:
        _print_setting_rejected(f"需要大于等于 {min_value}。")
        return None
    return value


def _bool_label(value: bool) -> str:
    return "开" if value else "关"


def _mask_key_preview(value: str | None, *, head: int = 6, tail: int = 5) -> str:
    text = str(value or "").strip()
    if not text:
        return "未设置"
    if len(text) <= 4:
        return f"{text[:1]}...{text[-1:]}" if len(text) > 1 else "*"
    if len(text) <= head + tail + 3:
        return f"{text[:2]}...{text[-2:]}"
    return f"{text[:head]}...{text[-tail:]}"


def _key_status(value: str | None) -> str:
    return _mask_key_preview(value)


def _tool_round_limit_label(value: int) -> str:
    rounds = max(0, int(value or 0))
    return "UNLIMITED" if rounds == 0 else str(rounds)


def _typewriter_bulk_threshold() -> int:
    raw = os.environ.get("PROJECTLING_TYPEWRITER_BULK_CHARS", str(TYPEWRITER_BULK_CHARS))
    try:
        return max(0, int(raw or str(TYPEWRITER_BULK_CHARS)))
    except ValueError:
        return TYPEWRITER_BULK_CHARS


def _render_command_help() -> None:
    width = _compact_render_width()
    entries = [
        ("/mode", "轮换协作模式；/mode 2 可直接切标准模式", "切协作模式"),
        ("/model", "打开协作模式菜单（兼容旧入口）", "模式菜单"),
        ("/send", "直接发送消息给执行星", "发给执行星"),
        ("/settings", "打开中文设置中心", "设置"),
        ("/settings api", "服务商 / Key / 中转站 / 双星模型", "API 和模型"),
        ("/settings websearch", "搜索 Key / 地址 / 测试", "搜索设置"),
        ("/models", "拉取当前服务商模型列表", "模型列表"),
        ("/api-test", "测试主星/辅星连通", "连通测试"),
        ("/codexurl", "打开 codexurl", "codexurl"),
        ("/help", "显示帮助", "帮助"),
        ("./run.sh cleanup", "清理日志/临时包；--deep 同时清 bytecode", "清理日志"),
    ]
    print("")
    _print_fit("PROJECT凌", width=width)
    if width >= 56:
        for command, description, _compact in entries:
            _print_fit(f"  {command:<18} {description}", width=width)
        return
    for command, _description, compact in entries:
        if width < 24 and " " in command:
            command_parts = command.split()
            _print_fit(f"  {command_parts[0]}", width=width)
            for part in command_parts[1:]:
                _print_fit(f"    {part}", width=width)
        else:
            _print_fit_wrapped(f"  {command}", width=width, max_lines=2)
        _print_fit(f"    {compact}", width=width)


def _render_settings_root(current: ProjectLingConfig) -> None:
    planner_model, executor_model = _collab_mode_models(current.collab_mode, current)
    width = _compact_render_width()
    print("")
    _print_fit("设置中心", width=width)
    mode_label = _collab_mode_detail(current.collab_mode, current).split(" · ")[0]
    _print_setting_pair("当前", f"{_provider_label(current)} · {mode_label}", width=width)
    _print_setting_pair("主星", planner_model, width=width)
    _print_setting_pair("执行", executor_model, width=width)
    _print_setting_section("设置", width=width)
    _print_setting_option(1, "API 与模型", width=width)
    _print_setting_option(2, "搜索", width=width)
    _print_setting_option(3, "系统", width=width)
    _print_setting_option(0, "返回", width=width)


def _render_api_settings(current: ProjectLingConfig) -> None:
    max_tokens_text = str(current.max_tokens) if current.max_tokens is not None else "自动"
    provider = _api_provider_value(getattr(current, "api_provider", ""))
    planner_model, executor_model = _collab_mode_models(current.collab_mode, current)
    width = _compact_render_width()
    top_p_text = current.gemini_top_p if current.gemini_top_p is not None else "自动"
    top_k_text = current.gemini_top_k if current.gemini_top_k is not None else "自动"
    if width < 24:
        gemini_param_summary = "自动" if top_p_text == "自动" and top_k_text == "自动" else f"p={top_p_text} k={top_k_text}"
    else:
        gemini_param_summary = f"top_p={top_p_text} / top_k={top_k_text}"
    print("")
    _print_fit("API 与模型", width=width)
    _print_setting_pair("当前", _provider_label(current), width=width)
    _print_setting_pair("主星", planner_model, width=width)
    _print_setting_pair("执行", executor_model, width=width)

    _print_setting_section("连接", width=width)
    _print_setting_option(1, "服务商", _provider_label(current), width=width)
    _print_setting_option(2, "密钥", "已设置" if current.api_key else "未设置", width=width)
    _print_setting_option(3, "地址", current.base_url, width=width)
    _print_setting_option(6, "模型列表", width=width)
    _print_setting_option(7, "连通测试", width=width)

    _print_setting_section("模型", width=width)
    _print_setting_option(4, "主星模型", planner_model, width=width)
    _print_setting_option(5, "执行模型", executor_model, width=width)

    _print_setting_section("生成", width=width)
    _print_setting_option(8, "流式", _bool_label(current.enable_sse), width=width)
    _print_setting_option(9, "输出上限", max_tokens_text, width=width)
    _print_setting_option(10, "温度", f"{current.temperature:g}", width=width)
    _print_setting_option(13, "推理", current.gemini_reasoning_effort if provider == "gemini" else current.reasoning_effort, width=width)
    if provider == "gemini":
        _print_setting_option(14, "更多参数", gemini_param_summary, width=width)

    _print_setting_section("运行", width=width)
    _print_setting_option(11, "超时", f"{current.timeout_seconds:g} 秒", width=width)
    _print_setting_option(12, "重试", current.retry_count, width=width)
    _print_setting_option(0, "返回", width=width)


def _render_system_settings(current: ProjectLingConfig) -> None:
    width = _compact_render_width()
    print("")
    if width < 34:
        _print_fit("系统设置", width=width)
        _print_setting_option(1, "角色停留时长", f"{current.role_ttl_hours}h", width=width)
        _print_setting_option(2, "协作模式", current.collab_mode, width=width)
        _print_setting_option(0, "返回上级", width=width)
        return
    print("系统设置")
    print(f"1. 角色停留时长  [{current.role_ttl_hours}h]")
    print(f"2. 协作模式      [{current.collab_mode}]")
    print("0. 返回上级")


def _render_websearch_settings(current: ProjectLingConfig) -> None:
    width = _compact_render_width()
    print("")
    _print_fit("搜索设置 / WEBSEARCH API", width=width)
    _print_setting_option(1, "摘要 Key", _key_status(current.websearch_summary_key), width=width)
    _print_setting_option(2, "网页 Key", _key_status(current.websearch_web_key), width=width)
    _print_setting_option(3, "接口地址", current.websearch_endpoint, width=width)
    _print_setting_option(4, "测试摘要搜索", width=width)
    _print_setting_option(5, "测试网页搜索", width=width)
    _print_setting_option(0, "返回上级", width=width)


def _run_websearch_test(config: ProjectLingConfig, *, mode: str) -> None:
    print("")
    width = _compact_render_width()
    query = _prompt_line("输入测试搜索词，留空使用默认 > ").strip() or "AI 大模型 最新热点"
    registry = ToolRegistry(config)
    context = ToolContext(cwd=Path.cwd(), home=Path.home(), config=config)
    tool_call = {
        "id": f"settings-websearch-{mode}",
        "function": {
            "name": "web_search",
            "arguments": json.dumps({"query": query, "mode": mode, "count": 3}, ensure_ascii=False),
        },
    }
    payload = registry.execute_tool_call(tool_call, context)
    try:
        result = json.loads(str(payload.get("content") or "{}"))
    except json.JSONDecodeError:
        _print_fit_wrapped("测试失败：工具返回不是合法 JSON。", width=width)
        _print_next_action_check(["工具返回", "日志", "Endpoint"], width=width)
        return

    status = str(result.get("status") or "")
    _print_setting_pair("status", status or "unknown", width=width)
    _print_setting_pair("mode", result.get("mode_used") or mode, width=width)
    _print_setting_pair("count", result.get("result_count", 0), width=width)
    if result.get("message"):
        _print_fit("message", width=width)
        _print_fit_wrapped(result.get("message"), width=width)
    if status and status != "ok":
        _print_next_action_check(["Summary Key", "Web Key", "Endpoint", "网络"], width=width)
    summary = str(result.get("summary") or "").strip()
    if summary:
        _print_setting_pair("summary", f"{summary[:220]}{'…' if len(summary) > 220 else ''}", width=width)
    for index, item in enumerate(result.get("results") or [], start=1):
        if index > 3:
            break
        _print_setting_pair(f"{index}.", item.get("title") or "", width=width)
        if item.get("url"):
            _print_setting_pair("url", item.get("url"), width=width)


def _run_websearch_settings_ui() -> None:
    while True:
        current = load_config()
        _render_websearch_settings(current)
        choice = _prompt_menu_choice("0 返回上级")

        if choice == "1":
            key = _prompt_line("输入 VOLC_WEBSEARCH_SUMMARY_KEY，留空保持原样 > ").strip()
            if key:
                _save_config_value(current, {"VOLC_WEBSEARCH_SUMMARY_KEY": key})
                _print_setting_saved("摘要 Key", "已写入")
            else:
                _print_setting_unchanged("摘要 Key 未修改。")
            continue

        if choice == "2":
            key = _prompt_line("输入 VOLC_WEBSEARCH_WEB_KEY，留空保持原样 > ").strip()
            if key:
                _save_config_value(current, {"VOLC_WEBSEARCH_WEB_KEY": key})
                _print_setting_saved("网页 Key", "已写入")
            else:
                _print_setting_unchanged("网页 Key 未修改。")
            continue

        if choice == "3":
            endpoint = _prompt_optional_text("输入 WebSearch Endpoint，留空保持原样 > ")
            if endpoint is not None:
                _save_config_value(current, {"VOLC_WEBSEARCH_ENDPOINT": endpoint})
                _print_setting_saved("接口地址", endpoint)
            else:
                _print_setting_unchanged("接口地址未修改。")
            continue

        if choice == "4":
            _run_websearch_test(current, mode="summary")
            continue

        if choice == "5":
            _run_websearch_test(current, mode="web")
            continue

        if choice == "0" or not choice:
            return

        _print_fit("无效输入。")


def _run_persona_settings_ui(config: ProjectLingConfig | None = None) -> int:
    current = config or load_config()
    roster = load_roster(current)
    while True:
        active_role, _seed = resolve_current_role(current)
        bundle = resolve_persona_bundle(current, role=active_role)
        locked = is_role_locked(current)
        remaining_text = "已锁定" if locked else _format_remaining_text(_remaining_seconds_for_role(current, active_role))
        width = _compact_render_width()
        print("")
        if width < 60:
            _print_fit("角色", width=width)
            _print_setting_pair("主星", f"{active_role.name_zh} / {active_role.name_en}", width=width)
            _print_setting_pair(
                "执行星",
                f"{bundle.liaison.name_zh} / {bundle.liaison.name_en}" if bundle.liaison is not None else "未配置",
                width=width,
            )
            _print_setting_pair("角色状态", remaining_text, width=width)
            _print_setting_option(1, "重新抽卡", width=width)
            _print_setting_option(2, "解锁角色" if locked else "锁定角色", width=width)
            _print_setting_option(3, "选择主星", width=width)
            _print_setting_option(4, "选择执行星", width=width)
            _print_setting_option(5, "停留时间", f"{current.role_ttl_hours}h", width=width)
            _print_setting_option(6, "角色列表", width=width)
            _print_setting_option(0, "返回", width=width)
        else:
            print("角色")
            print(f"当前主星: {active_role.name_zh} / {active_role.name_en}")
            print(f"当前执行星: {bundle.liaison.name_zh} / {bundle.liaison.name_en}" if bundle.liaison is not None else "当前执行星: 未配置")
            print(f"角色状态: {remaining_text}")
            print("1. 重新抽卡")
            print("2. " + ("解锁角色" if locked else "锁定角色"))
            print("3. 选择主星")
            print("4. 选择执行星")
            print(f"5. 停留时间 [{current.role_ttl_hours}h]")
            print("6. 角色列表")
            print("0. 返回")
        choice = _prompt_menu_choice("0 返回")

        if choice == "1":
            picked, _sequence_seed = reroll_active_role(current)
            print(f"已重新抽取角色：{picked.name_zh} / {picked.name_en}")
            current = load_config()
            continue

        if choice == "2":
            role, _sequence_seed = set_role_locked(not locked, current)
            print(f"已{'锁定' if not locked else '解锁'}角色：{role.name_zh} / {role.name_en}")
            current = load_config()
            continue

        if choice == "3":
            picked = _pick_role_from_roster(
                roster,
                header="手动选择主星角色",
                current_role=active_role,
            )
            if picked is None:
                print("未选择角色。")
                continue
            select_current_role_by_name(picked.name_en, current)
            print(f"已选择主星：{picked.name_zh} / {picked.name_en}")
            current = load_config()
            continue

        if choice == "4":
            picked = _pick_role_from_roster(
                roster,
                header="手动选择执行星角色",
                current_role=bundle.liaison,
            )
            if picked is None:
                print("未选择角色。")
                continue
            if picked.name_en == active_role.name_en:
                print("执行星不能与主星相同。")
                continue
            select_liaison_role_by_name(picked.name_en, current)
            print(f"已选择执行星：{picked.name_zh} / {picked.name_en}")
            current = load_config()
            continue

        if choice == "5":
            role_hours = _choose_role_ttl_hours(current)
            if isinstance(role_hours, int):
                _save_config_value(current, {"PROJECTLING_ROLE_TTL_HOURS": str(role_hours)})
                print(f"角色停留时间已更新：{role_hours}h")
                current = load_config()
            continue

        if choice == "6":
            print("")
            for index, role in enumerate(roster, start=1):
                print(f"{index:02d}. [{role.rarity}] {role.name_zh} / {role.name_en}")
            continue

        if choice == "0" or not choice:
            return 0

        print("无效输入。")


def _resolve_role_from_input(roster: list[LauncherRole], raw: str) -> LauncherRole | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(roster):
            return roster[index - 1]
    lowered = text.lower()
    for role in roster:
        if role.name_en.lower() == lowered or role.name_zh.lower() == lowered:
            return role
    return None


def _pick_role_from_roster(
    roster: list[LauncherRole],
    *,
    prompt: str = "输入角色序号或名称 > ",
    header: str = "PERSONA",
    current_role: LauncherRole | None = None,
) -> LauncherRole | None:
    print("")
    print(header)
    for index, role in enumerate(roster, start=1):
        marker = "  当前" if current_role is not None and role.name_en == current_role.name_en else ""
        print(f"{index:02d}. [{role.rarity}] {role.name_zh} / {role.name_en}{marker}")
    picked = _prompt_line(prompt).strip()
    return _resolve_role_from_input(roster, picked)


def _begin_route_status(printer: ShellStreamPrinter, route: dict[str, Any]) -> bool:
    show_status = route.get("show_initial_status")
    if show_status is None:
        category = str(route.get("category") or "")
        show_status = bool(route.get("thinking_enabled")) or category not in {"casual_chat", "strict_short_reply"}
    if not bool(show_status):
        return False
    printer.begin("thinking" if bool(route.get("thinking_enabled")) else "responding")
    return True


def _run_role_chat(
    config: ProjectLingConfig,
    role: LauncherRole,
    message: str,
    *,
    cwd: str | Path,
    allow_tools: bool,
    stream: bool,
    as_json: bool,
    persona_bundle: PersonaBundle | None = None,
) -> int:
    engine = ProjectLingEngine(config)
    selected_bundle = persona_bundle or PersonaBundle(main=role, source="direct")
    current_cwd = Path(cwd).expanduser()
    route = engine.preview_route(message, allow_tools=allow_tools, dispatch_mode="chat")
    use_stream = bool(stream and not as_json)

    if use_stream:
        printer = ShellStreamPrinter(
            engine.prompt_bundle,
            role,
            persona_bundle=selected_bundle,
            context_budget=load_context_budget(config),
        )
        _begin_route_status(printer, route)
        try:
            result = engine.chat(
                message,
                cwd=current_cwd,
                mode="chat",
                allow_tools=allow_tools,
                stream=True,
                on_stream_delta=printer.on_delta,
                on_stream_event=printer.on_event,
                role_override=role,
                persona_bundle_override=selected_bundle,
            )
        except KeyboardInterrupt:
            printer.emit_message("已中断。")
            printer.finish("")
            return 130
        except Exception as exc:  # pragma: no cover - CLI safety net
            printer.emit_message(f"运行失败：{exc}")
            printer.finish("")
            return 1
        if not result.text and not result.tool_traces:
            if result.finish_reason == "stream_limit":
                printer.finish("本轮输出已达到上限。")
            else:
                printer.finish("我没有得到有效回复。")
        else:
            printer.finish(result.text or "")
        return 0

    result = engine.chat(
        message,
        cwd=current_cwd,
        mode="chat",
        allow_tools=allow_tools,
        role_override=role,
        persona_bundle_override=selected_bundle,
    )
    if as_json:
        display_bundle = result.persona_bundle or selected_bundle
        print(
            json.dumps(
                {
                    "text": result.text,
                    "reasoning_text": result.reasoning_text,
                    "rounds": result.rounds,
                    "used_tools": result.used_tools,
                    "thinking_traces": list(result.thinking_traces),
                    "tool_traces": list(result.tool_traces),
                    "finish_reason": result.finish_reason,
                    "routing": result.routing,
                    "persona": {
                        "display_zh": display_bundle.main.name_zh,
                        "display_en": display_bundle.main.name_en,
                        "liaison_display_zh": display_bundle.liaison.name_zh if display_bundle.liaison else "",
                        "liaison_display_en": display_bundle.liaison.name_en if display_bundle.liaison else "",
                        "liaison": display_bundle.liaison_label,
                        "source": display_bundle.source,
                    },
                    "role": {
                        "rarity": result.role.rarity,
                        "name_zh": result.role.name_zh,
                        "name_en": result.role.name_en,
                    },
                    "raw_response": result.raw_response,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    receipts = _render_tool_receipts(result.tool_traces)
    display_bundle = result.persona_bundle or selected_bundle
    if receipts and result.text:
        print(f"{receipts}{_render_assistant_block(result.text, role=result.role, persona_bundle=display_bundle)}")
    elif receipts:
        print(receipts)
    else:
        print(_render_assistant_block(result.text, role=result.role, persona_bundle=display_bundle))
    return 0

def _api_test_messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "Connectivity check. Reply with exactly pong."},
        {"role": "user", "content": "ping"},
    ]


def _api_test_diagnostic_config(config: ProjectLingConfig, model: str) -> tuple[ProjectLingConfig, str]:
    provider = _api_provider_value(getattr(config, "api_provider", ""))
    normalized_model = str(model or "").strip().lower()
    if provider != "gemini" or not normalized_model.startswith("gemini-3") or "pro" not in normalized_model:
        return config, "disabled"

    raw_extra = str(getattr(config, "gemini_extra_body_json", "") or "").strip()
    try:
        extra_body = json.loads(raw_extra) if raw_extra else {}
    except json.JSONDecodeError:
        extra_body = {}
    if not isinstance(extra_body, dict):
        extra_body = {}
    google = extra_body.setdefault("google", {})
    if not isinstance(google, dict):
        google = {}
        extra_body["google"] = google
    google["thinking_config"] = {"thinking_level": "minimal"}
    return replace(config, gemini_extra_body_json=json.dumps(extra_body, ensure_ascii=False)), "minimal"


def _api_test_one_model(
    client: DeepSeekClient,
    *,
    role: str,
    label: str,
    model: str,
    thinking_enabled: bool,
    stream: bool,
    thinking_mode: str = "disabled",
) -> dict[str, Any]:
    started = time.time()
    safety = _api_test_model_safety(model)
    try:
        preview = ""
        if stream:
            for chunk in client.chat_completions_stream(
                messages=_api_test_messages(),
                tools=None,
                tool_choice="none",
                model=model,
                thinking_enabled=thinking_enabled,
                max_tokens=32,
            ):
                choice = ((chunk.get("choices") or [{}])[0] or {})
                delta = choice.get("delta") or {}
                preview += str(delta.get("content") or "")
                if str(choice.get("finish_reason") or "").strip():
                    break
                if len(preview.strip()) >= 16:
                    break
            preview = preview.strip() or "(收到流式事件，内容为空)"
        else:
            data = client.chat_completions(
                messages=_api_test_messages(),
                tools=None,
                tool_choice="none",
                model=model,
                thinking_enabled=thinking_enabled,
                max_tokens=32,
            )
            choice = ((data.get("choices") or [{}])[0] or {})
            message = choice.get("message") or {}
            preview = str(message.get("content") or "").strip() or "(响应为空)"
        return {
            "role": role,
            "label": label,
            "ok": True,
            "model": model,
            "tags": safety.get("tags", []),
            "risk": safety.get("risk", "normal"),
            "hint": safety.get("hint", ""),
            "thinking": bool(thinking_enabled),
            "thinking_mode": thinking_mode,
            "elapsed_seconds": round(time.time() - started, 3),
            "preview": preview[:120],
        }
    except Exception as exc:
        return {
            "role": role,
            "label": label,
            "ok": False,
            "model": model,
            "tags": safety.get("tags", []),
            "risk": safety.get("risk", "normal"),
            "hint": safety.get("hint", ""),
            "thinking": bool(thinking_enabled),
            "thinking_mode": thinking_mode,
            "elapsed_seconds": round(time.time() - started, 3),
            "error": str(exc),
        }


def _api_test_result_by_role(results: Sequence[dict[str, Any]], role: str) -> dict[str, Any]:
    for result in results:
        if str(result.get("role") or "") == role:
            return result
    return {}


def _api_test_first_error(results: Sequence[dict[str, Any]]) -> str:
    for result in results:
        error_text = str(result.get("error") or "").strip()
        if error_text:
            return error_text
    return ""


def _build_api_test_payload(
    config: ProjectLingConfig,
    *,
    override_model: str = "",
    force_no_stream: bool = False,
) -> dict[str, Any]:
    planner_model, executor_model = _collab_mode_models(config.collab_mode, config)
    planner_model = str(planner_model or "").strip()
    executor_model = str(executor_model or "").strip()
    override_model = str(override_model or "").strip()
    stream = bool(config.enable_sse and not force_no_stream)
    started = time.time()

    if override_model:
        executor_model = override_model
        targets = [
            (
                "executor",
                "辅星",
                executor_model,
                False,
            )
        ]
    else:
        targets = [
            (
                "planner",
                "主星",
                planner_model,
                False,
            ),
            (
                "executor",
                "辅星",
                executor_model,
                False,
            ),
        ]

    def run_target(target: tuple[str, str, str, bool]) -> dict[str, Any]:
        role, label, model, thinking_enabled = target
        diagnostic_config, thinking_mode = _api_test_diagnostic_config(config, model)
        return _api_test_one_model(
            DeepSeekClient(diagnostic_config),
            role=role,
            label=label,
            model=model,
            thinking_enabled=thinking_enabled,
            stream=stream,
            thinking_mode=thinking_mode,
        )

    if len(targets) > 1:
        with ThreadPoolExecutor(max_workers=len(targets), thread_name_prefix="projectling-api-test") as pool:
            results = list(pool.map(run_target, targets))
    else:
        results = [run_target(targets[0])]
    planner_result = _api_test_result_by_role(results, "planner")
    executor_result = _api_test_result_by_role(results, "executor")
    executor_safety = _api_test_model_safety(executor_model)
    payload = {
        "ok": all(bool(result.get("ok")) for result in results),
        "provider": _api_provider_value(getattr(config, "api_provider", "")),
        "base_url": config.base_url,
        "collab_mode": config.collab_mode,
        "planner_model": planner_model,
        "executor_model": executor_model,
        "planner_ok": planner_result.get("ok") if planner_result else None,
        "executor_ok": executor_result.get("ok") if executor_result else None,
        "planner_tags": planner_result.get("tags", []),
        "planner_risk": planner_result.get("risk", "normal"),
        "planner_hint": planner_result.get("hint", ""),
        "executor_tags": executor_result.get("tags", executor_safety.get("tags", [])),
        "executor_risk": executor_result.get("risk", executor_safety.get("risk", "normal")),
        "executor_hint": executor_result.get("hint", executor_safety.get("hint", "")),
        "stream": stream,
        "elapsed_seconds": round(time.time() - started, 3),
        "results": results,
    }
    preview = str((executor_result or planner_result).get("preview") or "").strip()
    if preview:
        payload["preview"] = preview[:120]
    error_text = _api_test_first_error(results)
    if error_text:
        payload["error"] = error_text
    return payload


def _print_api_test_payload(payload: dict[str, Any], *, width: int | None = None) -> None:
    render_width = width if width is not None else _compact_render_width()
    status = "ok" if payload.get("ok") else "fail"
    results = [result for result in payload.get("results", []) if isinstance(result, dict)]
    target_text = "单模型" if len(results) == 1 else "双星"
    if render_width >= 50:
        _print_fit(f"api-test {status} · {payload.get('provider')} · {target_text}", width=render_width)
        if not payload.get("ok"):
            _print_setting_pair("base", payload.get("base_url"), width=render_width)
    else:
        _print_fit(f"api-test {status}", width=render_width)
        _print_setting_pair("provider", payload.get("provider"), width=render_width)
        _print_setting_pair("target", target_text, width=render_width)
        if not payload.get("ok"):
            _print_setting_pair("base", payload.get("base_url"), width=render_width)
    for result in results:
        label = str(result.get("label") or result.get("role") or "模型")
        role_status = "OK" if result.get("ok") else "FAIL"
        model = str(result.get("model") or "")
        if render_width >= 50:
            _print_fit(f"{label} {role_status} · {model}", width=render_width)
        else:
            _print_setting_pair(label, f"{role_status} · {model}", width=render_width)
        preview = str(result.get("preview") or "").strip()
        if preview and render_width >= 34:
            _print_setting_pair(f"{label}预览", preview[:80], width=render_width)
        hint = str(result.get("hint") or "").strip()
        if hint:
            if render_width >= 50:
                _print_fit(f"{label}提示：{hint}", width=render_width)
            else:
                _print_setting_pair(f"{label}提示", hint, width=render_width)
        error_text = str(result.get("error") or "").strip()
        if error_text:
            if render_width >= 50:
                _print_fit_wrapped(f"{label}失败：{error_text}", width=render_width)
            else:
                _print_fit_wrapped(error_text, width=render_width)
    _print_setting_pair("stream", str(payload.get("stream")), width=render_width)
    if payload.get("error"):
        _print_next_action_check(["API Key", "Base URL", "模型名", "网络"], width=render_width)


def _run_api_test(config: ProjectLingConfig) -> None:
    width = _compact_render_width()
    print("")
    _print_fit(f"API TEST · {_provider_label(config)}", width=width)
    if not config.api_key:
        _print_fit("未设置 API Key。", width=width)
        _print_fit("下一步：先写入当前 Provider 的 API Key。", width=width)
        return

    payload = _build_api_test_payload(config)
    _print_api_test_payload(payload, width=width)


def _extract_model_ids(payload: dict[str, Any]) -> list[str]:
    data = payload.get("data") if isinstance(payload, dict) else None
    ids: list[str] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or item.get("name") or "").strip()
            if model_id:
                ids.append(model_id)
    elif isinstance(payload.get("models"), list):
        for item in payload.get("models") or []:
            if isinstance(item, dict):
                model_id = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
                if model_id:
                    ids.append(model_id)
            elif isinstance(item, str) and item.strip():
                ids.append(item.strip())
    return ids


def _run_model_list(config: ProjectLingConfig, *, limit: int = 40) -> None:
    width = _compact_render_width()
    print("")
    list_title = f"MODEL LIST · {_provider_label(config)}"
    _print_fit(list_title if _display_width(list_title) <= width else f"Models · {_provider_label(config)}", width=width)
    if not config.api_key:
        _print_fit("未设置 API Key。", width=width)
        _print_fit("下一步：先写入当前 Provider 的 API Key。", width=width)
        return
    try:
        payload = DeepSeekClient(config).list_models()
    except Exception as exc:
        _print_fit("模型列表拉取失败。", width=width)
        _print_fit_wrapped(exc, width=width)
        _print_fit("下一步：检查 API Key、Base URL、模型列表接口和网络。", width=width)
        return
    ids = _extract_model_ids(payload)
    if not ids:
        _print_fit("模型列表为空或响应结构不含 data[].id。", width=width)
        _print_fit_wrapped(json.dumps(payload, ensure_ascii=False)[:600], width=width)
        return
    _print_model_taxonomy_hint(ids, width=width)
    planner_model, executor_model = _collab_mode_models(config.collab_mode, config)
    planner_model = str(planner_model or "").strip()
    executor_model = str(executor_model or "").strip()
    for index, model_id in enumerate(ids[:limit], start=1):
        marker = " / ".join(_model_list_marker_parts(model_id, planner_model, executor_model, width=width))
        _print_indexed_model(index, model_id, marker, width=width)
    if len(ids) > limit:
        _print_fit(f"... 还有 {len(ids) - limit} 个模型", width=width)


def _run_gemini_params_settings_ui() -> None:
    while True:
        current = load_config()
        width = _compact_render_width()
        print("")
        if width < 34:
            _print_fit("Gemini 参数", width=width)
            _print_setting_option(1, "Top P", current.gemini_top_p if current.gemini_top_p is not None else "自动", width=width)
            _print_setting_option(2, "Top K", current.gemini_top_k if current.gemini_top_k is not None else "自动", width=width)
            _print_setting_option(3, "候选数量", current.gemini_candidate_count if current.gemini_candidate_count is not None else "自动", width=width)
            _print_setting_option(4, "Seed", current.gemini_seed if current.gemini_seed is not None else "自动", width=width)
            _print_setting_option(5, "存在惩罚", current.gemini_presence_penalty if current.gemini_presence_penalty is not None else "自动", width=width)
            _print_setting_option(6, "频率惩罚", current.gemini_frequency_penalty if current.gemini_frequency_penalty is not None else "自动", width=width)
            _print_setting_option(7, "停止词", ",".join(current.gemini_stop_sequences) if current.gemini_stop_sequences else "自动", width=width)
            _print_setting_option(8, "响应 MIME", current.gemini_response_mime_type or "自动", width=width)
            _print_setting_option(9, "Extra Body", "已设置" if current.gemini_extra_body_json else "未设置", width=width)
            _print_setting_option(0, "返回上级", width=width)
        else:
            print("Gemini 参数")
            print(f"1. Top P             [{current.gemini_top_p if current.gemini_top_p is not None else '自动'}]")
            print(f"2. Top K             [{current.gemini_top_k if current.gemini_top_k is not None else '自动'}]")
            print(f"3. Candidate Count   [{current.gemini_candidate_count if current.gemini_candidate_count is not None else '自动'}]")
            print(f"4. Seed              [{current.gemini_seed if current.gemini_seed is not None else '自动'}]")
            print(f"5. Presence Penalty  [{current.gemini_presence_penalty if current.gemini_presence_penalty is not None else '自动'}]")
            print(f"6. Frequency Penalty [{current.gemini_frequency_penalty if current.gemini_frequency_penalty is not None else '自动'}]")
            print(f"7. Stop Sequences    [{','.join(current.gemini_stop_sequences) if current.gemini_stop_sequences else '自动'}]")
            print(f"8. Response MIME     [{current.gemini_response_mime_type or '自动'}]")
            print(f"9. Extra Body JSON   [{'已设置' if current.gemini_extra_body_json else '未设置'}]")
            print("0. 返回上级")
        choice = _prompt_menu_choice("0 返回上级")
        if choice == "1":
            value = _prompt_float("输入 Top P (0.0 - 1.0)，留空清除 > ", min_value=0.0, max_value=1.0, allow_empty_clear=True)
            if value == "":
                _save_config_value(current, {"GEMINI_TOP_P": None})
                _print_setting_cleared("Top P", width=width)
            elif isinstance(value, float):
                _save_config_value(current, {"GEMINI_TOP_P": f"{value:g}"})
                _print_setting_saved("Top P", f"{value:g}", width=width)
            continue
        if choice == "2":
            value = _prompt_int("输入 Top K，留空清除 > ", min_value=1, allow_empty_clear=True)
            if value == "":
                _save_config_value(current, {"GEMINI_TOP_K": None})
                _print_setting_cleared("Top K", width=width)
            elif isinstance(value, int):
                _save_config_value(current, {"GEMINI_TOP_K": str(value)})
                _print_setting_saved("Top K", value, width=width)
            continue
        if choice == "3":
            value = _prompt_int("输入 Candidate Count (1-8)，留空清除 > ", min_value=1, allow_empty_clear=True)
            if isinstance(value, int) and value > 8:
                _print_setting_rejected(
                    "Candidate Count 需要小于等于 8。",
                    width=width,
                    keep_label="保留",
                    keep_value=current.gemini_candidate_count if current.gemini_candidate_count is not None else "自动",
                )
            elif value == "":
                _save_config_value(current, {"GEMINI_CANDIDATE_COUNT": None})
                _print_setting_cleared("Candidate Count", width=width)
            elif isinstance(value, int):
                _save_config_value(current, {"GEMINI_CANDIDATE_COUNT": str(value)})
                _print_setting_saved("Candidate Count", value, width=width)
            else:
                pass
            continue
        if choice == "4":
            value = _prompt_int("输入 Seed，留空清除 > ", min_value=0, allow_empty_clear=True)
            if value == "":
                _save_config_value(current, {"GEMINI_SEED": None})
                _print_setting_cleared("Seed", width=width)
            elif isinstance(value, int):
                _save_config_value(current, {"GEMINI_SEED": str(value)})
                _print_setting_saved("Seed", value, width=width)
            continue
        if choice == "5":
            value = _prompt_float("输入 Presence Penalty (-2.0 - 2.0)，留空清除 > ", min_value=-2.0, max_value=2.0, allow_empty_clear=True)
            if value == "":
                _save_config_value(current, {"GEMINI_PRESENCE_PENALTY": None})
                _print_setting_cleared("Presence Penalty", width=width)
            elif isinstance(value, float):
                _save_config_value(current, {"GEMINI_PRESENCE_PENALTY": f"{value:g}"})
                _print_setting_saved("Presence Penalty", f"{value:g}", width=width)
            continue
        if choice == "6":
            value = _prompt_float("输入 Frequency Penalty (-2.0 - 2.0)，留空清除 > ", min_value=-2.0, max_value=2.0, allow_empty_clear=True)
            if value == "":
                _save_config_value(current, {"GEMINI_FREQUENCY_PENALTY": None})
                _print_setting_cleared("Frequency Penalty", width=width)
            elif isinstance(value, float):
                _save_config_value(current, {"GEMINI_FREQUENCY_PENALTY": f"{value:g}"})
                _print_setting_saved("Frequency Penalty", f"{value:g}", width=width)
            continue
        if choice == "7":
            value = _prompt_line("输入 stop sequences，用英文逗号分隔，留空清除 > ").strip()
            _save_config_value(current, {"GEMINI_STOP_SEQUENCES": value or None})
            if value:
                _print_setting_saved("Stop Sequences", value, width=width)
            else:
                _print_setting_cleared("Stop Sequences", width=width)
            continue
        if choice == "8":
            value = _prompt_line("输入 response MIME，例如 application/json，留空清除 > ").strip()
            _save_config_value(current, {"GEMINI_RESPONSE_MIME_TYPE": value or None})
            if value:
                _print_setting_saved("Response MIME", value, width=width)
            else:
                _print_setting_cleared("Response MIME", width=width)
            continue
        if choice == "9":
            _print_fit_wrapped("输入 extra_body JSON，留空清除。", width=width, max_lines=3)
            value = _prompt_line("› JSON: ").strip()
            if value:
                try:
                    parsed = json.loads(value)
                    if not isinstance(parsed, dict):
                        _print_setting_rejected(
                            "必须是 JSON object。",
                            width=width,
                            keep_label="Extra Body",
                            keep_value="保留旧值" if current.gemini_extra_body_json else "未设置",
                        )
                        continue
                except json.JSONDecodeError as exc:
                    _print_setting_rejected(
                        f"JSON 无效：{exc}",
                        width=width,
                        keep_label="Extra Body",
                        keep_value="保留旧值" if current.gemini_extra_body_json else "未设置",
                    )
                    continue
            _save_config_value(current, {"GEMINI_EXTRA_BODY_JSON": value or None})
            if value:
                _print_setting_saved("Extra Body JSON", "已设置", width=width)
            else:
                _print_setting_cleared("Extra Body JSON", width=width)
            continue
        if choice == "0" or not choice:
            return
        print("无效输入。")


def _toggle_config_value(config: ProjectLingConfig, key: str, current: bool, label: str) -> ProjectLingConfig:
    updated = _save_config_value(config, {key: "0" if current else "1"})
    print(f"{label} 已切换为 {_bool_label(not current)}。")
    return updated


def _bootstrap_missing_key(config: ProjectLingConfig) -> ProjectLingConfig | None:
    role, _seed = resolve_current_role(config)
    print(_render_assistant_block("尚未配置 API Key。", role=role))
    key = _prompt_line("  输入 API Key，直接回车跳过 > ").strip()
    if not key:
        print("  已跳过配置。输入 /settings 继续设置。\n")
        return None

    updated = _save_config_value(config, {"DEEPSEEK_API_KEY": key})
    print("基础设置已写入。\n")
    return updated


def _thinking_block_lines(header: str, body_lines: list[str]) -> list[str]:
    lines = ["", header]
    for line in body_lines:
        body_line = _style_thought_text(line) if line else ""
        lines.append(f"  ┆ {body_line}" if line else "")
    lines.append("")
    return lines


class ShellStreamPrinter:
    def __init__(
        self,
        prompt_bundle: PromptBundle,
        role: LauncherRole,
        *,
        persona_bundle: PersonaBundle | None = None,
        show_role_heading: bool = True,
        context_budget: dict[str, Any] | None = None,
    ) -> None:
        self.prompt_bundle = prompt_bundle
        self.role = role
        self.persona_bundle = persona_bundle or PersonaBundle(main=role)
        self.typing = prompt_bundle.typing
        self.sanitizer = StreamingTextSanitizer()
        self.show_role_heading = bool(show_role_heading)
        self.heading_printed = not show_role_heading
        self.line_open = False
        self.status_visible = False
        self.status_kind = ""
        self.status_label = ""
        self.status_frame = 0
        self.status_lock = threading.RLock()
        self.working_active = False
        self.working_thread: threading.Thread | None = None
        self.working_stop_event = threading.Event()
        self.saw_content = False
        self.reasoning_buffer: list[str] = []
        self.reasoning_started_at: float | None = None
        self.reasoning_live_lines = 0
        self.last_reasoning_render_at = 0.0
        self.tool_active = False
        self.tool_payload: dict[str, Any] | None = None
        self.tool_block_rendered = False
        self.tool_group_family: str | None = None
        self.tool_running_rendered = False
        self.tool_running_lines = 0
        self.tool_running_started_at: float | None = None
        self.tool_saw_output = False
        self.tool_stream_seen = {"stdout": 0, "stderr": 0}
        self.pending_tool_receipts: list[dict[str, Any]] = []
        self.markdown_pending = ""
        self.assistant_content_seen = False
        self.can_control = _supports_tty_control()
        self.typewriter_enabled = bool(self.typing.get("enabled", True) and sys.stdout.isatty())
        self.renderer = MarkdownAnsiRenderer(tty=self.can_control)
        self.thinking_label = _normalize_status_label(prompt_bundle.status.get("thinking"), "Thinking")
        self.responding_label = _normalize_status_label(
            prompt_bundle.status.get("responding"),
            "Responding",
        )
        self.context_budget = dict(context_budget or load_context_budget(load_config()))

    def _update_context_budget(self, payload: dict[str, Any] | None) -> None:
        if not isinstance(payload, dict):
            return
        if "context_budget_percent" not in payload and str(payload.get("tool") or "") != "context":
            return
        self.context_budget = dict(payload)

    def _apply_persona_handoff_payload(self, payload: dict[str, Any]) -> bool:
        resolved = _persona_from_handoff_payload(payload)
        if resolved is None:
            return False
        role, persona_bundle = resolved
        current_liaison = self.persona_bundle.liaison.name_en if self.persona_bundle.liaison else ""
        next_liaison = persona_bundle.liaison.name_en if persona_bundle.liaison else ""
        same_heading = (
            self.role.name_en == role.name_en
            and self.persona_bundle.main.name_en == persona_bundle.main.name_en
            and current_liaison == next_liaison
        )
        self.role = role
        self.persona_bundle = persona_bundle
        if same_heading or not self.heading_printed:
            return True
        if self.assistant_content_seen:
            return True
        self.clear_status()
        if self.line_open:
            self._write("\n")
            self.line_open = False
        heading = _format_role_heading(self.role, self.persona_bundle)
        context_line = _context_budget_line(self.context_budget)
        if context_line:
            self._write(f"\n{heading}  {context_line}\n\n")
        else:
            self._write(f"\n{heading}\n\n")
        return True

    def _apply_executor_handoff_payload(self, payload: dict[str, Any]) -> bool:
        tool_name = str(payload.get("tool") or "")
        action_name = str(payload.get("action") or "").strip().lower()
        target = str(payload.get("target") or "").strip().lower()
        if tool_name != "link" or action_name != "continue" or target not in {"executor", "liaison"}:
            return False
        actor_kind = str(payload.get("actor_kind") or "").strip().lower()
        executor_keys = ["executor_name", "liaison_name"]
        planner_keys = ["planner_name", "main_name", "main_role"]
        if actor_kind == "executor":
            executor_keys.append("actor_name")
        elif actor_kind == "planner":
            planner_keys.append("actor_name")
        executor = _role_from_roster_payload_priority(payload, *executor_keys)
        planner = _role_from_roster_payload_priority(payload, *planner_keys)
        if executor is None:
            return False
        current_liaison = self.persona_bundle.liaison.name_en if self.persona_bundle.liaison else ""
        same_heading = (
            self.role.name_en == executor.name_en
            and self.persona_bundle.source == "executor_handoff"
            and current_liaison == (planner.name_en if planner else "")
        )
        self.role = executor
        self.persona_bundle = PersonaBundle(main=executor, liaison=planner, source="executor_handoff")
        if same_heading:
            return True
        self.clear_status()
        if self.line_open:
            self._write("\n")
            self.line_open = False
        heading = _format_role_heading(self.role, self.persona_bundle)
        context_line = _context_budget_line(self.context_budget)
        if context_line:
            self._write(f"\n{heading}  {context_line}\n\n")
        else:
            self._write(f"\n{heading}\n\n")
        return True

    def _write(self, text: str, *, flush: bool = True) -> None:
        sys.stdout.write(text)
        if flush:
            sys.stdout.flush()

    def _render_markdown_stream_text(self, text: str) -> str:
        if not text:
            return ""
        if not text.strip():
            return text
        rendered = self.renderer.render(text)
        if rendered or "\n" not in text:
            return rendered
        return text

    def _flush_markdown_pending(self, *, force: bool = False) -> None:
        while self.markdown_pending:
            if force:
                chunk = self.markdown_pending
                self.markdown_pending = ""
            else:
                split_index = _markdown_stream_flush_index(self.markdown_pending)
                if split_index <= 0:
                    return
                chunk = self.markdown_pending[:split_index]
                self.markdown_pending = self.markdown_pending[split_index:]

            rendered = self._render_markdown_stream_text(chunk)
            if rendered:
                self._write_indented(rendered)

            if not force and _markdown_stream_flush_index(self.markdown_pending) <= 0:
                return

    def _queue_markdown_stream_text(self, text: str) -> None:
        if not text:
            return
        if text.strip():
            self.saw_content = True
            self.assistant_content_seen = True
        self.markdown_pending += text
        self._flush_markdown_pending(force=False)

    def _status_text(self, kind: str, label: str | None = None) -> str:
        if kind == "thinking":
            base = "思考中"
        else:
            base = _normalize_status_label(label or self.responding_label, "处理中")
        trimmed = base.rstrip(".")
        dots = "..." if kind == "thinking" else "." * ((self.status_frame % 3) + 1)
        prefix = "◔" if kind == "thinking" else "●"
        text = f"{prefix} {trimmed}{dots}"
        return _style_status(text, kind)

    def start(self) -> None:
        if self.heading_printed:
            return
        heading = _format_role_heading(self.role, self.persona_bundle)
        context_line = _context_budget_line(self.context_budget)
        if context_line:
            self._write(f"\n{heading} · {context_line}\n\n")
        else:
            self._write(f"\n{heading}\n\n")
        self.heading_printed = True

    def show_status(self, kind: str) -> None:
        with self.status_lock:
            if not self.can_control:
                self.start()
                if self.line_open:
                    self._write("\n")
                    self.line_open = False
                if not self.status_visible:
                    self.status_kind = kind
                    self.status_label = self.thinking_label if kind == "thinking" else self.responding_label
                    self._write(f"{self._status_text(kind, self.status_label)}\n")
                    self.status_visible = True
                    self.start_working_animation()
                return
            self.start()
            if self.line_open:
                self._write("\n")
                self.line_open = False
            if self.status_visible:
                self._write("\033[A\r\033[2K")
            self.status_kind = kind
            self.status_label = self.thinking_label if kind == "thinking" else self.responding_label
            self._write(f"{self._status_text(kind, self.status_label)}\n")
            self.status_visible = True
            self.start_working_animation()

    def refresh_status(self) -> None:
        with self.status_lock:
            if not self.status_visible or not self.can_control:
                return
            self.status_frame += 1
            self._write(f"\033[A\r\033[2K{self._status_text(self.status_kind or 'thinking', self.status_label)}\n")

    def clear_status(self) -> None:
        with self.status_lock:
            if not self.status_visible or not self.can_control:
                self.status_visible = False
                self.status_kind = ""
                self.status_label = ""
                return
            self._write("\033[A\r\033[2K")
            self.status_visible = False
            self.status_kind = ""
            self.status_label = ""

    def _working_loop(self) -> None:
        while not self.working_stop_event.wait(WORKING_ANIMATION_INTERVAL_SECONDS):
            if not self.working_active:
                break
            try:
                self.refresh_status()
                self._refresh_tool_running_block()
            except Exception:
                break
        self.working_active = False

    def start_working_animation(self) -> None:
        if self.working_active or not self.can_control:
            return
        self.working_active = True
        self.working_stop_event.clear()
        worker = threading.Thread(
            target=self._working_loop,
            name="projectling-working",
            daemon=True,
        )
        self.working_thread = worker
        worker.start()

    def stop_working_animation(self) -> None:
        self.working_active = False
        self.working_stop_event.set()
        worker = self.working_thread
        if worker is not None and worker is not threading.current_thread():
            try:
                worker.join(timeout=0.4)
            except RuntimeError:
                pass
        self.working_thread = None

    def begin(self, kind: str) -> None:
        self.show_status(kind)

    def _current_speaker_label(self) -> str:
        return _speaker_label_for_bundle(self.persona_bundle)

    def _current_speaker_identity(self, *, include_context: bool = False) -> str:
        return _speaker_identity_text(
            self.role,
            self.persona_bundle,
            context_budget=self.context_budget if include_context else None,
        )

    def _thinking_header_text(
        self,
        *,
        role_label: str = "",
        actor_name: Any = "",
        context_percent: Any = None,
    ) -> str:
        return "◔ 思考中"

    def _trace_speaker_identity(
        self,
        *,
        role_label: str = "",
        actor_name: Any = "",
        context_percent: Any = None,
    ) -> str:
        context_budget = self.context_budget
        if context_percent is not None and context_percent != "":
            try:
                context_budget = {"percent": max(0, min(100, int(context_percent)))}
            except (TypeError, ValueError):
                context_budget = self.context_budget
        return _speaker_identity_text(
            self.role,
            self.persona_bundle,
            context_budget=context_budget,
            role_label=role_label,
            actor_name=actor_name,
        )

    def _with_current_actor_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(payload)
        label = str(enriched.get("actor_label") or "").strip()
        actor_kind = str(enriched.get("actor_kind") or "").strip().lower()
        if not label:
            label = self._current_speaker_label()
            enriched["actor_label"] = label
        if not actor_kind:
            actor_kind = "executor" if label == "执行星" else "planner"
            enriched["actor_kind"] = actor_kind
        if not str(enriched.get("actor_name") or "").strip():
            candidates: list[Any] = []
            if actor_kind == "executor" or label == "执行星":
                candidates.extend([enriched.get("executor_name"), enriched.get("liaison_name")])
            else:
                candidates.extend([enriched.get("planner_name"), enriched.get("main_name"), enriched.get("main_role")])
            candidates.append(f"{self.persona_bundle.main.name_zh} / {self.persona_bundle.main.name_en}")
            for candidate in candidates:
                if str(candidate or "").strip():
                    enriched["actor_name"] = str(candidate).strip()
                    break
        enriched.setdefault("context_budget_percent", _context_budget_percent(self.context_budget))
        current_label = self._current_speaker_label()
        current_name = _normalize_identity_name(
            f"{self.persona_bundle.main.name_zh} / {self.persona_bundle.main.name_en}"
        )
        payload_label = str(enriched.get("actor_label") or "").strip()
        payload_name = _normalize_identity_name(enriched.get("actor_name") or "")
        same_label = payload_label in {"", current_label}
        same_name = not payload_name or _identity_part_key(payload_name) == _identity_part_key(current_name)
        enriched["_suppress_actor_line"] = same_label and same_name
        return enriched

    def _thinking_body_lines(self, text: str) -> list[str]:
        cleaned = _strip_context_percent_marker_text(text or "")
        if not cleaned:
            return []
        rendered = _strip_ansi(self.renderer.render(cleaned).rstrip("\n"))
        wrap_width = max(12, _terminal_render_width() - 4)
        wrapped_lines: list[str] = []
        raw_lines = rendered.splitlines() if rendered else ["..."]
        for raw_line in raw_lines:
            if raw_line:
                wrapped_lines.extend(_wrap_ansi_display(raw_line, wrap_width))
            else:
                wrapped_lines.append("")
        body_lines = wrapped_lines or ["..."]
        if len(body_lines) > THINKING_PREVIEW_MAX_LINES:
            head_count = min(THINKING_PREVIEW_EDGE_LINES, len(body_lines))
            tail_count = min(THINKING_PREVIEW_EDGE_LINES, max(0, len(body_lines) - head_count))
            omitted = max(1, len(body_lines) - head_count - tail_count)
            marker = _truncate_display_ellipsis(f"… middle omitted · {omitted} lines …", wrap_width)
            body_lines = [*body_lines[:head_count], marker, *body_lines[-tail_count:]]
        return body_lines

    def _clear_live_reasoning(self) -> None:
        if not self.can_control or self.reasoning_live_lines <= 0:
            self.reasoning_live_lines = 0
            return
        for _ in range(self.reasoning_live_lines):
            self._write("\033[A\r\033[2K")
        self.reasoning_live_lines = 0

    def _render_live_reasoning(self) -> None:
        if not self.can_control:
            return
        self.show_status("thinking")

    def _fold_reasoning_summary(self, elapsed_seconds: float | None = None) -> None:
        text = _strip_context_percent_marker_text("".join(self.reasoning_buffer))
        self._clear_live_reasoning()
        self.reasoning_buffer = []
        self.reasoning_started_at = None
        self.reasoning_live_lines = 0
        self.last_reasoning_render_at = 0.0

    def _print_transient_block(self, header: str, text: str, *, elapsed_seconds: float | None = None) -> None:
        body_lines = self._thinking_body_lines(text)
        if not body_lines:
            return
        self.start()
        self.clear_status()
        if self.line_open:
            self._write("\n")
            self.line_open = False

        block_lines = _thinking_block_lines(header, body_lines)

        block_text = "\n".join(block_lines)
        self._write(f"{block_text}\n")
        summary_text = _style_thought_text(_format_thought_summary(elapsed_seconds))
        if not self.can_control:
            self._write(f"{summary_text}\n\n")
            return

        time.sleep(THINKING_FOLD_DELAY_SECONDS)
        for _ in range(len(block_lines)):
            self._write("\033[A\r\033[2K")
        self._write(f"{summary_text}\n\n")

    def flush_reasoning_trace(self) -> None:
        text = _strip_context_percent_marker_text("".join(self.reasoning_buffer))
        elapsed_seconds = None
        if self.reasoning_started_at is not None:
            elapsed_seconds = max(0.0, time.monotonic() - self.reasoning_started_at)
        if not text:
            self.reasoning_buffer = []
            self.reasoning_started_at = None
            self.reasoning_live_lines = 0
            self.last_reasoning_render_at = 0.0
            return
        self.reasoning_buffer = [text]
        self._fold_reasoning_summary(elapsed_seconds)

    def show_thinking_trace(
        self,
        text: str,
        *,
        elapsed_seconds: float | None = None,
        role_label: str = "",
        actor_name: Any = "",
        context_percent: Any = None,
    ) -> None:
        if _strip_context_percent_marker_text(text):
            self.show_status("thinking")

    def _reset_tool_state(self) -> None:
        self.tool_active = False
        self.tool_payload = None
        self.tool_block_rendered = False
        self.tool_group_family = None
        self.tool_running_rendered = False
        self.tool_running_lines = 0
        self.tool_running_started_at = None
        self.tool_saw_output = False
        self.tool_stream_seen = {"stdout": 0, "stderr": 0}

    def _emit_plain_block_direct(self, text: str, *, trailing_blank: bool = True) -> None:
        block = (text or "").rstrip()
        if not block:
            return
        self._flush_markdown_pending(force=True)
        self.start()
        self.clear_status()
        if self.line_open:
            self._write("\n")
            self.line_open = False
        self._write(f"{block}\n")
        if trailing_blank:
            self._write("\n")
        self.saw_content = True

    def _flush_pending_tool_receipts(self) -> None:
        if not self.pending_tool_receipts:
            return
        blocks: list[str] = []
        grouped_payloads: list[dict[str, Any]] = []
        grouped_family: str | None = None

        def flush_group() -> None:
            nonlocal grouped_payloads, grouped_family
            if not grouped_payloads:
                return
            if (len(grouped_payloads) == 1 and grouped_family != "explore") or grouped_family is None:
                blocks.append(_render_tool_receipt_payload(grouped_payloads[0]))
            else:
                blocks.append(_render_grouped_tool_receipt(grouped_payloads, grouped_family))
            grouped_payloads = []
            grouped_family = None

        for payload in self.pending_tool_receipts:
            if _should_suppress_tool_receipt(payload):
                continue
            family = _tool_group_family(payload)
            if family:
                if grouped_payloads and family != grouped_family:
                    flush_group()
                grouped_payloads.append(payload)
                grouped_family = family
                continue
            flush_group()
            blocks.append(_render_tool_receipt_payload(payload))
        flush_group()
        self.pending_tool_receipts = []
        if blocks:
            self._emit_plain_block_direct("\n\n".join(blocks), trailing_blank=True)

    def _queue_pending_tool_receipt(self, payload: dict[str, Any]) -> None:
        family = _tool_group_family(payload)
        if self.pending_tool_receipts and family and _tool_group_family(self.pending_tool_receipts[-1]) != family:
            self._flush_pending_tool_receipts()
        self.pending_tool_receipts.append(dict(payload))

    def _render_tool_running_block(self, payload: dict[str, Any]) -> None:
        if self.tool_running_rendered:
            return
        self.start()
        self.clear_status()
        if self.line_open:
            self._write("\n")
            self.line_open = False
        block = _render_tool_running_receipt(payload)
        self._write(f"{block}\n")
        self.tool_running_rendered = True
        self.tool_running_lines = max(1, len(block.splitlines()))
        self.tool_running_started_at = time.monotonic()
        self.saw_content = True
        self.start_working_animation()

    def _tool_running_output_line(self) -> str:
        dots = "." * ((self.status_frame % 3) + 1)
        parts: list[str] = [f"Running{dots}"]
        if self.tool_running_started_at is not None:
            elapsed = max(0.0, time.monotonic() - self.tool_running_started_at)
            if elapsed >= 1.0:
                parts.append(f"{elapsed:.0f}s")
        stdout_seen = int(self.tool_stream_seen.get("stdout") or 0)
        stderr_seen = int(self.tool_stream_seen.get("stderr") or 0)
        if stdout_seen:
            parts.append(f"stdout {stdout_seen} chars")
        if stderr_seen:
            parts.append(f"stderr {stderr_seen} chars")
        return _tool_meta_line("OUTPUT", *parts, color=ANSI_SOFT_PINK)

    def _refresh_tool_running_block(self) -> None:
        with self.status_lock:
            if not self.can_control or not self.tool_running_rendered or self.tool_running_lines <= 0:
                return
            self.status_frame += 1
            self._write(f"\033[A\r\033[2K{self._tool_running_output_line()}\n")

    def _clear_tool_running_block(self) -> None:
        with self.status_lock:
            if not self.can_control or not self.tool_running_rendered or self.tool_running_lines <= 0:
                self.tool_running_rendered = False
                self.tool_running_lines = 0
                self.tool_running_started_at = None
                return
            lines = self.tool_running_lines
            self.tool_running_rendered = False
            self.tool_running_lines = 0
            self.tool_running_started_at = None
            for _ in range(lines):
                self._write("\033[A\r\033[2K")

    def _start_tool_block(self, payload: dict[str, Any]) -> None:
        self.start()
        self.clear_status()
        if self.line_open:
            self._write("\n")
            self.line_open = False
        self._reset_tool_state()
        self.tool_active = True
        self.tool_block_rendered = True
        self.saw_content = True
        self.tool_payload = dict(payload)
        heading = _tool_heading(payload)
        width = max(24, _terminal_render_width() - 8)
        self._write(f"\n{heading}\n")
        for index, line in enumerate(_tool_preview_lines(_shorten_tool_text(str(payload.get("command") or "")), width=width, max_lines=3)):
            prefix = _tool_prefix("CMD", first=index == 0)
            self._write(f"{prefix}{_style_tool_omission(line)}\n")

    def _finish_tool_block(self, payload: dict[str, Any]) -> None:
        self._clear_tool_running_block()
        self._emit_plain_block_direct(_render_tool_receipt_payload(payload), trailing_blank=True)
        self._reset_tool_state()

    def on_event(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "thinking_trace":
            text = _strip_context_percent_marker_text(str(payload.get("text") or "").strip())
            if text:
                role_label = str(payload.get("actor_label") or "").strip()
                trace_role = str(payload.get("role") or "").strip().lower()
                if not role_label and trace_role.startswith("planner"):
                    role_label = "主星"
                elif not role_label and trace_role.startswith("executor"):
                    role_label = "执行星"
                self.show_thinking_trace(
                    text,
                    elapsed_seconds=payload.get("elapsed_seconds"),
                    role_label=role_label,
                    actor_name=payload.get("actor_name") or "",
                    context_percent=payload.get("context_budget_percent") or payload.get("context_percent"),
                )
                payload["_frontend_rendered"] = True
            return
        if kind == "stream_limit":
            if bool(payload.get("soft")):
                self.show_status("thinking")
                return
            self._clear_live_reasoning()
            self.reasoning_buffer = []
            self.reasoning_started_at = None
            self.last_reasoning_render_at = 0.0
            note = str(payload.get("message") or payload.get("reason") or "流式输出已达到上限。")
            self._emit_plain_block_direct(_style_tool_line(f"  {note}", ANSI_SOFT_RED, bold=True), trailing_blank=True)
            self.show_status("thinking")
            return
        if kind == "tool_start":
            payload = self._with_current_actor_payload(payload)
            tool_name = str(payload.get("tool") or "")
            if tool_name in {"persona_handoff", "persona_link", "liaison"}:
                self._flush_markdown_pending(force=True)
                if self.pending_tool_receipts:
                    self._flush_pending_tool_receipts()
                elapsed_seconds = None
                if self.reasoning_started_at is not None:
                    elapsed_seconds = max(0.0, time.monotonic() - self.reasoning_started_at)
                self._fold_reasoning_summary(elapsed_seconds)
                self._reset_tool_state()
                if tool_name == "persona_handoff":
                    self._apply_persona_handoff_payload(payload)
                return
            self._flush_markdown_pending(force=True)
            elapsed_seconds = None
            if self.reasoning_started_at is not None:
                elapsed_seconds = max(0.0, time.monotonic() - self.reasoning_started_at)
            self._fold_reasoning_summary(elapsed_seconds)
            family = _tool_group_family(payload)
            if family:
                if self.pending_tool_receipts and _tool_group_family(self.pending_tool_receipts[-1]) != family:
                    self._flush_pending_tool_receipts()
                self.start()
                self.clear_status()
                if self.line_open:
                    self._write("\n")
                    self.line_open = False
                self._reset_tool_state()
                self.tool_active = True
                self.tool_group_family = family
                self.saw_content = True
                self.tool_payload = dict(payload)
                if family != "explore":
                    self._render_tool_running_block(payload)
            else:
                if self.pending_tool_receipts:
                    self._flush_pending_tool_receipts()
                self.start()
                self.clear_status()
                if self.line_open:
                    self._write("\n")
                    self.line_open = False
                self._reset_tool_state()
                self.tool_active = True
                self.saw_content = True
                self.tool_payload = dict(payload)
                self._render_tool_running_block(payload)
            return
        if kind in {"tool_stdout", "tool_stderr"}:
            payload = self._with_current_actor_payload(payload)
            stream = "stdout" if kind == "tool_stdout" else "stderr"
            if not self.tool_active:
                self.start()
                self.clear_status()
                self._reset_tool_state()
                self.tool_active = True
                self.tool_payload = dict(payload)
                if _tool_group_family(payload) != "explore":
                    self._render_tool_running_block(payload)
            self.tool_stream_seen[stream] = self.tool_stream_seen.get(stream, 0) + len(str(payload.get("text") or ""))
            self.tool_saw_output = True
            return
        if kind == "tool_result":
            elapsed_seconds = None
            if self.reasoning_started_at is not None:
                elapsed_seconds = max(0.0, time.monotonic() - self.reasoning_started_at)
            self._fold_reasoning_summary(elapsed_seconds)
            self._update_context_budget(payload)
            payload = self._with_current_actor_payload(payload)
            tool_name = str(payload.get("tool") or "")
            action_name = str(payload.get("action") or payload.get("speaker_mode") or "").strip().lower()
            if (
                tool_name == "persona_handoff"
                or (tool_name == "persona_link" and action_name == "switch")
                or (tool_name == "link" and action_name == "switch")
            ):
                self._clear_tool_running_block()
                self._reset_tool_state()
                self._apply_persona_handoff_payload(payload)
                rendered = _render_tool_receipt_payload(payload)
                if rendered:
                    self._emit_plain_block_direct(rendered, trailing_blank=True)
                payload["_frontend_rendered"] = True
                return
            if self.tool_active:
                if self.tool_block_rendered:
                    self._finish_tool_block(payload)
                    if str(payload.get("tool") or "") in {"persona_link", "liaison"}:
                        payload["_frontend_rendered"] = True
                else:
                    self._clear_tool_running_block()
                    self._queue_pending_tool_receipt(payload)
                    self._reset_tool_state()
            else:
                if _tool_group_family(payload):
                    self._queue_pending_tool_receipt(payload)
                else:
                    self._flush_pending_tool_receipts()
                    rendered = _render_tool_receipt_payload(payload)
                    if rendered:
                        self._emit_plain_block_direct(rendered, trailing_blank=True)
                    if str(payload.get("tool") or "") in {"persona_link", "liaison"}:
                        payload["_frontend_rendered"] = True
            if tool_name == "link" and action_name == "continue" and str(payload.get("target") or "").strip().lower() in {"executor", "liaison"}:
                self._apply_executor_handoff_payload(payload)

    def _sleep_for_char(self, char: str, burst_count: int) -> int:
        if not self.typewriter_enabled:
            return burst_count
        punctuation_delay_ms = max(0, int(self.typing.get("punctuation_delay_ms", 10)))
        char_delay_ms = max(0, int(self.typing.get("char_delay_ms", 2)))
        burst_chars = max(1, int(self.typing.get("burst_chars", 3)))

        if char in "\n":
            return 0
        if char in "，。！？；：、,.!?;:" and punctuation_delay_ms > 0:
            time.sleep(punctuation_delay_ms / 1000.0)
            return 0
        if burst_count >= burst_chars and char_delay_ms > 0:
            time.sleep(char_delay_ms / 1000.0)
            return 0
        return burst_count

    def _write_indented(self, text: str) -> None:
        if not text:
            return
        self.start()
        self.clear_status()
        plain_len = len(_strip_ansi(text))
        bulk_threshold = _typewriter_bulk_threshold()
        bulk_mode = (
            not self.typewriter_enabled
            or (bulk_threshold > 0 and plain_len >= bulk_threshold)
            or "```" in text
            or "\n|" in text
        )
        burst_count = 0
        for token in _tokenize_ansi(text):
            if not token:
                continue
            if ANSI_PATTERN.fullmatch(token):
                if not self.line_open:
                    self._write("  ", flush=not bulk_mode)
                    self.line_open = True
                self._write(token, flush=not bulk_mode)
                continue
            for char in token:
                if char == "\n":
                    self._write("\n", flush=not bulk_mode)
                    self.line_open = False
                    burst_count = 0
                    continue
                if not self.line_open:
                    self._write("  ", flush=not bulk_mode)
                    self.line_open = True
                self._write(char, flush=not bulk_mode)
                burst_count += 1
                burst_count = self._sleep_for_char(char, burst_count)
        if bulk_mode:
            sys.stdout.flush()

    def on_delta(self, kind: str, text: str) -> None:
        if kind == "reasoning":
            self._flush_pending_tool_receipts()
            if text:
                if self.reasoning_started_at is None:
                    self.reasoning_started_at = time.monotonic()
                self.reasoning_buffer.append(text)
                cleaned_reasoning = _strip_context_percent_marker_text("".join(self.reasoning_buffer))
                self.reasoning_buffer = [cleaned_reasoning] if cleaned_reasoning else []
                if not self.saw_content:
                    self.show_status("thinking")
            elif not self.saw_content:
                self.show_status("thinking")
            return
        if kind != "content" or not text:
            return
        self.flush_reasoning_trace()
        self._flush_pending_tool_receipts()
        cleaned = self.sanitizer.push(text)
        if not cleaned:
            return
        self._queue_markdown_stream_text(cleaned)

    def emit_message(self, text: str) -> None:
        self._flush_markdown_pending(force=True)
        self._flush_pending_tool_receipts()
        cleaned = self.sanitizer.push(text or "")
        cleaned += self.sanitizer.finish()
        body = ((cleaned or "").strip() or "我没有得到有效回复。") + "\n"
        self.saw_content = True
        self.assistant_content_seen = True
        self._write_indented(self.renderer.render(body))

    def emit_plain_block(self, text: str, *, trailing_blank: bool = True) -> None:
        self._flush_markdown_pending(force=True)
        self._flush_pending_tool_receipts()
        self._emit_plain_block_direct(text, trailing_blank=trailing_blank)

    def finish(self, fallback_message: str | None = None) -> None:
        self.stop_working_animation()
        self.flush_reasoning_trace()
        self._flush_pending_tool_receipts()
        tail = self.sanitizer.finish()
        if tail:
            self._queue_markdown_stream_text(tail)
        self._flush_markdown_pending(force=True)
        fallback = str(fallback_message or "")
        if not self.assistant_content_seen:
            if fallback.strip() or not self.saw_content:
                self.emit_message(fallback or "我没有得到有效回复。")
            else:
                self.clear_status()
        else:
            self.clear_status()
        if self.line_open:
            self._write("\n")
            self.line_open = False
        self._write("\n")


class ChatCore(ProjectLingEngine):
    """Compatibility shim for the old in-package API."""

    def chat(
        self,
        user_message: str,
        *,
        cwd: str | Path | None = None,
        history: list[dict[str, Any]] | None = None,
        allow_tools: bool | None = None,
        system_prompt: str | None = None,
        stream: bool = False,
        on_stream_delta: Any = None,
        on_stream_event: Any = None,
        mode: str = "chat",
    ) -> ChatResult:
        del history, system_prompt
        return super().chat(
            user_message,
            cwd=cwd,
            mode=mode,
            allow_tools=allow_tools,
            stream=stream,
            on_stream_delta=on_stream_delta,
            on_stream_event=on_stream_event,
        )


def _tool_preview_lines(
    text: str,
    *,
    width: int,
    head_lines: int = TOOL_PREVIEW_HEAD_LINES,
    tail_lines: int = TOOL_PREVIEW_TAIL_LINES,
    max_lines: int | None = None,
) -> list[str]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    source_lines = [line.expandtabs(2).rstrip() for line in normalized.splitlines() if line.strip()]
    if not source_lines:
        return ["—"]

    if max_lines is not None:
        head_lines = max(1, (max_lines - 1) // 2)
        tail_lines = max(1, max_lines - head_lines - 1)

    visible_limit = max(1, head_lines) + max(1, tail_lines)
    if len(source_lines) > visible_limit:
        omitted = len(source_lines) - visible_limit
        selected = source_lines[:head_lines]
        selected.append(f"...   +{omitted} lines")
        selected.extend(source_lines[-tail_lines:])
    else:
        selected = source_lines
    lines = [_truncate_display_ellipsis(line, width) for line in selected]
    return lines


def _tool_line_count(text: str) -> int:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return len([line for line in normalized.splitlines() if line.strip()])


def _shorten_tool_text(text: str) -> str:
    value = str(text or "")
    home = str(Path.home())
    replacements = (
        ("/data/data/com.termux/files/home", "~"),
        (home, "~"),
        ("/data/data/com.termux/files/usr", "$PREFIX"),
    )
    for src, dst in replacements:
        if src and src in value:
            value = value.replace(src, dst)
    value = PATHLIKE_TOKEN_RE.sub(lambda match: _shorten_path_token(match.group(0)), value)
    return value


def _tool_heading_base(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "")
    channel = str(payload.get("channel") or payload.get("tool") or "Tool")
    tool = str(payload.get("tool") or "")
    if tool == "apply_patch":
        return "● Edit File"
    if tool == "link":
        return "● X-Link"
    if tool == "update_plan":
        return "● 计划"
    if tool == "model_mode":
        return "● 协作模式"
    if tool == "diary_keeper":
        return "● Diary Keeper"
    if tool == "persona_link":
        action = str(payload.get("action") or "").strip().lower()
        if action == "switch":
            return "● 角色切换"
        if action == "mission":
            return "● Mission"
        if action in {"send", "contact", "liaison"}:
            return "● 执行星"
        return "● Persona Link"
    if tool == "liaison":
        return "● Liaison"
    if tool == "memory_add":
        return "● Memory Add"
    if tool == "memory_check":
        return "● Memory Check"
    if tool == "memory_read":
        return "● Memory Read"
    if tool == "memory_status":
        return "● Memory Status"
    if tool == "web_search":
        return "● WebSearch"
    if tool == "context":
        return "● Context"
    if tool in {"context_manage", "contextmanage"}:
        return "● Context Manage"
    if tool == "tool_manage":
        return "● Tool Box"
    if tool == "aidebug":
        return "● Explored"
    if status == "pending_confirmation":
        return f"● Confirm {channel}"
    if status == "rejected":
        return f"● Canceled {channel}"
    if channel == "Bash":
        return "● Ran COMMAND"
    return f"● Ran {channel}"


def _tool_heading_color(payload: dict[str, Any]) -> str:
    channel = str(payload.get("channel") or payload.get("tool") or "Tool")
    tool = str(payload.get("tool") or "")
    if tool == "apply_patch":
        return ANSI_MAGENTA
    if tool == "link":
        return ANSI_VIOLET
    if tool == "update_plan":
        return ANSI_SOFT_BLUE
    if tool == "model_mode":
        return ANSI_SOFT_BLUE
    if tool == "diary_keeper":
        return ANSI_SOFT_BLUE
    if tool == "persona_link":
        action = str(payload.get("action") or "").strip().lower()
        if action == "switch":
            return ANSI_GOLD
        if action == "mission":
            return ANSI_VIOLET
        if action in {"send", "contact", "liaison"}:
            return ANSI_CYAN
        return ANSI_CYAN
    if tool == "liaison":
        return ANSI_CYAN
    if tool in {"memory_add", "memory_check", "memory_read", "memory_status"}:
        return ANSI_SOFT_BLUE
    if tool == "web_search":
        return ANSI_CYAN
    if tool == "context":
        return ANSI_CYAN
    if tool in {"context_manage", "contextmanage"}:
        return ANSI_MAGENTA
    if tool == "tool_manage":
        return ANSI_CYAN
    if tool == "aidebug":
        return ANSI_SOFT_BLUE
    if channel == "ADB":
        return ANSI_SOFT_PINK
    if channel == "Termux API":
        return ANSI_MAGENTA
    return ANSI_GOLD


def _tool_command_summary(command: str, *, width: int = 44) -> str:
    text = _shorten_tool_text(command).strip()
    if not text:
        return ""
    return _middle_truncate_display(text, width)


def _tool_actor_text(payload: dict[str, Any], *, width: int = 42) -> str:
    label = str(payload.get("actor_label") or "").strip()
    name = str(payload.get("actor_name") or "").strip()
    actor_kind = str(payload.get("actor_kind") or "").strip().lower()
    if actor_kind == "executor" and not label:
        label = "执行星"
    elif actor_kind == "planner" and not label:
        label = "主星"
    if not label and not name:
        return ""
    if actor_kind == "executor" or label in {"执行位", "执行星", "Executor"}:
        symbol = "↳"
    elif actor_kind == "planner" or label in {"主角色", "主星", "Planner"}:
        symbol = "◇"
    elif label in {"辅导位", "执行星", "Liaison"}:
        symbol = "◌"
    else:
        symbol = "·"
    name_text = _normalize_identity_name(name)
    display_parts = [part for part in (name_text, label) if part]
    context_percent = payload.get("context_budget_percent") or payload.get("context_percent")
    if context_percent is not None and context_percent != "":
        try:
            display_parts.append(f"CTK{max(0, min(100, int(context_percent)))}%")
        except (TypeError, ValueError):
            display_parts.append(f"CTK{context_percent}")
    display_name = " · ".join(display_parts) or label
    text = _middle_truncate_display(_shorten_tool_text(f"{symbol} {display_name}"), width)
    if _supports_tty_control() and display_name:
        plain_name = _middle_truncate_display(_shorten_tool_text(display_name), max(8, width - 2))
        return f"{symbol} {ANSI_ITALIC}{plain_name}{ANSI_RESET}"
    return text


def _should_render_tool_actor(payload: dict[str, Any]) -> bool:
    if bool(payload.get("_suppress_actor_line")):
        return False
    actor_kind = str(payload.get("actor_kind") or "").strip().lower()
    return bool(actor_kind or str(payload.get("actor_label") or "").strip() or str(payload.get("actor_name") or "").strip())


def _tool_manage_name_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, dict):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = []

    names: list[str] = []
    for item in items:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("tool") or "").strip()
            if not name:
                name = str(item.get("summary") or "").strip()
            if not name:
                continue
            if "expanded" in item:
                state = "expanded" if item.get("expanded") else "collapsed"
                names.append(f"{name} ({state})")
            else:
                names.append(name)
            continue
        text = str(item or "").strip()
        if text:
            names.append(text)
    return names


def _tool_explore_target(command: str, *, width: int = 58) -> str:
    tokens = _split_shell_words(command)
    if not tokens:
        return ""
    candidates = [token for token in tokens[1:] if "/" in token or token.startswith(("~", "$PREFIX"))]
    target = candidates[-1] if candidates else " ".join(tokens[1:]) if len(tokens) > 1 else tokens[0]
    return _middle_truncate_display(_shorten_tool_text(target), width)


def _command_is_explore_readonly(tokens: list[str]) -> bool:
    if not tokens:
        return False
    first = tokens[0]
    if first == "sed":
        return not any(token == "-i" or token == "--in-place" or token.startswith("-i") for token in tokens[1:])
    if first == "find":
        return not any(token in FIND_MUTATING_TOKENS or token.startswith("-exec") or token.startswith("-ok") for token in tokens[1:])
    return True


def _powershell_explore_kind(command: str) -> str | None:
    text = str(command or "").strip()
    if not text:
        return None
    lowered = text.lower()
    tokens = _split_shell_words(text)
    first = tokens[0].lower().rstrip(".exe") if tokens else ""
    if first not in {"powershell", "pwsh"}:
        return None
    mutating_markers = (
        " set-content",
        " add-content",
        " out-file",
        " remove-item",
        " move-item",
        " copy-item",
        " new-item",
        " rename-item",
        " clear-content",
        " start-process",
        ">",
        ">>",
    )
    if any(marker in lowered for marker in mutating_markers):
        return None
    if "select-string" in lowered:
        return "Search"
    if any(marker in lowered for marker in ("get-content", "gc ", "type ", "select-object")):
        return "Read"
    if any(marker in lowered for marker in ("get-childitem", "dir ", "ls ")):
        return "List"
    return None


def _powershell_explore_target(command: str, *, width: int = 58) -> str:
    text = _shorten_tool_text(str(command or ""))
    path_match = re.search(r"(?i)(?:-LiteralPath|-Path)\s+(['\"]?)([^'\"\s|]+)\1", text)
    if path_match:
        return _middle_truncate_display(path_match.group(2), width)
    quoted = re.findall(r"['\"]([^'\"]+\.[A-Za-z0-9_.-]+)['\"]", text)
    if quoted:
        return _middle_truncate_display(quoted[-1], width)
    return _tool_command_summary(text, width=width)


def _tool_group_family(payload: dict[str, Any]) -> str | None:
    status = str(payload.get("status") or "").strip()
    if status and status != "ok":
        return None
    if str(payload.get("stderr") or "").strip():
        return None
    tool = str(payload.get("tool") or "")
    if tool == "aidebug":
        action = str(payload.get("action") or "").strip().lower()
        return "explore" if action in {"read", "status"} else None
    if tool != "command" or str(payload.get("channel") or "") != "Bash":
        return None
    command = str(payload.get("command") or "")
    if _powershell_explore_kind(command):
        return "explore"
    tokens = _split_shell_words(command)
    if not tokens:
        return None
    first = tokens[0]
    if first in EXPLORE_SEARCH_COMMANDS or first in EXPLORE_READ_COMMANDS or first in EXPLORE_LIST_COMMANDS:
        if not _command_is_explore_readonly(tokens):
            return None
        return "explore"
    return None


def _tool_explore_label(payload: dict[str, Any]) -> str:
    if str(payload.get("tool") or "") == "aidebug":
        action = str(payload.get("action") or "").strip().lower()
        if action == "status":
            return "Status"
        return "Read"
    power_kind = _powershell_explore_kind(str(payload.get("command") or ""))
    if power_kind:
        return power_kind
    tokens = _split_shell_words(str(payload.get("command") or ""))
    first = tokens[0] if tokens else ""
    if first in EXPLORE_SEARCH_COMMANDS:
        return "Search"
    if first in EXPLORE_LIST_COMMANDS:
        return "List"
    return "Read"


def _tool_group_entry(payload: dict[str, Any], family: str) -> str:
    if family == "explore":
        if str(payload.get("tool") or "") == "aidebug":
            target = _shorten_tool_text(
                str(payload.get("relative_path") or payload.get("path") or payload.get("log_path") or "aidebug")
            )
            mode = str(payload.get("mode") or "").strip().lower()
            suffix = f" · {mode}" if mode and mode != "tail" else ""
            return f"{_tool_explore_label(payload)} {target or 'aidebug'}{suffix}"
        command = str(payload.get("command") or "")
        power_kind = _powershell_explore_kind(command)
        if power_kind:
            target = _powershell_explore_target(command, width=58) or "powershell"
            if power_kind == "Search":
                return f"Search in {target}"
            return f"{power_kind} {target}"
        command_text = _tool_explore_target(command, width=58) or _tool_command_summary(command, width=58) or "command"
        label = _tool_explore_label(payload)
        if label == "Search":
            return f"Search in {command_text}"
        return f"{label} {command_text}"
    return f"Brief {_tool_command_summary(str(payload.get('command') or ''), width=52) or _tool_brief(payload)}"


def _render_grouped_tool_receipt(payloads: list[dict[str, Any]], family: str) -> str:
    first = payloads[0]
    heading_plain = "● Explored" if family == "explore" else _tool_heading_base(first)
    heading = heading_plain
    if _supports_tty_control():
        heading = f"{ANSI_BOLD}{_tool_heading_color(first)}{heading_plain}{ANSI_RESET}"
    entry_width = max(18, _terminal_render_width() - 4)
    entries = [_tool_group_entry(payload, family) for payload in payloads]
    lines = [heading]
    actor_line = _tool_running_actor_line(first)
    if actor_line:
        lines.append(actor_line)
    for entry in entries:
        lines.append(_style_tool_line(f"└ {_middle_truncate_display(entry, entry_width)}", ANSI_WHITE, dim=True))
    return "\n".join(lines)


def _tool_brief(payload: dict[str, Any]) -> str:
    tool = str(payload.get("tool") or "")
    explicit = str(payload.get("brief") or "").strip()
    if explicit:
        return explicit
    command = _shorten_tool_text(str(payload.get("command") or "").strip())
    status = str(payload.get("status") or "").strip()

    if tool == "web_search":
        query = str(payload.get("query") or "").strip()
        intent = str(payload.get("summary") or payload.get("message") or payload.get("title") or "").strip()
        if intent:
            return _middle_truncate_display(_shorten_tool_text(intent), 44)
        return f"搜索{query or 'query'}"
    if tool == "apply_patch":
        changed = payload.get("changed_files") or []
        if isinstance(changed, list) and changed:
            return f"修改 {changed[0]}{' 等' if len(changed) > 1 else ''}"
        return "应用补丁"
    if tool == "link":
        action = str(payload.get("action") or "continue").strip().lower()
        target = str(payload.get("target") or "").strip().lower()
        message = str(payload.get("message") or payload.get("task") or "").strip()
        head = f"{action} {target}".strip()
        if message:
            return f"{head} · {_middle_truncate_display(_shorten_tool_text(message), 36)}" if head else _middle_truncate_display(_shorten_tool_text(message), 44)
        return head or "X-Link"
    if tool == "update_plan":
        action = str(payload.get("action") or "status").strip().lower()
        mode = str(payload.get("mode") or "todo").strip().lower()
        title = str(payload.get("title") or "").strip()
        head = f"{mode}/{action}"
        return f"{head} · {_middle_truncate_display(_shorten_tool_text(title), 36)}" if title else head
    if tool == "model_mode":
        mode = str(payload.get("mode") or "").strip()
        action = str(payload.get("action") or "status").strip()
        return f"{action} {mode}".strip()
    if tool == "diary_keeper":
        date = str(payload.get("date") or "").strip()
        return f"更新日记 {date}".strip()
    if tool == "persona_link":
        action = str(payload.get("action") or "").strip().lower()
        if action == "switch":
            target = str(payload.get("target") or payload.get("speaker_mode") or "liaison").strip().lower()
            return "切换执行星" if target == "liaison" else "切回主星"
        if action == "mission":
            task = str(payload.get("task") or payload.get("message") or "").strip()
            return _middle_truncate_display(_shorten_tool_text(task), 44) if task else "mission"
        if action in {"send", "contact", "liaison"}:
            message = str(payload.get("message") or payload.get("question") or payload.get("prompt") or "").strip()
            if message:
                return _middle_truncate_display(_shorten_tool_text(message), 44)
            return action
        return "role link"
    if tool == "terminal":
        action = str(payload.get("action") or "start").strip()
        session = str(payload.get("session_name") or "").strip()
        return f"{action} {session}".strip()
    if tool == "aidebug":
        action = str(payload.get("action") or "status").strip()
        return f"aidebug {action}"
    if tool == "context":
        percent = _payload_percent(payload)
        try:
            turns = int(payload.get("turns_remaining") or payload.get("turns") or 1)
        except (TypeError, ValueError):
            turns = 1
        if percent >= 100:
            return "100% full"
        suffix = f"{turns} turns" if turns > 1 else "next turn"
        return f"{percent}% {suffix}".strip()
    if tool in {"memory_add", "memory_check", "memory_read", "memory_status"}:
        action = str(payload.get("action") or "").strip().lower()
        if tool == "memory_add":
            date = str(payload.get("date") or "").strip()
            keywords = payload.get("keywords") or []
            kw_text = f"{len(keywords)} kw" if isinstance(keywords, list) else "kw"
            return f"{date} · {kw_text}".strip(" ·")
        if tool == "memory_check":
            keywords = payload.get("keywords") or []
            return f"{len(keywords)} kw" if isinstance(keywords, list) else "memory_check"
        if tool == "memory_read":
            requested = payload.get("requested") or payload.get("dates") or []
            return f"{len(requested)} dates" if isinstance(requested, list) else "memory_read"
        if action == "clear_datememory":
            return "clear datememory"
        return "status"
    if tool == "liaison":
        rounds = payload.get("rounds")
        label = str(payload.get("liaison_name") or payload.get("name") or "liaison")
        if isinstance(rounds, int) and rounds > 0:
            return f"{label} · {rounds} rounds"
        return label
    if tool in {"context_manage", "contextmanage"}:
        mode = str(payload.get("mode") or "compact").strip()
        target = str(payload.get("target") or "both").strip()
        return f"{mode} {target}".strip()
    if tool == "tool_manage":
        action = str(payload.get("action") or "list").strip().lower()
        if action == "list":
            expanded = payload.get("expanded_count")
            total = payload.get("total_count")
            if isinstance(expanded, int) and isinstance(total, int) and total > 0:
                return f"{expanded}/{total} tools"
            if isinstance(total, int) and total > 0:
                return f"{total} tools"
            return "list"
        if action == "inspect":
            names = _tool_manage_name_list(payload.get("tools"))
            return f"inspect {', '.join(names[:3])}" if names else "inspect tool"
        if action in {"expand", "collapse"}:
            names = _tool_manage_name_list(payload.get("requested") or payload.get("changed") or payload.get("tools") or payload.get("tool"))
            return f"{action} {', '.join(names[:3])}" if names else action
        if action in {"expand_all", "collapse_all", "reset"}:
            return action.replace("_", " ")
        return action or "toolbox"
    if command:
        return _tool_command_summary(command, width=42)
    return status or "tool event"


def _tool_heading(payload: dict[str, Any]) -> str:
    base = _tool_heading_base(payload)
    if not _supports_tty_control():
        return base
    color = _tool_heading_color(payload)
    return f"{ANSI_BOLD}{color}{base}{ANSI_RESET}"


def _tool_status_text(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "").strip()
    if status in STATUS_SUCCESS_TEXT:
        return STATUS_SUCCESS_TEXT[status]
    if status == "pending_confirmation":
        return "Pending"
    if status in {"error", "blocked", "timeout", "rejected"}:
        return "Failed"
    return status.capitalize() if status else "Done"


def _tool_count_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    stdout = str(payload.get("stdout") or "")
    stderr = str(payload.get("stderr") or "")
    patch = str(payload.get("patch") or "")
    tool = str(payload.get("tool") or "")
    lines = 0
    chars = 0
    if tool == "tool_manage":
        total = payload.get("total_count")
        expanded = payload.get("expanded_count")
        changed = payload.get("changed")
        tools = payload.get("tools")
        if isinstance(total, int) and total >= 0:
            parts.append(f"↗{total} tools")
        elif isinstance(tools, list) and tools:
            parts.append(f"↗{len(tools)} tools")
        if isinstance(expanded, int) and expanded >= 0:
            parts.append(f"{expanded} visible")
        if isinstance(changed, list) and changed:
            parts.append(f"{len(changed)} changed")
        return " · ".join(parts) if parts else "↗0 tools"
    if tool == "link":
        steps = payload.get("steps") or []
        if isinstance(steps, list) and steps:
            return f"↗{len(steps)} steps"
        action = str(payload.get("action") or "").strip().lower()
        if action == "switch":
            return "↗1 switch"
        return "↗1 link"
    if tool == "update_plan":
        total = payload.get("total_count")
        completed = payload.get("completed_count")
        if isinstance(total, int) and isinstance(completed, int):
            return f"↗{completed}/{total} steps"
        items = payload.get("items") or []
        if isinstance(items, list):
            return f"↗{len(items)} steps"
        return "↗0 steps"
    if tool == "model_mode":
        return "↗1 mode"
    if tool == "diary_keeper":
        keywords = payload.get("keywords") or []
        if isinstance(keywords, list):
            return f"↗{len(keywords)} kw"
        return "↗1 diary"
    if tool == "persona_link":
        action = str(payload.get("action") or "").strip().lower()
        if action == "switch":
            return "↗1 switch"
        if action == "mission":
            return "↗1 task"
        rounds = payload.get("rounds")
        if isinstance(rounds, int) and rounds > 0:
            parts.append(f"↗{rounds} rounds")
        transcript = payload.get("transcript") or []
        if isinstance(transcript, list) and transcript:
            parts.append(f"↗{len(transcript)} turns")
        return " · ".join(parts) if parts else "↗0 rounds"
    if tool == "liaison":
        rounds = payload.get("rounds")
        if isinstance(rounds, int) and rounds > 0:
            parts.append(f"↗{rounds} rounds")
        transcript = payload.get("transcript") or []
        if isinstance(transcript, list) and transcript:
            parts.append(f"↗{len(transcript)} turns")
        return " · ".join(parts) if parts else "↗0 rounds"
    if tool == "memory_add":
        keywords = payload.get("keywords") or []
        if isinstance(keywords, list):
            return f"↗{len(keywords)} kw"
        return "↗1 diary"
    if tool == "memory_check":
        result_count = payload.get("result_count")
        if isinstance(result_count, int):
            return f"↗{result_count} hits"
        return "↗0 hits"
    if tool == "memory_read":
        found = payload.get("found")
        if isinstance(found, int):
            return f"↗{found} entries"
        return "↗0 entries"
    if tool == "memory_status":
        days = payload.get("datememory_days")
        diaries = payload.get("memory_db_diaries")
        parts = []
        if isinstance(days, int):
            parts.append(f"↗{days} days")
        if isinstance(diaries, int):
            parts.append(f"↗{diaries} diaries")
        return " · ".join(parts) if parts else "↗0"
    if tool == "terminal":
        try:
            lines = int(payload.get("log_lines") or 0)
        except (TypeError, ValueError):
            lines = 0
        try:
            chars = int(payload.get("log_size") or payload.get("log_bytes") or 0)
        except (TypeError, ValueError):
            chars = 0
    elif patch:
        lines = _tool_line_count(patch)
        chars = len(patch)
    else:
        lines = _tool_line_count(stdout) + _tool_line_count(stderr)
        chars = len(stdout) + len(stderr)
    if lines:
        parts.append(f"↗{lines} Lines")
    if chars:
        parts.append(f"↗{chars} chars")
    return " · ".join(parts) if parts else "↗0 Lines · ↗0 chars"


def _tool_input_kind(payload: dict[str, Any]) -> str:
    tool = str(payload.get("tool") or "")
    if tool == "link":
        action = str(payload.get("action") or "").strip().lower()
        return f"X-Link/{action or 'continue'}"
    if tool == "update_plan":
        action = str(payload.get("action") or "status").strip().lower()
        mode = str(payload.get("mode") or "todo").strip().lower()
        return f"Plan/{mode}/{action}"
    if tool == "model_mode":
        action = str(payload.get("action") or "status").strip().lower()
        return f"模式/{action}"
    if tool == "diary_keeper":
        return "Diary"
    if tool == "persona_link":
        action = str(payload.get("action") or "").strip().lower()
        return f"Role/{action or 'link'}"
    if tool == "web_search":
        mode = str(payload.get("mode_used") or payload.get("mode") or "auto").strip()
        return mode.capitalize()
    if tool == "terminal":
        action = str(payload.get("action") or "start").strip()
        return f"Terminal/{action}"
    if tool == "aidebug":
        action = str(payload.get("action") or "status").strip()
        return f"Explore/{action}"
    if tool == "context":
        percent = _payload_percent(payload)
        return f"CTK{percent}%"
    if tool == "liaison":
        return "Liaison"
    if tool in {"memory_add", "memory_check", "memory_read", "memory_status", "diary_keeper"}:
        return "Memory"
    if tool in {"context_manage", "contextmanage"}:
        mode = str(payload.get("mode") or "compact").strip()
        target = str(payload.get("target") or "both").strip()
        return f"CTK/{mode}/{target}"
    if tool == "tool_manage":
        action = str(payload.get("action") or "list").strip().lower()
        return f"Tool/{action}"
    channel = str(payload.get("channel") or "")
    if channel:
        return "Command" if channel == "Bash" else channel
    return tool or "Tool"


def _tool_input_text(payload: dict[str, Any]) -> str:
    tool = str(payload.get("tool") or "")
    if tool == "link":
        action = str(payload.get("action") or "").strip().lower()
        if action == "switch":
            target = str(payload.get("target") or payload.get("speaker_mode") or "liaison").strip()
            return f"target {target}"
        steps = payload.get("steps") or []
        message = str(payload.get("message") or payload.get("task") or "").strip()
        if isinstance(steps, list) and steps:
            step_text = "; ".join(str(item).strip() for item in steps[:3] if str(item).strip())
            return f"{message} · {step_text}" if message else step_text
        return message or str(payload.get("brief") or "X-Link")
    if tool == "update_plan":
        title = str(payload.get("title") or "").strip()
        active = payload.get("active_item") or {}
        if isinstance(active, dict):
            active_title = str(active.get("title") or "").strip()
            if active_title:
                return f"{title} · {active_title}" if title else active_title
        return title or str(payload.get("message") or "plan")
    if tool == "model_mode":
        mode = str(payload.get("mode") or "").strip()
        planner = str(payload.get("planner_model") or "").strip()
        executor = str(payload.get("executor_model") or "").strip()
        pair = f"{planner} -> {executor}".strip(" ->")
        return f"{mode} · {pair}".strip(" ·") or "status"
    if tool == "diary_keeper":
        return str(payload.get("date") or payload.get("message") or "diary")
    if tool == "persona_link":
        action = str(payload.get("action") or "").strip().lower()
        if action == "switch":
            target = str(payload.get("target") or payload.get("speaker_mode") or "liaison").strip()
            return f"target {target}"
        if action == "mission":
            task = str(payload.get("task") or payload.get("message") or "").strip()
            objective = str(payload.get("objective") or "").strip()
            return f"{task} · {objective}" if objective else task or "mission"
        if action in {"send", "contact", "liaison"}:
            return str(payload.get("message") or payload.get("question") or payload.get("prompt") or "—")
        return str(payload.get("message") or "—")
    if tool == "web_search":
        return str(payload.get("query") or "—")
    if tool == "apply_patch":
        changed = payload.get("changed_files") or []
        if isinstance(changed, list) and changed:
            return ", ".join(_shorten_tool_text(str(item)) for item in changed[:3])
        return str(payload.get("message") or "patch")
    if tool == "terminal":
        text = str(payload.get("command") or payload.get("session_name") or payload.get("message") or "—")
        return text
    if tool == "aidebug":
        return str(payload.get("relative_path") or payload.get("path") or payload.get("log_path") or payload.get("action") or "aidebug")
    if tool == "context":
        percent = _payload_percent(payload)
        try:
            turns = int(payload.get("turns_remaining") or payload.get("turns") or 1)
        except (TypeError, ValueError):
            turns = 1
        if percent >= 100:
            return f"CTK{percent}% · full"
        turns_text = f"{turns} turns" if turns > 1 else "next"
        return f"CTK{percent}% · {turns_text}"
    if tool == "liaison":
        return str(payload.get("question") or payload.get("prompt") or payload.get("message") or "—")
    if tool == "memory_add":
        return str(payload.get("date") or "—")
    if tool == "memory_check":
        keywords = payload.get("keywords") or []
        if isinstance(keywords, list):
            return ", ".join(_shorten_tool_text(str(item)) for item in keywords[:5])
        return str(payload.get("message") or "—")
    if tool == "memory_read":
        dates = payload.get("requested") or payload.get("dates") or []
        if isinstance(dates, list):
            return ", ".join(_shorten_tool_text(str(item)) for item in dates[:5])
        return str(payload.get("message") or "—")
    if tool == "memory_status":
        return str(payload.get("memory_dir") or payload.get("datememory_path") or "—")
    if tool in {"context_manage", "contextmanage"}:
        saved = str(payload.get("saved_chars") or "0")
        target = str(payload.get("target") or "both")
        recommendation = str(payload.get("recommendation") or "").strip()
        if recommendation:
            return f"{target} · saved {saved} chars · {recommendation}"
        return f"{target} · saved {saved} chars"
    if tool == "tool_manage":
        action = str(payload.get("action") or "list").strip().lower()
        if action == "list":
            total = payload.get("total_count")
            expanded = payload.get("expanded_count")
            if isinstance(expanded, int) and isinstance(total, int) and total > 0:
                return f"{expanded}/{total} visible"
            if isinstance(total, int) and total > 0:
                return f"{total} tools"
            return "toolbox"
        if action == "inspect":
            names = _tool_manage_name_list(payload.get("tools"))
            return ", ".join(names[:4]) if names else "tool"
        names = _tool_manage_name_list(payload.get("requested") or payload.get("changed") or payload.get("tools") or payload.get("tool"))
        return ", ".join(names[:4]) if names else action
    return str(payload.get("command") or payload.get("message") or "—")


def _tool_meta_line(label: str, *parts: str, color: str = ANSI_WHITE) -> str:
    body = " · ".join(str(part).strip() for part in parts if str(part).strip())
    symbol = {
        "INPUT": "◇",
        "OUTPUT": "◆",
        "RUNNING": "◌",
        "WARN": "!",
        "HINT": "↳",
        "CTK": "◇",
        "CONFIRM": "!",
        "Edited": "◇",
        "结果": "◆",
        "提示": "!",
        "关键词": "◇",
        "数据库": "◇",
        "来源": "◇",
        "日期": "◇",
        "命中": "◇",
        "读取": "◇",
        "日记": "◇",
        "状态": "◇",
    }.get(str(label or "").strip(), "•")
    text = f"{symbol} {label} · {body}" if body else f"{symbol} {label}"
    return _style_tool_line(text, color, bold=True)


def _windows_ui_enabled() -> bool:
    return os.environ.get("PROJECTLING_WINDOWS_UI", "").strip().lower() in {"1", "true", "yes", "on"}


def _tool_frame_title(payload: dict[str, Any]) -> str:
    tool = str(payload.get("tool") or "").strip()
    action = str(payload.get("action") or "").strip().lower()
    status = str(payload.get("status") or "").strip().lower()
    status_prefix = "BLOCK " if status in {"blocked", "error", "timeout", "rejected"} else ""

    def detail(value: Any, width: int = 46) -> str:
        return _middle_truncate_display(_shorten_tool_text(str(value or "").strip()), width)

    if tool == "web_search":
        query = detail(payload.get("query") or _tool_brief(payload))
        return f"{status_prefix}SEARCH // {query}".rstrip()
    if tool == "command":
        command = detail(payload.get("command") or _tool_brief(payload))
        return f"{status_prefix}CMD // {command}".rstrip()
    if tool == "terminal":
        session = detail(payload.get("session_name") or payload.get("command") or _tool_brief(payload))
        return f"{status_prefix}TERMINAL // {action or 'run'} {session}".rstrip()
    if tool == "aidebug":
        target = detail(payload.get("relative_path") or payload.get("path") or payload.get("log_path") or _tool_brief(payload))
        return f"{status_prefix}AIDEBUG // {action or 'check'} {target}".rstrip()
    if tool == "link":
        target = str(payload.get("target") or "").strip().lower()
        route = f"{action or 'continue'}>{target}" if target else action or "link"
        context_percent = payload.get("context_percent")
        context_text = ""
        if context_percent not in {None, ""}:
            try:
                context_text = f"CTK{max(0, min(100, int(context_percent)))}%"
            except (TypeError, ValueError):
                context_text = f"CTK{context_percent}"
        suffix = " ".join(part for part in (route, context_text) if part)
        return f"{status_prefix}X-LINK // {suffix}".rstrip()
    if tool == "update_plan":
        progress = str(payload.get("progress_text") or "").strip()
        if not progress:
            total = payload.get("total_count")
            completed = payload.get("completed_count")
            if isinstance(total, int) and isinstance(completed, int):
                progress = f"{completed}/{total}"
        suffix = " ".join(part for part in (action or "status", progress) if part)
        return f"{status_prefix}PLAN // {suffix}".rstrip()
    if tool == "apply_patch":
        changed = payload.get("changed_files") or []
        target = ", ".join(str(item) for item in changed[:2]) if isinstance(changed, list) else ""
        return f"{status_prefix}EDIT // {detail(target or _tool_brief(payload))}".rstrip()
    if tool in {"context", "context_manage", "contextmanage"}:
        return f"{status_prefix}CTK // {detail(_tool_brief(payload))}".rstrip()
    if tool == "tool_manage":
        return f"{status_prefix}TOOLS // {detail(_tool_brief(payload))}".rstrip()
    if tool in {"memory_add", "memory_check", "memory_read", "memory_status", "diary_keeper"}:
        return f"{status_prefix}MEMORY // {detail(_tool_brief(payload))}".rstrip()
    if tool == "model_mode":
        return f"{status_prefix}MODE // {detail(_tool_brief(payload))}".rstrip()
    if tool in {"liaison", "persona_link"}:
        return f"{status_prefix}ROLE // {detail(_tool_brief(payload))}".rstrip()
    label = (tool or "TOOL").replace("_", " ").upper()
    brief = detail(_tool_brief(payload))
    return f"{status_prefix}{label} // {brief}".rstrip(" /")


def _windows_tool_frame(title: str, body: str, *, color: str = ANSI_CYAN) -> str:
    if not _windows_ui_enabled():
        return body
    width = max(24, min(96, _terminal_render_width() - 2))
    plain_title = ANSI_PATTERN.sub("", str(title or "TOOL")).strip() or "TOOL"
    header = f"▌ {_middle_truncate_display(plain_title, max(8, width - 5))}"
    if _display_width(header) + 3 <= width:
        header = f"{header} ╌╌"
    header = _pad_display(header, width)
    lines = [f"{ANSI_BOLD}{color}{header}{ANSI_RESET}" if _supports_tty_control() else header]
    lines.append("")
    content_width = max(12, width - 2)
    for raw in str(body or "").splitlines():
        if raw == "":
            lines.append("")
            continue
        for wrapped in _wrap_ansi_display(raw, content_width):
            content_line = f"  {wrapped}"
            lines.append(content_line + " " * max(0, width - _display_width(content_line)))
    if len(lines) == 2 and not str(body or "").strip():
        lines.append(_pad_display("  ◌ waiting", width))
    return "\n".join(lines)


def _tool_actor_signal_line(payload: dict[str, Any]) -> str:
    if not _should_render_tool_actor(payload):
        return ""
    actor_kind = str(payload.get("actor_kind") or "").strip().lower()
    label = str(payload.get("actor_label") or "").strip()
    name = _shorten_tool_text(str(payload.get("actor_name") or "").strip())
    if actor_kind == "executor":
        label = "执行星"
    elif actor_kind == "planner":
        label = "主星"
    label = label.replace("主角色", "主星").replace("执行位", "执行星") or ("执行星" if actor_kind == "executor" else "主星")
    value = _normalize_identity_name(name) or label
    context_percent = payload.get("context_budget_percent") or payload.get("context_percent")
    if context_percent is not None and context_percent != "":
        try:
            value = f"{value} · CTK{max(0, min(100, int(context_percent)))}%"
        except (TypeError, ValueError):
            value = f"{value} · CTK{context_percent}"
    width = max(18, min(94, _terminal_render_width() - 4))
    prefix = f"┆ {_pad_display(label, 6)} "
    available = max(4, width - _display_width(prefix))
    color = ANSI_VIOLET if label == "执行星" else ANSI_GOLD
    return _style_tool_line(f"{prefix}{_middle_truncate_display(value, available)}", color, bold=True)


def _tool_running_actor_line(payload: dict[str, Any]) -> str:
    if not _should_render_tool_actor(payload):
        return ""
    width = max(18, min(94, _terminal_render_width() - 4))
    actor_text = _tool_actor_text(payload, width=width)
    if not actor_text:
        return ""
    label = str(payload.get("actor_label") or "").strip()
    actor_kind = str(payload.get("actor_kind") or "").strip().lower()
    color = ANSI_VIOLET if actor_kind == "executor" or label == "执行星" else ANSI_GOLD
    return _style_tool_line(actor_text, color, bold=True)


def _dedupe_windows_tool_body(payload: dict[str, Any], body: str) -> str:
    lines = str(body or "").splitlines()
    if _windows_ui_enabled():
        while lines and not _strip_ansi(lines[0]).strip():
            lines.pop(0)
        if lines:
            first = _strip_ansi(lines[0]).strip()
            first_upper = first.upper().replace("-", "")
            tool = str(payload.get("tool") or "").strip()
            drop_inner = False
            if first.startswith("● "):
                drop_inner = True
            elif tool == "update_plan" and first_upper.startswith("▌ PLAN"):
                drop_inner = True
            elif tool == "link" and first_upper.startswith("▌ XLINK"):
                drop_inner = True
            if drop_inner:
                lines.pop(0)
                while lines and not _strip_ansi(lines[0]).strip():
                    lines.pop(0)
    actor_line = _tool_actor_signal_line(payload)
    if actor_line:
        actor_plain = _strip_ansi(actor_line).strip()
        if not any(_strip_ansi(line).strip() == actor_plain for line in lines[:3]):
            lines.insert(0, actor_line)
    return "\n".join(lines).strip()


def _tool_context_status_text(payload: dict[str, Any]) -> str:
    percent = _payload_percent(payload)
    try:
        turns = int(payload.get("turns_remaining") or payload.get("turns") or 1)
    except (TypeError, ValueError):
        turns = 1
    parts = [f"CTK{percent}%"]
    if percent >= 100:
        parts.append("full")
        return " · ".join(parts)
    turns_text = f"{turns} turns" if turns > 1 else "next"
    parts.append(turns_text)
    return " · ".join(parts)


def _should_suppress_tool_receipt(payload: dict[str, Any]) -> bool:
    if bool(payload.get("_frontend_rendered")):
        return True
    return str(payload.get("tool") or "") == "persona_handoff" and str(payload.get("status") or "") == "ok"


def _render_persona_chat_card(
    title: str,
    name: str,
    body: str,
    *,
    header_color: str,
    body_color: str,
    width: int,
) -> list[str]:
    header = f"▌ {title} // {name}".rstrip(" /")
    if _supports_tty_control():
        header = f"{ANSI_BOLD}{header_color}{header}{ANSI_RESET}"
    lines = [header]
    content = _shorten_tool_text(str(body or "").strip()) or "—"
    wrapped: list[str] = []
    for raw_line in content.splitlines() or ["—"]:
        wrapped.extend(_wrap_ansi_display(raw_line, max(16, width - 4)))
    if not wrapped:
        wrapped = ["—"]
    for line in wrapped:
        lines.append(_style_tool_line(f"  {line}", body_color, dim=True))
    return lines


def _render_liaison_receipt(payload: dict[str, Any]) -> str:
    width = max(24, _terminal_render_width() - 2)
    tool = str(payload.get("tool") or "")
    action = str(payload.get("action") or "").strip().lower()
    status = str(payload.get("status") or "").strip().lower()
    legacy_switch = tool == "persona_handoff"
    name = str(
        payload.get("liaison_name")
        or payload.get("speaker_name")
        or payload.get("name")
        or "执行星"
    ).strip()
    if (tool == "persona_link" and action == "switch") or legacy_switch:
        target = str(payload.get("target") or payload.get("speaker_mode") or "liaison").strip().lower()
        speaker = str(payload.get("speaker_name") or name).strip()
        standby_name = str(
            payload.get("main_name") if target == "liaison" else payload.get("liaison_name") or "执行星"
        ).strip()
        standby_role = "主星" if target == "liaison" else "执行星"
        speaker_role = "执行星" if target == "liaison" else "主星"
        note = str(payload.get("message") or "已切换当前说话者。").strip()
        context_percent = payload.get("context_percent")
        if context_percent not in {None, ""}:
            try:
                note += f"\nCTK{max(0, min(100, int(context_percent)))}%"
            except (TypeError, ValueError):
                note += f"\nCTK{context_percent}"
        speaker_block = _render_persona_chat_card(
            speaker_role,
            speaker,
            note,
            header_color=ANSI_GOLD,
            body_color=ANSI_GOLD,
            width=width,
        )
        standby_block = _render_persona_chat_card(
            standby_role,
            standby_name,
            "standby",
            header_color=ANSI_SOFT_BLUE,
            body_color=ANSI_SOFT_BLUE,
            width=width,
        )
        return "\n".join(speaker_block + [""] + standby_block)
    if tool == "persona_link" and action == "mission":
        mission_task = str(payload.get("task") or payload.get("message") or "").strip()
        objective = str(payload.get("objective") or "").strip()
        status_text = str(payload.get("mission_status") or status or "queued").strip() or "queued"
        main_name = str(payload.get("main_role") or payload.get("main_name") or "主星").strip()
        liaison_name = str(payload.get("liaison_name") or "执行星").strip()
        transcript = payload.get("transcript") or []
        lines: list[str] = []
        if isinstance(transcript, list) and transcript:
            ordered = sorted(
                [item for item in transcript if isinstance(item, dict)],
                key=lambda item: int(item.get("round") or 0),
                reverse=True,
            )
            for item in ordered:
                speaker = str(item.get("role") or "").strip()
                content = str(item.get("content") or "").strip()
                if not content:
                    continue
                round_label = str(item.get("round") or "").strip()
                title = "执行星"
                color = ANSI_VIOLET
                if speaker == main_name:
                    title = "主星"
                    color = ANSI_GOLD
                if round_label.isdigit():
                    title = f"{title} · 第{round_label}轮"
                lines.extend(
                    _render_persona_chat_card(
                        title,
                        speaker or (liaison_name if title.startswith("执行星") else main_name),
                        content,
                        header_color=color,
                        body_color=color,
                        width=width,
                    )
                )
                lines.append("")
            if lines:
                return "\n".join(lines[:-1])
        liaison_body = f"{str(payload.get('message') or '任务已入队')}\n状态：{status_text}"
        if str(payload.get("mission_path") or "").strip():
            liaison_body += f"\n记录：{_shorten_tool_text(str(payload.get('mission_path') or ''))}"
        liaison_block = _render_persona_chat_card(
            "执行星",
            liaison_name,
            liaison_body,
            header_color=ANSI_VIOLET,
            body_color=ANSI_VIOLET,
            width=width,
        )
        main_body = mission_task
        if objective:
            main_body = f"{main_body}\n目标：{objective}" if main_body else f"目标：{objective}"
        main_block = _render_persona_chat_card(
            "主星",
            main_name,
            main_body or "—",
            header_color=ANSI_GOLD,
            body_color=ANSI_GOLD,
            width=width,
        )
        return "\n".join(liaison_block + [""] + main_block)
    if (tool == "persona_link" and action in {"send", "contact", "liaison"}) or tool == "liaison":
        label = "执行星" if action == "liaison" else ("联系执行星" if action == "contact" else "发送消息")
        main_name = str(payload.get("main_role") or payload.get("main_name") or "主星").strip()
        liaison_name = str(payload.get("liaison_name") or name or "执行星").strip()
        if tool == "liaison":
            request_text = str(payload.get("original_message") or "").strip()
        else:
            request_text = str(payload.get("original_message") or payload.get("message") or "").strip()
        reply_text = str(payload.get("reply") or "").strip()
        transcript = payload.get("transcript") or []
        lines: list[str] = []
        if transcript and isinstance(transcript, list):
            for item in transcript:
                if not isinstance(item, dict):
                    continue
                speaker = str(item.get("role") or liaison_name).strip() or liaison_name
                content = str(item.get("content") or "").strip()
                if not content:
                    continue
                round_label = str(item.get("round") or "").strip()
                color = ANSI_VIOLET
                title = "执行星"
                if speaker == main_name:
                    title = "主星"
                    color = ANSI_GOLD
                if round_label.isdigit():
                    title = f"{title} · 第{round_label}轮"
                lines.extend(
                    _render_persona_chat_card(
                        title,
                        speaker,
                        content,
                        header_color=color,
                        body_color=color,
                        width=width,
                    )
                )
                lines.append("")
            if request_text:
                lines.extend(
                    _render_persona_chat_card(
                        "主星",
                        main_name,
                        request_text,
                        header_color=ANSI_GOLD,
                        body_color=ANSI_GOLD,
                        width=width,
                    )
                )
                lines.append("")
        else:
            if reply_text:
                lines.extend(
                    _render_persona_chat_card(
                        "执行星",
                        liaison_name,
                        reply_text,
                        header_color=ANSI_VIOLET,
                        body_color=ANSI_VIOLET,
                        width=width,
                    )
                )
                lines.append("")
        if not lines and request_text:
            lines.extend(
                _render_persona_chat_card(
                    "主星",
                    main_name,
                    request_text,
                    header_color=ANSI_GOLD,
                    body_color=ANSI_GOLD,
                    width=width,
                )
            )
        if not lines:
            lines.extend(
                _render_persona_chat_card(
                    label,
                    liaison_name,
                    str(payload.get("brief") or "已完成消息交换").strip(),
                    header_color=ANSI_VIOLET,
                    body_color=ANSI_VIOLET,
                    width=width,
                )
            )
        return "\n".join(lines)
    if tool == "persona_link":
        main_name = str(payload.get("main_role") or payload.get("main_name") or "主星").strip()
        liaison_name = str(payload.get("liaison_name") or name or "执行星").strip()
        status_text = "完成" if status in {"ok", "empty", "queued"} else _tool_status_text(payload)
        brief = str(payload.get("brief") or "已完成角色联动").strip()
        main_block = _render_persona_chat_card(
            "主星",
            main_name,
            brief,
            header_color=ANSI_GOLD,
            body_color=ANSI_GOLD,
            width=width,
        )
        liaison_block = _render_persona_chat_card(
            "执行星",
            liaison_name,
            f"状态：{status_text}",
            header_color=ANSI_VIOLET,
            body_color=ANSI_VIOLET,
            width=width,
        )
        return "\n".join(liaison_block + [""] + main_block)
    heading = f"● 执行星 · {name}"
    if _supports_tty_control():
        heading = f"{ANSI_BOLD}{ANSI_CYAN}{heading}{ANSI_RESET}"
    status_text = "完成" if status in {"ok", "empty"} else _tool_status_text(payload)
    brief = str(payload.get("brief") or "已完成辅助判断").strip()
    lines = [heading, _tool_meta_line("结果", status_text, _middle_truncate_display(_shorten_tool_text(brief), max(16, width - 18)), color=ANSI_SOFT_PINK)]
    if status not in {"ok", "empty"}:
        message = str(payload.get("message") or "").strip()
        if message:
            lines.append(_tool_meta_line("提示", _middle_truncate_display(_shorten_tool_text(message), max(16, width - 12)), color=ANSI_SOFT_RED))
    return "\n".join(lines)


def _render_link_receipt(payload: dict[str, Any]) -> str:
    width = max(18, min(94, _terminal_render_width() - 4))
    action = str(payload.get("action") or "continue").strip().lower()
    if action in {"switch", "liaison", "mission", "send", "contact"}:
        heading = "● X-Link"
        if _supports_tty_control():
            heading = f"{ANSI_BOLD}{ANSI_VIOLET}{heading}{ANSI_RESET}"
        legacy_payload = dict(payload)
        legacy_payload["tool"] = "persona_link"
        rendered = _render_liaison_receipt(legacy_payload)
        return f"{heading}\n\n{rendered}" if rendered else heading

    def clean_text(value: Any) -> str:
        return _shorten_tool_text(str(value or "").strip())

    def signal_line(label: str, value: str, *, color: str = ANSI_WHITE, bold: bool = False) -> str:
        value = clean_text(value)
        if not value:
            return ""
        prefix = f"┆ {_pad_display(label, 6)} "
        available = max(4, width - _display_width(prefix))
        return _style_tool_line(f"{prefix}{_middle_truncate_display(value, available)}", color, bold=bold, dim=not bold)

    action_code = {
        "continue": "GO",
        "done": "DONE",
        "blocked": "BLOCK",
        "review": "REVIEW",
        "ask": "ASK",
        "handoff": "HANDOFF",
    }.get(action, (action or "LINK").upper())
    action_label = {
        "continue": "接续",
        "done": "完成",
        "blocked": "阻塞",
        "review": "审查",
        "ask": "询问",
        "handoff": "交还",
    }.get(action, action or "link")
    target = str(payload.get("target") or ("executor" if action == "continue" else "planner")).strip().lower()
    target_code = {
        "executor": "EXEC",
        "planner": "PLAN",
        "main": "MAIN",
        "liaison": "LIAISON",
    }.get(target, target.upper() if target else "")
    target_label = {
        "executor": "执行星",
        "planner": "主星",
        "main": "主星",
        "liaison": "执行星",
    }.get(target, target or "target")

    message = clean_text(payload.get("message"))
    task = clean_text(payload.get("task"))
    objective = clean_text(payload.get("objective"))
    context_percent = payload.get("context_percent")
    context_text = ""
    if context_percent not in {None, ""}:
        try:
            percent = max(0, min(100, int(context_percent)))
            context_text = f"CTK{percent}%"
        except (TypeError, ValueError):
            context_text = f"CTK{context_percent}"

    if width < 34:
        header = f"▌ XLINK {action_code}"
        if target_code:
            header += f">{target_code}"
        if context_text:
            header += f" {context_text.replace(' ', '')}"
    else:
        header = f"▌ X-LINK // {action_code}"
        if target_code:
            header += f" -> {target_code}"
        if context_text:
            header += f" · {context_text}"
    lines = [_style_tool_line(_truncate_display_ellipsis(header, width), ANSI_VIOLET, bold=True)]
    lines.append(signal_line("状态", f"{action_label} -> {target_label}", color=ANSI_VIOLET, bold=True))
    if context_text:
        lines.append(signal_line("CTK", context_text.replace("CTK", ""), color=ANSI_MUTED_BLUE, bold=True))
    if target in {"executor", "liaison"}:
        executor_name = clean_text(payload.get("executor_name") or payload.get("liaison_name"))
        if executor_name:
            lines.append(signal_line("执行星", executor_name, color=ANSI_CYAN, bold=True))
    elif target in {"planner", "main"}:
        planner_name = clean_text(payload.get("planner_name") or payload.get("main_name") or payload.get("main_role"))
        if planner_name:
            lines.append(signal_line("主星", planner_name, color=ANSI_GOLD, bold=True))

    field_order = [("消息", message), ("任务", task), ("目标", objective)] if action in {"done", "blocked", "review"} else [
        ("任务", task),
        ("目标", objective),
        ("消息", message),
    ]
    emitted_values: set[str] = set()
    for label, value in field_order:
        if not value or value in emitted_values:
            continue
        line = signal_line(label, value, color=ANSI_WHITE)
        if line:
            lines.append(line)
            emitted_values.add(value)

    raw_steps = payload.get("steps") or []
    steps = [clean_text(item) for item in raw_steps if clean_text(item)] if isinstance(raw_steps, list) else []
    visible_steps = 2 if width < 34 else 3
    for index, step in enumerate(steps[:visible_steps], start=1):
        line = signal_line("步骤" if index == 1 else "", f"{index}/{len(steps)} {step}", color=ANSI_MUTED_BLUE)
        if line:
            lines.append(line)
    if len(steps) > visible_steps:
        line = signal_line("", f"+{len(steps) - visible_steps} steps", color=ANSI_MUTED_BLUE)
        if line:
            lines.append(line)
    if len(lines) == 2 and not any((message, task, objective, steps)):
        lines.append(signal_line("消息", "X-Link 已记录。", color=ANSI_WHITE))
    return "\n".join(line for line in lines if line)


def _render_memory_receipt(payload: dict[str, Any]) -> str:
    width = max(24, _terminal_render_width() - 2)
    tool = str(payload.get("tool") or "")
    action = str(payload.get("action") or "").strip().lower()
    if tool == "memory_add":
        heading = f"● Memory Add · {str(payload.get('date') or '—')}"
    elif tool == "diary_keeper":
        heading = f"● Diary Keeper · {str(payload.get('date') or '—')}"
    elif tool == "memory_check":
        heading = "● Memory Check"
    elif tool == "memory_read":
        heading = "● Memory Read"
    else:
        heading = "● Memory Status"
    if _supports_tty_control():
        heading = f"{ANSI_BOLD}{ANSI_SOFT_BLUE}{heading}{ANSI_RESET}"
    status_text = _tool_status_text(payload)
    brief = str(payload.get("brief") or "")
    if not brief:
        if tool == "memory_status":
            brief = "检查长期记忆状态"
        elif tool == "memory_add":
            brief = "写入长期记忆"
        elif tool == "memory_check":
            brief = "检索长期记忆"
        elif tool == "memory_read":
            brief = "按日期读取长期记忆"
    lines = [heading, _tool_meta_line("结果", status_text, _middle_truncate_display(_shorten_tool_text(brief), max(16, width - 18)), color=ANSI_SOFT_PINK)]
    if tool in {"memory_add", "diary_keeper"}:
        keywords = payload.get("keywords") or []
        if isinstance(keywords, list) and keywords:
            lines.append(_tool_meta_line("关键词", _middle_truncate_display(", ".join(_shorten_tool_text(str(item)) for item in keywords[:5]), max(16, width - 14)), color=ANSI_SOFT_BLUE))
        db_path = str(payload.get("db_path") or "").strip()
        if db_path:
            lines.append(_tool_meta_line("数据库", _middle_truncate_display(_shorten_tool_text(db_path), max(16, width - 14)), color=ANSI_SOFT_BLUE))
        if tool == "diary_keeper":
            lines.append(_tool_meta_line("来源", "datememory", "auto", color=ANSI_SOFT_BLUE))
    elif tool == "memory_check":
        dates = payload.get("dates") or []
        if isinstance(dates, list) and dates:
            lines.append(_tool_meta_line("日期", _middle_truncate_display(", ".join(_shorten_tool_text(str(item)) for item in dates[:5]), max(16, width - 14)), color=ANSI_SOFT_BLUE))
        best = payload.get("best_detail") or payload.get("best") or {}
        if isinstance(best, dict) and best:
            lines.append(_tool_meta_line("命中", _middle_truncate_display(_shorten_tool_text(str(best.get("date") or "")), max(16, width - 14)), color=ANSI_SOFT_BLUE))
    elif tool == "memory_read":
        requested = payload.get("requested") or []
        if isinstance(requested, list) and requested:
            lines.append(_tool_meta_line("读取", _middle_truncate_display(", ".join(_shorten_tool_text(str(item)) for item in requested[:5]), max(16, width - 14)), color=ANSI_SOFT_BLUE))
    else:
        lines.append(_tool_meta_line("日记", _middle_truncate_display(_shorten_tool_text(str(payload.get("datememory_bytes") or "")), max(16, width - 14)), color=ANSI_SOFT_BLUE))
        lines.append(_tool_meta_line("状态", _middle_truncate_display(_shorten_tool_text(f"{payload.get('datememory_days', 0)} days / {payload.get('memory_db_diaries', 0)} diaries"), max(16, width - 14)), color=ANSI_SOFT_BLUE))
    message = str(payload.get("message") or "").strip()
    if message and status_text not in {"完成", "Done", "Succeeded"}:
        lines.append(_tool_meta_line("提示", _middle_truncate_display(_shorten_tool_text(message), max(16, width - 14)), color=ANSI_SOFT_RED))
    return "\n".join(lines)


def _render_model_mode_receipt(payload: dict[str, Any]) -> str:
    heading = _tool_heading(payload)
    mode = _collab_mode_value(str(payload.get("mode") or "standard").strip())
    previous = _collab_mode_value(str(payload.get("previous_mode") or "").strip()) if str(payload.get("previous_mode") or "").strip() else ""
    planner = str(payload.get("planner_model") or "").strip()
    executor = str(payload.get("executor_model") or "").strip()
    action = str(payload.get("action") or "status").strip().lower()
    status_text = "已切换" if action == "set" and str(payload.get("status") or "") in STATUS_SUCCESS_TEXT else "当前"
    body = f"{status_text}：{_collab_mode_detail(mode)}"
    if previous and previous != mode:
        body += f"  ({_collab_mode_detail(previous)} -> {_collab_mode_detail(mode)})"
    lines = [heading, _style_tool_line(f"  {body}", ANSI_SOFT_BLUE, dim=True)]
    if planner or executor:
        if _supports_tty_control():
            planner_text = f"◇ {ANSI_ITALIC}{planner or '?'}"
            executor_text = f"↳ {ANSI_ITALIC}{executor or '?'}"
            lines.append(_style_tool_line(f"  {planner_text}  /  {executor_text}", ANSI_SOFT_BLUE, dim=True))
        else:
            lines.append(_style_tool_line(f"  ◇ {planner or '?'}  /  ↳ {executor or '?'}", ANSI_SOFT_BLUE, dim=True))
    reason = str(payload.get("reason") or "").strip()
    if reason:
        lines.append(_style_tool_line(f"  {reason}", ANSI_WHITE, dim=True))
    return "\n".join(lines)


def _plan_item_symbol(status: str) -> str:
    return {
        "done": "✓",
        "in_progress": "▶",
        "blocked": "!",
        "pending": "○",
    }.get(str(status or "").strip().lower(), "○")


def _plan_item_color(status: str) -> tuple[str, bool, bool]:
    normalized = str(status or "").strip().lower()
    if normalized == "done":
        return ANSI_SOFT_GREEN, True, False
    if normalized == "in_progress":
        return ANSI_SOFT_BLUE, True, False
    if normalized == "blocked":
        return ANSI_SOFT_RED, True, False
    return ANSI_MUTED_TEXT, False, True


def _render_update_plan_receipt(payload: dict[str, Any]) -> str:
    width = max(18, min(94, _terminal_render_width() - 4))
    mode = str(payload.get("mode") or "todo").strip().lower()
    action = str(payload.get("action") or "status").strip().lower()
    title = _shorten_tool_text(str(payload.get("title") or "").strip())
    plan_status = str(payload.get("plan_status") or "").strip().lower()
    try:
        completed = int(payload.get("completed_count") or 0)
        total = int(payload.get("total_count") or 0)
    except (TypeError, ValueError):
        completed = 0
        total = 0
    status_code = {
        "empty": "EMPTY",
        "pending": "READY",
        "in_progress": "ACTIVE",
        "blocked": "BLOCK",
        "done": "DONE",
    }.get(plan_status, (plan_status or action).upper())
    progress_text = f"{completed}/{total}" if total else "0/0"
    if width < 34:
        header = f"▌ PLAN {status_code} {progress_text}"
    else:
        header = f"▌ PLAN // {status_code} · {progress_text} · {action}"
    lines = [_style_tool_line(_truncate_display_ellipsis(header, width), ANSI_SOFT_BLUE, bold=True)]

    def signal_line(label: str, value: str, *, color: str = ANSI_WHITE, bold: bool = False) -> str:
        value = _shorten_tool_text(str(value or "").strip())
        if not value:
            return ""
        prefix = f"┆ {_pad_display(label, 6)} "
        available = max(4, width - _display_width(prefix))
        return _style_tool_line(f"{prefix}{_middle_truncate_display(value, available)}", color, bold=bold, dim=not bold)

    def item_line(symbol: str, value: str, *, color: str = ANSI_WHITE, bold: bool = False, dim: bool = False) -> str:
        value = _shorten_tool_text(str(value or "").strip())
        if not value:
            return ""
        prefix = f"┆ {symbol} "
        available = max(4, width - _display_width(prefix))
        return _style_tool_line(f"{prefix}{_middle_truncate_display(value, available)}", color, bold=bold, dim=dim)

    items = payload.get("items") or []
    if not isinstance(items, list):
        items = []
    if title:
        lines.append(signal_line("标题", title, color=ANSI_WHITE, bold=True))
    active_item = payload.get("active_item") or {}
    if not isinstance(active_item, dict):
        active_item = {}
    active_status = str(active_item.get("status") or "").strip().lower()
    active_id = str(active_item.get("id") or payload.get("current_step_id") or "").strip()
    active_phase = str(active_item.get("phase") or "").strip()
    active_title = _shorten_tool_text(str(active_item.get("title") or active_item.get("note") or "").strip())
    active_prefix = " ".join(part for part in (active_id, active_phase) if part)
    active_text = f"{active_prefix} · {active_title}" if active_prefix and active_title else active_title or active_prefix
    if active_text:
        active_color, active_bold, active_dim = _plan_item_color(active_status or plan_status)
        lines.append(signal_line("当前", active_text, color=active_color, bold=active_bold and not active_dim))
    next_text = _shorten_tool_text(str(payload.get("next") or "").strip())
    if next_text:
        lines.append(signal_line("下一步", next_text, color=ANSI_MUTED_BLUE))

    visible_items = [item for item in items if isinstance(item, dict)][: (4 if width < 34 else 8)]
    if not visible_items:
        message = str(payload.get("message") or "当前没有计划步骤。").strip()
        lines.append(signal_line("状态", message, color=ANSI_MUTED_TEXT))
    for item in visible_items:
        status = str(item.get("status") or "pending").strip().lower()
        symbol = _plan_item_symbol(status)
        color, bold, dim = _plan_item_color(status)
        item_id = str(item.get("id") or "").strip()
        phase = str(item.get("phase") or "").strip()
        title_text = _shorten_tool_text(str(item.get("title") or item.get("note") or "—").strip())
        lead = " ".join(part for part in (item_id, phase) if part)
        value = f"{lead} · {title_text}" if lead else title_text
        lines.append(item_line(symbol, value, color=color, bold=bold, dim=dim))
    if len(items) > len(visible_items):
        lines.append(item_line("…", f"+{len(items) - len(visible_items)} steps", color=ANSI_MUTED_TEXT, dim=True))
    message = str(payload.get("message") or "").strip()
    if message and visible_items and plan_status in {"blocked", "empty"}:
        lines.append(signal_line("提示", message, color=ANSI_MUTED_TEXT))
    return "\n".join(lines)


def _render_compact_tool_receipt(payload: dict[str, Any]) -> str:
    if _should_suppress_tool_receipt(payload):
        return ""
    tool = str(payload.get("tool") or "")
    if tool == "link":
        return _render_link_receipt(payload)
    if tool in {"persona_link", "liaison"}:
        return _render_liaison_receipt(payload)
    if tool == "model_mode":
        return _render_model_mode_receipt(payload)
    if tool == "update_plan":
        return _render_update_plan_receipt(payload)
    if tool in {"memory_add", "memory_check", "memory_read", "memory_status", "diary_keeper"}:
        return _render_memory_receipt(payload)
    heading_text = _tool_heading(payload)
    width = max(24, _terminal_render_width() - 2)
    input_text = _middle_truncate_display(_shorten_tool_text(_tool_input_text(payload)), max(16, width - 24))
    output_parts = [_tool_status_text(payload), _tool_count_text(payload)]
    message = str(payload.get("message") or "").strip()
    if message and str(payload.get("status") or "") not in {"ok", "empty"}:
        output_parts.append(_middle_truncate_display(_shorten_tool_text(message), max(16, width - 30)))
    lines = [heading_text, ""]
    if str(payload.get("tool") or "") == "context":
        lines.append(_tool_meta_line("CTK", _middle_truncate_display(_shorten_tool_text(_tool_context_status_text(payload)), max(16, width - 12)), color=ANSI_CYAN))
        lines.append("")
    lines.extend(
        [
            _tool_meta_line("INPUT", _tool_input_kind(payload), input_text, color=ANSI_MUTED_BLUE),
            _tool_meta_line("OUTPUT", *output_parts, color=ANSI_SOFT_PINK),
        ]
    )
    if str(payload.get("status") or "") == "pending_confirmation":
        confirm_token = str(payload.get("confirm_command") or "y").strip() or "y"
        deny_token = str(payload.get("deny_command") or "n").strip() or "n"
        lines.append("")
        lines.append(_tool_meta_line("CONFIRM", f"type {confirm_token} to run", f"{deny_token} to cancel", color=ANSI_SOFT_RED))
    warnings = payload.get("warnings") or []
    if isinstance(warnings, list) and warnings:
        lines.append("")
        for warning in warnings[:4]:
            lines.append(_tool_meta_line("WARN", _middle_truncate_display(_shorten_tool_text(str(warning)), max(16, width - 14)), color=ANSI_SOFT_RED))
    recovery_hint = payload.get("recovery_hint") or []
    if isinstance(recovery_hint, list) and recovery_hint:
        lines.append("")
        for hint in recovery_hint[:3]:
            lines.append(_tool_meta_line("HINT", _middle_truncate_display(_shorten_tool_text(str(hint)), max(16, width - 14)), color=ANSI_MUTED_BLUE))
    return "\n".join(lines)


def _render_tool_running_receipt(payload: dict[str, Any]) -> str:
    running_payload = dict(payload)
    running_payload["status"] = "running"
    heading_text = _tool_heading(running_payload)
    brief = _middle_truncate_display(_shorten_tool_text(_tool_brief(running_payload)), max(16, _terminal_render_width() - 24))
    if str(running_payload.get("tool") or "") == "update_plan":
        lines = [heading_text]
        if brief:
            lines.append(_style_tool_line(f"  ◌ {brief}", ANSI_MUTED_TEXT, dim=True))
        return "\n".join(lines)
    lines: list[str] = []
    actor_line = _tool_running_actor_line(running_payload)
    if actor_line:
        lines.append(actor_line)
    lines.append(heading_text)
    lines.append(_tool_meta_line("RUNNING", brief or "working", color=ANSI_SOFT_BLUE))
    return "\n".join(lines)


def _tool_body_preview(payload: dict[str, Any]) -> tuple[str, str]:
    status = str(payload.get("status") or "")
    if status == "pending_confirmation":
        return "brief", str(payload.get("reason") or "该命令需要确认后执行。")
    if status == "blocked":
        return "brief", str(payload.get("reason") or payload.get("message") or "该命令已被安全策略阻止。")
    if status == "rejected":
        return "brief", str(payload.get("message") or "已取消执行。")

    if str(payload.get("tool") or "") == "terminal":
        parts: list[str] = []
        summary = str(payload.get("summary") or "").strip()
        if summary:
            parts.append(summary)
        message = str(payload.get("message") or "").strip()
        if message:
            parts.append(message)
        setup_warning = str(payload.get("setup_warning") or "").strip()
        if setup_warning:
            parts.append(setup_warning)
        session_name = str(payload.get("session_name") or "").strip()
        if session_name:
            parts.append(f"session {session_name}")
        log_path = str(payload.get("log_path") or "").strip()
        if log_path:
            parts.append(
                f"log {log_path} ({payload.get('log_lines', 0)} lines, {payload.get('log_size') or payload.get('log_bytes', 0)})"
            )
            parts.append(f"head {payload.get('read_head_command')}")
            parts.append(f"tail {payload.get('read_tail_command')}")
            parts.append(f"slice {payload.get('read_slice_command')}")
        preview = str(payload.get("log_preview") or "").strip()
        if preview:
            parts.append(f"[log preview]\n{preview}")
        if not parts:
            parts.append("terminal completed with no log output.")
        return "out", _shorten_tool_text("\n".join(parts))

    if str(payload.get("tool") or "") == "aidebug":
        parts: list[str] = []
        summary = str(payload.get("summary") or "").strip()
        if summary:
            parts.append(summary)
        action = str(payload.get("action") or "").strip()
        if action:
            parts.append(f"action {action}")
        aidebug_dir = str(payload.get("aidebug_dir") or "").strip()
        if aidebug_dir:
            parts.append(f"dir {aidebug_dir}")
        relative_path = str(payload.get("relative_path") or payload.get("log_path") or "").strip()
        if relative_path:
            parts.append(f"path {relative_path}")
        stdout = str(payload.get("stdout") or "").strip()
        if stdout:
            parts.append(stdout)
        message = str(payload.get("message") or "").strip()
        if message:
            parts.append(message)
        if not parts:
            parts.append("aidebug completed with no output.")
        return "dbg", _shorten_tool_text("\n".join(parts))

    if str(payload.get("tool") or "") == "tool_manage":
        action = str(payload.get("action") or "list").strip().lower()
        parts: list[str] = []
        summary = str(payload.get("summary") or "").strip()
        if summary:
            parts.append(summary)
        total = payload.get("total_count")
        expanded = payload.get("expanded_count")
        changed = payload.get("changed")
        if action == "list":
            if isinstance(expanded, int) and isinstance(total, int) and total > 0:
                parts.append(f"{expanded}/{total} visible")
            elif isinstance(total, int) and total > 0:
                parts.append(f"{total} tools")
        elif action == "inspect":
            rows = payload.get("tools") or []
            if isinstance(rows, list):
                preview: list[str] = []
                for item in rows[:4]:
                    if isinstance(item, dict):
                        name = str(item.get("name") or "").strip()
                        if not name:
                            continue
                        state = "expanded" if item.get("expanded") else "collapsed"
                        preview.append(f"{name} ({state})")
                    else:
                        text = str(item or "").strip()
                        if text:
                            preview.append(text)
                if preview:
                    parts.append(", ".join(preview))
        else:
            names = _tool_manage_name_list(changed or payload.get("requested") or payload.get("tools") or payload.get("tool"))
            if names:
                parts.append(", ".join(names[:4]))
        message = str(payload.get("message") or "").strip()
        if message:
            parts.append(message)
        if not parts:
            parts.append("toolbox completed with no output.")
        return "box", _shorten_tool_text("\n".join(parts))

    if str(payload.get("tool") or "") == "liaison":
        parts: list[str] = []
        summary = str(payload.get("summary") or "").strip()
        if summary:
            parts.append(summary)
        reply = str(payload.get("reply") or payload.get("message") or "").strip()
        if reply:
            parts.append(reply)
        transcript = payload.get("transcript") or []
        if isinstance(transcript, list) and transcript:
            preview: list[str] = []
            for item in transcript[:3]:
                if not isinstance(item, dict):
                    continue
                content = str(item.get("content") or "").strip()
                if content:
                    preview.append(content)
            if preview:
                parts.append("\n".join(preview))
        if not parts:
            parts.append("liaison completed with no output.")
        return "liaison", _shorten_tool_text("\n".join(parts))

    stdout = str(payload.get("stdout") or "").strip()
    stderr = str(payload.get("stderr") or "").strip()
    parts: list[str] = []
    summary = str(payload.get("summary") or "").strip()
    if summary:
        parts.append(summary)
    if status == "timeout":
        parts.append(f"timeout after {payload.get('timeout_seconds', '?')}s")
    elif status == "error" and payload.get("returncode") is not None:
        parts.append(f"returncode {payload.get('returncode')}")

    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    if not parts:
        parts.append("completed with no output.")
    return "out", _shorten_tool_text("\n".join(parts))


def _tool_body_counter(payload: dict[str, Any], body_label: str, body_text: str) -> str:
    tool = str(payload.get("tool") or "")
    if tool == "terminal":
        try:
            count = int(payload.get("log_lines") or 0)
        except (TypeError, ValueError):
            count = 0
        return f"TOTAL {count} lines" if count > 0 else ""
    if tool == "aidebug":
        stdout = str(payload.get("stdout") or "").strip()
        count = _tool_line_count(stdout) or _tool_line_count(body_text)
        return f"TOTAL {count} lines" if count > 0 else ""
    if body_label == "out":
        stdout = str(payload.get("stdout") or "").strip()
        stderr = str(payload.get("stderr") or "").strip()
        count = _tool_line_count(stdout) + _tool_line_count(stderr)
        if count <= 0:
            count = _tool_line_count(body_text)
        return f"TOTAL {count} lines" if count > 0 else ""
    count = _tool_line_count(body_text)
    return f"TOTAL {count} lines" if count > 0 else ""


def _tool_prefix(label: str, *, first: bool, counter: str = "") -> str:
    if not first:
        return "        \t"
    suffix = f" {counter}" if counter else ""
    symbol = "✲" if label == "CMD" else "○"
    plain = f"  {symbol} {label}{suffix}\t"
    if not _supports_tty_control():
        return plain
    color = ANSI_WHITE
    if label == "CMD":
        color = ANSI_MUTED_BLUE
    elif label == "OUT":
        color = ANSI_SOFT_PINK
    elif label == "DBG":
        color = ANSI_SOFT_BLUE
    elif label == "BOX":
        color = ANSI_CYAN
    elif label == "BRIEF":
        color = f"{ANSI_DIM}{ANSI_WHITE}"
        return f"  {color}{symbol} {label}{suffix}{ANSI_RESET}\t"
    return f"  {ANSI_BOLD}{color}{symbol} {label}{suffix}{ANSI_RESET}\t"


def _render_patch_diff_lines(patch_text: str, *, width: int) -> list[str]:
    lines: list[str] = []
    old_line: int | None = None
    new_line: int | None = None
    for raw_line in str(patch_text or "").splitlines():
        line = raw_line.rstrip("\n")
        if line.startswith(("diff --git ", "index ", "--- ", "+++ ")):
            continue
        if line.startswith("@@"):
            match = re.search(r"-(\d+)(?:,\d+)? \+(\d+)", line)
            if match:
                old_line = int(match.group(1))
                new_line = int(match.group(2))
            continue
        if not line:
            continue
        marker = line[:1]
        body = line[1:] if marker in {" ", "+", "-"} else line
        if marker == "+" and not line.startswith("+++"):
            number = new_line or 0
            new_line = number + 1 if new_line is not None else None
            rendered = f"{number:>5} + {_middle_truncate_display(body, max(8, width - 9))}"
            lines.append(_style_tool_line(rendered, ANSI_SOFT_PINK, bold=True))
        elif marker == "-" and not line.startswith("---"):
            number = old_line or 0
            old_line = number + 1 if old_line is not None else None
            rendered = f"{number:>5} - {_middle_truncate_display(body, max(8, width - 9))}"
            lines.append(_style_tool_line(rendered, ANSI_SOFT_RED, bold=True))
        elif marker == " ":
            number = new_line if new_line is not None else old_line or 0
            if old_line is not None:
                old_line += 1
            if new_line is not None:
                new_line += 1
            rendered = f"{number:>5}   {_middle_truncate_display(body, max(8, width - 9))}"
            lines.append(_style_tool_line(rendered, ANSI_WHITE, dim=True))
        else:
            lines.append(_style_tool_line(_middle_truncate_display(line, width), ANSI_WHITE, dim=True))
        if len(lines) >= 80:
            lines.append(_style_tool_line("... patch preview truncated ...", ANSI_SOFT_RED, bold=True))
            break
    return lines or [_style_tool_line("  patch preview unavailable", ANSI_WHITE, dim=True)]


def _render_apply_patch_receipt(payload: dict[str, Any]) -> str:
    heading_text = _tool_heading(payload)
    width = max(24, _terminal_render_width() - 2)
    changed = payload.get("changed_files") or []
    changed_paths = [str(item) for item in changed] if isinstance(changed, list) else []
    lines = [heading_text, ""]
    if changed_paths:
        for path in changed_paths[:8]:
            lines.append(_tool_meta_line("Edited", _middle_truncate_display(_shorten_tool_text(path), max(16, width - 14)), color=ANSI_MAGENTA))
    else:
        lines.append(_tool_meta_line("Edited", "patch", color=ANSI_MAGENTA))
    lines.append(_tool_meta_line("OUTPUT", _tool_status_text(payload), _tool_count_text(payload), color=ANSI_SOFT_PINK))
    patch_text = str(payload.get("patch") or "")
    if patch_text:
        lines.append("")
        lines.extend(_render_patch_diff_lines(patch_text, width=width))
    else:
        stdout = str(payload.get("stdout") or payload.get("message") or "").strip()
        if stdout:
            lines.append("")
            lines.extend(_tool_preview_lines(_shorten_tool_text(stdout), width=width))
    warnings = payload.get("warnings") or []
    if isinstance(warnings, list) and warnings:
        lines.append("")
        for warning in warnings[:4]:
            lines.append(_tool_meta_line("WARN", _middle_truncate_display(_shorten_tool_text(str(warning)), max(16, width - 14)), color=ANSI_SOFT_RED))
    recovery_hint = payload.get("recovery_hint") or []
    if isinstance(recovery_hint, list) and recovery_hint:
        lines.append("")
        for hint in recovery_hint[:3]:
            lines.append(_tool_meta_line("HINT", _middle_truncate_display(_shorten_tool_text(str(hint)), max(16, width - 14)), color=ANSI_MUTED_BLUE))
    return "\n".join(lines)


def _render_tool_receipt_payload(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "")
    tool = str(payload.get("tool") or "")
    if _should_suppress_tool_receipt(payload):
        return ""
    if tool == "apply_patch":
        body = _dedupe_windows_tool_body(payload, _render_apply_patch_receipt(payload))
        return _windows_tool_frame(_tool_frame_title(payload), body, color=ANSI_MAGENTA)
    if tool in {"web_search", "command", "terminal", "aidebug", "context", "context_manage", "contextmanage", "tool_manage", "link", "liaison", "persona_link", "memory_add", "memory_check", "memory_read", "memory_status", "diary_keeper", "model_mode", "update_plan"} and _tool_group_family(payload) is None:
        body = _dedupe_windows_tool_body(payload, _render_compact_tool_receipt(payload))
        return _windows_tool_frame(_tool_frame_title(payload), body, color=_tool_heading_color(payload))
    width = max(24, _terminal_render_width() - 8)
    heading_text = _tool_heading(payload)
    command_lines = _tool_preview_lines(_shorten_tool_text(str(payload.get("command") or "")), width=width, max_lines=3)
    body_label, body_text = _tool_body_preview(payload)
    body_lines = _tool_preview_lines(body_text, width=width)
    body_counter = _tool_body_counter(payload, body_label, body_text)

    lines = [heading_text]
    for index, line in enumerate(command_lines):
        prefix = _tool_prefix("CMD", first=index == 0)
        lines.append(f"{prefix}{_style_tool_omission(line)}")
    for index, line in enumerate(body_lines):
        label = body_label.upper()
        prefix = _tool_prefix(label, first=index == 0, counter=body_counter if index == 0 else "")
        lines.append(f"{prefix}{_style_tool_omission(line)}")

    if status == "pending_confirmation":
        confirm_token = str(payload.get("confirm_command") or "y").strip() or "y"
        lines.append(f"  type {confirm_token} = run  ·  n = cancel")

    body = _dedupe_windows_tool_body(payload, "\n".join(lines))
    return _windows_tool_frame(_tool_frame_title(payload), body, color=_tool_heading_color(payload))


def _render_tool_receipts(tool_traces: tuple[dict[str, Any], ...]) -> str:
    payloads: list[dict[str, Any]] = []
    for trace in tool_traces:
        payload = trace.get("result") if isinstance(trace, dict) else None
        if isinstance(payload, dict):
            payloads.append(payload)
    blocks: list[str] = []
    grouped_payloads: list[dict[str, Any]] = []
    grouped_family: str | None = None

    def flush_group() -> None:
        nonlocal grouped_payloads, grouped_family
        if not grouped_payloads:
            return
        if (len(grouped_payloads) == 1 and grouped_family != "explore") or grouped_family is None:
            blocks.append(_render_tool_receipt_payload(grouped_payloads[0]))
        else:
            blocks.append(_render_grouped_tool_receipt(grouped_payloads, grouped_family))
        grouped_payloads = []
        grouped_family = None

    for payload in payloads:
        if _should_suppress_tool_receipt(payload):
            continue
        family = _tool_group_family(payload)
        if family:
            if grouped_payloads and family != grouped_family:
                flush_group()
            grouped_payloads.append(payload)
            grouped_family = family
            continue
        flush_group()
        blocks.append(_render_tool_receipt_payload(payload))
    flush_group()
    return "\n\n".join(blocks).strip()


def dispatch_shell_input(
    raw_input: str,
    *,
    mode: str = "command_not_found",
    cwd: str | Path | None = None,
    config: ProjectLingConfig | None = None,
    dry_run: bool = False,
) -> int:
    config = config or load_config()
    _cleanup_legacy_runtime(config)

    text = raw_input.strip()
    if not text:
        return 0

    normalized_mode = mode.strip().lower()
    if normalized_mode not in SHELL_DISPATCH_MODES:
        normalized_mode = "command_not_found"
    if normalized_mode == "command_not_found" and re.fullmatch(r"\d{1,3}", text):
        return 0

    engine = ProjectLingEngine(config)
    role, _role_seed, persona_bundle = engine.persona_for_dispatch_mode(normalized_mode)
    current_cwd = Path(cwd or Path.cwd()).expanduser()
    allow_tools = bool(config.allow_tools)
    use_stream = bool(config.enable_sse)
    route = engine.preview_route(text, allow_tools=allow_tools, dispatch_mode=normalized_mode)
    if bool(route.get("speaker_handoff_request")) and allow_tools:
        target = str(route.get("speaker_handoff_target") or "").strip().lower()
        if target in {"liaison", "main"}:
            role, _role_seed, persona_bundle = engine.persona_for_handoff_target(target)

    if dry_run:
        payload = {
            "raw": text,
            "mode": normalized_mode,
            "cwd": str(current_cwd),
            "role": {
                "display_zh": persona_bundle.main.name_zh,
                "display_en": persona_bundle.main.name_en,
            },
            "liaison": {
                "display_zh": persona_bundle.liaison.name_zh if persona_bundle.liaison else "",
                "display_en": persona_bundle.liaison.name_en if persona_bundle.liaison else "",
            },
            "route": route,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if not config.api_key:
        config = _bootstrap_missing_key(config) or config
        if not config.api_key:
            return 0
        engine = ProjectLingEngine(config)
        role, _role_seed, persona_bundle = engine.persona_for_dispatch_mode(normalized_mode)
        allow_tools = bool(config.allow_tools)
        use_stream = bool(config.enable_sse)
        route = engine.preview_route(text, allow_tools=allow_tools, dispatch_mode=normalized_mode)
        if bool(route.get("speaker_handoff_request")) and allow_tools:
            target = str(route.get("speaker_handoff_target") or "").strip().lower()
            if target in {"liaison", "main"}:
                role, _role_seed, persona_bundle = engine.persona_for_handoff_target(target)

    printer = ShellStreamPrinter(
        engine.prompt_bundle,
        role,
        persona_bundle=persona_bundle,
        context_budget=load_context_budget(config),
    )
    _begin_route_status(printer, route)

    try:
        result = engine.chat(
            text,
            cwd=current_cwd,
            mode=normalized_mode,
            allow_tools=allow_tools,
            stream=use_stream,
            on_stream_delta=printer.on_delta if use_stream else None,
            on_stream_event=printer.on_event,
        )
    except KeyboardInterrupt:
        printer.emit_message("已中断。")
        printer.finish("")
        return 130
    except DeepSeekAPIError as exc:
        printer.emit_message(f"请求失败：{exc}")
        printer.finish("")
        return 1
    except Exception as exc:  # pragma: no cover - shell safety net
        printer.emit_message(f"运行失败：{exc}")
        printer.finish("")
        return 1

    streamed_response = bool(
        isinstance(result.raw_response, dict)
        and result.raw_response.get("_projectling_streamed")
    )
    for trace in result.thinking_traces:
        if not isinstance(trace, dict):
            continue
        if bool(trace.get("_frontend_rendered")):
            continue
        trace_text = _strip_context_percent_marker_text(str(trace.get("text") or "").strip())
        if not trace_text:
            continue
        if streamed_response and trace_text == str(result.reasoning_text or "").strip():
            continue
        trace_role = str(trace.get("role") or "").strip().lower()
        role_label = str(trace.get("actor_label") or "").strip()
        if not role_label:
            role_label = "主星" if trace_role.startswith("planner") else "执行星" if trace_role.startswith("executor") else ""
        printer.show_thinking_trace(
            trace_text,
            elapsed_seconds=trace.get("elapsed_seconds"),
            role_label=role_label,
            actor_name=trace.get("actor_name") or "",
            context_percent=trace.get("context_budget_percent") or trace.get("context_percent"),
        )
    if not use_stream:
        frontend_receipts = _render_tool_receipts(result.tool_traces)
        if frontend_receipts:
            printer.emit_plain_block(frontend_receipts, trailing_blank=bool(result.text))
        if result.text:
            printer.emit_message(result.text)
    if result.finish_reason == "stream_limit" and not result.text and not result.tool_traces:
        printer.finish("本轮输出已达到上限。")
    else:
        printer.finish(result.text or "我没有得到有效回复。")
    return 0


def _settings_provider_view(config: ProjectLingConfig, provider: str | None) -> ProjectLingConfig:
    normalized = _api_provider_value(provider or getattr(config, "api_provider", ""))
    if normalized == _api_provider_value(getattr(config, "api_provider", "")):
        return config
    if normalized == "gemini":
        return replace(
            config,
            api_provider="gemini",
            api_key=config.gemini_api_key,
            base_url=config.gemini_base_url,
            model=config.gemini_executor_model,
        )
    return replace(
        config,
        api_provider="deepseek",
        api_key=config.deepseek_api_key,
        base_url=config.deepseek_base_url,
        model=config.deepseek_executor_model,
    )


def _run_api_settings_ui(provider_override: str | None = None) -> int:
    while True:
        current = _settings_provider_view(load_config(), provider_override)
        _render_api_settings(current)
        choice = _prompt_menu_choice("0 返回上级")

        if choice == "1":
            provider = _choose_provider_interactive(current)
            if provider is None:
                _print_setting_unchanged("服务商未修改。")
                continue
            _save_config_value(current, {"PROJECTLING_API_PROVIDER": provider})
            _print_setting_saved("服务商", _provider_label(load_config()))
            continue

        if choice == "2":
            print("")
            provider = _api_provider_value(getattr(current, "api_provider", ""))
            key_name = "GEMINI_API_KEY" if provider == "gemini" else "DEEPSEEK_API_KEY"
            key = _prompt_line(f"输入 {key_name}，留空保持原样 > ").strip()
            if key:
                _save_config_value(current, {key_name: key})
                _print_setting_saved("API Key", "已写入")
            else:
                _print_setting_unchanged("API Key 未修改。")
            continue

        if choice == "3":
            provider = _api_provider_value(getattr(current, "api_provider", ""))
            key_name = "GEMINI_BASE_URL" if provider == "gemini" else "DEEPSEEK_BASE_URL"
            base_url = _choose_base_url_interactive(current)
            if base_url is not None:
                _save_config_value(current, {key_name: base_url})
                _print_setting_saved("中转站", base_url)
            else:
                _print_setting_unchanged("中转站未修改。")
            continue

        if choice == "4":
            role = "planner"
            current_model = _configured_role_model(current, role)
            model = _pick_provider_model_interactive(current, role)
            if model:
                _save_config_value(current, {_model_update_key(current, role): model})
                _print_setting_saved("主星模型", model)
                _print_model_role_safety_hint(model, "主星")
            else:
                _print_setting_unchanged(f"主星模型保留 {current_model}。")
            continue

        if choice == "5":
            role = "executor"
            current_model = _configured_role_model(current, role)
            model = _pick_provider_model_interactive(current, role)
            if model:
                _save_config_value(current, {_model_update_key(current, role): model})
                _print_setting_saved("辅星模型", model)
                _print_model_role_safety_hint(model, "辅星")
            else:
                _print_setting_unchanged(f"辅星模型保留 {current_model}。")
            continue

        if choice == "6":
            _run_model_list(current)
            continue

        if choice == "7":
            _run_api_test(current)
            continue

        if choice == "8":
            print("")
            _toggle_config_value(
                current,
                "DEEPSEEK_ENABLE_SSE",
                current.enable_sse,
                "SSE",
            )
            continue

        if choice == "9":
            print("")
            max_tokens = _prompt_int("输入 Max Tokens > ", min_value=1, allow_empty_clear=True)
            key_name = "GEMINI_MAX_TOKENS" if _api_provider_value(getattr(current, "api_provider", "")) == "gemini" else "DEEPSEEK_MAX_TOKENS"
            if max_tokens == "":
                _save_config_value(current, {key_name: None})
                _print_setting_cleared("Max Tokens")
            elif isinstance(max_tokens, int):
                _save_config_value(current, {key_name: str(max_tokens)})
                _print_setting_saved("Max Tokens", max_tokens)
            continue

        if choice == "10":
            print("")
            temperature = _prompt_float("输入 Temperature (0.0 - 2.0) > ", min_value=0.0, max_value=2.0)
            if temperature is not None:
                key_name = "GEMINI_TEMPERATURE" if _api_provider_value(getattr(current, "api_provider", "")) == "gemini" else "DEEPSEEK_TEMPERATURE"
                _save_config_value(current, {key_name: f"{temperature:g}"})
                _print_setting_saved("Temperature", f"{temperature:g}")
            continue

        if choice == "11":
            print("")
            _print_fit_wrapped("API 超时默认 180s；SSE 会自动放宽读超时。")
            timeout_seconds = _prompt_float("输入 Timeout 秒数 > ", min_value=5.0, max_value=86400.0)
            if timeout_seconds is not None:
                _save_config_value(current, {"DEEPSEEK_TIMEOUT_SECONDS": f"{timeout_seconds:g}"})
                _print_setting_saved("Timeout", f"{timeout_seconds:g}s")
            continue

        if choice == "12":
            print("")
            _print_fit_wrapped("失败会用同一上下文重试，不写入历史。最大 10 次。")
            retries = _prompt_int("输入 Retry 次数 > ", min_value=0)
            if isinstance(retries, int):
                if retries > 10:
                    _print_setting_rejected("Retry 最大 10 次。", keep_label="保留", keep_value=current.retry_count)
                else:
                    _save_config_value(current, {"DEEPSEEK_RETRY_COUNT": str(retries)})
                    _print_setting_saved("Retry", retries)
            continue

        if choice == "13":
            print("")
            if _api_provider_value(getattr(current, "api_provider", "")) == "gemini":
                raw_effort = _prompt_line("输入 Gemini Reasoning Effort (none/low/high) > ").strip().lower()
                aliases = {"off": "none", "disabled": "none", "default": "high", "max": "high"}
                effort = aliases.get(raw_effort, raw_effort)
                if effort in {"none", "low", "high"}:
                    _save_config_value(current, {"GEMINI_REASONING_EFFORT": effort})
                    _print_setting_saved("Gemini Reasoning", effort)
                elif not raw_effort:
                    _print_setting_unchanged("Reasoning 未修改。")
                else:
                    _print_setting_rejected("选项 none low high。", keep_label="保留", keep_value=current.gemini_reasoning_effort)
                continue
            raw_effort = _prompt_line("输入 DeepSeek Reasoning Effort (high/max) > ").strip().lower()
            aliases = {"xhigh": "max", "x-high": "max", "maximum": "max", "medium": "high", "low": "high"}
            effort = aliases.get(raw_effort, raw_effort)
            if effort in {"high", "max"}:
                _save_config_value(current, {"DEEPSEEK_REASONING_EFFORT": effort})
                _print_setting_saved("Reasoning Effort", effort)
            elif not raw_effort:
                _print_setting_unchanged("Reasoning 未修改。")
            else:
                _print_setting_rejected("选项 high max。", keep_label="保留", keep_value=current.reasoning_effort)
            continue

        if choice == "14":
            _run_gemini_params_settings_ui()
            continue

        if choice == "15":
            _run_websearch_settings_ui()
            continue

        if choice == "0" or not choice:
            return 0

        print("无效输入。")


def _run_system_settings_ui() -> int:
    while True:
        current = load_config()
        _render_system_settings(current)
        choice = _prompt_menu_choice("0 返回上级")

        if choice == "1":
            role_hours = _choose_role_ttl_hours(current)
            if isinstance(role_hours, int):
                if role_hours > 48:
                    print("输入无效，需要小于等于 48。")
                else:
                    _save_config_value(current, {"PROJECTLING_ROLE_TTL_HOURS": str(role_hours)})
                    print(f"角色停留时间已更新：{role_hours}h")
            continue

        if choice == "2":
            print("")
            _run_model_mode_ui()
            continue

        if choice == "0" or not choice:
            return 0

        print("无效输入。")


def run_settings_ui(config: ProjectLingConfig | None = None, *, tab: str = "root") -> int:
    config = config or load_config()
    _cleanup_legacy_runtime(config)
    normalized_tab = (tab or "root").strip().lower()
    if normalized_tab in {"gemini_params", "gemini-params"}:
        return _run_gemini_params_settings_ui() or 0
    if normalized_tab == "gemini":
        return _run_api_settings_ui("gemini")
    if normalized_tab == "deepseek":
        return _run_api_settings_ui("deepseek")
    if normalized_tab == "api":
        return _run_api_settings_ui()
    if normalized_tab in {"persona", "role"}:
        return _run_persona_settings_ui(config)
    if normalized_tab in {"system", "settings"}:
        return _run_system_settings_ui()
    if normalized_tab in {"websearch", "web_search"}:
        return _run_websearch_settings_ui() or 0

    while True:
        current = load_config()
        _render_settings_root(current)
        choice = _prompt_menu_choice("0 返回")

        if choice == "1":
            _run_api_settings_ui()
            continue

        if choice == "2":
            _run_websearch_settings_ui()
            continue

        if choice == "3":
            _run_system_settings_ui()
            continue

        if choice == "0" or not choice:
            print("已返回。")
            return 0

        print("无效输入。")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="projectling")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="print config and runtime status")
    sub.add_parser("cleanup", help="clean runtime caches and temporary package archives")
    selftest = sub.add_parser("selftest", help="run offline release smoke tests")
    selftest.add_argument("--json", action="store_true", help="print structured selftest result")
    list_models = sub.add_parser(
        "list-models",
        aliases=["models", "model-list", "/models", "/model-list"],
        help="list models from the active OpenAI-compatible provider",
    )
    list_models.add_argument("--json", action="store_true", help="print raw provider response")
    list_models.add_argument("--limit", type=int, default=80, help="max model ids to print")
    list_models.add_argument("--base-url", help="diagnostic override for this model-list request only")
    list_models.add_argument("--timeout", type=float, help="diagnostic timeout override in seconds")
    api_test = sub.add_parser(
        "api-test",
        aliases=["apitest", "/api-test", "/apitest"],
        help="test main/support-star models on the active OpenAI-compatible provider",
    )
    api_test.add_argument("--json", action="store_true", help="print structured test result")
    api_test.add_argument("--no-stream", action="store_true", help="force non-streaming request")
    api_test.add_argument("--model", help="diagnostic model override for this request only")
    api_test.add_argument("--base-url", help="diagnostic base URL override for this request only")
    api_test.add_argument("--timeout", type=float, help="diagnostic timeout override in seconds")

    chat = sub.add_parser("chat", help="send one message to the active provider")
    chat.add_argument("--message", required=True, help="user message")
    chat.add_argument("--cwd", default=".", help="shell working directory")
    chat.add_argument("--mode", default="chat", choices=sorted(SHELL_DISPATCH_MODES))
    chat.add_argument("--no-tools", action="store_true", help="disable local tool calls")
    chat.add_argument("--stream", action="store_true", help="stream response to stdout")
    chat.add_argument("--json", action="store_true", help="print structured result")

    model = sub.add_parser("model", help="switch collaboration mode")
    model.add_argument("mode", nargs="*", default=[], help="rapid / standard / precise, or 1 / 2 / 3")
    mode = sub.add_parser("mode", help="switch collaboration mode")
    mode.add_argument("mode", nargs="*", default=[], help="rapid / standard / precise, or 1 / 2 / 3")

    sub.add_parser("help", aliases=["/help"], help="show the compact command list")
    sub.add_parser("codexurl", help="open the codexurl proxy menu")

    card = sub.add_parser("render-motd-card", help="render the launcher card text")
    card.add_argument("--width", type=int, default=80, help="terminal width")
    card.add_argument("--seed", type=int, default=None, help="fixed seed for deterministic output")
    card.add_argument("--max-lines", type=int, default=None, help="limit card output height")
    card.add_argument("--settings-label", default="输入 0 进入设置", help="settings hint text")
    card.add_argument("--reroll", action="store_true", help="pick a new launcher role before rendering")

    anim = sub.add_parser("animate-motd-card", help="render card animation frames separated by form-feed")
    anim.add_argument("--width", type=int, default=80, help="terminal width")
    anim.add_argument("--seed", type=int, default=None, help="fixed seed for deterministic output")
    anim.add_argument("--frames", type=int, default=8, help="frame count")
    anim.add_argument("--reroll", action="store_true", help="pick a new launcher role before animating")
    anim.add_argument("--final-card", action="store_true", help="append the final launcher card after animation")
    anim.add_argument("--max-lines", type=int, default=None, help="limit final card output height")
    anim.add_argument("--settings-label", default="输入 0 进入设置", help="settings hint text for final card")

    roster = sub.add_parser("show-roster", help="print roster entries")
    roster.add_argument("--json", action="store_true", help="print as json")

    tools = sub.add_parser("show-tools", help="print available tool-call schemas")
    tools.add_argument("--json", action="store_true", help="print raw api tool schema")

    pending = sub.add_parser("show-pending-command", help="show current pending command approval request")
    pending.add_argument("--json", action="store_true", help="print as json")

    confirm = sub.add_parser("confirm-command", help="execute current pending command after typing y or yes")
    confirm.add_argument("answer", nargs="?", default="", help="confirmation text, usually y or yes")
    confirm.add_argument("--json", action="store_true", help="print as json")

    deny = sub.add_parser("deny-command", help="reject current pending command")
    deny.add_argument("--json", action="store_true", help="print as json")

    sub.add_parser("has-pending-command", help="exit 0 when a pending command approval exists")

    reroll = sub.add_parser("reroll-role", help="force pick a new launcher role")
    reroll.add_argument("--json", action="store_true", help="print as json")

    shell_settings = sub.add_parser("shell-settings", help="interactive shell settings menu")
    shell_settings.add_argument(
        "--tab",
        default="root",
        choices=("root", "api", "deepseek", "gemini", "gemini_params", "gemini-params", "persona", "role", "system", "settings", "websearch", "web_search"),
        help="open a settings section directly",
    )
    settings = sub.add_parser(
        "settings",
        aliases=["/settings"],
        help="open a settings section directly",
    )
    settings.add_argument(
        "tab",
        nargs="?",
        default="root",
        choices=("root", "api", "deepseek", "gemini", "gemini_params", "gemini-params", "persona", "role", "system", "settings", "websearch", "web_search"),
        help="settings section",
    )

    shell_dispatch = sub.add_parser("shell-dispatch", help="dispatch one zsh input to the active provider")
    shell_dispatch.add_argument("--raw", required=True, help="raw input text")
    shell_dispatch.add_argument("--cwd", default=".", help="shell working directory")
    shell_dispatch.add_argument(
        "--mode",
        default="command_not_found",
        choices=sorted(SHELL_DISPATCH_MODES),
        help="dispatch context",
    )
    shell_dispatch.add_argument(
        "--dry-run",
        action="store_true",
        help="print shell-dispatch routing without calling the API",
    )
    return parser


def _cmd_doctor() -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    scrub_volatile_memory_entries(config)
    ensure_memory_layout(config)
    engine = ProjectLingEngine(config)
    prompt_bundle = engine.prompt_bundle
    active_role, _role_seed = resolve_current_role(config)
    persona_bundle = resolve_persona_bundle(config, role=active_role, seed=_role_seed)
    roster = load_roster(config)
    active_persona_path = persona_path_for_role(config, active_role)
    liaison_persona_path = persona_path_for_role(config, persona_bundle.liaison) if persona_bundle.liaison is not None else None
    shared_entries_path = context_entries_status(config).get("entries_path")
    external_context = load_external_context(config, role=active_role)
    active_context = load_role_context(config, role=active_role)
    liaison_context = load_role_context(config, role=persona_bundle.liaison) if persona_bundle.liaison is not None else ""

    def file_text_chars(path: Path | None) -> int:
        if path is None or not path.is_file():
            return 0
        try:
            return len(path.read_text(encoding="utf-8"))
        except OSError:
            return 0
    planner_model, executor_model = _collab_mode_models(config.collab_mode, config)

    payload = {
        "root_dir": str(config.root_dir),
        "config_dir": str(config.config_dir),
        "context_dir": str(config.context_dir),
        "runtime_dir": str(config.runtime_dir),
        "api_key_configured": bool(config.api_key),
        "api_provider": _api_provider_value(getattr(config, "api_provider", "")),
        "base_url": config.base_url,
        "deepseek_api_key_configured": bool(getattr(config, "deepseek_api_key", None)),
        "deepseek_base_url": getattr(config, "deepseek_base_url", ""),
        "deepseek_planner_model": getattr(config, "deepseek_planner_model", ""),
        "deepseek_executor_model": getattr(config, "deepseek_executor_model", ""),
        "gemini_api_key_configured": bool(getattr(config, "gemini_api_key", None)),
        "gemini_base_url": getattr(config, "gemini_base_url", ""),
        "gemini_planner_model": getattr(config, "gemini_planner_model", ""),
        "gemini_executor_model": getattr(config, "gemini_executor_model", ""),
        "gemini_top_p": getattr(config, "gemini_top_p", None),
        "gemini_top_k": getattr(config, "gemini_top_k", None),
        "gemini_candidate_count": getattr(config, "gemini_candidate_count", None),
        "gemini_reasoning_effort": getattr(config, "gemini_reasoning_effort", ""),
        "collab_mode": config.collab_mode,
        "planner_model": planner_model,
        "executor_model": executor_model,
        "temperature": config.temperature,
        "reasoning_effort": getattr(config, "reasoning_effort", ""),
        "max_tokens": config.max_tokens,
        "enable_sse": config.enable_sse,
        "thinking_control": "collab_mode",
        "retry_count": config.retry_count,
        "full_context_mode": config.full_context_mode,
        "context_mode": config.context_mode,
        "websearch_summary_key_configured": bool(config.websearch_summary_key),
        "websearch_web_key_configured": bool(config.websearch_web_key),
        "websearch_endpoint": config.websearch_endpoint,
        "allow_tools": config.allow_tools,
        "timeout_seconds": config.timeout_seconds,
        "role_ttl_hours": config.role_ttl_hours,
        "max_tool_rounds": config.max_tool_rounds,
        "context_max_chars": config.context_max_chars,
        "context_compact_target_chars": config.context_compact_target_chars,
        "contextmanage_context_max_chars": config.advisorling_context_max_chars,
        "contextmanage_context_max_tokens": config.advisorling_context_max_tokens,
        "contextmanage_compact_target_chars": config.advisorling_compact_target_chars,
        "prompt_path": str(prompt_bundle.path),
        "shared_entries_path": str(shared_entries_path or ""),
        "shared_entries_chars": len(external_context),
        "active_context_chars": len(active_context),
        "legacy_external_context_path": str(active_persona_path),
        "legacy_external_context_chars": file_text_chars(active_persona_path),
        "liaison_context_path": str(liaison_persona_path or ""),
        "liaison_context_chars": len(liaison_context),
        "liaison_legacy_context_chars": file_text_chars(liaison_persona_path),
        "role_context_chars": len(active_context),
        "context_entries": context_entries_status(config),
        "persona_display_zh": persona_bundle.main.name_zh,
        "persona_display_en": persona_bundle.main.name_en,
        "persona_liaison_display_zh": persona_bundle.liaison.name_zh if persona_bundle.liaison else "",
        "persona_liaison_display_en": persona_bundle.liaison.name_en if persona_bundle.liaison else "",
        "persona_liaison": persona_bundle.liaison_label,
        "persona_source": persona_bundle.source,
        "memory": memory_status(config),
        "roster_path": str(config.roster_path),
        "roster_entries": len(roster),
        "active_role": f"{active_role.name_zh} / {active_role.name_en}",
        "legacy_runtime_files": [
            name for name in LEGACY_RUNTIME_FILES if (config.runtime_dir / name).exists()
        ],
        "legacy_root_runtime_files": [
            name for name in LEGACY_ROOT_RUNTIME_FILES if (config.root_dir / name).exists()
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_cleanup() -> int:
    runner = PROJECTLING_DIR / "run.sh"
    if not runner.is_file():
        print("[projectling] cleanup runner missing.", file=sys.stderr)
        return 1
    if shutil.which("bash") is None:
        print("[projectling] cleanup skipped: bash not found on this host; run in Termux/WSL for shell cleanup.")
        return 0 if os.name == "nt" else 127
    completed = subprocess.run(["bash", str(runner), "cleanup"], cwd=str(PROJECTLING_DIR), check=False)
    return int(completed.returncode)


def _selftest_record(results: list[dict[str, Any]], name: str, ok: bool, detail: str = "", *, skipped: bool = False) -> None:
    results.append(
        {
            "name": name,
            "status": "skip" if skipped else "ok" if ok else "fail",
            "detail": detail,
        }
    )


def _selftest_run_command(
    results: list[dict[str, Any]],
    name: str,
    command: list[str],
    *,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 20,
) -> None:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    run_env.setdefault("PYTHONUTF8", "1")
    run_env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        completed = subprocess.run(
            command,
            cwd=str(PROJECTLING_DIR),
            input=input_text,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            env=run_env,
            check=False,
        )
    except FileNotFoundError:
        _selftest_record(results, name, False, f"command not found: {command[0]}")
        return
    except OSError as exc:
        _selftest_record(results, name, False, f"os error: {exc}")
        return
    except subprocess.TimeoutExpired:
        _selftest_record(results, name, False, f"timeout after {timeout}s")
        return
    output = (completed.stderr or completed.stdout or "").strip().splitlines()
    detail = output[0][:240] if output else f"rc={completed.returncode}"
    _selftest_record(results, name, completed.returncode == 0, detail)


def _selftest_command_available(command: str) -> bool:
    found = shutil.which(command)
    if not found:
        return False
    if os.name == "nt" and command.lower() in {"bash", "sh", "zsh"}:
        lowered = found.lower().replace("/", "\\")
        if lowered.endswith("\\bash.exe") and "\\windows\\system32\\" in lowered:
            return False
    return True


def _selftest_skip_missing_command(results: list[dict[str, Any]], name: str, command: str) -> bool:
    if _selftest_command_available(command):
        return False
    _selftest_record(results, name, True, f"{command} not found on this host", skipped=True)
    return True


def _cmd_selftest(args: argparse.Namespace) -> int:
    results: list[dict[str, Any]] = []

    python_files = ["core.py", "projectling.py", "tooling.py", "__init__.py"]
    python_syntax_script = (
        "import pathlib, sys, tokenize\n"
        "for name in sys.argv[1:]:\n"
        "    path = pathlib.Path(name)\n"
        "    with tokenize.open(path) as source_file:\n"
        "        source = source_file.read()\n"
        "    compile(source, str(path), 'exec')\n"
        "print(f'syntax ok no-pyc files={len(sys.argv) - 1}')\n"
    )
    _selftest_run_command(
        results,
        "python syntax",
        [sys.executable, "-c", python_syntax_script, *python_files],
        env={"PYTHONDONTWRITEBYTECODE": "1"},
    )
    if not _selftest_skip_missing_command(results, "run.sh syntax", "bash"):
        _selftest_run_command(results, "run.sh syntax", ["bash", "-n", "run.sh"])
    if not _selftest_skip_missing_command(results, "projectling.zsh syntax", "zsh"):
        _selftest_run_command(results, "projectling.zsh syntax", ["zsh", "-n", "projectling.zsh"])

    optional_shell_files = [
        PROJECTLING_DIR.parent / "Quickinstall" / "deploy" / "termux" / "motd.sh",
        PROJECTLING_DIR.parent / "Quickinstall" / "deploy" / "aitermux" / "bootstrap.sh",
        PROJECTLING_DIR.parent / "Quickinstall" / "deploy" / "aitermux" / "zshrc.autostart.zsh",
    ]
    for path in optional_shell_files:
        if not path.is_file():
            _selftest_record(results, f"{path.name} syntax", True, "file not present", skipped=True)
            continue
        shell = "zsh" if path.suffix == ".zsh" else "bash"
        if _selftest_skip_missing_command(results, f"{path.name} syntax", shell):
            continue
        _selftest_run_command(results, f"{path.name} syntax", [shell, "-n", str(path)])

    try:
        config = load_config()
        engine = ProjectLingEngine(config)
        roster = load_roster(config)
        _selftest_record(results, "config load", bool(config.root_dir and config.config_dir), str(config.root_dir))
        _selftest_record(results, "roster load", len(roster) > 0, f"{len(roster)} roles")
        planner_model, executor_model = _collab_mode_models(config.collab_mode, config)
        provider = _api_provider_value(getattr(config, "api_provider", ""))
        _selftest_record(
            results,
            "mode mapping",
            bool(planner_model and executor_model),
            f"{provider}/{config.collab_mode}: {planner_model}+{executor_model}",
        )
        mode_ok = (
            _collab_mode_value("1") == "rapid"
            and _collab_mode_value("2") == "standard"
            and _collab_mode_value("3") == "precise"
        )
        _selftest_record(results, "mode aliases", mode_ok, "1/2/3")

        tools = engine.registry.schemas()
        names = [str((item.get("function") or {}).get("name") or "") for item in tools]
        required_tools = {"link", "update_plan", "model_mode", "contextmanage", "apply_patch", "command"}
        _selftest_record(results, "tool schemas", required_tools.issubset(set(names)), ", ".join(names[:8]))
        apply_schema = next((item for item in tools if str((item.get("function") or {}).get("name") or "") == "apply_patch"), None)
        apply_params = ((apply_schema or {}).get("function") or {}).get("parameters") or {}
        apply_props = apply_params.get("properties") or {}
        apply_anyof = apply_params.get("anyOf") or []
        apply_ok = "operation" in apply_props and "edits" in apply_props and {"required": ["operation"]} in apply_anyof
        _selftest_record(results, "apply_patch structured schema", apply_ok, "operation/edits")

        casual = engine.preview_route("你好", dispatch_mode="chat")
        task = engine.preview_route("请帮我写一个网页版贪吃蛇，单文件 index.html", dispatch_mode="chat")
        code_only = engine.preview_route("写一个 Python 函数 is_even(n)，只给代码，不要创建文件。", dispatch_mode="chat")
        route_ok = (
            casual.get("model") == engine._executor_model_for_mode(config.collab_mode)
            and bool(casual.get("thinking_enabled")) is False
            and bool(task.get("tools_enabled"))
            and task.get("tool_scope") == "plan_gate"
            and bool(task.get("plan_required"))
            and code_only.get("category") == "code_generation"
            and not bool(code_only.get("tools_enabled"))
            and not bool(code_only.get("plan_required"))
        )
        _selftest_record(
            results,
            "routing policy",
            route_ok,
            f"casual={casual.get('model')} task={task.get('tool_scope')}/{task.get('task_complexity')} code={code_only.get('category')}/{code_only.get('tool_scope')}",
        )

        class _SelftestStatusPrinter:
            def __init__(self) -> None:
                self.kinds: list[str] = []

            def begin(self, kind: str) -> None:
                self.kinds.append(kind)

        status_printer = _SelftestStatusPrinter()
        casual_status = _begin_route_status(status_printer, casual)  # type: ignore[arg-type]
        strict_status = _begin_route_status(
            status_printer,
            {"category": "strict_short_reply", "thinking_enabled": False, "show_initial_status": False},
        )  # type: ignore[arg-type]
        analysis_status = _begin_route_status(
            status_printer,
            {"category": "analysis", "thinking_enabled": True, "show_initial_status": True},
        )  # type: ignore[arg-type]
        status_ok = not casual_status and not strict_status and analysis_status and status_printer.kinds == ["thinking"]
        _selftest_record(results, "initial status policy", status_ok, f"calls={','.join(status_printer.kinds) or '-'}")

        with tempfile.TemporaryDirectory(prefix="projectling-audit-selftest-") as audit_tmp:
            audit_path = Path(audit_tmp) / "model-requests.jsonl"
            audit_client = DeepSeekClient(config)
            audit_secret = "audit-secret-prompt-must-not-appear"
            audit_payload = audit_client._build_payload(
                messages=[{"role": "user", "content": audit_secret}],
                tools=[
                    {
                        "type": "function",
                        "function": {"name": "audit_probe", "parameters": {"type": "object", "properties": {}}},
                    }
                ],
                tool_choice="auto",
                temperature=0.0,
                stream=False,
                model=planner_model,
                thinking_enabled=True,
                max_tokens=8,
            )
            audit_record = audit_client._write_model_request_audit(
                audit_payload,
                started_at=time.monotonic(),
                status="ok",
                attempts=1,
                response_data={
                    "id": "audit-response-id",
                    "model": planner_model,
                    "choices": [{"finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                },
                path=audit_path,
            )
            audit_text = audit_path.read_text(encoding="utf-8") if audit_path.is_file() else ""
            audit_ok = (
                audit_record.get("request_model") == planner_model
                and audit_record.get("response_model") == planner_model
                and audit_record.get("tool_names") == ["audit_probe"]
                and audit_record.get("message_count") == 1
                and "messages" not in audit_record
                and audit_secret not in audit_text
                and (not config.api_key or str(config.api_key) not in audit_text)
            )
        _selftest_record(results, "model request audit redaction", audit_ok, f"model={audit_record.get('request_model')}")

        with tempfile.TemporaryDirectory(prefix="projectling-role-lock-selftest-") as role_tmp:
            role_root = Path(role_tmp)
            role_config = replace(config, runtime_dir=role_root, config_dir=role_root)
            role_before, _role_seed = resolve_current_role(role_config)
            set_role_locked(True, role_config)
            role_state_path = role_root / "role.json"
            role_state = json.loads(role_state_path.read_text(encoding="utf-8"))
            role_state["expires_at"] = 0
            role_state_path.write_text(json.dumps(role_state, ensure_ascii=False), encoding="utf-8")
            role_locked, _locked_seed = resolve_current_role(role_config)
            locked_ok = is_role_locked(role_config) and role_locked.name_en == role_before.name_en
            set_role_locked(False, role_config)
            unlocked_ok = not is_role_locked(role_config)
            _selftest_record(
                results,
                "role lock persistence",
                locked_ok and unlocked_ok,
                f"role={role_before.name_en} locked={int(locked_ok)} unlocked={int(unlocked_ok)}",
            )

        valid_tool_chain = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-a"}, {"id": "call-b"}]},
            {"role": "tool", "tool_call_id": "call-a", "content": "{}"},
            {"role": "tool", "tool_call_id": "call-b", "content": "{}"},
            {"role": "system", "content": "post-tool guidance"},
        ]
        invalid_tool_chain = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-a"}, {"id": "call-b"}]},
            {"role": "tool", "tool_call_id": "call-a", "content": "{}"},
            {"role": "system", "content": "inserted too early"},
            {"role": "tool", "tool_call_id": "call-b", "content": "{}"},
        ]
        valid_ok, _valid_error = engine._validate_tool_call_message_order(valid_tool_chain)
        invalid_ok, invalid_error = engine._validate_tool_call_message_order(invalid_tool_chain)
        _selftest_record(
            results,
            "tool call message order",
            valid_ok and not invalid_ok,
            invalid_error or "validator",
        )
        class _SelftestReviewClient:
            def chat_completions(self, **kwargs: Any) -> dict[str, Any]:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": f"复审通过：thinking={kwargs.get('thinking_enabled')}",
                                "reasoning_content": "检查计划状态和下一步。",
                            },
                            "finish_reason": "stop",
                        }
                    ]
                }

        review_engine = ProjectLingEngine(config, client=_SelftestReviewClient())  # type: ignore[arg-type]
        review_role, _review_seed, review_bundle = review_engine.persona_for_dispatch_mode("chat")
        review_route = {"collab_mode": "standard", "tools_enabled": True}
        review_messages: list[dict[str, Any]] = [{"role": "user", "content": "selftest"}]
        review_traces: list[dict[str, Any]] = []
        review_ok = review_engine._maybe_review_plan_update(
            payload={
                "tool": "update_plan",
                "status": "ok",
                "action": "start",
                "needs_review": True,
                "items": [{"id": "T1", "title": "step", "status": "in_progress"}],
            },
            route=review_route,
            role=review_role,
            bundle=review_bundle,
            cwd=PROJECTLING_DIR,
            conversation_messages=review_messages,
            thinking_traces=review_traces,
            on_stream_event=None,
        )
        _selftest_record(
            results,
            "planner review update_plan",
            review_ok and bool(review_traces) and "复审暂不可用" not in str(review_messages[-1].get("content") or ""),
            str((review_messages[-1] or {}).get("content") or "")[:120],
        )
    except Exception as exc:
        _selftest_record(results, "config/schema/routing", False, str(exc))

    try:
        with tempfile.TemporaryDirectory(prefix="projectling-selftest-") as tmp:
            root = Path(tmp)
            cwd = root / "cwd"
            cwd.mkdir(parents=True, exist_ok=True)
            cfg = SimpleNamespace(
                root_dir=root,
                config_dir=root / "config",
                context_dir=root / "context",
                context_entries_path=root / "context" / "entries.jsonl",
                runtime_dir=root / "config",
            )
            context = ToolContext(cwd=cwd, home=Path.home(), config=cfg)
            write_result = _execute_apply_patch_tool(
                {"operation": "write", "target_file": "app/index.html", "content": "<html>A</html>", "brief": "selftest write"},
                context,
            )
            replace_result = _execute_apply_patch_tool(
                {"operation": "replace", "target_file": "app/index.html", "find": "A", "replace": "B", "brief": "selftest replace"},
                context,
            )
            edits_result = _execute_apply_patch_tool(
                {
                    "target_file": "app/index.html",
                    "edits": [
                        {"operation": "insert_after", "find": "B", "content": "C"},
                        {"operation": "append", "content": "<!-- tail -->"},
                    ],
                    "brief": "selftest edits",
                },
                context,
            )
            parent_target_result = _execute_apply_patch_tool(
                {"operation": "write", "target_file": "../authorized-relative.txt", "content": "relative", "brief": "selftest parent target"},
                context,
            )
            absolute_target = root / "authorized-absolute.txt"
            absolute_target_result = _execute_apply_patch_tool(
                {"operation": "write", "target_file": str(absolute_target), "content": "absolute", "brief": "selftest absolute target"},
                context,
            )
            delete_target = cwd / "delete-me.txt"
            delete_target.write_text("keep", encoding="utf-8")
            delete_target_result = _execute_apply_patch_tool(
                {"operation": "delete", "target_file": "delete-me.txt", "brief": "selftest blocked file delete"},
                context,
            )
            raw_delete_result = _execute_apply_patch_tool(
                {
                    "patch": "*** Begin Patch\n*** Delete File: delete-me.txt\n*** End Patch",
                    "brief": "selftest blocked raw delete",
                },
                context,
            )
            private_target_result = _execute_apply_patch_tool(
                {
                    "operation": "write",
                    "target_file": str(root / ".ssh" / "id_ed25519"),
                    "content": "blocked",
                    "brief": "selftest private target",
                    "check_only": True,
                },
                context,
            )
            system_target = (
                Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "projectling-selftest.txt"
                if os.name == "nt"
                else Path("/etc/projectling-selftest.txt")
            )
            system_target_result = _execute_apply_patch_tool(
                {
                    "operation": "write",
                    "target_file": str(system_target),
                    "content": "blocked",
                    "brief": "selftest system target",
                    "check_only": True,
                },
                context,
            )
            app_text = (cwd / "app" / "index.html").read_text(encoding="utf-8")
            apply_ok = (
                write_result.get("status") == "ok"
                and replace_result.get("status") == "ok"
                and edits_result.get("status") == "ok"
                and "BC" in app_text
                and "<!-- tail -->" in app_text
                and parent_target_result.get("status") == "ok"
                and (root / "authorized-relative.txt").read_text(encoding="utf-8").strip() == "relative"
                and absolute_target_result.get("status") == "ok"
                and absolute_target.read_text(encoding="utf-8").strip() == "absolute"
                and str(absolute_target) in (absolute_target_result.get("resolved_files") or [])
                and delete_target_result.get("status") == "blocked"
                and raw_delete_result.get("status") == "blocked"
                and delete_target.read_text(encoding="utf-8") == "keep"
                and private_target_result.get("status") == "blocked"
                and system_target_result.get("status") == "blocked"
            )
            _selftest_record(
                results,
                "apply_patch execution",
                apply_ok,
                f"mode={edits_result.get('mode_used')} global=1 boundary=1",
            )

            command_write_result = _execute_command_tool(
                {"command": "cat > blocked.txt <<'EOF'\nx\nEOF", "brief": "selftest command write"},
                context,
            )
            command_read_result = _execute_command_tool(
                {"command": "pwd", "brief": "selftest command read"},
                context,
            )
            command_mkdir_result = _execute_command_tool(
                {"command": "mkdir -p app2", "brief": "selftest command mkdir"},
                context,
            )
            command_mutation_result = _execute_command_tool(
                {"command": "git commit -m selftest", "brief": "selftest command confirmation"},
                context,
            )
            command_delete_result = _execute_command_tool(
                {"command": "powershell -NoProfile -Command \"Remove-Item delete-me.txt\"", "brief": "selftest delete confirmation"},
                context,
            )
            command_root_result = _execute_command_tool(
                {"command": "sudo -n true", "brief": "selftest privilege confirmation"},
                context,
            )
            _selftest_record(
                results,
                "command write guard",
                command_write_result.get("status") == "blocked"
                and command_mkdir_result.get("status") == "blocked"
                and command_read_result.get("status") == "ok",
                str(command_write_result.get("message") or ""),
            )
            _selftest_record(
                results,
                "command confirmation guard",
                command_mutation_result.get("status") == "pending_confirmation"
                and command_mutation_result.get("confirm_command") == "y"
                and command_delete_result.get("status") == "pending_confirmation"
                and command_delete_result.get("confirm_command") == "yes"
                and command_root_result.get("status") == "pending_confirmation"
                and command_root_result.get("confirm_command") == "yes"
                and delete_target.read_text(encoding="utf-8") == "keep",
                str(command_delete_result.get("reason") or ""),
            )

            _execute_update_plan_tool(
                {
                    "action": "start",
                    "mode": "todo",
                    "title": "stale",
                    "next": "stale next should not survive",
                    "items": [{"id": "OLD", "title": "old", "status": "in_progress"}],
                },
                context,
            )
            plan_start = _execute_update_plan_tool(
                {
                    "action": "start",
                    "mode": "todo",
                    "title": "selftest",
                    "items": [{"id": "T1", "title": "step", "status": "in_progress"}],
                },
                context,
            )
            plan_done = _execute_update_plan_tool({"action": "complete", "step_id": "T1"}, context)
            _selftest_record(
                results,
                "update_plan execution",
                plan_start.get("status") == "ok"
                and plan_start.get("next") == ""
                and plan_done.get("status") == "ok",
                str(plan_done.get("message") or ""),
            )
            context_status = _execute_contextmanage_tool({"mode": "status"}, context)
            _selftest_record(results, "contextmanage execution", context_status.get("status") == "ok", str(context_status.get("message") or ""))
    except Exception as exc:
        _selftest_record(results, "tool execution", False, str(exc))

    if _selftest_skip_missing_command(results, "log housekeeping", "bash"):
        pass
    else:
        try:
            with tempfile.TemporaryDirectory(prefix="projectling-logtest-") as tmp:
                root = Path(tmp)
                aidebug = root / "aidebug"
                logs = aidebug / "logs"
                logs.mkdir(parents=True, exist_ok=True)
                (logs / "startup.log").write_text("S" * 9000, encoding="utf-8")
                (logs / "projectling.log").write_text("P" * 9000, encoding="utf-8")
                old_tmp = aidebug / "tmp" / "old.tmp"
                recent_tmp = aidebug / "tmp" / "recent.tmp"
                old_terminal = aidebug / "projectling" / "terminal output" / "old.log"
                notes_keep = aidebug / "notes" / "keep.md"
                for path, text in ((old_tmp, "old"), (recent_tmp, "recent"), (old_terminal, "old"), (notes_keep, "keep")):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                old_time = time.time() - 3 * 86400
                os.utime(old_tmp, (old_time, old_time))
                os.utime(old_terminal, (old_time, old_time))
                env = dict(os.environ)
                env.update(
                    {
                        "AITERMUX_AIDEBUG_DIR": str(aidebug),
                        "AITERMUX_LOG_CLEAN_INTERVAL_SECONDS": "0",
                        "AITERMUX_STARTUP_LOG_MAX_KB": "4",
                        "AITERMUX_STARTUP_LOG_KEEP_KB": "2",
                        "AITERMUX_PROJECTLING_LOG_MAX_KB": "4",
                        "AITERMUX_PROJECTLING_LOG_KEEP_KB": "2",
                        "AITERMUX_TMP_LOG_KEEP_DAYS": "1",
                        "AITERMUX_TERMINAL_LOG_KEEP_DAYS": "1",
                    }
                )
                completed = subprocess.run(
                    ["bash", str(PROJECTLING_DIR / "run.sh"), "doctor"],
                    cwd=str(PROJECTLING_DIR),
                    env=env,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=20,
                    check=False,
                )
                log_ok = (
                    completed.returncode == 0
                    and (logs / "startup.log").stat().st_size < 4096
                    and (logs / "projectling.log").stat().st_size < 4096
                    and not old_tmp.exists()
                    and recent_tmp.exists()
                    and not old_terminal.exists()
                    and notes_keep.exists()
                )
                _selftest_record(results, "log housekeeping", log_ok, f"rc={completed.returncode}")
        except Exception as exc:
            _selftest_record(results, "log housekeeping", False, str(exc))

    if _selftest_skip_missing_command(results, "single-instance non-tty concurrency", "bash"):
        pass
    else:
        try:
            with tempfile.TemporaryDirectory(prefix="projectling-single-instance-") as tmp:
                root = Path(tmp)
                fake_bin = root / "bin"
                runtime_dir = root / "runtime"
                fake_bin.mkdir(parents=True, exist_ok=True)
                runtime_dir.mkdir(parents=True, exist_ok=True)
                fake_python = fake_bin / "python"
                fake_python.write_text(
                    "#!/usr/bin/env bash\n"
                    "sleep 0.75\n"
                    "exit 0\n",
                    encoding="utf-8",
                )
                fake_python.chmod(0o755)
                script = "\n".join(
                    [
                        "set -euo pipefail",
                        "export PATH=\"${FAKE_BIN}:$PATH\"",
                        "export AITERMUX_PROJECTLING_RUNTIME_DIR=\"${RUNTIME_DIR}\"",
                        "export PROJECTLING_SINGLE_INSTANCE=auto",
                        "\"${PROJECTLING_RUN}\" shell-dispatch --mode chat --cwd . --raw \"pong\" --dry-run >/dev/null 2>&1 &",
                        "pid1=$!",
                        "sleep 0.15",
                        "\"${PROJECTLING_RUN}\" shell-dispatch --mode chat --cwd . --raw \"ping\" --dry-run >/dev/null 2>&1 &",
                        "pid2=$!",
                        "wait \"$pid1\"",
                        "rc1=$?",
                        "wait \"$pid2\"",
                        "rc2=$?",
                        "test \"$rc1\" -eq 0 && test \"$rc2\" -eq 0",
                    ]
                )
                completed = subprocess.run(
                    [
                        "bash",
                        "-c",
                        script,
                    ],
                    cwd=str(PROJECTLING_DIR),
                    env={
                        **os.environ,
                        "FAKE_BIN": str(fake_bin),
                        "RUNTIME_DIR": str(runtime_dir),
                        "PROJECTLING_RUN": str(PROJECTLING_DIR / "run.sh"),
                    },
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=20,
                    check=False,
                )
                _selftest_record(
                    results,
                    "single-instance non-tty concurrency",
                    completed.returncode == 0,
                    f"rc={completed.returncode}",
                )
        except Exception as exc:
            _selftest_record(results, "single-instance non-tty concurrency", False, str(exc))

    if not _selftest_skip_missing_command(results, "pending fast path empty", "bash"):
        _selftest_run_command(results, "pending fast path empty", ["bash", "-c", "./run.sh has-pending-command; test $? -eq 1"], timeout=10)
    if not _selftest_skip_missing_command(results, "pending fast path active", "bash"):
        _selftest_run_command(
            results,
            "pending fast path active",
            ["bash", "-c", "tmp=$(mktemp); trap 'rm -f \"$tmp\"' EXIT; printf '{\"expires_at\":9999999999}\\n' >\"$tmp\"; PROJECTLING_PENDING_COMMAND_FILE=\"$tmp\" ./run.sh has-pending-command"],
            timeout=10,
        )
    if not _selftest_skip_missing_command(results, "cleanup command", "bash"):
        _selftest_run_command(results, "cleanup command", ["bash", "run.sh", "cleanup"], timeout=10)

    _selftest_run_command(results, "settings root exits", [sys.executable, "core.py", "shell-settings"], input_text="0\n", timeout=10)
    _selftest_run_command(results, "slash help exits", [sys.executable, "core.py", "/help"], timeout=10)
    _selftest_run_command(results, "settings api direct exits", [sys.executable, "core.py", "settings", "api"], input_text="0\n", timeout=10)
    _selftest_run_command(results, "settings api slash exits", [sys.executable, "core.py", "/settings", "api"], input_text="0\n", timeout=10)
    _selftest_run_command(results, "settings gemini direct exits", [sys.executable, "core.py", "settings", "gemini"], input_text="0\n", timeout=10)
    _selftest_run_command(results, "settings gemini params exits", [sys.executable, "core.py", "settings", "gemini_params"], input_text="0\n", timeout=10)
    _selftest_run_command(results, "settings role direct exits", [sys.executable, "core.py", "settings", "role"], input_text="0\n", timeout=10)
    _selftest_run_command(results, "settings websearch direct exits", [sys.executable, "core.py", "settings", "websearch"], input_text="0\n", timeout=10)
    _selftest_run_command(results, "settings system exits", [sys.executable, "core.py", "shell-settings", "--tab", "system"], input_text="0\n", timeout=10)
    _selftest_run_command(results, "settings websearch exits", [sys.executable, "core.py", "shell-settings", "--tab", "websearch"], input_text="0\n", timeout=10)
    _selftest_run_command(results, "settings web_search exits", [sys.executable, "core.py", "shell-settings", "--tab", "web_search"], input_text="0\n", timeout=10)
    _selftest_run_command(results, "motd render", [sys.executable, "core.py", "render-motd-card", "--width", "80", "--max-lines", "12"], timeout=10)

    total = len(results)
    failed = [item for item in results if item["status"] == "fail"]
    skipped = [item for item in results if item["status"] == "skip"]
    passed = total - len(failed) - len(skipped)
    score = int(round((passed + len(skipped) * 0.5) * 100 / max(1, total)))
    payload = {
        "status": "ok" if not failed else "fail",
        "score": score,
        "passed": passed,
        "failed": len(failed),
        "skipped": len(skipped),
        "total": total,
        "results": results,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"ProjectLing selftest: {payload['status']} · score {score}% · {passed}/{total} passed")
        for item in results:
            marker = "✓" if item["status"] == "ok" else "-" if item["status"] == "skip" else "✗"
            detail = f" · {item['detail']}" if item.get("detail") else ""
            print(f"{marker} {item['name']}{detail}")
    return 0 if not failed else 1


def _cmd_chat(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    engine = ProjectLingEngine(config)
    role, sequence_seed, persona_bundle = engine.persona_for_dispatch_mode(args.mode)
    current_cwd = Path(args.cwd).expanduser()
    allow_tools = not args.no_tools
    route = engine.preview_route(args.message, allow_tools=allow_tools, dispatch_mode=args.mode)
    if bool(route.get("speaker_handoff_request")) and allow_tools:
        target = str(route.get("speaker_handoff_target") or "").strip().lower()
        if target in {"liaison", "main"}:
            target_role, _target_seed, target_bundle = engine.persona_for_handoff_target(target)
            role = target_role
            persona_bundle = target_bundle

    if args.stream and not args.json:
        printer = ShellStreamPrinter(
            engine.prompt_bundle,
            role,
            persona_bundle=persona_bundle,
            context_budget=load_context_budget(config),
        )
        _begin_route_status(printer, route)
        try:
            result = engine.chat(
                args.message,
                cwd=current_cwd,
                mode=args.mode,
                allow_tools=allow_tools,
                stream=True,
                on_stream_delta=printer.on_delta,
                on_stream_event=printer.on_event,
            )
        except KeyboardInterrupt:
            printer.emit_message("已中断。")
            printer.finish("")
            return 130
        except Exception as exc:  # pragma: no cover - CLI safety net
            printer.emit_message(f"运行失败：{exc}")
            printer.finish("")
            return 1
        if not result.text and not result.tool_traces:
            if result.finish_reason == "stream_limit":
                printer.finish("本轮输出已达到上限。")
            else:
                printer.finish("我没有得到有效回复。")
        else:
            printer.finish(result.text or "")
        return 0

    result = engine.chat(
        args.message,
        cwd=current_cwd,
        mode=args.mode,
        allow_tools=allow_tools,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "text": result.text,
                    "reasoning_text": result.reasoning_text,
                    "rounds": result.rounds,
                    "used_tools": result.used_tools,
                    "thinking_traces": list(result.thinking_traces),
                    "tool_traces": list(result.tool_traces),
                    "finish_reason": result.finish_reason,
                    "routing": result.routing,
                    "persona": {
                        "display_zh": (result.persona_bundle or persona_bundle).main.name_zh,
                        "display_en": (result.persona_bundle or persona_bundle).main.name_en,
                        "liaison_display_zh": (result.persona_bundle or persona_bundle).liaison.name_zh if (result.persona_bundle or persona_bundle).liaison else "",
                        "liaison_display_en": (result.persona_bundle or persona_bundle).liaison.name_en if (result.persona_bundle or persona_bundle).liaison else "",
                        "liaison": (result.persona_bundle or persona_bundle).liaison_label,
                        "source": (result.persona_bundle or persona_bundle).source,
                    },
                    "role": {
                        "rarity": result.role.rarity,
                        "name_zh": result.role.name_zh,
                        "name_en": result.role.name_en,
                    },
                    "raw_response": result.raw_response,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    receipts = _render_tool_receipts(result.tool_traces)
    if receipts and result.text:
        print(f"{receipts}\n\n{result.text}")
    elif receipts:
        print(receipts)
    else:
        print(result.text)
    return 0


def _cmd_model(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    raw_mode = getattr(args, "mode", "")
    if isinstance(raw_mode, list):
        raw_mode = raw_mode[0] if raw_mode else ""
    return _run_model_mode_ui(str(raw_mode or ""))


def _cmd_render_motd_card(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    persona_bundle = resolve_persona_bundle(config)
    if args.reroll:
        role, sequence_seed = reroll_active_role(config)
        remaining_text = "已锁定" if is_role_locked(config) else _format_remaining_text(_remaining_seconds_for_role(config, role))
        persona_bundle = resolve_persona_bundle(config, role=role, seed=sequence_seed)
    elif args.seed is None:
        role, sequence_seed = resolve_current_role(config)
        remaining_text = "已锁定" if is_role_locked(config) else _format_remaining_text(_remaining_seconds_for_role(config, role))
        persona_bundle = resolve_persona_bundle(config, role=role, seed=sequence_seed)
    else:
        role, sequence_seed = resolve_active_role(config, seed=args.seed)
        remaining_text = None
        persona_bundle = resolve_persona_bundle(config, role=role, seed=sequence_seed)

    for line in render_motd_card(
        args.width,
        role,
        seed=sequence_seed,
        remaining_text=remaining_text,
        settings_label=args.settings_label,
        max_lines=args.max_lines,
        persona_bundle=persona_bundle,
    ):
        print(line)
    return 0


def _cmd_animate_motd_card(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    if args.reroll:
        final_role, sequence_seed = reroll_active_role(config)
        persona_bundle = resolve_persona_bundle(config, role=final_role, seed=sequence_seed)
        sequence, final_role, sequence_seed = build_roll_sequence(
            config,
            frames=args.frames,
            final_role=final_role,
            sequence_seed=sequence_seed,
        )
    else:
        sequence, final_role, sequence_seed = build_roll_sequence(config, seed=args.seed, frames=args.frames)
        persona_bundle = resolve_persona_bundle(config, role=final_role, seed=sequence_seed)
    total_frames = max(1, len(sequence))
    animation_sequence = sequence[:-1] if args.final_card and len(sequence) > 1 else sequence
    for index, role in enumerate(animation_sequence):
        is_final_animation_frame = role.name_en == final_role.name_en and index == len(animation_sequence) - 1
        frame_bundle = (
            persona_bundle
            if is_final_animation_frame
            else resolve_persona_bundle(config, role=role, seed=sequence_seed + index)
        )
        for line in render_animation_frame(
            args.width,
            role,
            frame_index=index,
            total_frames=total_frames,
            persona_bundle=frame_bundle,
        ):
            print(line)
        if index != len(animation_sequence) - 1 or args.final_card:
            print("\f", flush=True)
    if args.final_card:
        remaining_text = "已锁定" if is_role_locked(config) else _format_remaining_text(_remaining_seconds_for_role(config, final_role))
        for line in render_motd_card(
            args.width,
            final_role,
            seed=sequence_seed,
            remaining_text=remaining_text,
            settings_label=args.settings_label,
            max_lines=args.max_lines,
            persona_bundle=persona_bundle,
        ):
            print(line)
    sys.stdout.flush()
    return 0


def _cmd_show_roster(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    roster = load_roster(config)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "rarity": role.rarity,
                        "name_zh": role.name_zh,
                        "name_en": role.name_en,
                        "quote": role.quote,
                        "profile": role.profile,
                    }
                    for role in roster
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    for index, role in enumerate(roster, start=1):
        print(f"{index:02d}. [{role.rarity}] {role.name_zh} / {role.name_en} :: {role.profile}")
    return 0


def _cmd_show_tools(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    registry = ProjectLingEngine(config).registry
    schemas = registry.schemas()

    if args.json:
        print(json.dumps(schemas, ensure_ascii=False, indent=2))
        return 0

    for index, tool in enumerate(schemas, start=1):
        fn = tool.get("function") or {}
        print(f"{index:02d}. {fn.get('name', 'unknown')} :: {fn.get('description', '')}")
    return 0


def _apply_diagnostic_api_overrides(config: ProjectLingConfig, args: argparse.Namespace) -> ProjectLingConfig:
    updates: dict[str, Any] = {}
    base_url = str(getattr(args, "base_url", "") or "").strip()
    if base_url:
        updates["base_url"] = base_url
        if _api_provider_value(getattr(config, "api_provider", "")) == "gemini":
            updates["gemini_base_url"] = base_url
        else:
            updates["deepseek_base_url"] = base_url
    timeout = getattr(args, "timeout", None)
    if timeout is not None:
        try:
            updates["timeout_seconds"] = max(5.0, float(timeout))
        except (TypeError, ValueError):
            pass
    return replace(config, **updates) if updates else config


def _cmd_list_models(args: argparse.Namespace) -> int:
    config = _apply_diagnostic_api_overrides(load_config(), args)
    _cleanup_legacy_runtime(config)
    client = DeepSeekClient(config)
    width = _compact_render_width()
    started = time.time()
    try:
        payload = client.list_models()
    except Exception as exc:
        error_payload = {
            "ok": False,
            "provider": _api_provider_value(getattr(config, "api_provider", "")),
            "base_url": config.base_url,
            "elapsed_seconds": round(time.time() - started, 3),
            "error": str(exc),
        }
        if args.json:
            print(json.dumps(error_payload, ensure_ascii=False, indent=2))
        else:
            _print_fit(_model_list_title(_provider_label(config), "fail", width=width), width=width)
            _print_setting_pair("base", error_payload["base_url"], width=width)
            _print_fit_wrapped(error_payload["error"], width=width)
            _print_next_action_check(["API Key", "Base URL", "模型列表接口", "网络"], width=width)
        return 1
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    ids = _extract_model_ids(payload)
    _print_fit(_model_list_title(_provider_label(config), len(ids), width=width), width=width)
    if not ids:
        _print_fit("模型列表为空", width=width)
        _print_fit_wrapped("Relay 返回成功，但没有可用模型。", width=width, max_lines=2)
        _print_next_action_check(["API Key", "Base URL", "模型列表接口", "Relay 渠道"], width=width)
        return 0
    _print_model_taxonomy_hint(ids, width=width)
    planner_model, executor_model = _collab_mode_models(config.collab_mode, config)
    planner_model = str(planner_model or "").strip()
    executor_model = str(executor_model or "").strip()
    for index, model_id in enumerate(ids[: max(1, int(args.limit or 80))], start=1):
        marker_parts = _model_list_marker_parts(model_id, planner_model, executor_model, width=width)
        _print_indexed_model(index, model_id, " / ".join(marker_parts), width=width)
    return 0


def _cmd_api_test(args: argparse.Namespace) -> int:
    config = _apply_diagnostic_api_overrides(load_config(), args)
    _cleanup_legacy_runtime(config)
    width = _compact_render_width()
    if not config.api_key:
        payload = {
            "ok": False,
            "provider": _api_provider_value(getattr(config, "api_provider", "")),
            "error": "api_key_missing",
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_fit("未设置当前 Provider 的 API Key。", width=width)
            _print_fit_wrapped("下一步：先写入当前 Provider 的 API Key。", width=width)
        return 1

    payload = _build_api_test_payload(
        config,
        override_model=str(getattr(args, "model", "") or "").strip(),
        force_no_stream=bool(getattr(args, "no_stream", False)),
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_api_test_payload(payload, width=width)
    return 0 if payload.get("ok") else 1


def _cmd_help() -> int:
    _render_command_help()
    return 0


def _cmd_codexurl() -> int:
    runner = shutil.which("codexurl")
    if runner is None:
        print("未找到 codexurl 命令。")
        return 0
    try:
        completed = subprocess.run([runner], check=False)
    except KeyboardInterrupt:
        return 130
    return int(completed.returncode)


def _print_command_control_payload(payload: dict[str, Any], *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    status = str(payload.get("status") or "unknown")
    if status == "empty":
        print(str(payload.get("message") or "当前没有待确认命令。"))
        return 0

    if status in {"pending_confirmation", "rejected", "ok", "error", "timeout", "blocked"}:
        print(_render_tool_receipt_payload(payload))
        return 0

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_show_pending_command(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    payload = show_pending_command(config)
    return _print_command_control_payload(payload, as_json=args.json)


def _cmd_confirm_command(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    if args.json:
        payload = confirm_pending_command(config, answer=args.answer)
        return _print_command_control_payload(payload, as_json=True)

    engine = ProjectLingEngine(config)
    persona_bundle = engine.current_persona()
    role = persona_bundle.main
    printer = ShellStreamPrinter(
        engine.prompt_bundle,
        role,
        persona_bundle=persona_bundle,
        show_role_heading=False,
        context_budget=load_context_budget(config),
    )
    payload = confirm_pending_command(config, answer=args.answer, event_callback=printer.on_event)
    status = str(payload.get("status") or "")
    if status in {"ok", "error", "timeout"}:
        if printer.line_open:
            printer._write("\n")
            printer.line_open = False
        return 0
    return _print_command_control_payload(payload, as_json=args.json)


def _cmd_deny_command(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    payload = reject_pending_command(config)
    return _print_command_control_payload(payload, as_json=args.json)


def _cmd_has_pending_command() -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    payload = show_pending_command(config)
    return 0 if str(payload.get("status") or "") == "pending_confirmation" else 1


def _cmd_reroll_role(args: argparse.Namespace) -> int:
    config = load_config()
    _cleanup_legacy_runtime(config)
    role, sequence_seed = reroll_active_role(config)
    payload = {
        "name_zh": role.name_zh,
        "name_en": role.name_en,
        "rarity": role.rarity,
        "sequence_seed": sequence_seed,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"{role.name_zh} / {role.name_en}")
    return 0


def _cmd_shell_settings(args: argparse.Namespace) -> int:
    return run_settings_ui(tab=args.tab)


def _cmd_shell_dispatch(args: argparse.Namespace) -> int:
    return dispatch_shell_input(
        args.raw,
        mode=args.mode,
        cwd=Path(args.cwd).expanduser(),
        dry_run=bool(getattr(args, "dry_run", False)),
    )


def main(argv: list[str] | None = None) -> int:
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "doctor":
            return _cmd_doctor()
        if args.command == "cleanup":
            return _cmd_cleanup()
        if args.command == "selftest":
            return _cmd_selftest(args)
        if args.command in {"list-models", "models", "model-list", "/models", "/model-list"}:
            return _cmd_list_models(args)
        if args.command in {"api-test", "apitest", "/api-test", "/apitest"}:
            return _cmd_api_test(args)
        if args.command == "chat":
            return _cmd_chat(args)
        if args.command in {"model", "mode"}:
            return _cmd_model(args)
        if args.command in {"help", "/help"}:
            return _cmd_help()
        if args.command == "codexurl":
            return _cmd_codexurl()
        if args.command == "render-motd-card":
            return _cmd_render_motd_card(args)
        if args.command == "animate-motd-card":
            return _cmd_animate_motd_card(args)
        if args.command == "show-roster":
            return _cmd_show_roster(args)
        if args.command == "show-tools":
            return _cmd_show_tools(args)
        if args.command == "show-pending-command":
            return _cmd_show_pending_command(args)
        if args.command == "confirm-command":
            return _cmd_confirm_command(args)
        if args.command == "deny-command":
            return _cmd_deny_command(args)
        if args.command == "has-pending-command":
            return _cmd_has_pending_command()
        if args.command == "reroll-role":
            return _cmd_reroll_role(args)
        if args.command in {"shell-settings", "settings", "/settings"}:
            return _cmd_shell_settings(args)
        if args.command == "shell-dispatch":
            return _cmd_shell_dispatch(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # pragma: no cover - CLI safety net
        print(f"[projectling] {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 1


__all__ = [
    "ChatCore",
    "ChatResult",
    "MODEL_CHOICES",
    "ProjectLingConfig",
    "ProjectLingEngine",
    "dispatch_shell_input",
    "main",
    "run_settings_ui",
]


if __name__ == "__main__":
    raise SystemExit(main())
