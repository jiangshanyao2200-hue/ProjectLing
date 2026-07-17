from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any
import unicodedata


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


def _fallback_home() -> str:
    try:
        return str(Path.home())
    except RuntimeError:
        return "/data/data/com.termux/files/home"


_SCRIPT_PATH = Path(__file__).resolve()
_SCRIPT_AIDEBUG_DIR = _SCRIPT_PATH.parents[1] if len(_SCRIPT_PATH.parents) >= 2 else None
HOME = Path(os.environ.get("HOME") or _fallback_home()).expanduser()
_DEFAULT_AITERMUX_HOME = Path(os.environ.get("AITERMUX_HOME", str(HOME / "AItermux"))).expanduser()
if os.environ.get("AITERMUX_AIDEBUG_DIR"):
    AIDEBUG_DIR = Path(os.environ["AITERMUX_AIDEBUG_DIR"]).expanduser()
elif _SCRIPT_AIDEBUG_DIR and (_SCRIPT_AIDEBUG_DIR / "runner" / "projectling_auto.py").exists():
    AIDEBUG_DIR = _SCRIPT_AIDEBUG_DIR
else:
    AIDEBUG_DIR = (_DEFAULT_AITERMUX_HOME / "projectling" / "aidebug").expanduser()
def _infer_projectling_dir_from_aidebug() -> Path | None:
    parent = AIDEBUG_DIR.parent
    for candidate in (parent, parent / "app"):
        if (candidate / "core.py").is_file() and (candidate / "run.sh").is_file():
            return candidate
    return parent if (parent / "run.sh").is_file() else None


_INFERRED_PROJECTLING_DIR = _infer_projectling_dir_from_aidebug()
PROJECTLING_DIR = Path(
    os.environ.get("PROJECTLING_DIR", str(_INFERRED_PROJECTLING_DIR or _DEFAULT_AITERMUX_HOME / "projectling"))
).expanduser()
AITERMUX_HOME = Path(os.environ.get("AITERMUX_HOME", str(PROJECTLING_DIR.parent))).expanduser()
PROJECTLING_RUN = PROJECTLING_DIR / "run.sh"
LOG_DIR = AIDEBUG_DIR / "logs"
NOTE_DIR = AIDEBUG_DIR / "notes"
STATE_DIR = AIDEBUG_DIR / "state" / "projectling-auto"
ROUND_DIR = STATE_DIR / "rounds"
STATE_DIR.mkdir(parents=True, exist_ok=True)
ROUND_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
NOTE_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("AITERMUX_HOME", str(AITERMUX_HOME))
os.environ.setdefault("AITERMUX_AIDEBUG_DIR", str(AIDEBUG_DIR))
os.environ.setdefault("PROJECTLING_DIR", str(PROJECTLING_DIR))
AUTO_SESSION_PREFIX = "aidebug-auto-"
ANSI_RE = re.compile(r"\x1b\[[0-9;:]*[A-Za-z]")
NOTE_PATH = NOTE_DIR / "projectling-auto.md"
ISSUES_PATH = LOG_DIR / "projectling-auto-issues.jsonl"
RESOLUTIONS_PATH = LOG_DIR / "projectling-auto-resolutions.jsonl"

sys.path.insert(0, str(PROJECTLING_DIR))

from projectling import ProjectLingEngine, deepseek_usage_cache_summary, load_config, persona_path_for_role  # noqa: E402
from tooling import (  # noqa: E402
    ToolContext,
    ToolRegistry,
    append_chat_turns,
    append_context_entry,
    clear_context_entries,
    load_context_entries,
    memory_pressure_message,
)
try:  # pragma: no cover - package import when loaded as a module
    from .runtime_state_guard import build_snapshot, compare_snapshots
except ImportError:  # pragma: no cover - direct script execution
    from runtime_state_guard import build_snapshot, compare_snapshots


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_log(component: str, message: str) -> None:
    line = f"{timestamp()} {component} {message}\n"
    with (LOG_DIR / "projectling-auto.log").open("a", encoding="utf-8") as handle:
        handle.write(line)


def _capture_runtime_state(label: str) -> tuple[dict[str, Any] | None, str]:
    try:
        return build_snapshot(label=label), ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _runtime_state_result(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    before_error: str = "",
    after_error: str = "",
) -> dict[str, Any]:
    capture_errors = [error for error in (before_error, after_error) if error]
    if before is None or after is None:
        return {
            "ok": False,
            "watched_files": 0,
            "changed_files": [],
            "forbidden_changes": [],
            "semantic_changes": {},
            "secret_presence_changed": False,
            "capture_errors": capture_errors or ["runtime state snapshot unavailable"],
        }
    comparison = compare_snapshots(before, after)
    return {
        "ok": bool(comparison.get("ok")) and not capture_errors,
        "watched_files": len(before.get("files") or {}),
        "changed_files": comparison.get("changed_files") or [],
        "forbidden_changes": comparison.get("forbidden_changes") or [],
        "semantic_changes": comparison.get("semantic_changes") or {},
        "secret_presence_changed": bool(comparison.get("secret_presence_changed")),
        "capture_errors": capture_errors,
    }


def compact_round_payload(payload: dict[str, Any], detail_path: Path) -> dict[str, Any]:
    def nested(source: dict[str, Any], *keys: str) -> Any:
        current: Any = source
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    terminal = payload.get("terminal_smoke") if isinstance(payload.get("terminal_smoke"), dict) else {}
    terminal_info = terminal.get("info") if isinstance(terminal.get("info"), dict) else {}
    command_matrix = payload.get("command_matrix_smoke") if isinstance(payload.get("command_matrix_smoke"), dict) else {}
    web = payload.get("web_smoke") if isinstance(payload.get("web_smoke"), dict) else None
    web_result = web.get("result") if isinstance(web, dict) and isinstance(web.get("result"), dict) else {}
    live = payload.get("live_chat_smoke") if isinstance(payload.get("live_chat_smoke"), dict) else None
    return {
        "round": payload.get("round"),
        "started_at": payload.get("started_at"),
        "run_mode": payload.get("run_mode"),
        "profile": payload.get("profile"),
        "ok": bool(payload.get("ok")),
        "elapsed_seconds": payload.get("elapsed_seconds"),
        "detail_path": str(detail_path),
        "findings": payload.get("findings") or [],
        "doctor_rc": payload.get("doctor_rc"),
        "tools": nested(payload, "schema_check", "names"),
        "ui": {
            "ok": nested(payload, "ui_smoke", "ok"),
            "touching_lines": nested(payload, "ui_smoke", "touching_lines"),
            "too_wide_lines": nested(payload, "ui_smoke", "too_wide_lines"),
        },
        "command": {
            "ok": nested(payload, "command_smoke", "ok"),
            "stdout_chars": nested(payload, "command_smoke", "stdout_chars"),
            "has_head": nested(payload, "command_smoke", "has_head"),
            "has_tail": nested(payload, "command_smoke", "has_tail"),
            "safety_ok": nested(payload, "command_safety", "ok"),
            "matrix_ok": nested(payload, "command_matrix_smoke", "ok"),
            "matrix_cases": nested(payload, "command_matrix_smoke", "case_count"),
            "matrix_details": [
                {
                    "label": item.get("label"),
                    "status": item.get("status"),
                    "returncode": item.get("returncode"),
                    "ok": item.get("ok"),
                }
                for item in (command_matrix.get("cases") if isinstance(command_matrix.get("cases"), list) else [])
                if isinstance(item, dict)
            ],
        },
        "apply_patch": {
            "ok": nested(payload, "patch_smoke", "ok"),
            "external_path_ok": nested(payload, "patch_security", "ok"),
        },
        "terminal": {
            "ok": terminal.get("ok"),
            "skipped": terminal.get("skipped"),
            "reason": terminal.get("reason"),
            "session_name": terminal.get("session_name"),
            "log_path": terminal.get("log_path"),
            "log_lines": terminal_info.get("log_lines"),
            "log_bytes": terminal_info.get("log_bytes"),
            "has_start": terminal.get("log_has_start"),
            "has_send": terminal.get("log_has_send"),
        },
        "aidebug": {
            "slice_ok": nested(payload, "aidebug_slice_smoke", "ok"),
            "precision_ok": nested(payload, "aidebug_slice_smoke", "precision_ok"),
            "head_ok": nested(payload, "aidebug_slice_smoke", "head_ok"),
            "tail_ok": nested(payload, "aidebug_slice_smoke", "tail_ok"),
            "slice_window_ok": nested(payload, "aidebug_slice_smoke", "slice_window_ok"),
            "truncation_ok": nested(payload, "aidebug_slice_smoke", "truncation_ok"),
            "security_ok": nested(payload, "aidebug_security", "ok"),
        },
        "compact_context": {
            "ok": nested(payload, "compact_smoke", "ok"),
            "chars": nested(payload, "compact_smoke", "chars"),
        },
        "context_pressure": {
            "ok": nested(payload, "context_pressure_smoke", "ok"),
            "status_ok": nested(payload, "context_pressure_smoke", "status_ok"),
            "list_ok": nested(payload, "context_pressure_smoke", "list_ok"),
            "replace_ok": nested(payload, "context_pressure_smoke", "replace_ok"),
            "fold_ok": nested(payload, "context_pressure_smoke", "fold_ok"),
            "budget_ok": nested(payload, "context_pressure_smoke", "budget_ok"),
            "freshness_ok": nested(payload, "context_pressure_smoke", "freshness_ok"),
            "entries_before": nested(payload, "context_pressure_smoke", "entries_before"),
            "entries_file_total": nested(payload, "context_pressure_smoke", "entries_file_total"),
            "hidden_after": nested(payload, "context_pressure_smoke", "hidden_after"),
            "folded": nested(payload, "context_pressure_smoke", "folded"),
            "active_chars": nested(payload, "context_pressure_smoke", "active_chars"),
            "compact_target": nested(payload, "context_pressure_smoke", "compact_target"),
            "summary_visible": nested(payload, "context_pressure_smoke", "summary_visible"),
        },
        "context_pressure_variants": {
            "ok": nested(payload, "context_pressure_variants_smoke", "ok"),
            "variant_count": nested(payload, "context_pressure_variants_smoke", "variant_count"),
            "passed": nested(payload, "context_pressure_variants_smoke", "passed"),
            "labels": nested(payload, "context_pressure_variants_smoke", "labels"),
        },
        "memory": {
            "ok": nested(payload, "memory_smoke", "ok"),
            "status_ok": nested(payload, "memory_smoke", "status_ok"),
            "add_ok": nested(payload, "memory_smoke", "add_ok"),
            "check_ok": nested(payload, "memory_smoke", "check_ok"),
            "alias_ok": nested(payload, "memory_smoke", "alias_ok"),
            "read_ok": nested(payload, "memory_smoke", "read_ok"),
            "reject_ok": nested(payload, "memory_smoke", "reject_ok"),
            "append_ok": nested(payload, "memory_smoke", "append_ok"),
            "db_integrity_ok": nested(payload, "memory_smoke", "db_integrity_ok"),
            "keyword_unique_ok": nested(payload, "memory_smoke", "keyword_unique_ok"),
            "journal_mode": nested(payload, "memory_smoke", "journal_mode"),
            "diaries": nested(payload, "memory_smoke", "diaries"),
            "events": nested(payload, "memory_smoke", "events"),
        },
        "memory_pressure": {
            "ok": nested(payload, "memory_pressure_smoke", "ok"),
            "append_ok": nested(payload, "memory_pressure_smoke", "append_ok"),
            "pressure_ok": nested(payload, "memory_pressure_smoke", "pressure_ok"),
            "consume_ok": nested(payload, "memory_pressure_smoke", "consume_ok"),
            "read_ok": nested(payload, "memory_pressure_smoke", "read_ok"),
            "bytes_before": nested(payload, "memory_pressure_smoke", "bytes_before"),
            "bytes_after": nested(payload, "memory_pressure_smoke", "bytes_after"),
            "memory_max_bytes": nested(payload, "memory_pressure_smoke", "memory_max_bytes"),
        },
        "web_search": {
            "ok": web.get("ok") if isinstance(web, dict) else None,
            "validation_ok": nested(payload, "web_validation", "ok"),
            "result_count": web_result.get("result_count"),
        },
        "runtime_state": {
            "ok": nested(payload, "runtime_state_guard", "ok"),
            "watched_files": nested(payload, "runtime_state_guard", "watched_files"),
            "forbidden_changes": nested(payload, "runtime_state_guard", "forbidden_changes"),
            "semantic_change_keys": sorted(
                (payload.get("runtime_state_guard") or {}).get("semantic_changes", {}).keys()
            )
            if isinstance((payload.get("runtime_state_guard") or {}).get("semantic_changes"), dict)
            else [],
            "secret_presence_changed": nested(payload, "runtime_state_guard", "secret_presence_changed"),
            "capture_errors": nested(payload, "runtime_state_guard", "capture_errors"),
        },
        "live_chat": None
        if live is None
        else {
            "ok": live.get("ok"),
            "provider": live.get("provider"),
            "main_provider": live.get("main_provider"),
            "executor_provider": live.get("executor_provider"),
            "rounds": live.get("rounds"),
            "tool_names": live.get("tool_names"),
            "tool_actor_labels": live.get("tool_actor_labels"),
            "tool_actor_kinds": live.get("tool_actor_kinds"),
            "thinking_roles": live.get("thinking_roles"),
            "thinking_actor_labels": live.get("thinking_actor_labels"),
            "planner_review_errors": live.get("planner_review_errors"),
            "command_executor_actor": live.get("command_executor_actor"),
            "dual_star_metadata_ok": live.get("dual_star_metadata_ok"),
            "usage": live.get("usage"),
            "request_usage_total": live.get("request_usage_total"),
            "request_breakdown": [
                {
                    "call": item.get("call"),
                    "kind": item.get("kind"),
                    "model": item.get("model"),
                    "thinking_enabled": item.get("thinking_enabled"),
                    "message_count": item.get("message_count"),
                    "message_role_chars": item.get("message_role_chars"),
                    "tool_schema_count": item.get("tool_schema_count"),
                    "tool_schema_names": item.get("tool_schema_names"),
                    "request_json_chars": item.get("request_json_chars"),
                    "tool_schema_json_chars": item.get("tool_schema_json_chars"),
                    "usage": item.get("usage"),
                }
                for item in (live.get("request_breakdown") if isinstance(live.get("request_breakdown"), list) else [])
                if isinstance(item, dict)
            ],
            "attempts": live.get("attempts"),
            "cache_ok": live.get("cache_ok"),
            "cache_warmup": live.get("cache_warmup"),
            "context_restored": live.get("context_restored"),
        },
        "resolutions": payload.get("resolutions") or [],
    }


def log_json(payload: dict[str, Any]) -> None:
    safe_started = re.sub(r"[^0-9A-Za-z_-]+", "-", str(payload.get("started_at") or timestamp())).strip("-")
    detail_path = ROUND_DIR / f"{safe_started}-round-{payload.get('round', 'unknown')}.json"
    tmp = detail_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(detail_path)
    payload["detail_path"] = str(detail_path)
    compact = compact_round_payload(payload, detail_path)
    with (LOG_DIR / "projectling-auto.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(compact, ensure_ascii=False) + "\n")


def log_issue(payload: dict[str, Any]) -> None:
    with ISSUES_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def log_resolution(payload: dict[str, Any]) -> None:
    with RESOLUTIONS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def context_entry_visible(entry: dict[str, Any]) -> bool:
    if not bool(entry.get("visible", True)):
        return False
    meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
    return not bool(meta.get("replaced_by"))


def context_freshness_metrics(config: Any, *, status_after: dict[str, Any], summary_id: str) -> dict[str, Any]:
    entries = load_context_entries(config)
    visible = [entry for entry in entries if context_entry_visible(entry)]
    hidden = len(entries) - len(visible)
    active_chars = sum(len(str(entry.get("content") or "")) for entry in visible)
    compact_target = int(getattr(config, "context_compact_target_chars", 0) or 0)
    context_max = int(getattr(config, "context_max_chars", 0) or 0)
    summary_visible = any(str(entry.get("id") or "") == str(summary_id or "") for entry in visible)
    try:
        status_total = int(status_after.get("entries_total") or -1)
        status_hidden = int(status_after.get("entries_hidden") or -1)
    except (TypeError, ValueError):
        status_total = -1
        status_hidden = -1
    return {
        "entries_file_total": len(entries),
        "visible_file_total": len(visible),
        "hidden_file_total": hidden,
        "active_chars": active_chars,
        "compact_target": compact_target,
        "context_max": context_max,
        "summary_visible": summary_visible,
        "ledger_matches_status": len(entries) == status_total and hidden == status_hidden,
        "ok": bool(
            entries
            and len(entries) == status_total
            and hidden == status_hidden
            and active_chars > 0
            and compact_target > 0
            and context_max >= compact_target
            and summary_visible
        ),
    }


def issue_key(issue: dict[str, Any]) -> str:
    return "|".join(
        [
            str(issue.get("at") or ""),
            str(issue.get("round") or ""),
            str(issue.get("component") or ""),
            str(issue.get("message") or ""),
        ]
    )


def component_resolution_status(payload: dict[str, Any], component: str) -> tuple[str, str]:
    if component == "projectling-auto":
        return ("resolved", "round_ok=true") if payload.get("ok") else ("", "round_ok=false")
    if component == "command":
        command = payload.get("command_smoke") if isinstance(payload.get("command_smoke"), dict) else {}
        safety = payload.get("command_safety") if isinstance(payload.get("command_safety"), dict) else {}
        matrix = payload.get("command_matrix_smoke") if isinstance(payload.get("command_matrix_smoke"), dict) else {}
        ok = bool(command.get("ok") and safety.get("ok") and (not matrix or matrix.get("ok")))
        return ("resolved", "command_receipt_safety_matrix_ok=true") if ok else ("", "command_receipt_safety_or_matrix_ok=false")
    if component == "terminal":
        terminal = payload.get("terminal_smoke") if isinstance(payload.get("terminal_smoke"), dict) else {}
        if terminal.get("ok") and terminal.get("skipped"):
            reason = str(terminal.get("reason") or "terminal_skipped")
            return ("compat-covered", f"terminal_skip={reason}")
        if terminal.get("ok"):
            return ("resolved", "terminal_log_markers_ok=true")
        return ("", "terminal_ok=false")
    if component == "aidebug":
        aidebug = payload.get("aidebug_slice_smoke") if isinstance(payload.get("aidebug_slice_smoke"), dict) else {}
        security = payload.get("aidebug_security") if isinstance(payload.get("aidebug_security"), dict) else {}
        ok = bool(aidebug.get("ok") and security.get("ok"))
        return ("resolved", "aidebug_slice_and_security_ok=true") if ok else ("", "aidebug_slice_or_security_ok=false")
    if component == "apply_patch":
        patch = payload.get("patch_smoke") if isinstance(payload.get("patch_smoke"), dict) else {}
        security = payload.get("patch_security") if isinstance(payload.get("patch_security"), dict) else {}
        ok = bool(patch.get("ok") and security.get("ok"))
        return ("resolved", "apply_patch_and_external_path_ok=true") if ok else ("", "apply_patch_or_external_path_ok=false")
    if component == "compact_context":
        compact = payload.get("compact_smoke") if isinstance(payload.get("compact_smoke"), dict) else {}
        return ("resolved", "compact_context_ok=true") if compact.get("ok") else ("", "compact_context_ok=false")
    if component == "context_pressure":
        pressure = payload.get("context_pressure_smoke") if isinstance(payload.get("context_pressure_smoke"), dict) else {}
        return ("resolved", "context_pressure_ok=true") if pressure.get("ok") else ("", "context_pressure_ok=false")
    if component == "context_pressure_variants":
        variants = payload.get("context_pressure_variants_smoke") if isinstance(payload.get("context_pressure_variants_smoke"), dict) else {}
        return ("resolved", "context_pressure_variants_ok=true") if variants.get("ok") else ("", "context_pressure_variants_ok=false")
    if component == "memory":
        memory = payload.get("memory_smoke") if isinstance(payload.get("memory_smoke"), dict) else {}
        return ("resolved", "memory_smoke_ok=true") if memory.get("ok") else ("", "memory_smoke_ok=false")
    if component == "memory_pressure":
        memory_pressure = payload.get("memory_pressure_smoke") if isinstance(payload.get("memory_pressure_smoke"), dict) else {}
        return (
            ("resolved", "memory_pressure_consume_source_ok=true")
            if memory_pressure.get("ok")
            else ("", "memory_pressure_consume_source_ok=false")
        )
    if component == "web_search":
        validation = payload.get("web_validation") if isinstance(payload.get("web_validation"), dict) else {}
        web = payload.get("web_smoke") if isinstance(payload.get("web_smoke"), dict) else None
        if validation.get("ok") and (web is None or web.get("ok")):
            return ("resolved", "web_validation_and_optional_live_ok=true")
        return ("", "web_validation_or_live_ok=false")
    if component == "live_chat":
        live = payload.get("live_chat_smoke") if isinstance(payload.get("live_chat_smoke"), dict) else {}
        provider = str(live.get("provider") or getattr(payload.get("config", None), "api_provider", "") or "").strip().lower()
        usage = live.get("usage") if isinstance(live.get("usage"), dict) else {}
        try:
            miss = int(usage.get("cache_miss_tokens") or 0)
        except (TypeError, ValueError):
            miss = 0
        try:
            hit_rate = float(usage.get("cache_hit_rate") or 0.0)
        except (TypeError, ValueError):
            hit_rate = 0.0
        if provider and provider != "deepseek":
            ok = bool(live.get("ok") and live.get("context_restored"))
            evidence = f"provider={provider} live_chat_functional_ok={ok} miss={miss} hit_rate={hit_rate}"
            return ("resolved", evidence) if ok else ("", evidence)
        ok = bool(live.get("ok") and miss <= 1000 and hit_rate >= 85.0 and live.get("context_restored"))
        return ("resolved", f"live_chat_cache_ok=true miss={miss} hit_rate={hit_rate}") if ok else ("", f"live_chat_cache_ok=false miss={miss} hit_rate={hit_rate}")
    if component == "live_chat_cost":
        live = payload.get("live_chat_smoke") if isinstance(payload.get("live_chat_smoke"), dict) else {}
        usage_total = live.get("request_usage_total") if isinstance(live.get("request_usage_total"), dict) else {}
        try:
            prompt_tokens = int(usage_total.get("prompt_tokens"))
        except (TypeError, ValueError):
            return ("", "live_chat_cost_prompt_tokens=missing")
        try:
            api_calls = int(usage_total.get("api_calls") or 0)
        except (TypeError, ValueError):
            api_calls = 0
        ok = bool(live.get("ok") and live.get("context_restored") and prompt_tokens <= 50000)
        evidence = f"live_chat_cost_ok={str(ok).lower()} prompt={prompt_tokens} api_calls={api_calls}"
        return ("resolved", evidence) if ok else ("", evidence)
    ui = payload.get("ui_smoke") if isinstance(payload.get("ui_smoke"), dict) else {}
    if component == "ui" and ui.get("ok"):
        return ("resolved", "ui_smoke_ok=true")
    return ("", f"component={component} has no resolution evidence")


def collect_issue_resolutions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not payload.get("ok"):
        return []
    issues = read_jsonl(ISSUES_PATH)
    if not issues:
        return []
    resolved_keys = {
        str(item.get("issue_key") or "")
        for item in read_jsonl(RESOLUTIONS_PATH)
        if item.get("issue_key")
    }
    resolutions: list[dict[str, Any]] = []
    for issue in issues:
        key = issue_key(issue)
        if key in resolved_keys:
            continue
        component = str(issue.get("component") or "")
        status, evidence = component_resolution_status(payload, component)
        if not status:
            continue
        resolutions.append(
            {
                "issue_key": key,
                "issue_at": issue.get("at"),
                "issue_round": issue.get("round"),
                "issue_component": component,
                "issue_message": issue.get("message"),
                "resolved_at": payload.get("started_at"),
                "resolved_round": payload.get("round"),
                "status": status,
                "evidence": evidence,
            }
        )
    return resolutions


def run_cmd(command: list[str], *, cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AITERMUX_HOME"] = str(AITERMUX_HOME)
    env["AITERMUX_AIDEBUG_DIR"] = str(AIDEBUG_DIR)
    env["PROJECTLING_DIR"] = str(PROJECTLING_DIR)
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
    )


def display_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def cleanup_stale_auto_sessions() -> list[str]:
    if not shutil.which("tmux"):
        return []
    completed = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        return []
    killed: list[str] = []
    for raw_name in completed.stdout.splitlines():
        name = raw_name.strip()
        if not name.startswith(AUTO_SESSION_PREFIX):
            continue
        subprocess.run(["tmux", "kill-session", "-t", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        killed.append(name)
    if killed:
        write_log("projectling-auto", f"cleanup killed_sessions={','.join(killed)}")
    return killed


def tool_call(registry: ToolRegistry, ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    call = {
        "id": f"auto-{name}-{time.time_ns()}",
        "function": {
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }
    result = registry.execute_tool_call(call, ctx)
    return json.loads(result["content"])


def smoke_projectling_ui() -> dict[str, Any]:
    rendered = projectling_cli(
        "render-motd-card",
        "--width",
        "69",
        "--max-lines",
        "12",
        "--settings-label",
        "",
        timeout=40,
    )
    snapshot = STATE_DIR / "motd-card.txt"
    snapshot.write_text(rendered.stdout, encoding="utf-8")
    plain = ANSI_RE.sub("", rendered.stdout)
    lines = plain.splitlines()
    touching_lines = [index + 1 for index, line in enumerate(lines) if line.strip() and not line.startswith("  ")]
    too_wide = [index + 1 for index, line in enumerate(lines) if display_width(line) > 80]
    return {
        "tool": "projectling-ui",
        "returncode": rendered.returncode,
        "snapshot": str(snapshot),
        "lines": len(lines),
        "touching_lines": touching_lines[:10],
        "too_wide_lines": too_wide[:10],
        "stdout_tail": rendered.stdout[-1200:],
        "stderr_tail": rendered.stderr[-800:],
        "ok": rendered.returncode == 0 and bool(lines) and not touching_lines and not too_wide,
    }


def smoke_command_receipt(registry: ToolRegistry, ctx: ToolContext) -> dict[str, Any]:
    python_cmd = "python" if os.name == "nt" else "python3"
    command = (
        f"{python_cmd} -c \"for i in range(2200): "
        "print(f'COMMAND_STRESS_{i:04d}_' + 'x'*40)\""
    )
    result = tool_call(
        registry,
        ctx,
        "command",
        {
            "command": command,
            "timeout_seconds": 20,
        },
    )
    stdout = str(result.get("stdout") or "")
    return {
        "tool": "command",
        "result": result,
        "stdout_chars": len(stdout),
        "has_head": "COMMAND_STRESS_0000" in stdout,
        "has_tail": "COMMAND_STRESS_2199" in stdout,
        "ok": (
            result.get("status") == "ok"
            and result.get("returncode") == 0
            and bool(result.get("stdout_truncated_for_model"))
            and len(stdout) <= 12500
            and "COMMAND_STRESS_0000" in stdout
            and "COMMAND_STRESS_2199" in stdout
        ),
    }


def smoke_command_safety(registry: ToolRegistry, ctx: ToolContext) -> dict[str, Any]:
    cases = [
        ("blocked_rm_root", "rm -rf /", {"blocked"}),
        ("blocked_python_repl", "python3", {"blocked"}),
        ("blocked_adb_shell_repl", "adb shell", {"blocked"}),
    ]
    results: list[dict[str, Any]] = []
    ok = True
    for label, command, expected in cases:
        result = tool_call(
            registry,
            ctx,
            "command",
            {
                "command": command,
                "timeout_seconds": 5,
            },
        )
        status = str(result.get("status") or "")
        case_ok = status in expected
        ok = ok and case_ok
        results.append(
            {
                "label": label,
                "command": command,
                "status": status,
                "risk": str(result.get("risk") or ""),
                "reason": str(result.get("reason") or ""),
                "ok": case_ok,
            }
        )
    return {"tool": "command", "kind": "safety", "cases": results, "ok": ok}


def smoke_command_matrix(registry: ToolRegistry, ctx: ToolContext) -> dict[str, Any]:
    python_cmd = "python -X utf8" if os.name == "nt" else "python3"
    cases = [
        {
            "label": "unicode_stdout",
            "command": f"{python_cmd} -c \"print('UNICODE_STRESS_OK_项目_✓')\"",
            "timeout_seconds": 10,
            "expect_status": "ok",
            "expect_returncode": 0,
            "stdout_contains": "UNICODE_STRESS_OK_项目_✓",
        },
        {
            "label": "stderr_capture",
            "command": f"{python_cmd} -c \"import sys; print('STDOUT_MARK'); print('STDERR_MARK', file=sys.stderr)\"",
            "timeout_seconds": 10,
            "expect_status": "ok",
            "expect_returncode": 0,
            "stdout_contains": "STDOUT_MARK",
            "stderr_contains": "STDERR_MARK",
        },
        {
            "label": "nonzero_exit",
            "command": f"{python_cmd} -c \"import sys; print('NONZERO_MARK'); sys.exit(7)\"",
            "timeout_seconds": 10,
            "expect_status": "error",
            "expect_nonzero": True,
            "stdout_contains": "NONZERO_MARK",
        },
        {
            "label": "timeout",
            "command": f"{python_cmd} -c \"import time; print('TIMEOUT_START', flush=True); time.sleep(8)\"",
            "timeout_seconds": 5,
            "expect_status": "timeout",
            "expect_returncode": None,
            "stdout_contains": "TIMEOUT_START",
        },
    ]
    results: list[dict[str, Any]] = []
    ok = True
    for case in cases:
        result = tool_call(
            registry,
            ctx,
            "command",
            {
                "command": case["command"],
                "timeout_seconds": case["timeout_seconds"],
                "brief": f"command matrix {case['label']}",
            },
        )
        stdout = str(result.get("stdout") or "")
        stderr = str(result.get("stderr") or "")
        expected_returncode = case.get("expect_returncode")
        if case.get("expect_nonzero"):
            returncode_ok = isinstance(result.get("returncode"), int) and int(result.get("returncode") or 0) != 0
        else:
            returncode_ok = result.get("returncode") == expected_returncode
        if expected_returncode is None and not case.get("expect_nonzero"):
            returncode_ok = result.get("returncode") is None
        case_ok = (
            result.get("status") == case.get("expect_status")
            and returncode_ok
            and str(case.get("stdout_contains") or "") in stdout
            and (not case.get("stderr_contains") or str(case.get("stderr_contains")) in stderr)
        )
        ok = ok and case_ok
        results.append(
            {
                "label": case["label"],
                "status": result.get("status"),
                "returncode": result.get("returncode"),
                "stdout_sample": stdout[:160],
                "stderr_sample": stderr[:160],
                "stdout_truncated": result.get("stdout_truncated"),
                "stderr_truncated": result.get("stderr_truncated"),
                "timeout_seconds": result.get("timeout_seconds"),
                "ok": case_ok,
            }
        )
    return {"tool": "command", "kind": "matrix", "case_count": len(results), "cases": results, "ok": ok}


def smoke_apply_patch(registry: ToolRegistry, ctx: ToolContext) -> dict[str, Any]:
    scratch = STATE_DIR / "apply-patch-smoke"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    run_cmd(["git", "init", "-q"], cwd=scratch, timeout=20)
    run_cmd(["git", "config", "user.email", "aidebug@example.com"], cwd=scratch, timeout=20)
    run_cmd(["git", "config", "user.name", "aidebug"], cwd=scratch, timeout=20)
    (scratch / "sample.txt").write_text("hello\n", encoding="utf-8")
    patch = """diff --git a/sample.txt b/sample.txt
index 1111111..2222222 100644
--- a/sample.txt
+++ b/sample.txt
@@ -1 +1,2 @@
 hello
+world
"""
    result = tool_call(
        registry,
        ctx,
        "apply_patch",
        {
            "cwd": str(scratch),
            "patch": patch,
            "strip": 1,
        },
    )
    content = (scratch / "sample.txt").read_text(encoding="utf-8", errors="replace")
    return {
        "tool": "apply_patch",
        "result": result,
        "content": content,
        "ok": result.get("status") == "ok" and content == "hello\nworld\n",
    }


def smoke_apply_patch_security(registry: ToolRegistry, ctx: ToolContext) -> dict[str, Any]:
    root = STATE_DIR / "apply-patch-external-path"
    scratch = root / "cwd"
    target = root / "authorized-target.txt"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    write_result = tool_call(
        registry,
        ctx,
        "apply_patch",
        {
            "cwd": str(scratch),
            "operation": "write",
            "target_file": str(target),
            "content": "external path v1\n",
        },
    )
    replace_result = tool_call(
        registry,
        ctx,
        "apply_patch",
        {
            "cwd": str(scratch),
            "operation": "replace",
            "target_file": str(target),
            "find": "v1",
            "replace": "v2",
        },
    )
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        content = ""
    delete_result = tool_call(
        registry,
        ctx,
        "apply_patch",
        {
            "cwd": str(scratch),
            "operation": "delete",
            "target_file": str(target),
        },
    )
    return {
        "tool": "apply_patch",
        "kind": "authorized-external-path",
        "write": write_result,
        "replace": replace_result,
        "delete": delete_result,
        "target_path": str(target),
        "content_before_delete": content,
        "target_exists_after_delete": target.exists(),
        "ok": (
            write_result.get("status") == "ok"
            and replace_result.get("status") == "ok"
            and content == "external path v2\n"
            and delete_result.get("status") == "ok"
            and not target.exists()
        ),
    }


def smoke_web_search(registry: ToolRegistry, ctx: ToolContext, query: str) -> dict[str, Any] | None:
    query = query.strip()
    if not query:
        return None
    result = tool_call(
        registry,
        ctx,
        "web_search",
        {
            "query": query,
            "max_results": 3,
        },
    )
    return {
        "tool": "web_search",
        "result": result,
        "ok": result.get("status") in {"ok", "empty"},
    }


def smoke_web_search_validation(registry: ToolRegistry, ctx: ToolContext) -> dict[str, Any]:
    result = tool_call(registry, ctx, "web_search", {"query": "", "max_results": 3})
    return {
        "tool": "web_search",
        "kind": "validation",
        "result": result,
        "ok": result.get("status") == "error",
    }


def smoke_terminal(registry: ToolRegistry, ctx: ToolContext, round_id: int) -> dict[str, Any]:
    session_name = f"{AUTO_SESSION_PREFIX}{os.getpid()}-{round_id}"
    start = tool_call(
        registry,
        ctx,
        "terminal",
        {
            "action": "start",
            "session_name": session_name,
            "cwd": str(PROJECTLING_DIR),
        },
    )
    blocked_message = str(start.get("message") or "")
    if start.get("status") == "blocked" and ("Android am" in blocked_message or "tmux" in blocked_message):
        return {
            "tool": "terminal",
            "session_name": session_name,
            "start": start,
            "start_send": None,
            "send": None,
            "info": None,
            "close": None,
            "log_path": "",
            "log_has_start": False,
            "log_has_send": False,
            "skipped": True,
            "reason": "tmux_missing" if "tmux" in blocked_message else "android_am_missing",
            "ok": True,
        }
    time.sleep(1.0)
    start_send = tool_call(
        registry,
        ctx,
        "terminal",
        {
            "action": "send",
            "session_name": session_name,
            "command": f"printf 'AIDEBUG_ROUND_{round_id}_START\\n'",
        },
    )
    time.sleep(0.8)
    send = tool_call(
        registry,
        ctx,
        "terminal",
        {
            "action": "send",
            "session_name": session_name,
            "command": f"printf 'AIDEBUG_ROUND_{round_id}_SEND\\n'",
        },
    )
    time.sleep(0.8)
    info = tool_call(
        registry,
        ctx,
        "terminal",
        {
            "action": "info",
            "session_name": session_name,
        },
    )
    close = tool_call(registry, ctx, "terminal", {"action": "close", "session_name": session_name})
    log_path = Path(str(info.get("log_path") or start.get("log_path") or ""))
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    return {
        "tool": "terminal",
        "session_name": session_name,
        "start": start,
        "start_send": start_send,
        "send": send,
        "info": info,
        "close": close,
        "log_path": str(log_path),
        "log_has_start": f"AIDEBUG_ROUND_{round_id}_START" in log_text,
        "log_has_send": f"AIDEBUG_ROUND_{round_id}_SEND" in log_text,
        "ok": (
            close.get("status") == "ok"
            and f"AIDEBUG_ROUND_{round_id}_START" in log_text
            and f"AIDEBUG_ROUND_{round_id}_SEND" in log_text
        ),
    }


def smoke_aidebug_read_precision(
    registry: ToolRegistry,
    ctx: ToolContext,
    terminal_smoke: dict[str, Any],
    round_id: int,
) -> dict[str, Any]:
    precision_dir = STATE_DIR / "aidebug-read-precision"
    precision_dir.mkdir(parents=True, exist_ok=True)
    precision_path = precision_dir / f"round-{round_id}.log"
    lines = [
        f"AIDEBUG_PRECISION_L{index:03d} round={round_id} payload={'x' * (index % 7)}"
        for index in range(1, 41)
    ]
    precision_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    precision_relative = str(precision_path.resolve().relative_to(AIDEBUG_DIR.resolve()))
    head = tool_call(
        registry,
        ctx,
        "aidebug",
        {
            "action": "read",
            "path": precision_relative,
            "mode": "head",
            "lines": 5,
        },
    )
    tail = tool_call(
        registry,
        ctx,
        "aidebug",
        {
            "action": "read",
            "path": precision_relative,
            "mode": "tail",
            "lines": 5,
        },
    )
    sliced = tool_call(
        registry,
        ctx,
        "aidebug",
        {
            "action": "read",
            "path": precision_relative,
            "mode": "slice",
            "start_line": 17,
            "end_line": 23,
        },
    )
    large_path = precision_dir / f"round-{round_id}-large.log"
    large_lines = [
        f"AIDEBUG_TRUNC_HEAD round={round_id}",
        *[
            f"AIDEBUG_TRUNC_MIDDLE_{index:04d} round={round_id} payload={'m' * 100}"
            for index in range(1, 520)
        ],
        f"AIDEBUG_TRUNC_TAIL round={round_id}",
    ]
    large_path.write_text("\n".join(large_lines) + "\n", encoding="utf-8")
    large_relative = str(large_path.resolve().relative_to(AIDEBUG_DIR.resolve()))
    large = tool_call(
        registry,
        ctx,
        "aidebug",
        {
            "action": "read",
            "path": large_relative,
            "mode": "tail",
            "lines": 1000,
        },
    )
    head_stdout = str(head.get("stdout") or "")
    tail_stdout = str(tail.get("stdout") or "")
    slice_stdout = str(sliced.get("stdout") or "")
    large_stdout = str(large.get("stdout") or "")
    head_ok = (
        head.get("status") == "ok"
        and "AIDEBUG_PRECISION_L001" in head_stdout
        and "AIDEBUG_PRECISION_L005" in head_stdout
        and "AIDEBUG_PRECISION_L006" not in head_stdout
    )
    tail_ok = (
        tail.get("status") == "ok"
        and "AIDEBUG_PRECISION_L036" in tail_stdout
        and "AIDEBUG_PRECISION_L040" in tail_stdout
        and "AIDEBUG_PRECISION_L035" not in tail_stdout
    )
    slice_window_ok = (
        sliced.get("status") == "ok"
        and "AIDEBUG_PRECISION_L017" in slice_stdout
        and "AIDEBUG_PRECISION_L023" in slice_stdout
        and "AIDEBUG_PRECISION_L016" not in slice_stdout
        and "AIDEBUG_PRECISION_L024" not in slice_stdout
    )
    truncation_ok = (
        large.get("status") == "ok"
        and bool(large.get("stdout_truncated"))
        and "AIDEBUG_TRUNC_HEAD" in large_stdout
        and "AIDEBUG_TRUNC_TAIL" in large_stdout
        and "middle omitted for stability" in large_stdout
    )
    terminal_result: dict[str, Any] | None = None
    terminal_ok = True
    terminal_relative = ""
    if terminal_smoke.get("skipped"):
        terminal_result = {
            "status": "skipped",
            "reason": str(terminal_smoke.get("reason") or "terminal_skipped"),
        }
    else:
        log_path = Path(str(terminal_smoke.get("log_path") or ""))
        try:
            terminal_relative = str(log_path.resolve().relative_to(AIDEBUG_DIR.resolve()))
        except ValueError:
            terminal_relative = ""
        info = terminal_smoke.get("info") if isinstance(terminal_smoke.get("info"), dict) else {}
        try:
            total_lines = max(1, int(info.get("log_lines") or 1))
        except (TypeError, ValueError):
            total_lines = 1
        start_line = max(1, total_lines - 12)
        if not terminal_relative:
            terminal_ok = False
            terminal_result = {
                "status": "error",
                "message": "terminal log path is outside aidebug",
            }
        else:
            terminal_result = tool_call(
                registry,
                ctx,
                "aidebug",
                {
                    "action": "read",
                    "path": terminal_relative,
                    "mode": "slice",
                    "start_line": start_line,
                    "end_line": total_lines,
                },
            )
            terminal_ok = (
                terminal_result.get("status") == "ok"
                and f"AIDEBUG_ROUND_{round_id}_SEND" in str(terminal_result.get("stdout") or "")
            )
    return {
        "tool": "aidebug",
        "action": "read-precision",
        "relative_path": precision_relative,
        "large_relative_path": large_relative,
        "terminal_relative_path": terminal_relative,
        "head_ok": head_ok,
        "tail_ok": tail_ok,
        "slice_window_ok": slice_window_ok,
        "truncation_ok": truncation_ok,
        "terminal_ok": terminal_ok,
        "precision_ok": bool(head_ok and tail_ok and slice_window_ok and truncation_ok),
        "head": head,
        "tail": tail,
        "slice": sliced,
        "large": large,
        "terminal": terminal_result,
        "ok": bool(head_ok and tail_ok and slice_window_ok and truncation_ok and terminal_ok),
    }


def smoke_aidebug_security(registry: ToolRegistry, ctx: ToolContext) -> dict[str, Any]:
    result = tool_call(
        registry,
        ctx,
        "aidebug",
        {
            "action": "read",
            "path": "../projectling/config/env",
            "mode": "tail",
            "lines": 5,
        },
    )
    return {
        "tool": "aidebug",
        "kind": "security",
        "result": result,
        "ok": result.get("status") == "blocked",
    }


def smoke_context_compact(ctx: ToolContext) -> dict[str, Any]:
    compact_registry = ToolRegistry(ctx.config, include_command=False, include_compact=True)
    persona_path = STATE_DIR / "compact-context-smoke.txt"
    compact_ctx = ToolContext(cwd=ctx.cwd, home=ctx.home, config=ctx.config, persona_path=persona_path)
    summary = "projectling aidebug compact smoke\n" + ("保留：工具、路径、错误码、下一步。\n" * 2600)
    result = tool_call(
        compact_registry,
        compact_ctx,
        "compact_context",
        {
            "summary": summary,
            "preserved_details": "smoke-test=1 path=aidebug/state/projectling-auto",
        },
    )
    text = persona_path.read_text(encoding="utf-8", errors="replace") if persona_path.is_file() else ""
    target = int(getattr(ctx.config, "advisorling_compact_target_chars", 48000) or 48000)
    return {
        "tool": "compact_context",
        "result": result,
        "path": str(persona_path),
        "chars": len(text),
        "target": target,
        "ok": result.get("status") == "ok" and persona_path.is_file() and len(text) <= max(1000, target) + 1,
    }


def smoke_context_pressure(registry: ToolRegistry, ctx: ToolContext, round_id: int) -> dict[str, Any]:
    context_dir = STATE_DIR / "context-pressure"
    context_dir.mkdir(parents=True, exist_ok=True)
    pressure_config = replace(
        ctx.config,
        context_dir=context_dir,
        context_entries_path=context_dir / "entries.jsonl",
        shared_context_path=context_dir / "shared_context.txt",
        runtime_dir=context_dir / "runtime",
        context_max_chars=8000,
        context_compact_target_chars=4000,
        advisorling_context_max_chars=8000,
        advisorling_compact_target_chars=4000,
    )
    pressure_ctx = ToolContext(
        cwd=ctx.cwd,
        home=ctx.home,
        config=pressure_config,
        persona_path=context_dir / "persona.txt",
    )
    clear_context_entries(pressure_config)
    for index in range(1, 17):
        kind = "tool" if index % 2 == 0 else "assistant"
        append_context_entry(
            pressure_config,
            kind=kind,
            speaker="aidebug-context-pressure",
            scope="tool_trace" if kind == "tool" else "shared",
            content=(
                f"context pressure round={round_id} entry={index} kind={kind} "
                f"path=aidebug/state/projectling-auto/context-pressure decision=preserve "
                + ("tool-output-line " * 80)
            ),
            meta={"round": round_id, "pressure_index": index},
        )
    status_before = tool_call(
        registry,
        pressure_ctx,
        "contextmanage",
        {"mode": "status", "brief": "context pressure status before"},
    )
    listed = tool_call(
        registry,
        pressure_ctx,
        "contextmanage",
        {"mode": "list", "limit": 6, "brief": "context pressure list"},
    )
    replaced = tool_call(
        registry,
        pressure_ctx,
        "contextmanage",
        {
            "mode": "replace",
            "id_range": "E000001~E000004",
            "summary": "context pressure summary preserves paths, decisions, errors, and next actions.",
            "brief": "context pressure replace",
        },
    )
    folded = tool_call(
        registry,
        pressure_ctx,
        "contextmanage",
        {"mode": "fold", "keep_last": 2, "brief": "context pressure fold tools"},
    )
    budget = tool_call(
        registry,
        pressure_ctx,
        "context",
        {
            "percent": 42,
            "turns": 2,
            "brief": "context pressure budget",
            "reason": "aidebug pressure smoke",
        },
    )
    status_after = tool_call(
        registry,
        pressure_ctx,
        "contextmanage",
        {"mode": "status", "brief": "context pressure status after"},
    )
    entries_before = int(status_before.get("entries_total") or 0)
    hidden_after = int(status_after.get("entries_hidden") or 0)
    replace_ok = replaced.get("status") == "ok" and bool(replaced.get("summary_id"))
    fold_ok = folded.get("status") == "ok" and int(folded.get("folded") or 0) >= 1
    budget_ok = budget.get("status") == "ok" and int(budget.get("context_budget_percent") or 0) == 42
    freshness = context_freshness_metrics(
        pressure_config,
        status_after=status_after,
        summary_id=str(replaced.get("summary_id") or ""),
    )
    return {
        "tool": "context_pressure",
        "context_dir": str(context_dir),
        "status_ok": status_before.get("status") == "ok" and status_after.get("status") == "ok",
        "list_ok": listed.get("status") == "ok" and len(listed.get("entries") or []) > 0,
        "replace_ok": replace_ok,
        "fold_ok": fold_ok,
        "budget_ok": budget_ok,
        "freshness_ok": freshness.get("ok"),
        "entries_before": entries_before,
        "entries_after": status_after.get("entries_total"),
        "entries_file_total": freshness.get("entries_file_total"),
        "visible_file_total": freshness.get("visible_file_total"),
        "hidden_after": hidden_after,
        "hidden_file_total": freshness.get("hidden_file_total"),
        "active_chars": freshness.get("active_chars"),
        "compact_target": freshness.get("compact_target"),
        "context_max": freshness.get("context_max"),
        "summary_visible": freshness.get("summary_visible"),
        "ledger_matches_status": freshness.get("ledger_matches_status"),
        "folded": folded.get("folded"),
        "summary_id": replaced.get("summary_id"),
        "ok": bool(
            entries_before >= 16
            and hidden_after >= 4
            and replace_ok
            and fold_ok
            and budget_ok
            and freshness.get("ok")
        ),
    }


def smoke_context_pressure_variants(registry: ToolRegistry, ctx: ToolContext, round_id: int) -> dict[str, Any]:
    variants = [
        {"label": "small", "max_chars": 5000, "target": 2200, "entries": 8, "keep_last": 1, "budget": 24},
        {"label": "medium", "max_chars": 10000, "target": 4500, "entries": 14, "keep_last": 2, "budget": 48},
        {"label": "near_limit", "max_chars": 18000, "target": 9000, "entries": 22, "keep_last": 3, "budget": 72},
    ]
    results: list[dict[str, Any]] = []
    variants_dir = STATE_DIR / "context-pressure-variants"
    variants_dir.mkdir(parents=True, exist_ok=True)
    for variant in variants:
        label = str(variant["label"])
        context_dir = variants_dir / label
        context_dir.mkdir(parents=True, exist_ok=True)
        max_chars = int(variant["max_chars"])
        target = int(variant["target"])
        variant_config = replace(
            ctx.config,
            context_dir=context_dir,
            context_entries_path=context_dir / "entries.jsonl",
            shared_context_path=context_dir / "shared_context.txt",
            runtime_dir=context_dir / "runtime",
            context_max_chars=max_chars,
            context_compact_target_chars=target,
            advisorling_context_max_chars=max_chars,
            advisorling_compact_target_chars=target,
        )
        variant_ctx = ToolContext(
            cwd=ctx.cwd,
            home=ctx.home,
            config=variant_config,
            persona_path=context_dir / "persona.txt",
        )
        clear_context_entries(variant_config)
        entry_count = int(variant["entries"])
        for index in range(1, entry_count + 1):
            kind = "tool" if index % 2 == 0 else "assistant"
            append_context_entry(
                variant_config,
                kind=kind,
                speaker=f"aidebug-context-{label}",
                scope="tool_trace" if kind == "tool" else "shared",
                content=(
                    f"context variant={label} round={round_id} entry={index} "
                    f"target={target} max={max_chars} decision=preserve "
                    + ("variant-payload " * (20 + index))
                ),
                meta={"round": round_id, "variant": label, "pressure_index": index},
            )
        status_before = tool_call(
            registry,
            variant_ctx,
            "contextmanage",
            {"mode": "status", "brief": f"context variant {label} status before"},
        )
        replaced = tool_call(
            registry,
            variant_ctx,
            "contextmanage",
            {
                "mode": "replace",
                "id_range": "E000001~E000002",
                "summary": f"context variant {label} summary preserves target, paths, and next actions.",
                "brief": f"context variant {label} replace",
            },
        )
        folded = tool_call(
            registry,
            variant_ctx,
            "contextmanage",
            {
                "mode": "fold",
                "keep_last": int(variant["keep_last"]),
                "brief": f"context variant {label} fold",
            },
        )
        budget = tool_call(
            registry,
            variant_ctx,
            "context",
            {
                "percent": int(variant["budget"]),
                "turns": 2,
                "brief": f"context variant {label} budget",
                "reason": "aidebug variant pressure smoke",
            },
        )
        status_after = tool_call(
            registry,
            variant_ctx,
            "contextmanage",
            {"mode": "status", "brief": f"context variant {label} status after"},
        )
        freshness = context_freshness_metrics(
            variant_config,
            status_after=status_after,
            summary_id=str(replaced.get("summary_id") or ""),
        )
        try:
            entries_before = int(status_before.get("entries_total") or 0)
            hidden_after = int(status_after.get("entries_hidden") or 0)
        except (TypeError, ValueError):
            entries_before = 0
            hidden_after = 0
        replace_ok = replaced.get("status") == "ok" and bool(replaced.get("summary_id"))
        fold_ok = folded.get("status") == "ok" and int(folded.get("folded") or 0) >= 1
        budget_ok = budget.get("status") == "ok" and int(budget.get("context_budget_percent") or 0) == int(variant["budget"])
        result = {
            "label": label,
            "max_chars": max_chars,
            "target": target,
            "entries_before": entries_before,
            "hidden_after": hidden_after,
            "folded": folded.get("folded"),
            "active_chars": freshness.get("active_chars"),
            "freshness_ok": freshness.get("ok"),
            "replace_ok": replace_ok,
            "fold_ok": fold_ok,
            "budget_ok": budget_ok,
            "ok": bool(
                entries_before == entry_count
                and hidden_after >= 2
                and replace_ok
                and fold_ok
                and budget_ok
                and freshness.get("ok")
            ),
        }
        results.append(result)
    passed = sum(1 for item in results if item.get("ok"))
    return {
        "tool": "context_pressure_variants",
        "variant_count": len(results),
        "passed": passed,
        "labels": [item.get("label") for item in results],
        "variants": results,
        "ok": passed == len(results),
    }


def smoke_memory_tools(registry: ToolRegistry, ctx: ToolContext, round_id: int) -> dict[str, Any]:
    memory_dir = STATE_DIR / "memory-smoke"
    memory_dir.mkdir(parents=True, exist_ok=True)
    smoke_config = replace(
        ctx.config,
        memory_dir=memory_dir,
        datememory_path=memory_dir / "datememory.json",
        memory_db_path=memory_dir / "memory.db",
        memory_max_bytes=4096,
    )
    memory_ctx = ToolContext(
        cwd=ctx.cwd,
        home=ctx.home,
        config=smoke_config,
        persona_path=STATE_DIR / "memory-smoke-persona.txt",
    )
    date = "2099-01-01"
    keywords = [
        "projectling",
        "aidebug",
        "deepseek",
        "cache",
        "permanent-memory",
        f"round-{round_id}",
    ]
    diary = (
        "Aidebug isolated memory smoke verifies permanent memory write, keyword retrieval, "
        "date readback, alias compatibility, and validation rejection without touching user memory."
    )
    append_diary = (
        "Append-mode replay adds a second same-date permanent memory segment, verifies merged diary "
        "text, keeps keyword uniqueness, and records a second memory event."
    )
    append_keywords = [
        "projectling",
        "aidebug",
        "append-mode",
        "keyword-unique",
        "sqlite-integrity",
        f"round-{round_id}",
    ]
    status_before = tool_call(
        registry,
        memory_ctx,
        "memory_status",
        {"action": "status", "brief": "memory smoke status before"},
    )
    add = tool_call(
        registry,
        memory_ctx,
        "memory_add",
        {
            "date": date,
            "diary": diary,
            "keywords": keywords,
            "mode": "replace",
            "consume_source": False,
            "brief": "memory smoke write",
        },
    )
    append = tool_call(
        registry,
        memory_ctx,
        "memory_add",
        {
            "date": date,
            "diary": append_diary,
            "keywords": append_keywords,
            "mode": "append",
            "consume_source": False,
            "brief": "memory smoke append",
        },
    )
    check = tool_call(
        registry,
        memory_ctx,
        "memory_check",
        {"keywords": keywords[:5], "limit": 3, "brief": "memory smoke check"},
    )
    alias = tool_call(
        registry,
        memory_ctx,
        "memorycheak",
        {"keywords": keywords[:5], "limit": 1, "brief": "memory smoke alias check"},
    )
    read = tool_call(
        registry,
        memory_ctx,
        "memory_read",
        {"dates": [date], "brief": "memory smoke read"},
    )
    reject = tool_call(
        registry,
        memory_ctx,
        "memory_check",
        {"keywords": ["too", "few"], "limit": 1, "brief": "memory smoke reject"},
    )
    status_after = tool_call(
        registry,
        memory_ctx,
        "memory_status",
        {"action": "status", "brief": "memory smoke status after"},
    )
    db_path = memory_dir / "memory.db"
    journal_mode = ""
    db_diary_count = -1
    db_event_count = -1
    try:
        with sqlite3.connect(str(db_path), timeout=10) as db:
            journal_mode = str(db.execute("PRAGMA journal_mode").fetchone()[0] or "").lower()
            db_diary_count = int(db.execute("SELECT COUNT(*) FROM diaries").fetchone()[0] or 0)
            db_event_count = int(db.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0] or 0)
    except Exception as exc:
        journal_mode = f"error:{exc}"
    status_ok = status_before.get("status") == "ok" and status_after.get("status") == "ok"
    add_ok = add.get("status") == "ok" and add.get("date") == date and int(add.get("keyword_count") or 0) >= 5
    append_ok = append.get("status") == "ok" and append.get("date") == date and append.get("mode") == "append"
    check_ok = check.get("status") == "ok" and int(check.get("result_count") or 0) >= 1
    alias_ok = alias.get("status") == "ok" and int(alias.get("result_count") or 0) >= 1
    read_entries = read.get("entries") if isinstance(read.get("entries"), list) else []
    read_entry = read_entries[0] if read_entries and isinstance(read_entries[0], dict) else {}
    read_diary = str(read_entry.get("diary") or "")
    read_keywords = read_entry.get("keywords") if isinstance(read_entry.get("keywords"), list) else []
    keyword_unique_ok = len(read_keywords) == len(set(str(item).lower() for item in read_keywords))
    read_ok = (
        read.get("status") == "ok"
        and int(read.get("found") or 0) == 1
        and "Append-mode replay" in read_diary
        and "isolated memory smoke" in read_diary
    )
    reject_ok = reject.get("status") == "error" and "keywords" in str(reject.get("message") or "").lower()
    db_integrity_ok = (
        journal_mode == "wal"
        and db_diary_count >= 1
        and db_event_count >= int(status_after.get("memory_db_events") or 0) - 1
    )
    return {
        "tool": "memory",
        "memory_dir": str(memory_dir),
        "db_path": str(db_path),
        "status_ok": status_ok,
        "add_ok": add_ok,
        "append_ok": append_ok,
        "check_ok": check_ok,
        "alias_ok": alias_ok,
        "read_ok": read_ok,
        "reject_ok": reject_ok,
        "keyword_unique_ok": keyword_unique_ok,
        "db_integrity_ok": db_integrity_ok,
        "journal_mode": journal_mode,
        "db_diary_count": db_diary_count,
        "db_event_count": db_event_count,
        "diaries": status_after.get("memory_db_diaries"),
        "events": status_after.get("memory_db_events"),
        "datememory_bytes": status_after.get("datememory_bytes"),
        "ok": bool(
            status_ok
            and add_ok
            and append_ok
            and check_ok
            and alias_ok
            and read_ok
            and reject_ok
            and keyword_unique_ok
            and db_integrity_ok
        ),
    }


def smoke_memory_pressure(registry: ToolRegistry, ctx: ToolContext, round_id: int) -> dict[str, Any]:
    memory_dir = STATE_DIR / "memory-pressure"
    memory_dir.mkdir(parents=True, exist_ok=True)
    pressure_config = replace(
        ctx.config,
        memory_dir=memory_dir,
        datememory_path=memory_dir / "datememory.json",
        memory_db_path=memory_dir / "memory.db",
        memory_max_bytes=900,
    )
    pressure_ctx = ToolContext(
        cwd=ctx.cwd,
        home=ctx.home,
        config=pressure_config,
        persona_path=STATE_DIR / "memory-pressure-persona.txt",
    )
    clear_before = tool_call(
        registry,
        pressure_ctx,
        "memory_status",
        {"action": "clear_datememory", "brief": "memory pressure clear before"},
    )
    text = (
        f"ProjectLing aidebug memory pressure round={round_id} verifies datememory threshold, "
        "DeepSeek cache continuity, permanent memory consume_source cleanup, and isolated test state. "
    ) * 12
    append_result = append_chat_turns(
        pressure_config,
        persona="aidebug-memory-pressure",
        turns=[
            ("user", text),
            ("assistant", text + "Next action: summarize into permanent memory and clear datememory source."),
        ],
    )
    status_before = tool_call(
        registry,
        pressure_ctx,
        "memory_status",
        {"action": "status", "brief": "memory pressure status before"},
    )
    pressure_message = memory_pressure_message(pressure_config)
    pressure_content = str((pressure_message or {}).get("content") or "")
    date = "2099-01-02"
    keywords = [
        "projectling",
        "aidebug",
        "datememory",
        "pressure",
        "consume-source",
        f"round-{round_id}",
    ]
    add = tool_call(
        registry,
        pressure_ctx,
        "memory_add",
        {
            "date": date,
            "diary": (
                "Aidebug pressure smoke observed datememory crossing the configured threshold, "
                "then stored a permanent-memory summary and consumed the short-term source."
            ),
            "keywords": keywords,
            "mode": "replace",
            "consume_source": True,
            "brief": "memory pressure consume source",
        },
    )
    read = tool_call(
        registry,
        pressure_ctx,
        "memory_read",
        {"dates": [date], "brief": "memory pressure readback"},
    )
    status_after = tool_call(
        registry,
        pressure_ctx,
        "memory_status",
        {"action": "status", "brief": "memory pressure status after"},
    )
    bytes_before = int(status_before.get("datememory_bytes") or 0)
    bytes_after = int(status_after.get("datememory_bytes") or 0)
    max_bytes = int(status_before.get("memory_max_bytes") or 0)
    append_ok = append_result.get("turns_added") == 2 and int(append_result.get("bytes") or 0) >= max_bytes
    pressure_ok = bool(
        pressure_message
        and bytes_before >= max_bytes
        and "datememory.json" in pressure_content
        and "memory_add" in pressure_content
    )
    consume_ok = bool(
        add.get("status") == "ok"
        and add.get("consume_source") is True
        and add.get("source_cleared") is True
        and int(status_after.get("datememory_days") or 0) == 0
        and bytes_after < bytes_before
    )
    read_ok = read.get("status") == "ok" and int(read.get("found") or 0) == 1
    return {
        "tool": "memory_pressure",
        "memory_dir": str(memory_dir),
        "clear_before_ok": clear_before.get("status") == "ok",
        "append_ok": append_ok,
        "pressure_ok": pressure_ok,
        "consume_ok": consume_ok,
        "read_ok": read_ok,
        "turns_added": append_result.get("turns_added"),
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
        "memory_max_bytes": max_bytes,
        "pressure_content_chars": len(pressure_content),
        "diaries": status_after.get("memory_db_diaries"),
        "events": status_after.get("memory_db_events"),
        "ok": bool(
            clear_before.get("status") == "ok"
            and append_ok
            and pressure_ok
            and consume_ok
            and read_ok
        ),
    }


def smoke_live_chat_tool_call(ctx: ToolContext) -> dict[str, Any]:
    source_engine = ProjectLingEngine(ctx.config, registry=ToolRegistry(ctx.config))
    role, role_seed, persona_bundle = source_engine.persona_for_dispatch_mode("chat")
    sandbox_root = STATE_DIR / "live-smoke-sandbox" / f"{os.getpid()}-{int(time.time() * 1000)}"
    sandbox_config_dir = sandbox_root / "config"
    sandbox_context_dir = sandbox_root / "context"
    sandbox_memory_dir = sandbox_root / "memory"
    sandbox_config_dir.mkdir(parents=True, exist_ok=True)
    sandbox_context_dir.mkdir(parents=True, exist_ok=True)
    sandbox_memory_dir.mkdir(parents=True, exist_ok=True)
    sandbox_env = sandbox_config_dir / "env"
    sandbox_env.write_text("", encoding="utf-8")
    sandbox_config = replace(
        ctx.config,
        config_dir=sandbox_config_dir,
        runtime_dir=sandbox_config_dir,
        env_file_path=sandbox_env,
        context_dir=sandbox_context_dir,
        external_context_path=sandbox_context_dir / "shared_context.txt",
        shared_context_path=sandbox_context_dir / "shared_context.txt",
        context_entries_path=sandbox_context_dir / "entries.jsonl",
        persona_dir=sandbox_context_dir / "persona",
        dualstar_dir=sandbox_context_dir / "dualstar",
        memory_dir=sandbox_memory_dir,
        datememory_path=sandbox_memory_dir / "datememory.json",
        memory_db_path=sandbox_memory_dir / "memory.db",
    )
    registry = ToolRegistry(sandbox_config)
    engine = ProjectLingEngine(sandbox_config, registry=registry)
    provider = str(getattr(sandbox_config, "api_provider", "deepseek") or "deepseek").strip().lower()
    api_calls: list[dict[str, Any]] = []

    def _json_chars(value: Any) -> int:
        try:
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        except Exception:
            return len(str(value or ""))

    def _request_metrics(kwargs: dict[str, Any]) -> dict[str, Any]:
        messages = kwargs.get("messages") if isinstance(kwargs.get("messages"), list) else []
        role_chars: dict[str, int] = {}
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_role = str(message.get("role") or "unknown")
            role_chars[message_role] = role_chars.get(message_role, 0) + _json_chars(message.get("content") or "")
            if message.get("tool_calls"):
                role_chars[message_role] += _json_chars(message.get("tool_calls"))
        tools = kwargs.get("tools") if isinstance(kwargs.get("tools"), list) else []
        tool_names = [
            str((item.get("function") or {}).get("name") or "")
            for item in tools
            if isinstance(item, dict)
        ]
        messages_chars = _json_chars(messages)
        tools_chars = _json_chars(tools)
        max_tokens = kwargs.get("max_tokens")
        if tools:
            call_kind = "executor_tool_round"
        elif max_tokens == 1800:
            call_kind = "planner"
        elif max_tokens == 700:
            call_kind = "planner_review"
        else:
            call_kind = "model_call"
        return {
            "call": len(api_calls) + 1,
            "kind": call_kind,
            "model": str(kwargs.get("model") or ""),
            "thinking_enabled": bool(kwargs.get("thinking_enabled")),
            "message_count": len(messages),
            "message_role_chars": role_chars,
            "messages_json_chars": messages_chars,
            "tool_schema_count": len(tools),
            "tool_schema_names": tool_names,
            "tool_schema_json_chars": tools_chars,
            "request_json_chars": messages_chars + tools_chars,
            "max_tokens": max_tokens,
        }

    class _UsageRecordingClient:
        def __init__(self, delegate: Any) -> None:
            self._delegate = delegate

        def __getattr__(self, name: str) -> Any:
            return getattr(self._delegate, name)

        def chat_completions(self, **kwargs: Any) -> dict[str, Any]:
            record = _request_metrics(kwargs)
            try:
                response = self._delegate.chat_completions(**kwargs)
            except Exception as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
                api_calls.append(record)
                raise
            usage = response.get("usage") if isinstance(response, dict) and isinstance(response.get("usage"), dict) else {}
            cache_summary = deepseek_usage_cache_summary(usage)
            record["usage"] = {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "cached_tokens": cache_summary.get("cache_hit_tokens"),
                "cache_miss_tokens": cache_summary.get("cache_miss_tokens"),
                "cache_hit_rate": cache_summary.get("cache_hit_rate"),
            }
            api_calls.append(record)
            return response

    engine.main_client = _UsageRecordingClient(engine.main_client)
    engine.executor_client = _UsageRecordingClient(engine.executor_client)
    engine.client = engine.main_client
    live_command = (
        "Write-Output PROJECTLING_LIVE_TOOLCALL_SMOKE"
        if os.name == "nt"
        else "printf PROJECTLING_LIVE_TOOLCALL_SMOKE"
    )
    prompt = (
        "这是 AIDEBUG 强制 function-calling smoke，不是普通聊天。"
        "必须按双星协作流程处理这个小任务："
        "第一步调用 update_plan 建立两步计划；"
        f"第二步必须调用 command 工具执行命令 `{live_command}`；"
        "第三步调用 link.action=done 向主星回报。"
        "禁止只调用 link，禁止跳过 command；如果没有 command 工具结果，本轮测试视为失败。"
        "最后只用一句中文总结命令输出。"
    )
    started = time.time()

    def cache_ok_from_usage(usage_payload: dict[str, Any]) -> bool:
        if provider != "deepseek":
            return True
        try:
            miss = int(usage_payload.get("cache_miss_tokens") or 0)
        except (TypeError, ValueError):
            miss = 0
        try:
            hit_rate = float(usage_payload.get("cache_hit_rate") or 0.0)
        except (TypeError, ValueError):
            hit_rate = 0.0
        return miss <= 1000 and hit_rate >= 85.0

    def run_live_attempt(attempt: int) -> dict[str, Any]:
        attempt_started = time.time()
        call_start = len(api_calls)
        result = engine.chat(
            prompt,
            cwd=PROJECTLING_DIR,
            mode="chat",
            allow_tools=True,
            role_override=role,
            role_seed=role_seed,
            persona_bundle_override=persona_bundle,
            post_plan_tool_scope="plan_command",
        )
        attempt_api_calls = [dict(item) for item in api_calls[call_start:]]
        tool_traces = list(result.tool_traces)
        usage = {}
        raw_response = result.raw_response if isinstance(result.raw_response, dict) else {}
        if isinstance(raw_response.get("usage"), dict):
            usage = raw_response["usage"]
        command_stdout = ""
        tool_names: list[str] = []
        tool_actor_labels: list[str] = []
        tool_actor_names: list[str] = []
        tool_actor_kinds: list[str] = []
        command_executor_actor = False
        for trace in tool_traces:
            if not isinstance(trace, dict):
                continue
            trace_name = str(trace.get("name") or "")
            tool_names.append(trace_name)
            tool_result_payload = trace.get("result") if isinstance(trace.get("result"), dict) else {}
            actor_label = str(tool_result_payload.get("actor_label") or "").strip()
            actor_name = str(tool_result_payload.get("actor_name") or "").strip()
            actor_kind = str(tool_result_payload.get("actor_kind") or "").strip()
            if actor_label and actor_label not in tool_actor_labels:
                tool_actor_labels.append(actor_label)
            if actor_name and actor_name not in tool_actor_names:
                tool_actor_names.append(actor_name)
            if actor_kind and actor_kind not in tool_actor_kinds:
                tool_actor_kinds.append(actor_kind)
            if trace_name == "command" and (actor_label == "执行星" or actor_kind == "executor"):
                command_executor_actor = True
            command_stdout += str(tool_result_payload.get("stdout") or "")
        thinking_traces = list(result.thinking_traces)
        thinking_roles: list[str] = []
        thinking_actor_labels: list[str] = []
        planner_review_errors = 0
        for trace in thinking_traces:
            if not isinstance(trace, dict):
                continue
            role_name = str(trace.get("role") or "").strip()
            actor_label = str(trace.get("actor_label") or "").strip()
            trace_text = str(trace.get("text") or "")
            if role_name and role_name not in thinking_roles:
                thinking_roles.append(role_name)
            if actor_label and actor_label not in thinking_actor_labels:
                thinking_actor_labels.append(actor_label)
            if "复审暂不可用" in trace_text or "contents is not specified" in trace_text:
                planner_review_errors += 1
        cache_summary = deepseek_usage_cache_summary(usage)
        normalized_usage = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "cached_tokens": cache_summary.get("cache_hit_tokens"),
            "cache_miss_tokens": cache_summary.get("cache_miss_tokens"),
            "cache_hit_rate": cache_summary.get("cache_hit_rate"),
        }
        cumulative_prompt_tokens = sum(
            int((item.get("usage") or {}).get("prompt_tokens") or 0)
            for item in attempt_api_calls
            if isinstance(item.get("usage"), dict)
        )
        cumulative_completion_tokens = sum(
            int((item.get("usage") or {}).get("completion_tokens") or 0)
            for item in attempt_api_calls
            if isinstance(item.get("usage"), dict)
        )
        cumulative_cached_tokens = sum(
            int((item.get("usage") or {}).get("cached_tokens") or 0)
            for item in attempt_api_calls
            if isinstance(item.get("usage"), dict)
        )
        cumulative_cache_miss_tokens = sum(
            int((item.get("usage") or {}).get("cache_miss_tokens") or 0)
            for item in attempt_api_calls
            if isinstance(item.get("usage"), dict)
        )
        cumulative_usage = {
            "api_calls": len(attempt_api_calls),
            "prompt_tokens": cumulative_prompt_tokens,
            "completion_tokens": cumulative_completion_tokens,
            "total_tokens": cumulative_prompt_tokens + cumulative_completion_tokens,
            "cached_tokens": cumulative_cached_tokens,
            "cache_miss_tokens": cumulative_cache_miss_tokens,
            "cache_hit_rate": round(cumulative_cached_tokens * 100.0 / cumulative_prompt_tokens, 1)
            if cumulative_prompt_tokens > 0
            else 0.0,
            "request_json_chars": sum(int(item.get("request_json_chars") or 0) for item in attempt_api_calls),
            "tool_schema_json_chars": sum(int(item.get("tool_schema_json_chars") or 0) for item in attempt_api_calls),
        }
        functional_ok = (
            bool(result.used_tools)
            and "command" in tool_names
            and "PROJECTLING_LIVE_TOOLCALL_SMOKE" in command_stdout
        )
        cache_ok = cache_ok_from_usage(normalized_usage)
        dual_star_metadata_ok = (
            bool(tool_actor_labels)
            and command_executor_actor
            and "执行星" in tool_actor_labels
            and "主星" in thinking_actor_labels
            and planner_review_errors == 0
        )
        return {
            "tool": "live_chat",
            "provider": provider,
            "main_provider": sandbox_config.main_api.provider,
            "executor_provider": sandbox_config.executor_api.provider,
            "attempt": attempt,
            "returncode": 0,
            "elapsed_seconds": round(time.time() - attempt_started, 3),
            "stdout_chars": len(result.text or ""),
            "stderr_tail": "",
            "used_tools": bool(result.used_tools),
            "rounds": int(result.rounds or 0),
            "tool_names": tool_names,
            "tool_actor_labels": tool_actor_labels,
            "tool_actor_names": tool_actor_names,
            "tool_actor_kinds": tool_actor_kinds,
            "thinking_roles": thinking_roles,
            "thinking_actor_labels": thinking_actor_labels,
            "planner_review_errors": planner_review_errors,
            "command_executor_actor": command_executor_actor,
            "dual_star_metadata_ok": dual_star_metadata_ok,
            "text": str(result.text or "")[:500],
            "routing": result.routing,
            "usage": normalized_usage,
            "request_usage_total": cumulative_usage,
            "request_breakdown": attempt_api_calls,
            "cache_ok": cache_ok,
            "functional_ok": functional_ok,
            "context_restored": True,
            "ok": functional_ok,
        }

    try:
        attempts = [run_live_attempt(1)]
        first = attempts[0]
        warmup = {
            "attempted": False,
            "resolved": False,
            "first_cache_ok": first.get("cache_ok"),
            "first_miss": (first.get("usage") or {}).get("cache_miss_tokens") if isinstance(first.get("usage"), dict) else None,
            "first_hit_rate": (first.get("usage") or {}).get("cache_hit_rate") if isinstance(first.get("usage"), dict) else None,
        }
        if not first.get("functional_ok"):
            attempts.append(run_live_attempt(2))
        if first.get("functional_ok") and not first.get("cache_ok"):
            warmup["attempted"] = True
            attempts.append(run_live_attempt(2))
            second = attempts[-1]
            warmup.update(
                {
                    "second_cache_ok": second.get("cache_ok"),
                    "second_miss": (second.get("usage") or {}).get("cache_miss_tokens") if isinstance(second.get("usage"), dict) else None,
                    "second_hit_rate": (second.get("usage") or {}).get("cache_hit_rate") if isinstance(second.get("usage"), dict) else None,
                    "resolved": bool(second.get("functional_ok") and second.get("cache_ok")),
                }
            )
        selected = attempts[-1] if attempts[-1].get("functional_ok") else first
        payload = dict(selected)
        payload["elapsed_seconds"] = round(time.time() - started, 3)
        payload["attempts"] = len(attempts)
        payload["cache_warmup"] = warmup
        payload["attempt_summaries"] = [
            {
                "attempt": item.get("attempt"),
                "functional_ok": item.get("functional_ok"),
                "cache_ok": item.get("cache_ok"),
                "rounds": item.get("rounds"),
                "tool_names": item.get("tool_names"),
                "usage": item.get("usage"),
                "request_usage_total": item.get("request_usage_total"),
            }
            for item in attempts
        ]
        return payload
    finally:
        shutil.rmtree(sandbox_root, ignore_errors=True)


def collect_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    def add(severity: str, component: str, message: str) -> None:
        findings.append(
            {
                "severity": severity,
                "component": component,
                "message": message,
                "round": payload.get("round"),
                "at": payload.get("started_at"),
            }
        )

    if not payload.get("ok"):
        add("error", "projectling-auto", "round failed; inspect projectling-auto.jsonl for component details")
    ui = payload.get("ui_smoke") if isinstance(payload.get("ui_smoke"), dict) else {}
    if ui.get("touching_lines"):
        add("warning", "ui", f"MOTD rendered text touches boundary: lines={ui.get('touching_lines')}")
    if ui.get("too_wide_lines"):
        add("warning", "ui", f"MOTD rendered lines exceed width budget: lines={ui.get('too_wide_lines')}")

    command = payload.get("command_smoke") if isinstance(payload.get("command_smoke"), dict) else {}
    if command and not command.get("ok"):
        add("error", "command", "large-output receipt did not preserve bounded head/tail output")
    safety = payload.get("command_safety") if isinstance(payload.get("command_safety"), dict) else {}
    if safety and not safety.get("ok"):
        add("error", "command", "command safety matrix failed")
    matrix = payload.get("command_matrix_smoke") if isinstance(payload.get("command_matrix_smoke"), dict) else {}
    if matrix and not matrix.get("ok"):
        add("error", "command", "command behavior matrix failed")

    runner_exception = payload.get("runner_exception") if isinstance(payload.get("runner_exception"), dict) else {}
    if runner_exception:
        add(
            "error",
            "projectling-auto",
            f"runner exception: {runner_exception.get('type')}: {runner_exception.get('message')}",
        )
    runtime_state = payload.get("runtime_state_guard") if isinstance(payload.get("runtime_state_guard"), dict) else {}
    if runtime_state and not runtime_state.get("ok"):
        add(
            "error",
            "runtime_state",
            "protected Provider/model/key/role/focus/context/memory state changed or could not be verified",
        )

    for key, component in (
        ("patch_smoke", "apply_patch"),
        ("patch_security", "apply_patch"),
        ("terminal_smoke", "terminal"),
        ("aidebug_slice_smoke", "aidebug"),
        ("aidebug_security", "aidebug"),
        ("compact_smoke", "compact_context"),
        ("context_pressure_smoke", "context_pressure"),
        ("context_pressure_variants_smoke", "context_pressure_variants"),
        ("memory_smoke", "memory"),
        ("memory_pressure_smoke", "memory_pressure"),
        ("web_validation", "web_search"),
    ):
        item = payload.get(key) if isinstance(payload.get(key), dict) else {}
        if item and not item.get("ok"):
            add("error", component, f"{key} failed")

    live = payload.get("live_chat_smoke") if isinstance(payload.get("live_chat_smoke"), dict) else None
    if live is not None:
        provider = str(live.get("provider") or "").strip().lower()
        if not live.get("ok"):
            provider_label = provider.capitalize() if provider else "Provider"
            add("error", "live_chat", f"{provider_label} live function-calling smoke failed")
        elif not live.get("dual_star_metadata_ok"):
            add("error", "live_chat", "live dual-star actor metadata is incomplete")
        usage = live.get("usage") if isinstance(live.get("usage"), dict) else {}
        request_usage_total = live.get("request_usage_total") if isinstance(live.get("request_usage_total"), dict) else {}
        prompt_tokens = usage.get("prompt_tokens")
        cache_miss_tokens = usage.get("cache_miss_tokens")
        cache_hit_rate = usage.get("cache_hit_rate")
        if (
            (not provider or provider == "deepseek")
            and
            isinstance(prompt_tokens, int)
            and prompt_tokens > 6000
            and (
                not isinstance(cache_miss_tokens, int)
                or cache_miss_tokens > 2500
                or not isinstance(cache_hit_rate, (int, float))
                or float(cache_hit_rate) < 70.0
            )
        ):
            add(
                "warning",
                "live_chat",
                f"prompt token pressure is high: prompt={prompt_tokens} miss={cache_miss_tokens} hit_rate={cache_hit_rate}",
            )
        cumulative_prompt_tokens = request_usage_total.get("prompt_tokens")
        if isinstance(cumulative_prompt_tokens, int) and cumulative_prompt_tokens > 50000:
            add(
                "warning",
                "live_chat_cost",
                f"cumulative live smoke prompt cost exceeds target: prompt={cumulative_prompt_tokens} api_calls={request_usage_total.get('api_calls')}",
            )

    return findings


def write_round_notes(payload: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    command = payload.get("command_smoke") if isinstance(payload.get("command_smoke"), dict) else {}
    command_matrix = payload.get("command_matrix_smoke") if isinstance(payload.get("command_matrix_smoke"), dict) else {}
    terminal = payload.get("terminal_smoke") if isinstance(payload.get("terminal_smoke"), dict) else {}
    schema = payload.get("schema_check") if isinstance(payload.get("schema_check"), dict) else {}
    aidebug_slice = payload.get("aidebug_slice_smoke") if isinstance(payload.get("aidebug_slice_smoke"), dict) else {}
    context_pressure = payload.get("context_pressure_smoke") if isinstance(payload.get("context_pressure_smoke"), dict) else {}
    context_variants = payload.get("context_pressure_variants_smoke") if isinstance(payload.get("context_pressure_variants_smoke"), dict) else {}
    memory = payload.get("memory_smoke") if isinstance(payload.get("memory_smoke"), dict) else {}
    memory_pressure = payload.get("memory_pressure_smoke") if isinstance(payload.get("memory_pressure_smoke"), dict) else {}
    web = payload.get("web_smoke") if isinstance(payload.get("web_smoke"), dict) else None
    web_result = web.get("result") if isinstance(web, dict) and isinstance(web.get("result"), dict) else {}
    live = payload.get("live_chat_smoke") if isinstance(payload.get("live_chat_smoke"), dict) else None
    runtime_state = payload.get("runtime_state_guard") if isinstance(payload.get("runtime_state_guard"), dict) else {}
    resolutions = payload.get("resolutions") if isinstance(payload.get("resolutions"), list) else []
    lines = [
        f"## {payload.get('started_at')} round={payload.get('round')} mode={payload.get('run_mode')} ok={int(bool(payload.get('ok')))}",
        "",
        f"- elapsed={payload.get('elapsed_seconds')}s",
        f"- detail={payload.get('detail_path')}",
        f"- tools={','.join(schema.get('names', []))}",
        f"- command_receipt_chars={command.get('stdout_chars')} head={command.get('has_head')} tail={command.get('has_tail')}",
        f"- command_matrix=ok={command_matrix.get('ok')} cases={command_matrix.get('case_count')} details={','.join(str(item.get('label')) + ':' + str(item.get('ok')) for item in (command_matrix.get('cases') if isinstance(command_matrix.get('cases'), list) else []) if isinstance(item, dict))}",
        f"- terminal_log={terminal.get('log_path')} start={terminal.get('log_has_start')} send={terminal.get('log_has_send')}",
        f"- aidebug_precision=ok={aidebug_slice.get('ok')} head={aidebug_slice.get('head_ok')} tail={aidebug_slice.get('tail_ok')} slice={aidebug_slice.get('slice_window_ok')} trunc={aidebug_slice.get('truncation_ok')} terminal={aidebug_slice.get('terminal_ok')}",
        f"- context_pressure=ok={context_pressure.get('ok')} hidden={context_pressure.get('hidden_after')} folded={context_pressure.get('folded')} budget={context_pressure.get('budget_ok')} fresh={context_pressure.get('freshness_ok')} active_chars={context_pressure.get('active_chars')} target={context_pressure.get('compact_target')}",
        f"- context_variants=ok={context_variants.get('ok')} passed={context_variants.get('passed')}/{context_variants.get('variant_count')} labels={context_variants.get('labels')}",
        f"- memory=ok={memory.get('ok')} diaries={memory.get('diaries')} events={memory.get('events')} reject={memory.get('reject_ok')} append={memory.get('append_ok')} db={memory.get('db_integrity_ok')} journal={memory.get('journal_mode')}",
        f"- memory_pressure=ok={memory_pressure.get('ok')} bytes={memory_pressure.get('bytes_before')}/{memory_pressure.get('memory_max_bytes')} after={memory_pressure.get('bytes_after')} pressure={memory_pressure.get('pressure_ok')} consume={memory_pressure.get('consume_ok')} read={memory_pressure.get('read_ok')}",
        f"- runtime_state=ok={runtime_state.get('ok')} watched={runtime_state.get('watched_files')} forbidden={runtime_state.get('forbidden_changes')} semantic={sorted((runtime_state.get('semantic_changes') or {}).keys()) if isinstance(runtime_state.get('semantic_changes'), dict) else []} secret_presence_changed={runtime_state.get('secret_presence_changed')} capture_errors={runtime_state.get('capture_errors')}",
    ]
    if web is not None:
        lines.append(f"- web_search=ok={web.get('ok')} results={web_result.get('result_count')}")
    if live is not None:
        usage = live.get("usage") if isinstance(live.get("usage"), dict) else {}
        request_usage_total = live.get("request_usage_total") if isinstance(live.get("request_usage_total"), dict) else {}
        warmup = live.get("cache_warmup") if isinstance(live.get("cache_warmup"), dict) else {}
        lines.append(
            "- live_chat="
            f"ok={live.get('ok')} rounds={live.get('rounds')} tools={live.get('tool_names')} "
            f"prompt_tokens={usage.get('prompt_tokens')} cached={usage.get('cached_tokens')} "
            f"miss={usage.get('cache_miss_tokens')} hit_rate={usage.get('cache_hit_rate')} "
            f"cumulative_prompt={request_usage_total.get('prompt_tokens')} api_calls={request_usage_total.get('api_calls')} "
            f"request_chars={request_usage_total.get('request_json_chars')} schema_chars={request_usage_total.get('tool_schema_json_chars')} "
            f"attempts={live.get('attempts')} cache_ok={live.get('cache_ok')} "
            f"warmup_attempted={warmup.get('attempted')} warmup_resolved={warmup.get('resolved')} "
            f"context_restored={live.get('context_restored')} "
            f"dual_star_metadata_ok={live.get('dual_star_metadata_ok')} "
            f"actors={live.get('tool_actor_labels')} thinking={live.get('thinking_actor_labels')} "
            f"review_errors={live.get('planner_review_errors')}"
        )
    if findings:
        lines.append("- findings:")
        for finding in findings:
            lines.append(f"  - [{finding['severity']}] {finding['component']}: {finding['message']}")
    else:
        lines.append("- findings: none")
    if resolutions:
        lines.append("- resolutions:")
        for resolution in resolutions[:8]:
            lines.append(
                "  - "
                f"[{resolution.get('status')}] {resolution.get('issue_component')}: "
                f"{resolution.get('issue_message')} -> {resolution.get('evidence')}"
            )
        if len(resolutions) > 8:
            lines.append(f"  - ... {len(resolutions) - 8} more")
    else:
        lines.append("- resolutions: none")
    lines.append("")
    with NOTE_PATH.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def shlex_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def projectling_cli(command: str, *extra: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AITERMUX_HOME"] = str(AITERMUX_HOME)
    env["AITERMUX_AIDEBUG_DIR"] = str(AIDEBUG_DIR)
    env["PROJECTLING_DIR"] = str(PROJECTLING_DIR)
    if os.name == "nt":
        return subprocess.run(
            [sys.executable, str(PROJECTLING_DIR / "core.py"), command, *extra],
            cwd=str(PROJECTLING_DIR),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )
    return subprocess.run(
        [str(PROJECTLING_RUN), command, *extra],
        cwd=str(PROJECTLING_DIR),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
    )


def verify_tool_schema(stdout: str) -> dict[str, Any]:
    names = []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "show-tools output is not valid json"}
    for item in data:
        fn = item.get("function") or {}
        if isinstance(fn, dict):
            name = str(fn.get("name") or "")
            if name:
                names.append(name)
    required = {"command", "terminal", "aidebug", "apply_patch", "web_search"}
    missing = sorted(required - set(names))
    return {"ok": not missing, "names": names, "missing": missing}


def run_round(
    registry: ToolRegistry,
    ctx: ToolContext,
    round_id: int,
    *,
    web_query: str = "",
    live_chat_smoke: bool = False,
    local_stress: bool = False,
    profile: str = "custom",
) -> dict[str, Any]:
    started = time.time()
    if local_stress:
        run_mode = "local_stress"
    elif live_chat_smoke and web_query:
        run_mode = "live_web"
    elif live_chat_smoke:
        run_mode = "live"
    elif web_query:
        run_mode = "web"
    else:
        run_mode = "local"
    result: dict[str, Any] = {
        "round": round_id,
        "started_at": timestamp(),
        "run_mode": run_mode,
        "profile": profile,
    }
    state_label = f"projectling-auto-round-{round_id}-{profile}"
    state_before, state_before_error = _capture_runtime_state(f"{state_label}-before")
    previous_runtime_read_only = os.environ.get("PROJECTLING_RUNTIME_STATE_READ_ONLY")
    os.environ["PROJECTLING_RUNTIME_STATE_READ_ONLY"] = "1"
    try:
        killed_sessions = cleanup_stale_auto_sessions()
        doctor = projectling_cli("doctor", timeout=40)
        ui_smoke = smoke_projectling_ui()
        tools = projectling_cli("show-tools", "--json", timeout=40)
        schema_check = verify_tool_schema(tools.stdout)
        aidebug_status = tool_call(registry, ctx, "aidebug", {"action": "status"})
        aidebug_tail = tool_call(
            registry,
            ctx,
            "aidebug",
            {"action": "read", "path": "logs/projectling.log", "mode": "tail", "lines": 20},
        )
        command_smoke = smoke_command_receipt(registry, ctx)
        command_safety = smoke_command_safety(registry, ctx)
        command_matrix_smoke = smoke_command_matrix(registry, ctx)
        patch_smoke = smoke_apply_patch(registry, ctx)
        patch_security = smoke_apply_patch_security(registry, ctx)
        terminal_smoke = smoke_terminal(registry, ctx, round_id)
        aidebug_slice_smoke = smoke_aidebug_read_precision(registry, ctx, terminal_smoke, round_id)
        aidebug_security = smoke_aidebug_security(registry, ctx)
        compact_smoke = smoke_context_compact(ctx)
        context_pressure_smoke = smoke_context_pressure(registry, ctx, round_id)
        context_pressure_variants_smoke = smoke_context_pressure_variants(registry, ctx, round_id)
        memory_smoke = smoke_memory_tools(registry, ctx, round_id)
        memory_pressure_smoke = smoke_memory_pressure(registry, ctx, round_id)
        web_validation = smoke_web_search_validation(registry, ctx)
        web_smoke = smoke_web_search(registry, ctx, web_query) if web_query else None
        live_smoke = smoke_live_chat_tool_call(ctx) if live_chat_smoke else None

        result.update(
            {
                "killed_sessions": killed_sessions,
                "doctor_rc": doctor.returncode,
                "doctor_stdout": doctor.stdout[-2000:],
                "doctor_stderr": doctor.stderr[-1000:],
                "ui_smoke": ui_smoke,
                "tools_rc": tools.returncode,
                "schema_check": schema_check,
                "aidebug_status": aidebug_status,
                "aidebug_tail": aidebug_tail,
                "command_smoke": command_smoke,
                "command_safety": command_safety,
                "command_matrix_smoke": command_matrix_smoke,
                "patch_smoke": patch_smoke,
                "patch_security": patch_security,
                "terminal_smoke": terminal_smoke,
                "aidebug_slice_smoke": aidebug_slice_smoke,
                "aidebug_security": aidebug_security,
                "compact_smoke": compact_smoke,
                "context_pressure_smoke": context_pressure_smoke,
                "context_pressure_variants_smoke": context_pressure_variants_smoke,
                "memory_smoke": memory_smoke,
                "memory_pressure_smoke": memory_pressure_smoke,
                "web_validation": web_validation,
                "web_smoke": web_smoke,
                "live_chat_smoke": live_smoke,
            }
        )
    except Exception as exc:
        result["runner_exception"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        if previous_runtime_read_only is None:
            os.environ.pop("PROJECTLING_RUNTIME_STATE_READ_ONLY", None)
        else:
            os.environ["PROJECTLING_RUNTIME_STATE_READ_ONLY"] = previous_runtime_read_only
        state_after, state_after_error = _capture_runtime_state(f"{state_label}-after")
        result["runtime_state_guard"] = _runtime_state_result(
            state_before,
            state_after,
            before_error=state_before_error,
            after_error=state_after_error,
        )
        result["elapsed_seconds"] = round(time.time() - started, 3)

    ui_smoke = result.get("ui_smoke") if isinstance(result.get("ui_smoke"), dict) else {}
    schema_check = result.get("schema_check") if isinstance(result.get("schema_check"), dict) else {}
    aidebug_status = result.get("aidebug_status") if isinstance(result.get("aidebug_status"), dict) else {}
    web_smoke = result.get("web_smoke") if isinstance(result.get("web_smoke"), dict) else None
    live_smoke = result.get("live_chat_smoke") if isinstance(result.get("live_chat_smoke"), dict) else None
    result["ok"] = (
        not result.get("runner_exception")
        and result.get("doctor_rc") == 0
        and result.get("tools_rc") == 0
        and ui_smoke.get("ok")
        and schema_check.get("ok")
        and aidebug_status.get("status") == "ok"
        and all(
            isinstance(result.get(key), dict) and result[key].get("ok")
            for key in (
                "command_smoke",
                "command_safety",
                "command_matrix_smoke",
                "patch_smoke",
                "patch_security",
                "terminal_smoke",
                "aidebug_slice_smoke",
                "aidebug_security",
                "compact_smoke",
                "context_pressure_smoke",
                "context_pressure_variants_smoke",
                "memory_smoke",
                "memory_pressure_smoke",
                "web_validation",
            )
        )
        and (web_smoke is None or web_smoke.get("ok"))
        and (live_smoke is None or (live_smoke.get("ok") and live_smoke.get("dual_star_metadata_ok")))
        and bool((result.get("runtime_state_guard") or {}).get("ok"))
    )
    findings = collect_findings(result)
    result["findings"] = findings
    resolutions = collect_issue_resolutions(result)
    result["resolutions"] = resolutions
    log_json(result)
    write_round_notes(result, findings)
    for finding in findings:
        log_issue(finding)
    for resolution in resolutions:
        log_resolution(resolution)
    write_log("projectling-auto", f"round={round_id} ok={int(bool(result['ok']))} elapsed={result['elapsed_seconds']}s")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aidebug projectling-auto")
    parser.add_argument("--rounds", type=int, default=1, help="number of rounds; 0 means run forever")
    parser.add_argument("--interval", type=float, default=0.0, help="sleep seconds between rounds")
    parser.add_argument("--web-query", default="", help="optional web search smoke query")
    parser.add_argument("--live-chat-smoke", action="store_true", help="also run a real active-provider function-calling smoke per round")
    parser.add_argument(
        "--profile",
        choices=("local", "live", "full"),
        help="named smoke profile: local=deterministic stress, live=active-provider smoke, full=active-provider plus web smoke",
    )
    parser.add_argument(
        "--local-stress",
        action="store_true",
        help="run deterministic local stress only; disables web search and live active-provider smoke",
    )
    return parser


def normalize_profile_args(args: argparse.Namespace) -> tuple[str, argparse.Namespace]:
    profile = str(args.profile or "")
    if args.local_stress:
        profile = "local"
    elif not profile:
        if args.live_chat_smoke and args.web_query:
            profile = "full"
        elif args.live_chat_smoke:
            profile = "live"
        else:
            profile = "custom"

    if profile == "local":
        args.local_stress = True
        args.web_query = ""
        args.live_chat_smoke = False
    elif profile == "live":
        args.local_stress = False
        args.live_chat_smoke = True
    elif profile == "full":
        args.local_stress = False
        args.live_chat_smoke = True
        if not args.web_query:
            args.web_query = "ProjectLing aidebug full profile smoke"
    return profile, args


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    profile, args = normalize_profile_args(args)
    config = load_config()
    registry = ToolRegistry(config)
    ctx = ToolContext(cwd=PROJECTLING_DIR, home=HOME, config=config)
    write_log(
        "projectling-auto",
        f"start rounds={args.rounds} interval={args.interval} web={bool(args.web_query)} "
        f"live={bool(args.live_chat_smoke)} local_stress={bool(args.local_stress)} profile={profile}",
    )
    cleanup_stale_auto_sessions()
    round_id = 0
    failures = 0
    try:
        while args.rounds == 0 or round_id < args.rounds:
            round_id += 1
            payload = run_round(
                registry,
                ctx,
                round_id,
                web_query=args.web_query,
                live_chat_smoke=bool(args.live_chat_smoke),
                local_stress=bool(args.local_stress),
                profile=profile,
            )
            if not payload.get("ok"):
                failures += 1
                write_log("projectling-auto", f"round={round_id} failure_detected")
            if args.interval > 0 and (args.rounds == 0 or round_id < args.rounds):
                time.sleep(args.interval)
    except KeyboardInterrupt:
        write_log("projectling-auto", "interrupted")
        return 130
    write_log("projectling-auto", f"done rounds={round_id} failures={failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
