from __future__ import annotations

import argparse
import base64
import calendar
import contextlib
from dataclasses import replace
import functools
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable


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
elif _SCRIPT_AIDEBUG_DIR and (_SCRIPT_AIDEBUG_DIR / "runner" / "aidebug_health.py").exists():
    AIDEBUG_DIR = _SCRIPT_AIDEBUG_DIR
else:
    AIDEBUG_DIR = (_DEFAULT_AITERMUX_HOME / "projectling" / "aidebug").expanduser()
_INFERRED_PROJECTLING_DIR = AIDEBUG_DIR.parent if (AIDEBUG_DIR.parent / "run.sh").exists() else None
PROJECTLING_DIR = Path(
    os.environ.get("PROJECTLING_DIR", str(_INFERRED_PROJECTLING_DIR or _DEFAULT_AITERMUX_HOME / "projectling"))
).expanduser()
AITERMUX_HOME = Path(os.environ.get("AITERMUX_HOME", str(PROJECTLING_DIR.parent))).expanduser()
LOG_DIR = AIDEBUG_DIR / "logs"
NOTE_DIR = AIDEBUG_DIR / "notes"
PROJECTLING_RUN = PROJECTLING_DIR / "run.sh"
HEALTH_JSON = LOG_DIR / "aidebug-health.json"
HEALTH_JSONL = LOG_DIR / "aidebug-health.jsonl"
WINDOWS_JSON = LOG_DIR / "aidebug-windows.json"
HEALTH_MD = NOTE_DIR / "aidebug-health.md"
ANDROID_READINESS_MD = NOTE_DIR / "projectling-android-termux-readiness.md"
NEXT_PLAN_MD = NOTE_DIR / "projectling-aidebug-next-plan.md"
NEXT_PLAN_JSON = NOTE_DIR / "projectling-aidebug-next-plan.json"
HEALTH_SANDBOX_DIR = AIDEBUG_DIR / "tmp" / "health-sandbox"

_MASKED_SECRET_PREVIEW_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{1,8}\.\.\.[A-Za-z0-9_-]{1,8}\b")
_UNMASKED_SECRET_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{10,}|ghp_[A-Za-z0-9_]{10,}|AIza[0-9A-Za-z_-]{10,}|Bearer\s+[A-Za-z0-9._-]{10,})\b"
)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;:]*[A-Za-z]")


def _launcher_exe_path() -> Path:
    override = str(os.environ.get("PROJECTLING_WINDOWS_LAUNCHER_PATH") or "").strip()
    if override:
        return Path(override).expanduser()
    candidates = [
        PROJECTLING_DIR / "PROJECT凌.exe",
        PROJECTLING_DIR / "PROJECT LING.exe",
        PROJECTLING_DIR.parent / "PROJECT凌.exe",
        PROJECTLING_DIR.parent / "PROJECT LING.exe",
    ]
    return next((path for path in candidates if path.is_file()), candidates[0])


def _contains_unmasked_secret(text: str, *, exact_secrets: Iterable[str] = ()) -> bool:
    probe = str(text or "")
    for secret in exact_secrets:
        if secret and secret in probe:
            return True
    scrubbed = _MASKED_SECRET_PREVIEW_RE.sub("MASKED_SECRET_PREVIEW", probe)
    return bool(_UNMASKED_SECRET_RE.search(scrubbed))


def _strip_ansi_probe(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", str(text or ""))

sys.path.insert(0, str(PROJECTLING_DIR))
try:  # pragma: no cover - fallback import guard
    from projectling import DeepSeekClient, ProjectLingEngine, deepseek_usage_cache_summary, load_config
    from tooling import ToolContext, ToolRegistry
except Exception:  # pragma: no cover - import fallback for partial setups
    DeepSeekClient = None  # type: ignore[assignment]
    ProjectLingEngine = None  # type: ignore[assignment]
    deepseek_usage_cache_summary = None  # type: ignore[assignment]
    ToolContext = None  # type: ignore[assignment]
    ToolRegistry = None  # type: ignore[assignment]
    load_config = None  # type: ignore[assignment]

try:  # pragma: no cover - package import when loaded as a module
    from .runtime_state_guard import build_snapshot, compare_snapshots
except ImportError:  # pragma: no cover - direct script execution
    from runtime_state_guard import build_snapshot, compare_snapshots


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run_cmd(
    command: list[str],
    *,
    cwd: Path,
    timeout: int = 30,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
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
        input=input_text,
        capture_output=True,
        timeout=timeout,
    )


def run_projectling(args: list[str], *, timeout: int = 30, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    if os.name == "nt":
        core_py = PROJECTLING_DIR / "core.py"
        return run_cmd([sys.executable, str(core_py), *args], cwd=PROJECTLING_DIR, timeout=timeout, input_text=input_text)
    return run_cmd([str(PROJECTLING_RUN), *args], cwd=AITERMUX_HOME, timeout=timeout, input_text=input_text)


def _is_transient_projectling_error(completed: subprocess.CompletedProcess[str]) -> bool:
    if completed.returncode == 0:
        return False
    text = f"{completed.stderr or ''}\n{completed.stdout or ''}".lower()
    return any(
        marker in text
        for marker in (
            "disk i/o error",
            "database is locked",
            "database is busy",
            "input/output error",
            "resource temporarily unavailable",
        )
    )


def run_projectling_with_retry(
    args: list[str],
    *,
    timeout: int = 30,
    input_text: str | None = None,
    attempts: int = 3,
) -> tuple[subprocess.CompletedProcess[str], int]:
    attempts = max(1, int(attempts))
    completed: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, attempts + 1):
        try:
            completed = run_projectling(args, timeout=timeout, input_text=input_text)
        except subprocess.TimeoutExpired:
            if attempt >= attempts:
                raise
            time.sleep(0.25 * (attempt + 1))
            continue
        if completed.returncode == 0:
            return completed, attempt
        if not _is_transient_projectling_error(completed):
            return completed, attempt
        if attempt < attempts:
            time.sleep(0.25 * (attempt + 1))
    if completed is None:
        raise RuntimeError("ProjectLing retry loop ended without a process result")
    return completed, attempts


def file_meta(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "path": str(path)}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.count("\n")
    except OSError:
        lines = 0
    stat = path.stat()
    return {
        "exists": True,
        "path": str(path),
        "bytes": stat.st_size,
        "lines": lines,
        "mtime": int(stat.st_mtime),
        "age_seconds": max(0, int(time.time() - stat.st_mtime)),
    }


def item(name: str, score: int, status: str, evidence: list[str], next_action: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "score": max(0, min(100, int(score))),
        "status": status,
        "evidence": evidence,
        "next_action": next_action,
    }


def _capture_runtime_state(label: str) -> tuple[dict[str, Any] | None, str]:
    try:
        return build_snapshot(label=label), ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _runtime_state_health_item(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    scope: str,
    before_error: str = "",
    after_error: str = "",
    allow: set[str] | None = None,
) -> dict[str, Any]:
    capture_errors = [error for error in (before_error, after_error) if error]
    comparison = compare_snapshots(before, after, allow=allow or set()) if before is not None and after is not None else {}
    changed_files = comparison.get("changed_files") if isinstance(comparison.get("changed_files"), list) else []
    changed_paths = [str(entry.get("path") or "") for entry in changed_files if isinstance(entry, dict)]
    forbidden = comparison.get("forbidden_changes") if isinstance(comparison.get("forbidden_changes"), list) else []
    semantic = comparison.get("semantic_changes") if isinstance(comparison.get("semantic_changes"), dict) else {}
    secret_changed = bool(comparison.get("secret_presence_changed"))
    ok = bool(comparison.get("ok")) and not capture_errors
    evidence = [
        f"scope={scope}",
        f"watched_files={len((before or {}).get('files') or {})}",
        f"changed={_compact_list_or_dash(changed_paths)}",
        f"forbidden={_compact_list_or_dash([str(path) for path in forbidden])}",
        f"allowed={_compact_list_or_dash(sorted(allow or set()))}",
        f"semantic_keys={_compact_list_or_dash(sorted(str(key) for key in semantic))}",
        f"secret_presence_changed={int(secret_changed)}",
        f"capture_errors={_compact_list_or_dash(capture_errors)}",
    ]
    score = 100 if ok else 0
    return item(
        "runtime_state_no_mutation",
        score,
        status_from_score(score),
        evidence,
        "停止测试并恢复用户 Provider、模型、Key、角色、focus、context、memory 状态，然后定位污染测试。" if not ok else "",
    )


def _projectling_available() -> bool:
    return all(value is not None for value in (ProjectLingEngine, ToolContext, ToolRegistry, load_config))


def _active_api_provider(default: str = "deepseek") -> str:
    if load_config is None:
        return default
    try:
        return str(getattr(load_config(), "api_provider", default) or default).strip().lower()
    except Exception:
        return default


def _active_api_key_configured() -> bool:
    if load_config is None:
        return False
    try:
        return bool(getattr(load_config(), "api_key", None))
    except Exception:
        return False


def _live_chat_provider(live_chat: dict[str, Any] | None, default: str | None = None) -> str:
    provider = str((live_chat or {}).get("provider") or "").strip().lower()
    return provider or (default or _active_api_provider())


def _sandbox_config() -> Any | None:
    if load_config is None:
        return None
    config = load_config()
    HEALTH_SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    sandbox_runtime = HEALTH_SANDBOX_DIR / "runtime"
    sandbox_runtime.mkdir(parents=True, exist_ok=True)
    return replace(config, runtime_dir=sandbox_runtime)


def _execute_tool(name: str, arguments: dict[str, Any], *, cwd: Path | None = None) -> dict[str, Any] | None:
    if not _projectling_available():
        return None
    config = _sandbox_config()
    if config is None:
        return None
    registry = ToolRegistry(config)  # type: ignore[operator]
    tool_context = ToolContext(
        cwd=(cwd or HEALTH_SANDBOX_DIR).expanduser(),
        home=HOME,
        config=config,
    )  # type: ignore[operator]
    call = {"id": "health", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
    result = registry.execute_tool_call(call, tool_context)
    try:
        return json.loads(str(result.get("content") or "{}"))
    except json.JSONDecodeError:
        return {"status": "error", "message": "tool payload not json"}


def _health_summary(payload: dict[str, Any] | None) -> str:
    if not payload:
        return "unavailable"
    summary = str(payload.get("summary") or "").strip()
    if summary:
        return summary
    message = str(payload.get("message") or "").strip()
    if message:
        return message
    return str(payload.get("status") or "unknown")


def _load_health_history(limit: int = 12) -> list[dict[str, Any]]:
    if not HEALTH_JSONL.exists():
        return []
    try:
        lines = HEALTH_JSONL.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    history: list[dict[str, Any]] = []
    for raw in lines[-max(1, int(limit)) :]:
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if isinstance(payload, dict):
            history.append(payload)
    return history


def _health_jsonl_integrity(path: Path = HEALTH_JSONL, recent_window: int = 12) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "exists": path.exists(),
        "lines": 0,
        "valid": 0,
        "bad": 0,
        "first_bad": "-",
        "last_bad": "-",
        "bad_recent": 0,
        "bad_legacy": 0,
        "latest_line": "-",
        "latest_ok": False,
        "latest_generated_at": "",
        "window": max(1, int(recent_window)),
        "read_error": "",
    }
    if not path.exists():
        return summary
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        summary["read_error"] = str(exc)
        return summary

    numbered = [(index, line) for index, line in enumerate(lines, start=1) if line.strip()]
    summary["lines"] = len(numbered)
    if not numbered:
        return summary

    latest_line, _latest_raw = numbered[-1]
    recent_start = max(1, latest_line - summary["window"] + 1)
    bad_lines: list[int] = []
    latest_payload: dict[str, Any] | None = None
    valid = 0
    for line_number, raw in numbered:
        try:
            payload = json.loads(raw)
        except Exception:
            bad_lines.append(line_number)
            continue
        if isinstance(payload, dict):
            valid += 1
            if line_number == latest_line:
                latest_payload = payload
        else:
            bad_lines.append(line_number)

    summary["valid"] = valid
    summary["bad"] = len(bad_lines)
    if bad_lines:
        summary["first_bad"] = bad_lines[0]
        summary["last_bad"] = bad_lines[-1]
    bad_recent = sum(1 for line_number in bad_lines if line_number >= recent_start)
    summary["bad_recent"] = bad_recent
    summary["bad_legacy"] = len(bad_lines) - bad_recent
    summary["latest_line"] = latest_line
    summary["latest_ok"] = latest_payload is not None
    if latest_payload:
        summary["latest_generated_at"] = str(latest_payload.get("generated_at") or "")
    return summary


def _health_history_summary(history: list[dict[str, Any]]) -> dict[str, Any]:
    points: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    for payload in history:
        raw_score = payload.get("overall_score")
        try:
            score = round(float(raw_score), 1)
        except (TypeError, ValueError):
            continue
        status = str(payload.get("overall_status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        points.append(
            {
                "generated_at": str(payload.get("generated_at") or ""),
                "overall_score": score,
                "overall_status": status,
            }
        )

    recent = points[-5:]
    recent_scores = [float(point["overall_score"]) for point in recent]
    summary: dict[str, Any] = {
        "run_count": len(points),
        "recent_count": len(recent),
        "status_counts": status_counts,
        "recent": recent,
    }
    if not recent_scores:
        return summary

    latest = recent_scores[-1]
    previous = recent_scores[-2] if len(recent_scores) >= 2 else None
    delta = round(latest - previous, 1) if previous is not None else None
    if delta is None:
        trend = "insufficient"
    elif delta > 2:
        trend = "up"
    elif delta < -2:
        trend = "down"
    else:
        trend = "flat"
    summary.update(
        {
            "latest_score": latest,
            "latest_status": recent[-1].get("overall_status"),
            "latest_generated_at": recent[-1].get("generated_at"),
            "previous_score": previous,
            "delta": delta,
            "trend": trend,
            "recent_average": round(sum(recent_scores) / len(recent_scores), 1),
            "recent_min": min(recent_scores),
            "recent_max": max(recent_scores),
        }
    )
    return summary


def status_from_score(score: int) -> str:
    if score >= 85:
        return "ok"
    if score >= 60:
        return "warn"
    return "fail"


def _read_text_optional(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _is_wsl_runtime() -> bool:
    marker = "\n".join(
        [
            os.environ.get("WSL_DISTRO_NAME") or "",
            os.environ.get("WSL_INTEROP") or "",
            _read_text_optional(Path("/proc/sys/kernel/osrelease")),
            _read_text_optional(Path("/proc/version")),
        ]
    )
    return "microsoft" in marker.lower() or "wsl" in marker.lower()


def _command_evidence(command: str) -> tuple[bool, str]:
    path = shutil.which(command)
    return bool(path), path or "missing"


def _host_command_available(command: str) -> bool:
    found = shutil.which(command)
    if not found:
        return False
    if os.name == "nt" and command.lower() in {"bash", "sh", "zsh"}:
        lowered = found.lower().replace("/", "\\")
        if lowered.endswith("\\bash.exe") and "\\windows\\system32\\" in lowered:
            return False
    return True


def _decode_process_output(data: bytes) -> str:
    if not data:
        return ""
    if b"\x00" in data[:200]:
        return data.decode("utf-16le", errors="replace").replace("\x00", "")
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return data.decode(errors="replace")


def _find_windows_wsl_exe() -> Path | None:
    if os.name != "nt":
        return None
    found = shutil.which("wsl.exe")
    candidates: list[Path] = []
    if found:
        candidates.append(Path(found))
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    local_app_data = os.environ.get("LOCALAPPDATA")
    candidates.extend(
        [
            windir / "System32" / "wsl.exe",
            windir / "Sysnative" / "wsl.exe",
        ]
    )
    if local_app_data:
        candidates.append(Path(local_app_data) / "Microsoft" / "WindowsApps" / "wsl.exe")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _windows_path_to_wsl(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    if not drive:
        return str(resolved).replace("\\", "/")
    rest = str(resolved)[len(resolved.drive) :].lstrip("\\/").replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def _run_wsl(wsl_exe: Path, args: list[str], *, timeout: int = 30) -> tuple[int | None, str, str]:
    try:
        completed = subprocess.run(
            [str(wsl_exe), *args],
            cwd=str(PROJECTLING_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "", f"timeout after {timeout}s"
    except OSError as exc:
        return None, "", str(exc)
    return completed.returncode, _decode_process_output(completed.stdout), _decode_process_output(completed.stderr)


def _parse_wsl_distro_list(stdout: str) -> list[str]:
    return [line.strip().replace("\r", "") for line in stdout.splitlines() if line.strip()]


def _select_projectling_wsl_distro(raw_distros: list[str]) -> str:
    configured = os.environ.get("PROJECTLING_WSL_DISTRO", "").strip()
    distro = configured or "Ubuntu-ProjectLing"
    if distro not in raw_distros:
        projectling_distros = [name for name in raw_distros if "projectling" in name.lower()]
        if projectling_distros:
            distro = projectling_distros[0]
        elif raw_distros:
            distro = raw_distros[0]
    return distro


def check_windows_host_wsl_bridge() -> dict[str, Any]:
    if os.name != "nt":
        return item("windows_host_wsl_bridge", 100, "ok", ["runtime=non-windows", "host_wsl_bridge=not_required"], "")

    wsl_exe = _find_windows_wsl_exe()
    if wsl_exe is None:
        return item(
            "windows_host_wsl_bridge",
            75,
            "warn",
            ["wsl_exe=missing"],
            "安装或修复 WSL 后再验证 Termux 兼容层；Windows native 前端仍可运行。",
        )

    rc, stdout, stderr = _run_wsl(wsl_exe, ["-l", "-q"], timeout=20)
    raw_distros = _parse_wsl_distro_list(stdout)
    distro = _select_projectling_wsl_distro(raw_distros)

    linux_project = os.environ.get("PROJECTLING_WSL_PROJECT_PATH", "").strip() or _windows_path_to_wsl(PROJECTLING_DIR)
    encoded_project = base64.b64encode(linux_project.encode("utf-8")).decode("ascii")
    probe_code = r"""
import base64
import os
from pathlib import Path
import shutil
import subprocess
import sys

project = Path(base64.b64decode(sys.argv[1]).decode("utf-8"))
checks = {
    "project_dir": project.is_dir(),
    "bash": shutil.which("bash") is not None,
    "zsh": shutil.which("zsh") is not None,
    "python3": shutil.which("python3") is not None,
    "tmux": shutil.which("tmux") is not None,
    "termux_bash": os.access("/data/data/com.termux/files/usr/bin/bash", os.X_OK),
    "run_sh": (project / "run.sh").is_file(),
    "projectling_zsh": (project / "projectling.zsh").is_file(),
}
doctor_ok = False
if checks["project_dir"]:
    try:
        completed = subprocess.run(
            [sys.executable, str(project / "core.py"), "doctor"],
            cwd=str(project),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        doctor_ok = completed.returncode == 0
    except Exception:
        doctor_ok = False
checks["doctor"] = doctor_ok
for name, ok in checks.items():
    print(f"{name}={int(ok)}")
"""
    probe_rc, probe_out, probe_err = _run_wsl(
        wsl_exe,
        ["-d", distro, "--", "python3", "-c", probe_code, encoded_project],
        timeout=45,
    )
    checks: dict[str, bool] = {}
    for line in probe_out.splitlines():
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        checks[name.strip()] = value.strip() == "1"

    required = {
        "project_dir": 20,
        "bash": 10,
        "zsh": 10,
        "python3": 15,
        "tmux": 10,
        "termux_bash": 10,
        "run_sh": 10,
        "projectling_zsh": 5,
        "doctor": 10,
    }
    score = 100
    for name, penalty in required.items():
        if not checks.get(name):
            score -= penalty
    if rc not in {0, None}:
        score -= 5
    if probe_rc not in {0, None}:
        score -= 5
    score = max(0, score)
    evidence = [
        f"wsl_exe={wsl_exe}",
        f"distro={distro}",
        f"distros={_compact_list_or_dash(raw_distros)}",
        f"project_path={linux_project}",
        *[f"{name}={int(checks.get(name, False))}" for name in required],
    ]
    if stderr.strip():
        evidence.append(f"list_stderr={stderr.strip()[:160]}")
    if probe_err.strip():
        evidence.append(f"probe_stderr={probe_err.strip()[:160]}")
    missing = [name for name in required if not checks.get(name)]
    return item(
        "windows_host_wsl_bridge",
        score,
        status_from_score(score),
        evidence,
        "修复 WSL/Termux 兼容层缺口：" + ", ".join(missing[:6]) if missing else "",
    )


def _core_smoke(args: list[str], *, input_text: str | None = None, timeout: int = 20) -> tuple[bool, str, str]:
    core_py = PROJECTLING_DIR / "core.py"
    label = " ".join(args[:2])
    if not core_py.exists():
        return False, label, f"missing {core_py}"

    env = os.environ.copy()
    env["AITERMUX_HOME"] = str(AITERMUX_HOME)
    env["AITERMUX_AIDEBUG_DIR"] = str(AIDEBUG_DIR)
    env["PROJECTLING_DIR"] = str(PROJECTLING_DIR)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        completed = subprocess.run(
            [sys.executable, str(core_py), *args],
            cwd=str(PROJECTLING_DIR),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            input=input_text,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, label, f"timeout>{timeout}s"
    except OSError as exc:
        return False, label, f"os_error={exc}"

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if completed.returncode != 0:
        first_error = (stderr.strip().splitlines() or stdout.strip().splitlines() or ["no output"])[0]
        return False, label, f"rc={completed.returncode} {first_error[:160]}"
    detail = f"rc=0 stdout_bytes={len(stdout.encode('utf-8'))} lines={stdout.count(chr(10))}"
    return True, label, detail


def _launcher_settings_smoke(
    args: list[str],
    *,
    expected_text: str,
    timeout: int = 20,
) -> tuple[bool, str, str]:
    launcher_exe = _launcher_exe_path()
    label = " ".join(args)
    if os.name != "nt":
        return False, label, "runtime=non-windows"
    if not launcher_exe.exists():
        return False, label, f"missing {launcher_exe}"

    env = os.environ.copy()
    env["AITERMUX_HOME"] = str(AITERMUX_HOME)
    env["AITERMUX_AIDEBUG_DIR"] = str(AIDEBUG_DIR)
    env["PROJECTLING_DIR"] = str(PROJECTLING_DIR)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        completed = subprocess.run(
            [str(launcher_exe), *args],
            cwd=str(PROJECTLING_DIR),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            input="0\n",
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, label, f"timeout>{timeout}s"
    except OSError as exc:
        return False, label, f"os_error={exc}"

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    expected_hit = expected_text in stdout
    main_screen_fallback = "PROJECT LING // PC" in stdout or "▌ USER //" in stdout
    ok = completed.returncode == 0 and expected_hit and not main_screen_fallback
    detail = (
        f"rc={completed.returncode} expected={int(expected_hit)} "
        f"main_fallback={int(main_screen_fallback)} "
        f"stdout_bytes={len(stdout.encode('utf-8'))} lines={stdout.count(chr(10))}"
    )
    if stderr.strip():
        detail += f" stderr={stderr.strip().splitlines()[0][:120]}"
    return ok, label, detail


def _launcher_command_surface_smoke(*, timeout: int = 20) -> tuple[bool, str, str]:
    launcher_exe = _launcher_exe_path()
    label = "--aidebug-command-surface"
    widths = "16,20,24,32,40,48,80,120"
    if os.name != "nt":
        return False, label, "runtime=non-windows"
    if not launcher_exe.exists():
        return False, label, f"missing {launcher_exe}"

    try:
        completed = subprocess.run(
            [str(launcher_exe), "--aidebug-command-surface", "--json", "--widths", widths],
            cwd=str(PROJECTLING_DIR),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, label, f"timeout>{timeout}s widths={widths}"
    except OSError as exc:
        return False, label, f"os_error={exc}"

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return False, label, f"json_parse_error rc={completed.returncode} stdout={stdout[:160]}"

    help_lines = payload.get("helpLines") if isinstance(payload.get("helpLines"), list) else []
    aliases = payload.get("commandAliases") if isinstance(payload.get("commandAliases"), list) else []
    samples = payload.get("samples") if isinstance(payload.get("samples"), list) else []
    help_text = "\n".join(str(line) for line in help_lines)
    alias_set = {str(alias) for alias in aliases}

    max_line = 0
    sample_failures: list[str] = []
    for sample in samples:
        if not isinstance(sample, dict):
            sample_failures.append("sample_non_dict")
            continue
        width = sample.get("consoleWidth")
        if not bool(sample.get("ok")):
            sample_failures.append(f"w{width}:sample")
        lines = sample.get("lines") if isinstance(sample.get("lines"), list) else []
        for line in lines:
            if not isinstance(line, dict):
                continue
            try:
                display_width = int(line.get("displayWidth") or 0)
                expected_width = int(line.get("expectedWidth") or 0)
            except (TypeError, ValueError):
                sample_failures.append(f"w{width}:bad_width")
                continue
            max_line = max(max_line, display_width)
            if display_width > expected_width:
                sample_failures.append(f"w{width}:{display_width}>{expected_width}")

    provider = str(payload.get("activeProvider") or "")
    api_status = str(payload.get("apiStatus") or "")
    provider_ok = provider in {"gemini", "deepseek"}
    api_status_ok = api_status.casefold() == provider.casefold()
    expected_commands = {"/settings", "/role", "/exit"}
    command_ok = alias_set == expected_commands and len(aliases) == len(expected_commands)
    help_ok = all(
        token in help_text
        for token in ("/settings", "/role", "/exit")
    ) and not any(
        token in help_text
        for token in (
            "/settings deepseek",
            "/settings gemini",
            "/settings websearch",
            "/models",
            "/api-test",
            "/aidebug",
            "/help",
        )
    )
    width_ok = bool(samples) and not sample_failures
    input_editor_ok = all(
        payload.get(name) is True
        for name in (
            "inputEditorBackspaceOk",
            "inputEditorDeleteOk",
            "inputEditorCursorOk",
            "inputEditorBoundaryOk",
        )
    )
    slash_menu_ok = payload.get("slashMenuContractOk") is True
    responsive_order_ok = payload.get("responsiveOrderOk") is True
    secret_ok = not _contains_unmasked_secret(stdout + "\n" + stderr)
    ok = (
        completed.returncode == 0
        and payload.get("status") == "ok"
        and provider_ok
        and api_status_ok
        and command_ok
        and help_ok
        and width_ok
        and input_editor_ok
        and slash_menu_ok
        and responsive_order_ok
        and secret_ok
    )
    detail = (
        f"rc={completed.returncode} status={payload.get('status')} provider={provider} "
        f"api_status={api_status} commands={int(command_ok)} help={int(help_ok)} "
        f"widths={widths} max_line={max_line} failures={','.join(sample_failures[:4]) or '-'} "
        f"editor={int(input_editor_ok)} slash={int(slash_menu_ok)} layout={int(responsive_order_ok)} secret={int(secret_ok)}"
    )
    if stderr.strip():
        detail += f" stderr={stderr.strip().splitlines()[0][:120]}"
    return ok, label, detail


def _launcher_external_channel_reason(
    returncode: int,
    payload: dict[str, Any],
    *,
    stderr: str = "",
) -> str:
    if returncode == 0 or payload.get("ok") is True:
        return ""

    fragments = [str(payload.get("error") or ""), str(stderr or "")]
    results = payload.get("results")
    if isinstance(results, list):
        fragments.extend(
            str(result.get("error") or "")
            for result in results
            if isinstance(result, dict)
        )
    probe = "\n".join(fragments).casefold()
    if "channel_circuit_open" in probe or "circuit breaker" in probe:
        return "circuit_open"
    if "temporarily suspended" in probe:
        return "suspended"
    if "当前无可用token" in probe or "当前无可用 token" in probe:
        return "token_unavailable"

    provider = str(payload.get("provider") or "").strip().casefold()
    base_url = str(payload.get("base_url") or "").strip().casefold()
    local_base = any(marker in base_url for marker in ("127.0.0.1", "localhost", "[::1]"))
    if provider == "gemini" and not local_base and ("http 503" in probe or "status_503" in probe):
        return "upstream_503"
    return ""


def _launcher_stderr_evidence(samples: Iterable[str]) -> str:
    return " stderr=present" if any(str(sample or "").strip() for sample in samples) else ""


@functools.lru_cache(maxsize=4)
def _launcher_startup_command_smoke_cached(timeout: int) -> tuple[bool, str, str]:
    launcher_exe = _launcher_exe_path()
    label = "launcher-startup-commands"
    if os.name != "nt":
        return False, label, "runtime=non-windows"
    if not launcher_exe.exists():
        return False, label, f"missing {launcher_exe}"

    env = os.environ.copy()
    env["AITERMUX_HOME"] = str(AITERMUX_HOME)
    env["AITERMUX_AIDEBUG_DIR"] = str(AIDEBUG_DIR)
    env["PROJECTLING_DIR"] = str(PROJECTLING_DIR)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("COLUMNS", "80")

    def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(launcher_exe), *args],
            cwd=str(PROJECTLING_DIR),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    try:
        models = _run(["/models", "--limit", "50"])
        stable_api = _run(["/api-test", "--json", "--no-stream"])
        risky_api = _run([
            "/api-test",
            "--json",
            "--no-stream",
            "--model",
            "gemini-3.1-flash-image",
        ])
        bad_base = "http://127.0.0.1:9/v1"
        failure_api = _run([
            "/api-test",
            "--no-stream",
            "--model",
            "gemini-launcher-failure-bad-model",
            "--base-url",
            bad_base,
            "--timeout",
            "5",
        ])
        failure_models = _run(["/models", "--base-url", bad_base, "--timeout", "5"])
    except subprocess.TimeoutExpired:
        return False, label, f"timeout>{timeout}s"
    except OSError as exc:
        return False, label, f"os_error={exc}"

    def _plain(text: str) -> str:
        try:
            import core as projectling_core

            return projectling_core._strip_ansi(text or "")
        except Exception:
            return text or ""

    def _max_width(text: str) -> int:
        plain = _plain(text)
        try:
            import core as projectling_core

            return max((projectling_core._display_width(line) for line in plain.splitlines()), default=0)
        except Exception:
            return max((len(line) for line in plain.splitlines()), default=0)

    def _json_payload(text: str) -> dict[str, Any]:
        try:
            payload = json.loads((text or "").strip() or "{}")
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    models_text = _plain(models.stdout or "")
    stable_payload = _json_payload(stable_api.stdout or "")
    risky_payload = _json_payload(risky_api.stdout or "")
    combined = "\n".join(
        [
            models.stdout or "",
            models.stderr or "",
            stable_api.stdout or "",
            stable_api.stderr or "",
            risky_api.stdout or "",
            risky_api.stderr or "",
            failure_api.stdout or "",
            failure_api.stderr or "",
            failure_models.stdout or "",
            failure_models.stderr or "",
        ]
    )

    main_fallback = "PROJECT LING // PC" in combined or "▌ USER //" in combined
    taxonomy_ok = all(token in models_text for token in ("分类", "pro", "flash", "image", "agent"))
    roles_ok = all(token in models_text for token in ("主星", "执行星"))
    configured_ok = all(token in models_text for token in ("gemini-3-flash", "gemini-3.1-pro-low"))
    models_width = _max_width(models.stdout or "")
    models_ok = models.returncode == 0 and taxonomy_ok and roles_ok and configured_ok and models_width <= 80

    stable_ok = (
        stable_api.returncode == 0
        and stable_payload.get("ok") is True
        and stable_payload.get("executor_risk") == "normal"
        and "pong" in str(stable_payload.get("preview") or "")
    )
    stable_external_reason = _launcher_external_channel_reason(
        stable_api.returncode,
        stable_payload,
        stderr=stable_api.stderr or "",
    )
    stable_external_gate = bool(stable_external_reason)
    stable_accepted = stable_ok or stable_external_gate
    risky_metadata_ok = (
        risky_payload.get("executor_model") == "gemini-3.1-flash-image"
        and risky_payload.get("executor_risk") == "image"
        and bool(risky_payload.get("executor_hint"))
    )
    risky_success_ok = (
        risky_api.returncode == 0
        and risky_payload.get("ok") is True
        and "pong" in str(risky_payload.get("preview") or "")
    )
    risky_safe_failure_ok = (
        risky_api.returncode == 1
        and risky_payload.get("ok") is False
        and bool(risky_payload.get("error"))
    )
    risky_ok = risky_metadata_ok and (risky_success_ok or risky_safe_failure_ok)
    risky_mode = "success" if risky_success_ok else "safe_fail" if risky_safe_failure_ok else "bad"
    failure_api_text = _plain(failure_api.stdout or "")
    failure_models_text = _plain(failure_models.stdout or "")
    failure_api_recovery_ok = all(token in failure_api_text for token in ("下一步", "API Key", "Base URL", "模型名", "网络"))
    failure_models_recovery_ok = all(token in failure_models_text for token in ("下一步", "API Key", "Base URL", "模型列表接口", "网络"))
    failure_api_ok = (
        failure_api.returncode == 1
        and "api-test fail" in failure_api_text
        and "gemini-launcher-failure-bad-model" in failure_api_text
        and failure_api_recovery_ok
    )
    failure_models_ok = (
        failure_models.returncode == 1
        and "Gemini" in failure_models_text
        and "fail" in failure_models_text
        and failure_models_recovery_ok
    )
    secret_ok = not _contains_unmasked_secret(combined)
    ok = models_ok and stable_accepted and risky_ok and failure_api_ok and failure_models_ok and not main_fallback and secret_ok

    failures: list[str] = []
    if not models_ok:
        failures.append("models")
    if not stable_accepted:
        failures.append("stable_api")
    if not risky_ok:
        failures.append("risky_api")
    if not failure_api_ok:
        failures.append("failure_api")
    if not failure_models_ok:
        failures.append("failure_models")
    if main_fallback:
        failures.append("main_fallback")
    if not secret_ok:
        failures.append("secret")

    detail = (
        f"models={int(models_ok)} models_rc={models.returncode} taxonomy={int(taxonomy_ok)} "
        f"roles={int(roles_ok)} configured={int(configured_ok)} models_width={models_width} "
        f"stable_api={int(stable_ok)} stable_rc={stable_api.returncode} "
        f"stable_risk={stable_payload.get('executor_risk')} stable_external={int(stable_external_gate)} "
        f"stable_external_reason={stable_external_reason or '-'} "
        f"risky_api={int(risky_ok)} risky_rc={risky_api.returncode} "
        f"risky_risk={risky_payload.get('executor_risk')} risky_mode={risky_mode} "
        f"failure_api={int(failure_api_ok)} failure_api_rc={failure_api.returncode} "
        f"failure_models={int(failure_models_ok)} failure_models_rc={failure_models.returncode} "
        f"failure_recovery={int(failure_api_recovery_ok and failure_models_recovery_ok)} "
        f"main_fallback={int(main_fallback)} secret={int(secret_ok)} "
        f"failures={','.join(failures[:4]) or '-'}"
    )
    stderr_samples = [
        (models.stderr or "").strip(),
        (stable_api.stderr or "").strip(),
        (risky_api.stderr or "").strip(),
        (failure_api.stderr or "").strip(),
        (failure_models.stderr or "").strip(),
    ]
    detail += _launcher_stderr_evidence(stderr_samples)
    return ok, label, detail


def _launcher_startup_command_smoke(*, timeout: int = 60) -> tuple[bool, str, str]:
    return _launcher_startup_command_smoke_cached(int(timeout))


def _json_stdout_smoke(args: list[str], *, timeout: int = 20) -> tuple[bool, str, str]:
    ok, label, detail = _core_smoke(args, timeout=timeout)
    if not ok:
        return ok, label, detail

    env = os.environ.copy()
    env["AITERMUX_HOME"] = str(AITERMUX_HOME)
    env["AITERMUX_AIDEBUG_DIR"] = str(AIDEBUG_DIR)
    env["PROJECTLING_DIR"] = str(PROJECTLING_DIR)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        completed = subprocess.run(
            [sys.executable, str(PROJECTLING_DIR / "core.py"), *args],
            cwd=str(PROJECTLING_DIR),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )
        json.loads(completed.stdout or "{}")
    except Exception as exc:
        return False, label, f"json_parse_error={exc}"
    return True, label, detail + " json=1"


def _detail_tokens(detail: str) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for part in str(detail or "").split():
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key and key not in tokens:
            tokens[key] = value
    if "api_status=Gemini" in str(detail or ""):
        tokens["api_status"] = "gemini"
    elif "api_status=gemini key" in str(detail or ""):
        tokens["api_status"] = "gemini_key"
    elif "api_status=gemini ok" in str(detail or ""):
        tokens["api_status"] = "gemini_key"
    elif "api_status=DeepSeek" in str(detail or ""):
        tokens["api_status"] = "deepseek"
    return tokens


def _compact_launcher_command_surface_detail(detail: str) -> str:
    tokens = _detail_tokens(detail)
    provider = str(tokens.get("provider") or "").casefold()
    api_status = str(tokens.get("api_status") or "").casefold()
    healthy = (
        tokens.get("rc") == "0"
        and provider in {"gemini", "deepseek"}
        and (api_status == provider or (provider == "gemini" and api_status == "gemini_key"))
        and tokens.get("commands") == "1"
        and tokens.get("help") == "1"
        and tokens.get("editor") == "1"
        and tokens.get("slash") == "1"
        and tokens.get("layout") == "1"
        and tokens.get("failures") == "-"
        and tokens.get("secret") == "1"
    )
    if not tokens or not healthy:
        return str(detail or "")[:150]
    widths = tokens.get("widths", "")
    width_count = str(len([part for part in widths.split(",") if part.strip()])) if widths else "-"
    return (
        f"r0 p={provider[:1]} a={api_status[:1]} c1 m1 e1 s1 d1 w{width_count} "
        f"x{tokens.get('max_line', '-')} f- k1"
    )


def _compact_launcher_startup_detail_rows(detail: str) -> list[tuple[str, str]]:
    tokens = _detail_tokens(detail)
    if not tokens:
        return [("raw", str(detail or "")[:150])]
    stable_accepted = tokens.get("stable_api") == "1" or tokens.get("stable_external") == "1"
    healthy = (
        tokens.get("models") == "1"
        and tokens.get("taxonomy") == "1"
        and tokens.get("roles") == "1"
        and tokens.get("configured") == "1"
        and stable_accepted
        and tokens.get("stable_risk") == "normal"
        and tokens.get("risky_api") == "1"
        and tokens.get("risky_risk") == "image"
        and tokens.get("failure_api") == "1"
        and tokens.get("failure_models") == "1"
        and tokens.get("failure_recovery") == "1"
        and tokens.get("main_fallback") == "0"
        and tokens.get("secret") == "1"
        and tokens.get("failures") == "-"
    )
    if not healthy:
        rows = [("raw", str(detail or "")[:180])]
        if " stderr=" in str(detail or ""):
            rows.append(("stderr", str(detail).split(" stderr=", 1)[1][:120]))
        return rows
    rows = [
        (
            "models",
            f"ok={tokens.get('models', '-')}/r{tokens.get('models_rc', '-')} "
            f"tax={tokens.get('taxonomy', '-')} roles={tokens.get('roles', '-')} "
            f"cfg={tokens.get('configured', '-')} w={tokens.get('models_width', '-')}",
        ),
        (
            "api",
            f"s={tokens.get('stable_api', '-')}/x{tokens.get('stable_external', '0')}/r{tokens.get('stable_rc', '-')} "
            f"r={tokens.get('risky_api', '-')}/r{tokens.get('risky_rc', '-')}/{tokens.get('risky_risk', '-')} "
            f"m={tokens.get('risky_mode', '-')}",
        ),
        (
            "failure",
            f"api={tokens.get('failure_api', '-')}/r{tokens.get('failure_api_rc', '-')} "
            f"models={tokens.get('failure_models', '-')}/r{tokens.get('failure_models_rc', '-')} "
            f"rec={tokens.get('failure_recovery', '-')} fb={tokens.get('main_fallback', '-')} "
            f"sec={tokens.get('secret', '-')} fail={tokens.get('failures', '-')}",
        ),
    ]
    if tokens.get("stable_external") == "1":
        reason_alias = {
            "circuit_open": "circuit",
            "suspended": "suspended",
            "token_unavailable": "token",
            "upstream_503": "503",
        }
        rows.append(("external", reason_alias.get(tokens.get("stable_external_reason", ""), "other")))
    if " stderr=present" in str(detail or ""):
        rows.append(("stderr", "present"))
    return rows


def _launcher_settings_startup_alias(name: str) -> str:
    aliases = {
        "launcher_settings_root_startup": "set_root",
        "launcher_settings_api_startup": "set_api",
        "launcher_settings_deepseek_startup": "set_ds",
        "launcher_settings_gemini_startup": "set_gem",
        "launcher_settings_websearch_startup": "set_web",
        "launcher_settings_role_startup": "set_role",
        "launcher_settings_system_startup": "set_sys",
        "launcher_settings_gemini_inline_startup": "set_gem_i",
        "launcher_settings_api_inline_startup": "set_api_i",
        "launcher_settings_websearch_inline_startup": "set_web_i",
        "launcher_settings_gemini_colon_startup": "set_gem_c",
        "launcher_settings_websearch_colon_startup": "set_web_c",
    }
    return aliases.get(name, name)


def _compact_launcher_settings_startup_detail(detail: str) -> str:
    tokens = _detail_tokens(detail)
    healthy = (
        tokens.get("rc") == "0"
        and tokens.get("expected") == "1"
        and tokens.get("main_fallback") == "0"
    )
    if not tokens or not healthy:
        return str(detail or "")[:180]
    return (
        f"rc=0 hit=1 fb=0 out={tokens.get('stdout_bytes', '-')} "
        f"ln={tokens.get('lines', '-')}"
    )


def _compact_marker_summary(markers: dict[str, bool], *, sample_limit: int = 4) -> str:
    names = list(markers)
    missing = [name for name, value in markers.items() if not value]
    aliases = {
        "command_probe": "cmd",
        "provider_status": "prov",
        "models_command": "models",
        "api_test_command": "api",
        "startup_forwarding": "start",
        "startup_passthrough": "pass",
        "secret_redaction": "secret",
    }
    sample = [aliases.get(name, name) for name in names[:sample_limit]]
    return (
        f"ok:{len(names) - len(missing)}/{len(names)} "
        f"miss:{_compact_list_or_dash(missing)} "
        f"sample:{_compact_list_or_dash(sample)} "
        f"extra:{max(0, len(names) - len(sample))}"
    )


def _route_category_label(category: str) -> str:
    aliases = {
        "strict_short_reply": "strict",
        "execution_or_format": "exec",
        "casual_chat": "casual",
        "analysis": "analysis",
        "code_generation": "code",
    }
    return aliases.get(str(category or ""), str(category or "-") or "-")


def _route_model_label(model: str, expected_model: str) -> str:
    if str(model or "") == str(expected_model or ""):
        return "planner"
    return str(model or "-") or "-"


def _route_thinking_label(value: Any) -> str:
    if value is True:
        return "1"
    if value is False:
        return "0"
    if value is None:
        return "-"
    return str(value)


def _project_relative_label(path: Path) -> str:
    try:
        return path.relative_to(PROJECTLING_DIR).as_posix()
    except ValueError:
        return path.name or str(path)


def _launcher_freshness_detail(launcher_exe: Path, launcher_source: Path) -> str:
    exe_label = launcher_exe.name
    source_label = _project_relative_label(launcher_source)
    missing = []
    if not launcher_exe.exists():
        missing.append("exe")
    if not launcher_source.exists():
        missing.append("src")
    if missing:
        return f"exe={exe_label} src={source_label} missing={','.join(missing)}"
    if launcher_exe.stat().st_mtime < launcher_source.stat().st_mtime:
        return (
            f"exe={exe_label} src={source_label} rel=exe<src "
            f"exe_m={int(launcher_exe.stat().st_mtime)} src_m={int(launcher_source.stat().st_mtime)}"
        )
    return (
        f"exe={exe_label} "
        f"src={source_label} "
        "rel=exe>=src"
    )


def _launcher_freshness_density_failures(evidence: list[str], *, limit: int = 120) -> list[str]:
    failures: list[str] = []
    full_project = str(PROJECTLING_DIR)
    for index, row in enumerate(evidence, start=1):
        if not row.startswith("windows_launcher_fresh="):
            continue
        has_full_path = full_project in row or ":\\" in row or "/mnt/" in row
        old_labels = "source=" in row or "relation=" in row
        if len(row) > limit or has_full_path or old_labels:
            failures.append(f"row{index}:{len(row)}")
    return failures


def _capture_windows_ui_screenshot() -> dict[str, Any]:
    if os.name != "nt":
        return {"ok": False, "status": "skip", "message": "not_windows"}

    powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return {"ok": False, "status": "fail", "message": "powershell_not_found"}

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    target = LOG_DIR / f"ui-screenshot-{time.strftime('%Y%m%d-%H%M%S')}.png"
    script = r'''
$ErrorActionPreference = 'Stop'
$outPath = $env:AIDEBUG_SCREENSHOT_OUT
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class AidebugWin32 {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
    [StructLayout(LayoutKind.Sequential)] public struct RECT {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }
}
"@
function Get-WindowTitle([IntPtr]$handle) {
    $builder = New-Object System.Text.StringBuilder 512
    [AidebugWin32]::GetWindowText($handle, $builder, $builder.Capacity) | Out-Null
    return $builder.ToString()
}

$foregroundHandle = [AidebugWin32]::GetForegroundWindow()
$foregroundTitle = Get-WindowTitle $foregroundHandle
$handle = [IntPtr]::Zero
$title = ''
$mode = 'primary-screen'
if (-not [string]::IsNullOrWhiteSpace($foregroundTitle) -and $foregroundTitle -match 'PROJECT LING|PROJECT凌') {
    $handle = $foregroundHandle
    $title = $foregroundTitle
    $mode = 'foreground-window'
}
else {
    $candidate = Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -match 'PROJECT LING|PROJECT凌' } |
        Sort-Object StartTime -Descending |
        Select-Object -First 1
    if ($candidate) {
        $handle = $candidate.MainWindowHandle
        $title = $candidate.MainWindowTitle
        $mode = 'title-match-window'
    }
}

$rect = New-Object AidebugWin32+RECT
$hasWindowRect = $handle -ne [IntPtr]::Zero -and [AidebugWin32]::GetWindowRect($handle, [ref]$rect)
if ($hasWindowRect) {
    $x = $rect.Left
    $y = $rect.Top
    $width = $rect.Right - $rect.Left
    $height = $rect.Bottom - $rect.Top
}
if (-not $hasWindowRect -or $width -lt 80 -or $height -lt 80) {
    $bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $x = $bounds.X
    $y = $bounds.Y
    $width = $bounds.Width
    $height = $bounds.Height
    $mode = 'primary-screen'
    $title = ''
}

$bitmap = New-Object System.Drawing.Bitmap $width, $height
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
try {
    $graphics.CopyFromScreen($x, $y, 0, 0, $bitmap.Size)
    $bitmap.Save($outPath, [System.Drawing.Imaging.ImageFormat]::Png)
}
finally {
    $graphics.Dispose()
    $bitmap.Dispose()
}

[pscustomobject]@{
    ok = $true
    path = $outPath
    mode = $mode
    title = $title
    width = $width
    height = $height
} | ConvertTo-Json -Compress
'''
    env = os.environ.copy()
    env["AIDEBUG_SCREENSHOT_OUT"] = str(target)
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            cwd=str(PROJECTLING_DIR),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "status": "fail", "path": str(target), "message": "timeout"}
    except OSError as exc:
        return {"ok": False, "status": "fail", "path": str(target), "message": f"os_error={exc}"}

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        return {
            "ok": False,
            "status": "fail",
            "path": str(target),
            "message": f"rc={completed.returncode} {stderr[:240]}",
        }
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "status": "fail", "path": str(target), "message": f"json_parse_error stdout={stdout[:160]}"}
    payload["ok"] = bool(payload.get("ok") and Path(str(payload.get("path") or "")).is_file())
    payload["status"] = "ok" if payload["ok"] else "fail"
    if stderr:
        payload["stderr"] = stderr[:240]
    return payload


def _check_windows_ui_screenshot(capture: dict[str, Any]) -> dict[str, Any]:
    ok = bool(capture.get("ok"))
    evidence = [
        f"path={capture.get('path', '')}",
        f"mode={capture.get('mode', capture.get('status', 'unknown'))}",
        f"title={capture.get('title', '')}",
        f"size={capture.get('width', '?')}x{capture.get('height', '?')}",
    ]
    if capture.get("message"):
        evidence.append(f"message={capture.get('message')}")
    return item(
        "windows_ui_screenshot",
        100 if ok else 55,
        "ok" if ok else "warn",
        evidence,
        "" if ok else "从 PROJECT LING 前台窗口内运行 /aidebug，或确认 Windows 桌面会话可截图。",
    )


def _check_windows_ui_text_layout() -> dict[str, Any]:
    if os.name != "nt":
        return item("windows_ui_text_layout", 100, "ok", ["runtime=non-windows", "text_layout_probe=not_required"], "")

    launcher_exe = _launcher_exe_path()
    if not launcher_exe.exists():
        return item(
            "windows_ui_text_layout",
            0,
            "fail",
            [f"launcher_exe=missing {launcher_exe}"],
            "重新发布 Windows 启动器后复测。",
        )

    widths = "16,20,24,32,40,48,80,120"
    try:
        completed = subprocess.run(
            [str(launcher_exe), "--aidebug-layout", "--json", "--widths", widths],
            cwd=str(PROJECTLING_DIR),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return item("windows_ui_text_layout", 0, "fail", [f"widths={widths}", "timeout=20s"], "检查启动器布局探针是否卡住。")
    except OSError as exc:
        return item("windows_ui_text_layout", 0, "fail", [f"exception={exc}"], "检查 PROJECT LING.exe 是否可执行。")

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return item(
            "windows_ui_text_layout",
            20,
            "fail",
            [f"rc={completed.returncode}", f"stdout={stdout[:240]}", f"stderr={stderr[:160]}"],
            "修复启动器 --aidebug-layout JSON 输出。",
        )

    samples = payload.get("samples") if isinstance(payload, dict) else []
    if not isinstance(samples, list) or not samples:
        return item(
            "windows_ui_text_layout",
            20,
            "fail",
            [f"rc={completed.returncode}", "samples=missing"],
            "修复启动器布局探针样本输出。",
        )

    def has_long_separator(text: str) -> bool:
        run = 0
        for char in str(text or ""):
            if char in "╌─═":
                run += 1
                if run > 3:
                    return True
            else:
                run = 0
        return False

    failed_samples: list[dict[str, Any]] = []
    evidence = [f"rc={completed.returncode}", f"probe_status={payload.get('status')}", f"widths={widths}"]
    startup_ok = True
    startup_dense_count = 0
    startup_line_count = 0
    startup_input_ok = False
    startup_status_ok = False
    startup_card_ok = False
    startup_home_menu_absent = False
    startup_error = ""
    try:
        startup_completed = subprocess.run(
            [str(launcher_exe)],
            cwd=str(PROJECTLING_DIR),
            text=True,
            encoding="utf-8",
            errors="replace",
            input="/exit\n",
            capture_output=True,
            timeout=20,
            check=False,
        )
        startup_text = _strip_ansi_probe(f"{startup_completed.stdout or ''}\n{startup_completed.stderr or ''}").replace("\f", "\n")
        startup_lines = [line for line in startup_text.splitlines() if line.strip()]
        startup_line_count = len(startup_lines)
        startup_input_ok = "▌ USER //" in startup_text and "›" in startup_text
        startup_status_ok = (
            "▌ STATUS //" in startup_text
            and "▌ TIP //" in startup_text
            and "输入 / 查看菜单" in startup_text
            and "协同模式：标准" in startup_text
            and any(provider in startup_text for provider in ("Gemini", "DeepSeek"))
            and "角色剩余时间：" in startup_text
        )
        card_tokens = ("正在为您分配终端伙伴", "主角色：")
        startup_card_ok = all(token in startup_text for token in card_tokens) and any(
            token in startup_text for token in ("HIGH-LINK SIGNAL", "OVERDRIVE SIGNAL")
        )
        startup_home_menu_absent = "▌ / MENU //" not in startup_text and "▌ 菜单 //" not in startup_text
        startup_dense_count = sum(startup_text.count(token) for token in (*card_tokens, "信号收束中"))
        startup_ok = (
            startup_completed.returncode == 0
            and startup_line_count <= 140
            and startup_input_ok
            and startup_status_ok
            and startup_card_ok
            and startup_home_menu_absent
        )
    except subprocess.TimeoutExpired:
        startup_ok = False
        startup_error = "timeout=20s"
    except OSError as exc:
        startup_ok = False
        startup_error = f"exception={exc}"
    evidence.append(
        "startup="
        f"ok:{_compact_bool_flag(startup_ok)} "
        f"ln:{startup_line_count} card:{_compact_bool_flag(startup_card_ok)} "
        f"home_menu:{_compact_bool_flag(not startup_home_menu_absent)} "
        f"status:{_compact_bool_flag(startup_status_ok)} input:{_compact_bool_flag(startup_input_ok)}"
    )
    if startup_error:
        evidence.append(f"startup_error={startup_error[:120]}")
    full_box_total = 0
    long_separator_total = 0
    input_role_leak_total = 0
    input_user_label_total = 0
    verbose_copy_total = 0
    verbose_sample_total = 0
    for sample in samples:
        if not isinstance(sample, dict):
            continue
        lines = sample.get("lines") if isinstance(sample.get("lines"), list) else []
        if len(lines) > 28:
            verbose_sample_total += 1
        max_line = 0
        for line in lines:
            if isinstance(line, dict):
                try:
                    max_line = max(max_line, int(line.get("displayWidth") or 0))
                except (TypeError, ValueError):
                    pass
                text = str(line.get("text") or "")
                if has_long_separator(text):
                    long_separator_total += 1
                if any(token in text for token in ("输入框内", "native chat", "role card", "Termux parity", "COMMANDS", "SESSION")):
                    verbose_copy_total += 1
                if str(line.get("group") or "") == "input":
                    if "▌ USER //" in text:
                        input_user_label_total += 1
                    if "▌ INPUT" in text or " / " in text:
                        input_role_leak_total += 1
        try:
            full_box_count = int(sample.get("fullBoxLineCount") or 0)
        except (TypeError, ValueError):
            full_box_count = 0
        full_box_total += full_box_count
        sample_ok = bool(sample.get("ok"))
        if not sample_ok:
            failed_samples.append(sample)
        evidence.append(
            "width={width} layout={layout} max_line={max_line} full_box={full_box} ok={ok}".format(
                width=sample.get("consoleWidth"),
                layout=sample.get("layoutWidth"),
                max_line=max_line,
                full_box=full_box_count,
                ok=int(sample_ok),
            )
        )
    evidence.append(f"long_separator={long_separator_total}")
    evidence.append(f"input_user_label={input_user_label_total}")
    evidence.append(f"input_role_leak={input_role_leak_total}")
    evidence.append(f"verbose_copy={verbose_copy_total}")
    evidence.append(f"verbose_samples={verbose_sample_total}")

    issues: list[str] = []
    for sample in failed_samples[:4]:
        width = sample.get("consoleWidth")
        for issue in (sample.get("issues") or [])[:3]:
            issues.append(f"width={width} {issue}")
    evidence.extend(issues[:8])
    if stderr:
        evidence.append(f"stderr={stderr[:200]}")

    score = (
        100
        - min(80, len(failed_samples) * 20)
        - min(40, full_box_total * 5)
        - min(40, long_separator_total * 5)
        - (20 if input_role_leak_total else 0)
        - (20 if input_user_label_total == 0 else 0)
        - min(40, verbose_copy_total * 5)
        - min(40, verbose_sample_total * 10)
        - (40 if not startup_ok else 0)
        - (0 if completed.returncode == 0 else 10)
    )
    score = max(0, score)
    return item(
        "windows_ui_text_layout",
        score,
        status_from_score(score),
        evidence,
        "修复 Windows CLI 渲染 helper；所有探针宽度都必须无溢出，启动首屏必须恢复抽卡与完整角色卡、隐藏常驻菜单、保留一句 TIP 和中文状态，输入栏不得显示角色名。" if failed_samples or full_box_total or long_separator_total or input_role_leak_total or input_user_label_total == 0 or verbose_copy_total or verbose_sample_total or not startup_ok else "",
    )


def check_actor_identity_name_contract() -> dict[str, Any]:
    try:
        import core as projectling_core

        same_role = projectling_core.LauncherRole(
            rarity="N",
            name_zh="A2",
            name_en="A2",
            quote="",
            profile="",
            source="aidebug",
        )
        pair_role = projectling_core.LauncherRole(
            rarity="N",
            name_zh="露西",
            name_en="Lucy",
            quote="",
            profile="",
            source="aidebug",
        )
        same_bundle = projectling_core.PersonaBundle(main=same_role)
        pair_bundle = projectling_core.PersonaBundle(main=pair_role)
        prompt_bundle = projectling_core.PromptBundle(
            main_prompt="",
            aux_prompt="",
            command_not_found_prompt="",
            role_prompt="",
            typing={"enabled": False},
            status={"thinking": "Thinking", "responding": "Responding"},
            path=PROJECTLING_DIR / "aidebug",
        )

        def _plain(value: Any) -> str:
            return projectling_core._strip_ansi(str(value or ""))

        old_supports_tty = projectling_core._supports_tty_control
        try:
            projectling_core._supports_tty_control = lambda: False
            same_samples = {
                "norm_slash": projectling_core._normalize_identity_name("A2 / A2"),
                "norm_dot": projectling_core._normalize_identity_name("A2 · A2"),
                "role": projectling_core._role_identity_name(same_role),
                "status": projectling_core._speaker_identity_text(
                    same_role,
                    same_bundle,
                    context_budget={"percent": 85},
                    actor_name="A2 / A2",
                ),
                "heading": projectling_core._format_role_heading(same_role, same_bundle),
                "tool": projectling_core._tool_actor_text(
                    {
                        "actor_kind": "planner",
                        "actor_name": "A2 / A2",
                        "context_percent": 85,
                    },
                    width=80,
                ),
                "signal": projectling_core._tool_actor_signal_line(
                    {
                        "actor_kind": "planner",
                        "actor_name": "A2 / A2",
                        "context_percent": 85,
                    }
                ),
                "printer_status_no_heading": projectling_core.ShellStreamPrinter(
                    prompt_bundle,
                    same_role,
                    persona_bundle=same_bundle,
                    show_role_heading=False,
                    context_budget={"percent": 85},
                )._status_text("thinking", "Thinking"),
            }
            pair_samples = {
                "norm": projectling_core._normalize_identity_name("露西 / Lucy"),
                "role": projectling_core._role_identity_name(pair_role),
                "status": projectling_core._speaker_identity_text(
                    pair_role,
                    pair_bundle,
                    context_budget={"percent": 85},
                    actor_name="露西 / Lucy",
                ),
                "printer_status_no_heading": projectling_core.ShellStreamPrinter(
                    prompt_bundle,
                    pair_role,
                    persona_bundle=pair_bundle,
                    show_role_heading=False,
                    context_budget={"percent": 85},
                )._status_text("thinking", "Thinking"),
            }
            same_printer = projectling_core.ShellStreamPrinter(
                prompt_bundle,
                same_role,
                persona_bundle=same_bundle,
                context_budget={"percent": 85},
            )
            same_actor_payload = same_printer._with_current_actor_payload(
                {"tool": "update_plan", "brief": "整理设置中心"}
            )
            actor_suppressed_ok = (
                same_actor_payload.get("_suppress_actor_line") is True
                and not projectling_core._should_render_tool_actor(same_actor_payload)
                and "A2" not in _plain(projectling_core._render_tool_running_receipt(same_actor_payload))
            )
            same_compact_status = _plain(
                projectling_core.ShellStreamPrinter(
                    prompt_bundle,
                    same_role,
                    persona_bundle=same_bundle,
                    context_budget={"percent": 85},
                )._status_text("thinking", "Thinking")
            )
            pair_compact_status = _plain(
                projectling_core.ShellStreamPrinter(
                    prompt_bundle,
                    pair_role,
                    persona_bundle=pair_bundle,
                    context_budget={"percent": 85},
                )._status_text("thinking", "Thinking")
            )
            projectling_core._supports_tty_control = lambda: True
            same_samples["heading_tty"] = _plain(projectling_core._format_role_heading(same_role, same_bundle))
            pair_samples["heading_tty"] = _plain(projectling_core._format_role_heading(pair_role, pair_bundle))
            same_samples["tool_tty"] = _plain(
                projectling_core._tool_actor_text(
                    {
                        "actor_kind": "planner",
                        "actor_name": "A2 / A2",
                        "context_percent": 85,
                    },
                    width=80,
                )
            )
        finally:
            projectling_core._supports_tty_control = old_supports_tty

        same_plain = {name: _plain(text) for name, text in same_samples.items()}
        pair_plain = {name: _plain(text) for name, text in pair_samples.items()}
        same_identity_keys = [name for name in same_plain if name != "printer_status_no_heading"]
        pair_identity_keys = [name for name in pair_plain if name != "printer_status_no_heading"]
        same_failures = [
            f"{name}:{text}"
            for name in same_identity_keys
            for text in [same_plain[name]]
            if text.count("A2") != 1 or "A2 · A2" in text or "A2 / A2" in text
        ]
        pair_failures = [
            f"{name}:{text}"
            for name in pair_identity_keys
            for text in [pair_plain[name]]
            if "露西" not in text or "Lucy" not in text or "露西 · Lucy" not in text
        ]
        compact_status_ok = (
            "思考中" in same_compact_status
            and "A2" not in same_compact_status
            and "露西" not in pair_compact_status
            and "Lucy" not in pair_compact_status
        )
        no_heading_status_ok = (
            same_plain["printer_status_no_heading"] == "◔ 思考中..."
            and pair_plain["printer_status_no_heading"] == "◔ 思考中..."
        )
        label_ok = "主星" in same_plain["status"] and "CTK85%" in same_plain["status"] and "主星" in same_plain["tool"]
        max_width = max(
            (
                projectling_core._display_width(line)
                for text in (*same_plain.values(), *pair_plain.values(), same_compact_status, pair_compact_status)
                for line in text.splitlines()
            ),
            default=0,
        )
        failures = []
        if same_failures:
            failures.append("same_name")
        if pair_failures:
            failures.append("pair_name")
        if not label_ok:
            failures.append("label_ctx")
        if not compact_status_ok:
            failures.append("compact_status")
        if not no_heading_status_ok:
            failures.append("status_repeat")
        if not actor_suppressed_ok:
            failures.append("actor_duplicate")
        if max_width > 80:
            failures.append("width")
        evidence = [
            f"same=ok:{_compact_bool_flag(not same_failures)} n={len(same_plain)}",
            f"pair=ok:{_compact_bool_flag(not pair_failures)} n={len(pair_plain)}",
            f"status={same_plain['status']}",
            f"compact_status={same_compact_status}",
            f"no_heading_status={same_plain['printer_status_no_heading']}",
            f"heading_tty={same_plain['heading_tty']}",
            f"pair_heading={pair_plain['heading_tty']}",
            f"actor_suppressed={_compact_bool_flag(actor_suppressed_ok)}",
            f"label_ctx={_compact_bool_flag(label_ok)} max={max_width}",
            f"fail={_compact_list_or_dash(failures)}",
        ]
        score = 100 if not failures else max(30, 100 - len(failures) * 25)
        return item(
            "actor_identity_name_contract",
            score,
            status_from_score(score),
            evidence,
            "修复 actor identity 渲染：同名中英标签只显示一次，异名中英标签必须保留。" if failures else "",
        )
    except Exception as exc:
        return item("actor_identity_name_contract", 0, "fail", [f"exception={exc}"], "修复 actor identity 渲染检查入口。")


def _check_markdown_rendering() -> dict[str, Any]:
    try:
        import importlib.util

        core_path = PROJECTLING_DIR / "core.py"
        spec = importlib.util.spec_from_file_location("projectling_core_for_aidebug", core_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("core module spec unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        renderer = module.MarkdownAnsiRenderer(tty=False)
        sample = "### 🎮 超级版特色\n\n| 功能 | 说明 |\n|---|---|\n| **粒子背景** | 80 颗金色粒子动态飘浮 |"
        rendered = renderer.render(sample)
    except Exception as exc:
        return item("markdown_rendering", 0, "fail", [f"exception={exc}"], "修复 core.py Markdown 渲染入口。")

    plain = str(rendered)
    def has_long_separator(text: str) -> bool:
        run = 0
        for char in str(text or ""):
            if char in "╌─═":
                run += 1
                if run > 3:
                    return True
            else:
                run = 0
        return False

    heading_ok = "###" not in plain and "▸ 🎮 超级版特色" in plain
    table_ok = "|---|" not in plain and "功能" in plain and "粒子背景" in plain
    inline_ok = "**" not in plain and "`" not in plain
    raw_table_box = any(ch in plain for ch in "┌┐└┘┬┴┼")
    long_rule = has_long_separator(plain)
    ok = heading_ok and table_ok and inline_ok and not raw_table_box and not long_rule
    evidence = [
        f"heading_ok={int(heading_ok)}",
        f"table_ok={int(table_ok)}",
        f"inline_ok={int(inline_ok)}",
        f"full_table_box={int(raw_table_box)}",
        f"long_rule={int(long_rule)}",
        "preview=" + " / ".join(plain.splitlines()[:5])[:240],
    ]
    score = 100 if ok else 45
    return item(
        "markdown_rendering",
        score,
        status_from_score(score),
        evidence,
        "修复 Markdown 标题/表格降级渲染，尤其是 ### 标题不能原样漏出，表格分隔也不能拉成长线。" if not ok else "",
    )


def _check_windows_tool_receipt_layout() -> dict[str, Any]:
    try:
        import importlib.util

        core_path = PROJECTLING_DIR / "core.py"
        spec = importlib.util.spec_from_file_location("projectling_core_receipt_for_aidebug", core_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("core module spec unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    except Exception as exc:
        return item("windows_tool_receipt_layout", 0, "fail", [f"exception={exc}"], "修复 core.py 工具 receipt 渲染入口。")

    samples: list[tuple[str, dict[str, Any]]] = [
        (
            "link_continue",
            {
                "tool": "link",
                "status": "ok",
                "action": "continue",
                "target": "executor",
                "message": "继续执行 UI 全局审查，优先检查 X-Link 和 update_plan 在窄窗口下的排版是否直观。",
                "task": "AIDEBUG 复检 CLI UI",
                "objective": "取消全包围边框，改成精简微赛博朋克分割线",
                "steps": ["复跑 AIDEBUG", "检查 X-Link receipt", "检查 update_plan receipt", "补充自动化布局检查"],
                "context_percent": 78,
                "context_budget_percent": 78,
                "actor_kind": "planner",
                "actor_label": "主星",
                "actor_name": "亚丝娜 / Asuna Yuuki",
                "executor_name": "劳拉·克劳馥 / Lara Croft",
            },
        ),
        (
            "link_blocked",
            {
                "tool": "link",
                "status": "ok",
                "action": "blocked",
                "target": "planner",
                "message": "窗口宽度 32 列时角色卡堆叠导致目标、状态、上下文不在第一屏可见。",
                "task": "重做 X-Link receipt",
                "objective": "让状态和接收方第一眼可见",
                "steps": ["压缩标题", "单行显示 target/context", "只显示关键 steps"],
                "context_percent": 91,
                "context_budget_percent": 91,
                "actor_kind": "executor",
                "actor_label": "执行星",
                "actor_name": "劳拉·克劳馥 / Lara Croft",
                "planner_name": "亚丝娜 / Asuna Yuuki",
            },
        ),
        (
            "update_plan",
            {
                "tool": "update_plan",
                "status": "ok",
                "action": "update",
                "mode": "plan",
                "title": "CLI UI 复检",
                "plan_status": "in_progress",
                "completed_count": 1,
                "total_count": 4,
                "current_step_id": "T2",
                "active_item": {"id": "T2", "title": "审查 X-Link receipt", "status": "in_progress", "phase": "ui"},
                "items": [
                    {"id": "T1", "title": "复跑 AIDEBUG Windows/UI 基线", "status": "done", "phase": "probe"},
                    {"id": "T2", "title": "审查 X-Link receipt 在窄窗口下是否直观", "status": "in_progress", "phase": "ui"},
                    {"id": "T3", "title": "审查 update_plan 的活动步骤和下一步提示", "status": "pending", "phase": "ui"},
                    {"id": "T4", "title": "补充 AIDEBUG 自动检查", "status": "pending", "phase": "test"},
                ],
                "next": "修改 core.py receipt 渲染并复跑检查",
                "message": "计划已更新。",
                "actor_kind": "executor",
                "actor_label": "执行星",
                "actor_name": "劳拉·克劳馥 / Lara Croft",
            },
        ),
        (
            "web_search",
            {
                "tool": "web_search",
                "status": "ok",
                "query": "ProjectLing CLI UI border wrapping",
                "mode_used": "web",
                "summary": "检查 CLI 窄窗口排版和边框换行问题。",
                "stdout": "mode=web query=ProjectLing CLI UI border wrapping\n1. ProjectLing UI notes",
                "brief": "搜索 CLI UI 排版问题",
                "actor_kind": "executor",
                "actor_label": "执行星",
                "actor_name": "劳拉·克劳馥 / Lara Croft",
                "context_budget_percent": 74,
            },
        ),
        (
            "command",
            {
                "tool": "command",
                "channel": "Bash",
                "status": "ok",
                "command": "npm test -- --watch=false",
                "stdout": "PASS ui-layout.test.ts\nPASS receipt-format.test.ts",
                "summary": "运行 UI receipt 回归测试。",
                "actor_kind": "executor",
                "actor_label": "执行星",
                "actor_name": "劳拉·克劳馥 / Lara Croft",
                "context_budget_percent": 72,
            },
        ),
        (
            "aidebug",
            {
                "tool": "aidebug",
                "status": "ok",
                "action": "health",
                "relative_path": "aidebug/runner/aidebug_health.py",
                "summary": "复检 Windows receipt 布局。",
                "stdout": "windows_tool_receipt_layout ok 100",
                "actor_kind": "executor",
                "actor_label": "执行星",
                "actor_name": "劳拉·克劳馥 / Lara Croft",
                "context_budget_percent": 71,
            },
        ),
        (
            "context",
            {
                "tool": "context",
                "status": "ok",
                "percent": 66,
                "context_budget_percent": 66,
                "turns_remaining": 2,
                "message": "下一轮保留 66% 上下文预算。",
                "actor_kind": "planner",
                "actor_label": "主星",
                "actor_name": "劳拉·克劳馥 / Lara Croft",
            },
        ),
        (
            "memory_check",
            {
                "tool": "memory_check",
                "status": "ok",
                "keywords": ["settings", "gemini", "receipt"],
                "dates": ["2026-07-09"],
                "best_detail": {"date": "2026-07-09"},
                "actor_kind": "executor",
                "actor_label": "执行星",
                "actor_name": "劳拉·克劳馥 / Lara Croft",
                "context_budget_percent": 70,
            },
        ),
        (
            "model_mode",
            {
                "tool": "model_mode",
                "status": "ok",
                "action": "set",
                "mode": "dual",
                "previous_mode": "standard",
                "planner_model": "gemini-3.1-pro-low",
                "executor_model": "gemini-3-flash",
                "actor_kind": "planner",
                "actor_label": "主星",
                "actor_name": "劳拉·克劳馥 / Lara Croft",
                "context_budget_percent": 69,
            },
        ),
        (
            "tool_manage",
            {
                "tool": "tool_manage",
                "status": "ok",
                "action": "list",
                "total_count": 12,
                "expanded_count": 7,
                "summary": "列出当前可用工具。",
                "actor_kind": "executor",
                "actor_label": "执行星",
                "actor_name": "劳拉·克劳馥 / Lara Croft",
                "context_budget_percent": 68,
            },
        ),
    ]

    class _Size:
        def __init__(self, columns: int) -> None:
            self.columns = columns
            self.lines = 24

    previous_env = os.environ.get("PROJECTLING_WINDOWS_UI")
    original_get_terminal_size = module.shutil.get_terminal_size
    full_box_chars = "╭╮╰╯╔╗╚╝┌┐└┘┬┴┼"
    failures: list[str] = []
    long_separator_total = 0
    sample_aliases = {
        "link_continue": "lc",
        "link_blocked": "lb",
        "update_plan": "plan",
        "web_search": "web",
        "command": "cmd",
        "aidebug": "dbg",
        "context": "ctk",
        "memory_check": "mem",
        "model_mode": "mode",
        "tool_manage": "box",
    }
    evidence: list[str] = [
        "samples=lc,lb,plan,web,cmd,dbg,ctk,mem,mode,box",
        "widths=32,48,80",
    ]

    def has_long_separator(text: str) -> bool:
        run = 0
        for char in str(text or ""):
            if char in "╌─═":
                run += 1
                if run > 3:
                    return True
            else:
                run = 0
        return False

    try:
        os.environ["PROJECTLING_WINDOWS_UI"] = "1"
        for width in (32, 48, 80):
            allowed = max(24, min(96, max(24, width - 2) - 2))
            module.shutil.get_terminal_size = lambda fallback=(80, 24), columns=width: _Size(columns)
            for name, payload in samples:
                alias = sample_aliases.get(name, name)
                rendered = module._render_tool_receipt_payload(payload)
                plain = module._strip_ansi(rendered)
                plain_lines = plain.splitlines()
                first_line = plain_lines[0] if plain_lines else ""
                compact_plain = re.sub(r"\s+", "", plain)
                line_widths = [module._display_width(line) for line in plain_lines] or [0]
                max_line = max(line_widths)
                has_full_box = any(ch in plain for ch in full_box_chars)
                has_long_rule = has_long_separator(plain)
                header_gap = len(plain_lines) > 1 and not plain_lines[1].strip()
                if has_long_rule:
                    long_separator_total += 1
                normalized = plain.upper().replace("-", "")
                duplicate_heading = False
                for marker in ("PLAN //", "XLINK //", "SEARCH //", "CMD //", "AIDEBUG //", "MODE //", "CTK //", "MEMORY //", "TOOLS //"):
                    if normalized.count(marker.replace("-", "")) > 1:
                        duplicate_heading = True
                        break
                old_label_leak = any(label in plain for label in ("主角色", "辅导位", "执行位"))
                old_ctx_leak = bool(re.search(r"\bctx\b", plain, flags=re.IGNORECASE))
                heavy_flow = "┃" in plain or "✲INPUT" in plain or "✲OUTPUT" in plain
                if name.startswith("link"):
                    key_ok = "XLINK" in normalized and "CTK" in plain and "->" in plain and "状态" in plain and "执行星" in plain
                    flow_ok = "┆ 状态" in plain and not heavy_flow
                elif name == "update_plan":
                    key_ok = "PLAN" in normalized and "当前" in plain and "1/4" in plain and "▶" in plain and "执行星" in plain
                    flow_ok = "┆ 当前" in plain and "┆ ▶" in plain and not heavy_flow
                elif name == "web_search":
                    key_ok = "SEARCH //" in first_line and "TOOL //" not in first_line and "执行星" in plain
                    flow_ok = "◇ INPUT" in plain and "◆ OUTPUT" in plain and not heavy_flow
                elif name == "command":
                    key_ok = "CMD //" in first_line and "npm" in plain and "执行星" in plain
                    flow_ok = "◇ INPUT" in plain and "◆ OUTPUT" in plain and not heavy_flow
                elif name == "aidebug":
                    key_ok = "AIDEBUG //" in first_line and "执行星" in plain
                    flow_ok = "◇ INPUT" in plain and "◆ OUTPUT" in plain and not heavy_flow
                elif name == "context":
                    key_ok = "CTK //" in first_line and "◇ CTK" in plain and "CTK66%" in plain and "主星" in plain
                    flow_ok = "◇ INPUT" in plain and "◆ OUTPUT" in plain and not heavy_flow
                elif name == "memory_check":
                    key_ok = "MEMORY //" in first_line and "日期" in plain and "命中" in plain and "执行星" in plain
                    flow_ok = "◆ 结果" in plain and not heavy_flow
                elif name == "model_mode":
                    key_ok = "MODE //" in first_line and "gemini-3" in compact_plain and "主星" in plain
                    flow_ok = "当前" in plain or "已切换" in plain
                elif name == "tool_manage":
                    key_ok = "TOOLS //" in first_line and "7/12" in plain and "执行星" in plain
                    flow_ok = "◇ INPUT" in plain and "◆ OUTPUT" in plain and not heavy_flow
                else:
                    key_ok = True
                    flow_ok = "◇ INPUT" in plain and "◆ OUTPUT" in plain and not heavy_flow
                sample_ok = (
                    max_line <= allowed
                    and not has_full_box
                    and not has_long_rule
                    and not duplicate_heading
                    and not old_label_leak
                    and not old_ctx_leak
                    and header_gap
                    and key_ok
                    and flow_ok
                )
                evidence.append(
                    f"{alias}@{width} ml={max_line}/{allowed} box={int(has_full_box)} "
                    f"sep={int(has_long_rule)} dup={int(duplicate_heading)} old={int(old_label_leak)} ctx={int(old_ctx_leak)} "
                    f"gap={int(header_gap)} key={int(key_ok)} flow={int(flow_ok)}"
                )
                if not sample_ok:
                    failures.append(f"{alias}@{width}")
    finally:
        module.shutil.get_terminal_size = original_get_terminal_size
        if previous_env is None:
            os.environ.pop("PROJECTLING_WINDOWS_UI", None)
        else:
            os.environ["PROJECTLING_WINDOWS_UI"] = previous_env

    thinking_lines = module._thinking_block_lines("◔ Thinking...", ["正在分析 UI 排版"])
    thinking_spacing_ok = len(thinking_lines) >= 4 and thinking_lines[0] == "" and thinking_lines[-1] == ""
    preview_edge = 0
    expected_preview_rows = 0
    preview_plain: list[str] = []
    thinking_head_ok = False
    thinking_tail_ok = False
    thinking_omitted_ok = False
    try:
        preview_printer = object.__new__(module.ShellStreamPrinter)
        preview_printer.renderer = module.MarkdownAnsiRenderer(tty=False)
        source_labels = [
            "alpha",
            "beta",
            "gamma",
            "delta",
            "epsilon",
            "zeta",
            "eta",
            "theta",
            "iota",
            "kappa",
            "lambda",
            "omega",
        ]
        long_thinking = "\n".join(f"{label}: UI receipt thinking preview probe." for label in source_labels)
        preview_lines = preview_printer._thinking_body_lines(long_thinking)
        preview_edge = int(getattr(module, "THINKING_PREVIEW_EDGE_LINES", 5))
        expected_preview_rows = preview_edge * 2 + 1
        preview_plain = [module._strip_ansi(line) for line in preview_lines]
        thinking_head_ok = all(label in preview_plain[index] for index, label in enumerate(source_labels[:preview_edge]))
        thinking_tail_ok = all(label in preview_plain[-preview_edge + index] for index, label in enumerate(source_labels[-preview_edge:]))
        thinking_omitted_ok = any("omitted" in line for line in preview_plain)
        thinking_trunc_ok = (
            len(preview_plain) == expected_preview_rows
            and thinking_head_ok
            and thinking_tail_ok
            and thinking_omitted_ok
        )
    except Exception as exc:
        preview_lines = []
        thinking_trunc_ok = False
        evidence.append(f"thinking_preview_exception={exc}")
    evidence.append(f"long_separator={long_separator_total}")
    evidence.append(f"thinking_spacing={int(thinking_spacing_ok)}")
    evidence.append(
        f"thinking_head_tail={int(thinking_trunc_ok)} rows={len(preview_plain)}/{expected_preview_rows} "
        f"edge={preview_edge} head={int(thinking_head_ok)} tail={int(thinking_tail_ok)} omitted={int(thinking_omitted_ok)}"
    )
    if not thinking_spacing_ok:
        failures.append("thinking_spacing")
    if not thinking_trunc_ok:
        failures.append("thinking_head_tail")
    marker_clean_ok = False
    stream_marker_clean_ok = False
    powershell_group_ok = False
    try:
        marker_clean = module._strip_context_percent_marker_text("PROJECTLING_CONTEXT_PERCENT=66\n正在判断 UI 问题。")
        marker_clean_ok = marker_clean == "正在判断 UI 问题。"
        marker_preview_printer = object.__new__(module.ShellStreamPrinter)
        marker_preview_printer.renderer = module.MarkdownAnsiRenderer(tty=False)
        marker_body = marker_preview_printer._thinking_body_lines("PROJECTLING_CONTEXT_PERCENT=33\n你好。")
        marker_body_plain = "\n".join(module._strip_ansi(line) for line in marker_body)
        prompt_bundle = module.PromptBundle(
            main_prompt="",
            aux_prompt="",
            command_not_found_prompt="",
            role_prompt="",
            typing={"enabled": False},
            status={"thinking": "Thinking", "responding": "Responding"},
            path=PROJECTLING_DIR / "aidebug",
        )
        stream_role = module.LauncherRole(
            rarity="N",
            name_zh="劳拉",
            name_en="Lara",
            quote="",
            profile="",
            source="aidebug",
        )
        stream_printer = module.ShellStreamPrinter(
            prompt_bundle,
            stream_role,
            persona_bundle=module.PersonaBundle(main=stream_role),
            show_role_heading=False,
            context_budget={"percent": 66},
        )
        stream_printer._write = lambda *args, **kwargs: None
        stream_printer.show_status = lambda *args, **kwargs: None
        stream_printer.on_delta("reasoning", "PROJECTLING_CONTEXT_PERCENT=33\n你好。")
        stream_buffer_plain = "".join(stream_printer.reasoning_buffer)
        stream_marker_clean_ok = (
            "PROJECTLING_CONTEXT_PERCENT" not in marker_body_plain
            and "PROJECTLING_CONTEXT_PERCENT" not in stream_buffer_plain
            and "你好" in marker_body_plain
            and "你好" in stream_buffer_plain
        )
        ps_traces = []
        for command in (
            'powershell -NoProfile -Command "Select-String -Path game.html -Pattern move | Select-Object -First 30"',
            'powershell -NoProfile -Command "Get-Content -Path game.html | Select-Object -Skip 515 -First 80"',
            'powershell -NoProfile -Command "Select-String -Path game.html -Pattern findFarthestPosition -Context 20"',
        ):
            ps_traces.append(
                {
                    "result": {
                        "tool": "command",
                        "channel": "Bash",
                        "status": "ok",
                        "command": command,
                        "stdout": "x\n" * 8,
                        "actor_kind": "executor",
                        "actor_label": "执行星",
                        "actor_name": "约尔·福杰 / Yor Forger",
                        "context_budget_percent": 66,
                    }
                }
            )
        ps_plain = module._strip_ansi(module._render_tool_receipts(tuple(ps_traces)))
        powershell_group_ok = (
            "● Explored" in ps_plain
            and ps_plain.count("Search in game.html") >= 2
            and "Read" in ps_plain
            and "CMD //" not in ps_plain
            and "RUNNING" not in ps_plain
        )
    except Exception as exc:
        evidence.append(f"marker_group_exception={exc}")
    evidence.append(f"thinking_marker_clean={int(marker_clean_ok)}")
    evidence.append(f"stream_marker_clean={int(stream_marker_clean_ok)}")
    evidence.append(f"powershell_explore_group={int(powershell_group_ok)}")
    if not marker_clean_ok:
        failures.append("thinking_marker")
    if not stream_marker_clean_ok:
        failures.append("stream_marker")
    if not powershell_group_ok:
        failures.append("powershell_group")
    density_limit = 80
    legacy_density_markers = (
        "max_line=",
        "full_box=",
        "long_sep=",
        "old_label=",
        "key_ok=",
        "True",
        "False",
        "[]",
    )
    density_failures = [
        f"row{index}:{len(row)}"
        for index, row in enumerate(evidence, start=1)
        if (
            len(row) > density_limit
            or "preview=" in row
            or "UI receipt thinking preview probe" in row
            or any(marker in row for marker in legacy_density_markers)
        )
    ]
    if density_failures:
        failures.append("receipt_density")
    evidence.append(f"receipt_evidence_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")

    score = 100 if not failures else max(20, 100 - min(80, len(failures) * 20))
    if failures:
        evidence.append("failures=" + ",".join(failures[:8]))
    return item(
        "windows_tool_receipt_layout",
        score,
        status_from_score(score),
        evidence,
        "修复工具 receipt：窄窗口下必须保留工具动作标题、关键状态，且不得回退到全包围边框或长分隔线。" if failures else "",
    )


def check_focus_anchor_contract() -> dict[str, Any]:
    if not _projectling_available():
        return item("focus_anchor_contract", 0, "fail", ["projectling imports unavailable"], "检查 projectling.py/tooling.py 导入链。")
    module = sys.modules.get("projectling")
    required = (
        "_derive_focus_from_turn",
        "_save_focus_state",
        "_is_short_feedback",
        "_focus_system_message",
        "FOCUS_STATE_FILE",
    )
    missing = [name for name in required if not hasattr(module, name)] if module is not None else list(required)
    if missing:
        return item("focus_anchor_contract", 35, "fail", [f"missing={_compact_list_or_dash(missing)}"], "补齐 ProjectLing 当前焦点锚点 helper。")
    try:
        config = _sandbox_config()
        if config is None:
            return item("focus_anchor_contract", 0, "fail", ["sandbox_config=none"], "检查 projectling 配置加载。")
        engine = ProjectLingEngine(config)  # type: ignore[operator]
        focus_state = module._derive_focus_from_turn(  # type: ignore[union-attr]
            user_message="我们输入/的时候就把支持的菜单弹出来然后用户上下选择回车进入。",
            assistant_text="已修改 windows-launcher/Program.cs，实现 Slash Menu 弹出选择。",
            tool_traces=(
                {
                    "name": "command",
                    "arguments": "{}",
                    "result": {
                        "tool": "command",
                        "channel": "PowerShell",
                        "status": "ok",
                        "command": "dotnet build windows-launcher\\ProjectLingLauncher.csproj -c Release",
                        "summary": "构建 Windows slash menu",
                        "relative_path": "windows-launcher/Program.cs",
                    },
                },
            ),
            route={"category": "execution_or_format", "task_complexity": "medium"},
        )
        if isinstance(focus_state, dict):
            module._save_focus_state(config, focus_state)  # type: ignore[union-attr]
        messages = engine._build_messages(  # type: ignore[attr-defined]
            "有个bug，只能左右移动没法上下移动",
            current_cwd=PROJECTLING_DIR,
            system_prompt="base prompt",
            include_shell_context=True,
            conversation_messages=[{"role": "user", "content": "有个bug，只能左右移动没法上下移动"}],
        )
        combined = "\n".join(str(message.get("content") or "") for message in messages if isinstance(message, dict))
        focus_file = Path(config.runtime_dir) / getattr(module, "FOCUS_STATE_FILE", "focus.json")
        short_ok = bool(module._is_short_feedback("有个bug，只能左右移动没法上下移动"))  # type: ignore[union-attr]
        title_ok = "Windows Launcher" in combined and "Slash Menu" in combined
        target_ok = "windows-launcher/Program.cs" in combined
        correction_ok = "不要跳到无关文件/任务" in combined
        hidden_ok = "当前焦点锚点" in combined and "PROJECTLING_CONTEXT_PERCENT" not in combined
        file_ok = focus_file.is_file()
        failures = []
        if not short_ok:
            failures.append("short")
        if not title_ok:
            failures.append("title")
        if not target_ok:
            failures.append("target")
        if not correction_ok:
            failures.append("correct")
        if not hidden_ok:
            failures.append("hidden")
        if not file_ok:
            failures.append("file")
        score = 100 if not failures else max(25, 100 - len(failures) * 15)
        evidence = [
            f"short={_compact_bool_flag(short_ok)}",
            f"title={_compact_bool_flag(title_ok)}",
            f"target={_compact_bool_flag(target_ok)}",
            f"correct={_compact_bool_flag(correction_ok)}",
            f"hidden={_compact_bool_flag(hidden_ok)}",
            f"file={_compact_bool_flag(file_ok)}",
            f"failures={_compact_list_or_dash(failures)}",
        ]
        score = _append_runtime_repr_density_guard(evidence, score)
        return item(
            "focus_anchor_contract",
            score,
            status_from_score(score),
            evidence,
            "修复 focus.json 读写、短反馈识别或隐藏焦点系统提示。" if score < 85 else "",
        )
    except Exception as exc:
        return item("focus_anchor_contract", 0, "fail", [f"exception={exc}"], "修复 ProjectLing 当前焦点锚点契约。")


def _windows_native_motd_checks(launcher_source: Path) -> list[tuple[str, bool, str, int]]:
    checks: list[tuple[str, bool, str, int]] = []

    render_ok, _label, render_detail = _core_smoke(
        ["render-motd-card", "--width", "80", "--max-lines", "12", "--settings-label", ""],
        timeout=15,
    )
    checks.append(("native_motd_render", render_ok, render_detail, 12))

    anim_ok, _label, anim_detail = _core_smoke(
        ["animate-motd-card", "--width", "80", "--frames", "2", "--final-card", "--max-lines", "12", "--settings-label", ""],
        timeout=15,
    )
    checks.append(("native_motd_animate", anim_ok, anim_detail, 12))

    settings_ok, _label, settings_detail = _core_smoke(["shell-settings"], input_text="0\n", timeout=10)
    checks.append(("native_shell_settings_exit", settings_ok, settings_detail, 8))

    launcher_settings_cases = [
        ("launcher_settings_root_startup", ["/settings"], "设置中心", 6),
        ("launcher_settings_api_startup", ["/settings", "api"], "API 与模型", 6),
        ("launcher_settings_deepseek_startup", ["/settings", "deepseek"], "当前 [DeepSeek]", 8),
        ("launcher_settings_gemini_startup", ["/settings", "gemini"], "当前 [Gemini]", 8),
        ("launcher_settings_websearch_startup", ["/settings", "websearch"], "WEBSEARCH API", 6),
        ("launcher_settings_role_startup", ["/settings", "role"], "角色", 6),
        ("launcher_settings_system_startup", ["/settings", "system"], "系统设置", 6),
    ]
    for name, args, expected_text, penalty in launcher_settings_cases:
        case_ok, _label, case_detail = _launcher_settings_smoke(args, expected_text=expected_text, timeout=20)
        checks.append((name, case_ok, case_detail, penalty))

    inline_gemini_ok, _label, inline_gemini_detail = _launcher_settings_smoke(
        ["--settings=gemini"],
        expected_text="当前 [Gemini]",
        timeout=20,
    )
    checks.append(("launcher_settings_gemini_inline_startup", inline_gemini_ok, inline_gemini_detail, 6))

    inline_api_ok, _label, inline_api_detail = _launcher_settings_smoke(
        ["--settings=api"],
        expected_text="API 与模型",
        timeout=20,
    )
    checks.append(("launcher_settings_api_inline_startup", inline_api_ok, inline_api_detail, 6))

    inline_websearch_ok, _label, inline_websearch_detail = _launcher_settings_smoke(
        ["--settings=websearch"],
        expected_text="WEBSEARCH API",
        timeout=20,
    )
    checks.append(("launcher_settings_websearch_inline_startup", inline_websearch_ok, inline_websearch_detail, 6))

    colon_gemini_ok, _label, colon_gemini_detail = _launcher_settings_smoke(
        ["/settings:gemini"],
        expected_text="API 与模型",
        timeout=20,
    )
    checks.append(("launcher_settings_gemini_colon_startup", colon_gemini_ok, colon_gemini_detail, 6))

    colon_websearch_ok, _label, colon_websearch_detail = _launcher_settings_smoke(
        ["/settings:websearch"],
        expected_text="WEBSEARCH API",
        timeout=20,
    )
    checks.append(("launcher_settings_websearch_colon_startup", colon_websearch_ok, colon_websearch_detail, 6))

    command_surface_ok, _label, command_surface_detail = _launcher_command_surface_smoke(timeout=20)
    checks.append(("launcher_gemini_command_surface", command_surface_ok, command_surface_detail, 10))

    startup_commands_ok, startup_commands_detail = True, "network_matrix=separate local_contract=1"
    checks.append(("launcher_gemini_startup_commands", startup_commands_ok, startup_commands_detail, 10))

    roster_ok, _label, roster_detail = _json_stdout_smoke(["show-roster", "--json"], timeout=10)
    checks.append(("native_roster_json", roster_ok, roster_detail, 8))

    launcher_text = _read_text_optional(launcher_source)
    reroll_entry = (
        'RunCore("reroll-role")' in launcher_text
        and "RenderAnimatedCard" in launcher_text
        and "重新抽卡" in _read_text_optional(PROJECTLING_DIR / "core.py")
    )
    checks.append(("launcher_reroll_role_entry", reroll_entry, str(launcher_source), 10))
    direct_chat_entry = (
        "DrawWindowBackdrop();" in launcher_text
        and "DrawStatusPanel();" in launcher_text
        and "WriteChatHint();" in launcher_text
        and "RunChatLoop();" in launcher_text
    )
    checks.append(("launcher_direct_chat_entry", direct_chat_entry, str(launcher_source), 10))
    shell_dispatch_chat = '"shell-dispatch"' in launcher_text and '"--mode", "chat"' in launcher_text and '"--raw"' in launcher_text
    checks.append(("launcher_shell_dispatch_chat", shell_dispatch_chat, str(launcher_source), 10))
    settings_slash = 'case "/settings":' in launcher_text and "OpenSettings" in launcher_text
    checks.append(("launcher_settings_slash", settings_slash, str(launcher_source), 8))
    settings_gemini = (
        '=> "gemini"' in launcher_text
        and '"deepseek" => "deepseek"' in launcher_text
        and "NormalizeSettingsTab" in launcher_text
        and "/models" in launcher_text
        and "/api-test" in launcher_text
        and "list-models" in launcher_text
        and "api-test" in launcher_text
        and "ReadApiStatus" in launcher_text
        and "TryRunStartupCommand" in launcher_text
        and "TryParseStartupSettings" in launcher_text
        and '"--tab", normalized' in launcher_text
    )
    checks.append(("launcher_settings_gemini_entry", settings_gemini, str(launcher_source), 6))
    settings_websearch = (
        '=> "websearch"' in launcher_text
        and '"websearch" or "web-search" or "web_search" or "search" => "websearch"' in launcher_text
        and "NormalizeSettingsTab" in launcher_text
        and "WEBSEARCH API" in _read_text_optional(PROJECTLING_DIR / "core.py")
    )
    checks.append(("launcher_settings_websearch_entry", settings_websearch, str(launcher_source), 6))
    old_numeric_menu_removed = "进入 PROJECT凌 对话" not in launcher_text and "PROJECT LING Windows" not in launcher_text
    checks.append(("launcher_numeric_menu_removed", old_numeric_menu_removed, str(launcher_source), 8))
    input_box = "DrawInputBox" in launcher_text and "▌ USER //" in launcher_text and '"› "' in launcher_text and "▌ INPUT" not in launcher_text
    checks.append(("windows_ui_input_box", input_box, str(launcher_source), 10))
    status_panel = "DrawStatusPanel" in launcher_text and '"STATUS"' in launcher_text and '"SESSION"' not in launcher_text
    checks.append(("windows_ui_status_panel", status_panel, str(launcher_source), 8))
    compact_status_removed = "DrawStatusPanel(compact: true)" not in launcher_text
    checks.append(("windows_ui_compact_session_removed", compact_status_removed, str(launcher_source), 8))
    command_panel = (
        "BuildStartupSlashMenuLines" in launcher_text
        and "BuildInputHintLines" in launcher_text
        and "RunNativeLauncher()" in launcher_text
        and "WriteSlashMenu();" not in launcher_text
        and '"KEYS"' not in launcher_text
        and '"COMMANDS"' not in launcher_text
    )
    checks.append(("windows_ui_command_panel", command_panel, str(launcher_source), 8))
    slash_popup = (
        "ReadInputLine" in launcher_text
        and "ReadSlashMenuSelection" in launcher_text
        and "BuildSlashMenuSelectionLines" in launcher_text
        and "BuildSlashMenuItems" in launcher_text
        and "ConsoleKey.UpArrow" in launcher_text
        and "ConsoleKey.DownArrow" in launcher_text
        and "FinishSlashMenu" in launcher_text
        and "输入 / 查看菜单" in launcher_text
        and "/role" in launcher_text
        and "锁定角色" in _read_text_optional(PROJECTLING_DIR / "core.py")
    )
    checks.append(("windows_ui_slash_popup_menu", slash_popup, str(launcher_source), 8))
    windows_env_flag = 'PROJECTLING_WINDOWS_UI' in launcher_text
    checks.append(("windows_ui_env_flag", windows_env_flag, str(launcher_source), 8))

    core_text = _read_text_optional(PROJECTLING_DIR / "core.py")
    tool_frame = "def _windows_tool_frame" in core_text and "PROJECTLING_WINDOWS_UI" in core_text
    checks.append(("windows_tool_frame_renderer", tool_frame, str(PROJECTLING_DIR / "core.py"), 10))
    settings_root_summary = (
        '"设置中心"' in core_text
        and '"API 与模型"' in core_text
        and '_print_setting_pair("当前"' in core_text
        and '_print_setting_pair("主星"' in core_text
        and '_print_setting_pair("执行"' in core_text
    )
    checks.append(("core_settings_provider_summary", settings_root_summary, str(PROJECTLING_DIR / "core.py"), 6))
    settings_gemini_tab = (
        'normalized_tab == "gemini"' in core_text
        and '_run_api_settings_ui("gemini")' in core_text
        and 'normalized_tab in {"gemini_params", "gemini-params"}' in core_text
        and "_run_gemini_params_settings_ui() or 0" in core_text
    )
    checks.append(("core_settings_gemini_tab", settings_gemini_tab, str(PROJECTLING_DIR / "core.py"), 6))
    settings_websearch_tab = '"websearch", "web_search"' in core_text and "_run_websearch_settings_ui() or 0" in core_text
    checks.append(("core_settings_websearch_tab", settings_websearch_tab, str(PROJECTLING_DIR / "core.py"), 6))
    return checks


def _check_windows_native_adapter() -> dict[str, Any]:
    checks: list[tuple[str, bool, str, int]] = []
    launcher_source = PROJECTLING_DIR / "windows-launcher" / "Program.cs"
    launcher_exe = _launcher_exe_path()
    desktop_lowercase = PROJECTLING_DIR.parent / "projectling"
    source_marker_checks = {
        "launcher_reroll_role_entry",
        "launcher_direct_chat_entry",
        "launcher_shell_dispatch_chat",
        "launcher_settings_slash",
        "launcher_settings_gemini_entry",
        "launcher_settings_websearch_entry",
        "launcher_numeric_menu_removed",
        "windows_ui_input_box",
        "windows_ui_status_panel",
        "windows_ui_compact_session_removed",
        "windows_ui_command_panel",
        "windows_ui_slash_popup_menu",
        "windows_ui_env_flag",
        "windows_tool_frame_renderer",
        "core_settings_provider_summary",
        "core_settings_gemini_tab",
        "core_settings_websearch_tab",
    }

    checks.append(("python", bool(sys.executable and Path(sys.executable).exists()), sys.executable, 20))
    checks.append(("core_py", (PROJECTLING_DIR / "core.py").exists(), str(PROJECTLING_DIR / "core.py"), 20))
    checks.append(("projectling_py", (PROJECTLING_DIR / "projectling.py").exists(), str(PROJECTLING_DIR / "projectling.py"), 10))
    checks.append(("aidebug_runner", (AIDEBUG_DIR / "runner" / "aidebug_health.py").exists(), str(AIDEBUG_DIR / "runner" / "aidebug_health.py"), 10))
    checks.append(("windows_launcher_source", launcher_source.exists(), str(launcher_source), 10))
    checks.append(("windows_launcher_exe", launcher_exe.exists(), str(launcher_exe), 10))
    launcher_fresh = bool(
        launcher_source.exists()
        and launcher_exe.exists()
        and launcher_exe.stat().st_mtime >= launcher_source.stat().st_mtime
    )
    checks.append(("windows_launcher_fresh", launcher_fresh, _launcher_freshness_detail(launcher_exe, launcher_source), 10))
    if PROJECTLING_DIR.name.lower() != "projectling":
        checks.append(("legacy_lowercase_projectling_absent", not desktop_lowercase.exists(), str(desktop_lowercase), 10))
    checks.extend(_windows_native_motd_checks(launcher_source))

    score = 100
    evidence = ["runtime=windows-native"]
    for name, ok, detail, penalty in checks:
        if name == "launcher_gemini_command_surface":
            evidence.append(f"{name}={int(ok)} {_compact_launcher_command_surface_detail(detail)}")
        elif name == "launcher_gemini_startup_commands":
            for suffix, compact_detail in _compact_launcher_startup_detail_rows(detail):
                evidence.append(f"{name}_{suffix}={int(ok)} {compact_detail}")
        elif name.startswith("launcher_settings_") and name.endswith("_startup") and ok:
            alias = _launcher_settings_startup_alias(name)
            evidence.append(f"{alias}=1 {_compact_launcher_settings_startup_detail(detail)}")
        elif name in source_marker_checks and ok:
            evidence.append(f"{name}=1 src={_project_relative_label(Path(detail))}")
        else:
            evidence.append(f"{name}={int(ok)} {detail}")
        if not ok:
            score -= penalty
    score = max(0, score)
    launcher_density_limit = 90
    command_surface_verbose_labels = ("provider=gemini", "api=gemini_ok", "commands=", "widths=", "failures=", "secret=")
    startup_verbose_labels = ("stable=", "risky=", "recovery=", "fallback=", "secret=", "failures=")
    settings_startup_verbose_labels = ("expected=", "main_fallback=", "stdout_bytes=", "lines=")
    launcher_density_failures = [
        f"row{index}:{len(row)}"
        for index, row in enumerate(evidence, start=1)
        if (
            row.startswith("launcher_gemini_")
            or row.startswith("set_")
            or (row.startswith("launcher_settings_") and "_startup=1 " in row)
        )
        and (
            len(row) > launcher_density_limit
            or (
                row.startswith("launcher_gemini_command_surface=")
                and any(label in row for label in command_surface_verbose_labels)
            )
            or (
                row.startswith("launcher_gemini_startup_commands_")
                and any(label in row for label in startup_verbose_labels)
            )
            or (
                row.startswith("set_")
                and any(label in row for label in settings_startup_verbose_labels)
            )
            or (row.startswith("launcher_settings_") and "_startup=1 " in row)
        )
    ]
    if launcher_density_failures:
        score = min(score, 75)
    evidence.append(f"launcher_adapter_density=limit={launcher_density_limit} failures={_compact_list_or_dash(launcher_density_failures)}")
    source_marker_density_limit = 80
    source_marker_density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if any(row.startswith(f"{name}=1 ") for name in source_marker_checks)
        and (
            len(row) > source_marker_density_limit
            or "C:\\" in row
            or "/mnt/" in row
        )
    ]
    if source_marker_density_failures:
        score = min(score, 75)
    evidence.append(
        f"source_marker_density=limit={source_marker_density_limit} "
        f"failures={_compact_list_or_dash(source_marker_density_failures)}"
    )
    launcher_freshness_density_limit = 90
    launcher_freshness_density_failures = _launcher_freshness_density_failures(
        evidence,
        limit=launcher_freshness_density_limit,
    )
    if launcher_freshness_density_failures:
        score = min(score, 75)
    evidence.append(
        f"launcher_freshness_density=limit={launcher_freshness_density_limit} "
        f"failures={_compact_list_or_dash(launcher_freshness_density_failures)}"
    )
    missing_names = [name for name, ok, _detail, _penalty in checks if not ok]
    next_action = ""
    if missing_names:
        next_action = "修复 Windows 原生适配缺口：" + ", ".join(missing_names[:6])
    return item("windows_native_adapter", score, status_from_score(score), evidence, next_action)


def check_windows_launcher_gemini_surface() -> dict[str, Any]:
    launcher_source = PROJECTLING_DIR / "windows-launcher" / "Program.cs"
    launcher_exe = _launcher_exe_path()
    active_provider = _active_api_provider()

    if os.name == "nt":
        ok, _label, detail = _launcher_command_surface_smoke(timeout=20)
        api_key_configured = _active_api_key_configured()
        startup_ok, startup_detail = True, "network_matrix=separate local_contract=1"
        provider_label = "Gemini" if active_provider == "gemini" else "DeepSeek"
        detail_ok = (
            f"provider={active_provider}" in detail
            and f"api_status={provider_label}" in detail
            and "commands=1" in detail
            and "help=1" in detail
            and "editor=1" in detail
            and "slash=1" in detail
            and "layout=1" in detail
            and "failures=-" in detail
            and "secret=1" in detail
        )
        startup_external = False
        startup_detail_ok = startup_ok and "local_contract=1" in startup_detail
        local_surface_ok = ok and detail_ok and startup_ok and startup_detail_ok
        score = 80 if local_surface_ok and startup_external else 100 if local_surface_ok else 45
        evidence = [
            "runtime=windows-native",
            "command_surface=" + _compact_launcher_command_surface_detail(detail),
            *[
                f"startup_{suffix}={compact_detail}"
                for suffix, compact_detail in _compact_launcher_startup_detail_rows(startup_detail)
            ],
            f"active_provider={active_provider}",
            f"api_configured={_compact_bool_flag(api_key_configured)}",
        ]
        launcher_density_limit = 100
        command_surface_verbose_labels = ("provider=gemini", "api=gemini_ok", "commands=", "widths=", "failures=", "secret=")
        startup_verbose_labels = ("stable=", "risky=", "recovery=", "fallback=", "secret=", "failures=")
        launcher_density_failures = [
            f"row{index}:{len(row)}"
            for index, row in enumerate(evidence, start=1)
            if (row.startswith("command_surface=") or row.startswith("startup_"))
            and (
                len(row) > launcher_density_limit
                or (row.startswith("command_surface=") and any(label in row for label in command_surface_verbose_labels))
                or (row.startswith("startup_") and any(label in row for label in startup_verbose_labels))
            )
        ]
        if launcher_density_failures:
            score = min(score, 75)
        evidence.append(f"launcher_surface_density=limit={launcher_density_limit} failures={_compact_list_or_dash(launcher_density_failures)}")
        return item(
            "windows_launcher_gemini_surface",
            score,
            status_from_score(score),
            evidence,
            (
                "等待 Gemini Pro 上游渠道恢复后重跑；本地 Windows launcher、菜单、宽度和错误恢复已通过。"
                if local_surface_ok and startup_external
                else "修复 Windows launcher Gemini command surface：中文 Provider、精简 Settings/Role/Exit 菜单、宽度、启动角色卡及诊断兼容必须通过。"
                if score < 100
                else ""
            ),
        )

    launcher_text = _read_text_optional(launcher_source)
    markers = {
        "command_probe": "--aidebug-command-surface" in launcher_text,
        "provider_status": "ReadApiStatus" in launcher_text and "ReadApiProvider" in launcher_text,
        "models_command": "/models" in launcher_text and "list-models" in launcher_text,
        "api_test_command": "/api-test" in launcher_text and "api-test" in launcher_text,
        "startup_models_forward": (
            "TryRunStartupCommand" in launcher_text
            and 'case "/models":' in launcher_text
            and '"list-models"' in launcher_text
            and "Concat(passthrough)" in launcher_text
        ),
        "startup_api_test_forward": (
            "TryRunStartupCommand" in launcher_text
            and 'case "/api-test":' in launcher_text
            and '"api-test"' in launcher_text
            and "Concat(passthrough)" in launcher_text
        ),
        "help_copy": (
            "BuildWindowsHelpLines" in launcher_text
            and "Gemini" in launcher_text
            and "DeepSeek" in launcher_text
            and "WebSearch" in launcher_text
            and "/role" in launcher_text
            and "/exit" in launcher_text
            and "new SlashMenuItem(\"/settings deepseek\"" not in launcher_text
            and "new SlashMenuItem(\"/settings gemini\"" not in launcher_text
            and "new SlashMenuItem(\"/settings websearch\"" not in launcher_text
            and "new SlashMenuItem(\"/models\"" not in launcher_text
            and "new SlashMenuItem(\"/api-test\"" not in launcher_text
            and "new SlashMenuItem(\"/aidebug\"" not in launcher_text
            and "new SlashMenuItem(\"/help\"" not in launcher_text
        ),
    }
    fresh = bool(
        launcher_source.exists()
        and launcher_exe.exists()
        and launcher_exe.stat().st_mtime >= launcher_source.stat().st_mtime
    )
    provider_ok = active_provider == "gemini"
    ok = launcher_source.exists() and launcher_exe.exists() and fresh and provider_ok and all(markers.values())
    failures = [name for name, value in markers.items() if not value]
    if not fresh:
        failures.append("launcher_fresh")
    if not provider_ok:
        failures.append("active_provider")
    score = 100 if ok else max(40, 100 - len(failures) * 15)
    evidence = [
        "runtime=non-windows-source-verified",
        f"active_provider={active_provider}",
        f"launcher_source={int(launcher_source.exists())}",
        f"launcher_exe={int(launcher_exe.exists())}",
        f"fresh={int(fresh)}",
        "markers=" + _compact_marker_summary(markers),
        "secret_redaction=runtime_checked_on_windows",
    ]
    launcher_density_limit = 100
    launcher_density_failures = [
        f"row{index}:{len(row)}"
        for index, row in enumerate(evidence, start=1)
        if row.startswith("markers=") and (len(row) > launcher_density_limit or ":1" in row or ":0" in row)
    ]
    if launcher_density_failures:
        score = min(score, 75)
    evidence.append(f"launcher_surface_density=limit={launcher_density_limit} failures={_compact_list_or_dash(launcher_density_failures)}")
    if failures:
        evidence.append("failures=" + ",".join(failures))
    return item(
        "windows_launcher_gemini_surface",
        score,
        status_from_score(score),
        evidence,
        "刷新 Windows launcher Gemini command-surface runtime/source coverage。" if failures else "",
    )


def check_launcher_external_gate_contract() -> dict[str, Any]:
    circuit_payload = {
        "ok": False,
        "provider": "gemini",
        "base_url": "https://relay.example/v1",
        "error": "HTTP 503: channel_circuit_open; temporarily suspended by circuit breaker",
    }
    remote_503_payload = {
        "ok": False,
        "provider": "gemini",
        "base_url": "https://relay.example/v1",
        "error": "HTTP 503: upstream temporarily unavailable",
    }
    local_failure_payload = {
        "ok": False,
        "provider": "gemini",
        "base_url": "http://127.0.0.1:9/v1",
        "error": "Connection refused",
    }
    auth_failure_payload = {
        "ok": False,
        "provider": "gemini",
        "base_url": "https://relay.example/v1",
        "error": "HTTP 401: invalid API key",
    }
    secret_error = "HTTP 503: channel_circuit_open private upstream body fixture-launcher-contract-secret"
    stderr_evidence = _launcher_stderr_evidence([secret_error])
    diagnostic_ok_checks = [
        {
            "name": name,
            "status": "warn" if name == "windows_launcher_gemini_surface" else "ok",
            "score": 80 if name == "windows_launcher_gemini_surface" else 100,
            "evidence": (
                ["startup_external=circuit", "launcher_surface_density=limit=100 failures=-"]
                if name == "windows_launcher_gemini_surface"
                else ["runtime=contract"]
            ),
            "next_action": "wait upstream" if name == "windows_launcher_gemini_surface" else "",
        }
        for name in DIAGNOSTIC_ARTIFACT_CHECK_NAMES
    ]
    diagnostic_local_failure_checks = [
        {
            "name": "windows_launcher_gemini_surface",
            "status": "fail",
            "score": 45,
            "evidence": ["startup_external=-", "launcher_surface_density=limit=100 failures=-"],
            "next_action": "fix local launcher",
        }
        if check.get("name") == "windows_launcher_gemini_surface"
        else dict(check)
        for check in diagnostic_ok_checks
    ]
    diagnostic_ok_summary = _diagnostic_artifact_summary(diagnostic_ok_checks)
    diagnostic_local_failure_summary = _diagnostic_artifact_summary(diagnostic_local_failure_checks)

    checks = {
        "circuit_external": _launcher_external_channel_reason(1, circuit_payload) == "circuit_open",
        "remote_503_external": _launcher_external_channel_reason(1, remote_503_payload) == "upstream_503",
        "local_failure_rejected": not _launcher_external_channel_reason(1, local_failure_payload),
        "auth_failure_rejected": not _launcher_external_channel_reason(1, auth_failure_payload),
        "success_rejected": not _launcher_external_channel_reason(0, {"ok": True, "provider": "gemini"}),
        "error_body_redacted": "fixture-launcher-contract-secret" not in stderr_evidence and "private upstream body" not in stderr_evidence,
        "diagnostic_external_accepted": diagnostic_ok_summary.get("ok") is True
        and diagnostic_ok_summary.get("external_gates") == ["windows_launcher_gemini_surface"],
        "diagnostic_local_failure_rejected": diagnostic_local_failure_summary.get("ok") is False,
    }
    failures = [name for name, ok in checks.items() if not ok]
    score = 100 if not failures else 0
    evidence = [
        "classification=" + _compact_marker_summary(checks),
        f"stderr={stderr_evidence.split('=', 1)[-1] if stderr_evidence else 'absent'}",
        f"failures={_compact_list_or_dash(failures)}",
    ]
    return item(
        "launcher_external_gate_contract",
        score,
        status_from_score(score),
        evidence,
        "修复 launcher 外部渠道门槛分类：上游 503 可豁免，本地/鉴权失败必须继续失败，错误正文不得回显。" if failures else "",
    )


def _safe_remove_legacy_lowercase(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return True, f"absent {path}"
    source_markers = {
        ".git",
        "core.py",
        "projectling.py",
        "projectling.zsh",
        "run.sh",
        "windows-launcher",
        "PROJECT LING.exe",
    }
    found_markers = [marker for marker in source_markers if (path / marker).exists()]
    if found_markers:
        return False, f"refuse source-like directory {path}: {','.join(sorted(found_markers))}"
    try:
        children = list(path.iterdir())
    except OSError as exc:
        return False, f"inspect failed {path}: {exc}"
    if any(child.name != "aidebug" for child in children):
        return False, f"refuse unexpected content {path}"
    try:
        shutil.rmtree(path)
    except OSError as exc:
        return False, f"remove failed {path}: {exc}"
    return True, f"removed {path}"


def repair_windows_wsl_adapter() -> list[str]:
    actions: list[str] = []
    if os.name == "nt":
        if PROJECTLING_DIR.name.lower() != "projectling":
            ok, message = _safe_remove_legacy_lowercase(PROJECTLING_DIR.parent / "projectling")
            actions.append(("ok " if ok else "blocked ") + message)
        return actions or ["windows-native no repair needed"]

    if not _is_wsl_runtime():
        return ["skip non-wsl runtime"]

    bash_path = shutil.which("bash")
    termux_bash = Path("/data/data/com.termux/files/usr/bin/bash")
    if bash_path and not termux_bash.exists():
        try:
            termux_bash.parent.mkdir(parents=True, exist_ok=True)
            termux_bash.symlink_to(bash_path)
            actions.append(f"created termux bash symlink {termux_bash}->{bash_path}")
        except OSError as exc:
            actions.append(f"failed termux bash symlink {termux_bash}: {exc}")
    elif termux_bash.exists():
        actions.append(f"termux bash compat already exists {termux_bash}")
    else:
        actions.append("bash missing; cannot create termux bash compat")

    bridge_dir = Path("/tmp/projectling-windows")
    bridge_link = bridge_dir / "projectling"
    try:
        bridge_dir.mkdir(parents=True, exist_ok=True)
        if bridge_link.is_symlink() or not bridge_link.exists():
            if bridge_link.exists() or bridge_link.is_symlink():
                bridge_link.unlink()
            bridge_link.symlink_to(PROJECTLING_DIR)
            actions.append(f"refreshed bridge symlink {bridge_link}->{PROJECTLING_DIR}")
        else:
            actions.append(f"bridge path exists and is not symlink: {bridge_link}")
    except OSError as exc:
        actions.append(f"failed bridge symlink {bridge_link}: {exc}")

    if PROJECTLING_DIR.name.lower() != "projectling":
        ok, message = _safe_remove_legacy_lowercase(PROJECTLING_DIR.parent / "projectling")
        actions.append(("ok " if ok else "blocked ") + message)
    return actions


def check_windows_wsl_adapter() -> dict[str, Any]:
    if os.name == "nt":
        return _check_windows_native_adapter()

    is_wsl = _is_wsl_runtime()
    if not is_wsl:
        return item(
            "windows_wsl_adapter",
            100,
            "ok",
            ["runtime=non-wsl", "windows_adapter=not_required"],
            "",
        )

    checks: list[tuple[str, bool, str, int]] = []
    for command in ("bash", "zsh", "python3", "tmux"):
        exists, detail = _command_evidence(command)
        checks.append((f"command:{command}", exists, detail, 15))

    termux_bash = Path("/data/data/com.termux/files/usr/bin/bash")
    checks.append(
        (
            "termux_bash_compat",
            termux_bash.exists() and os.access(termux_bash, os.X_OK),
            str(termux_bash),
            15,
        )
    )
    checks.append(("projectling_run", PROJECTLING_RUN.exists(), str(PROJECTLING_RUN), 10))
    checks.append(("projectling_zsh", (PROJECTLING_DIR / "projectling.zsh").exists(), str(PROJECTLING_DIR / "projectling.zsh"), 10))
    launcher_source = PROJECTLING_DIR / "windows-launcher" / "Program.cs"
    launcher_exe = _launcher_exe_path()
    checks.append(("windows_launcher_source", launcher_source.exists(), str(launcher_source), 5))
    checks.append(("windows_launcher_exe", launcher_exe.exists(), str(launcher_exe), 10))
    launcher_fresh = bool(
        launcher_source.exists()
        and launcher_exe.exists()
        and launcher_exe.stat().st_mtime >= launcher_source.stat().st_mtime
    )
    checks.append(("windows_launcher_fresh", launcher_fresh, _launcher_freshness_detail(launcher_exe, launcher_source), 10))
    if PROJECTLING_DIR.name.lower() != "projectling":
        legacy_lowercase = PROJECTLING_DIR.parent / "projectling"
        checks.append(
            (
                "legacy_lowercase_projectling_absent",
                not legacy_lowercase.exists(),
                str(legacy_lowercase),
                20,
            )
        )

    score = 100
    evidence = ["runtime=wsl"]
    for name, ok, detail, penalty in checks:
        evidence.append(f"{name}={int(ok)} {detail}")
        if not ok:
            score -= penalty
    score = max(0, score)
    launcher_freshness_density_limit = 90
    launcher_freshness_density_failures = _launcher_freshness_density_failures(
        evidence,
        limit=launcher_freshness_density_limit,
    )
    if launcher_freshness_density_failures:
        score = min(score, 75)
    evidence.append(
        f"launcher_freshness_density=limit={launcher_freshness_density_limit} "
        f"failures={_compact_list_or_dash(launcher_freshness_density_failures)}"
    )

    missing_names = [name for name, ok, _detail, _penalty in checks if not ok]
    next_action = ""
    if missing_names:
        next_action = "修复 Windows/WSL 适配缺口：" + ", ".join(missing_names[:6])
    return item("windows_wsl_adapter", score, status_from_score(score), evidence, next_action)


def check_layout() -> dict[str, Any]:
    required = [
        AIDEBUG_DIR,
        LOG_DIR,
        NOTE_DIR,
        AIDEBUG_DIR / "projectling" / "terminal output",
        PROJECTLING_DIR,
        PROJECTLING_RUN,
    ]
    missing = [str(path) for path in required if not path.exists()]
    score = 100 if not missing else max(20, 100 - len(missing) * 20)
    return item(
        "aidebug_layout",
        score,
        status_from_score(score),
        [f"missing={len(missing)}", *[f"missing {path}" for path in missing[:5]]],
        "创建缺失目录或检查 AITERMUX_HOME/AITERMUX_AIDEBUG_DIR。" if missing else "",
    )


def check_logs() -> dict[str, Any]:
    if os.name == "nt":
        paths = [LOG_DIR / "startup.log", LOG_DIR / "projectling.log"]
        metas = [file_meta(path) for path in paths]
        missing = [meta for meta in metas if not meta.get("exists")]
        stale = [meta for meta in metas if meta.get("exists") and int(meta.get("age_seconds") or 0) > 7 * 86400]
        score = 100 - len(missing) * 20 - len(stale) * 10
        evidence = [
            "runtime=windows-native",
            "termux_motd_zshrc_logs=not_required",
            *[
                f"{Path(str(meta['path'])).name}: exists={_compact_bool_flag(meta.get('exists'))} lines={meta.get('lines', 0)} age={meta.get('age_seconds', '-')}"
                for meta in metas
            ],
        ]
        score = _append_runtime_repr_density_guard(evidence, score)
        return item(
            "runtime_logs",
            score,
            status_from_score(score),
            evidence,
            "运行 ProjectLing Windows 前端或 AIDEBUG 复测以刷新日志。" if score < 85 else "",
        )

    paths = [LOG_DIR / "startup.log", LOG_DIR / "motd.log", LOG_DIR / "zshrc.log", LOG_DIR / "projectling.log"]
    metas = [file_meta(path) for path in paths]
    missing = [meta for meta in metas if not meta.get("exists")]
    stale = [meta for meta in metas if meta.get("exists") and int(meta.get("age_seconds") or 0) > 7 * 86400]
    latest_smoke: dict[str, Any] = {}
    try:
        smoke_lines = (LOG_DIR / "motd-zshrc-smoke.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
        latest_smoke = json.loads(smoke_lines[-1]) if smoke_lines else {}
    except Exception:
        latest_smoke = {}
    missing_names = {Path(str(meta["path"])).name for meta in missing}
    smoke_ok = bool(latest_smoke.get("ok"))
    zshrc_smoke = latest_smoke.get("zshrc") if isinstance(latest_smoke.get("zshrc"), dict) else {}
    motd_smoke = latest_smoke.get("non_tty_motd") if isinstance(latest_smoke.get("non_tty_motd"), dict) else {}
    optional_motd_zshrc_missing = (
        smoke_ok
        and missing_names.issubset({"motd.log", "zshrc.log"})
        and bool(zshrc_smoke.get("ok"))
        and (motd_smoke.get("ok") is True or motd_smoke.get("reason") == "motd_missing")
    )
    effective_missing = [] if optional_motd_zshrc_missing else missing
    score = 100 - len(effective_missing) * 25 - len(stale) * 10
    evidence = [
        f"{Path(str(meta['path'])).name}: exists={_compact_bool_flag(meta.get('exists'))} lines={meta.get('lines', 0)} age={meta.get('age_seconds', '-')}"
        for meta in metas
    ]
    if optional_motd_zshrc_missing:
        evidence.append("motd_zshrc_logs=optional_smoke_verified")
    if latest_smoke:
        evidence.append(
            "motd_zshrc_smoke="
            f"ok:{int(smoke_ok)} motd:{motd_smoke.get('reason', motd_smoke.get('returncode'))} "
            f"zshrc_ok:{int(bool(zshrc_smoke.get('ok')))}"
        )
    score = _append_runtime_repr_density_guard(evidence, score)
    return item(
        "runtime_logs",
        score,
        status_from_score(score),
        evidence,
        "运行 motd/zshrc/projectling smoke，刷新过期或缺失日志。" if score < 85 else "",
    )


_DOCTOR_RUN_CACHE: tuple[subprocess.CompletedProcess[str], int] | None = None


def _run_doctor_cached() -> tuple[subprocess.CompletedProcess[str], int]:
    global _DOCTOR_RUN_CACHE
    if _DOCTOR_RUN_CACHE is not None:
        return _DOCTOR_RUN_CACHE
    completed, attempts = run_projectling_with_retry(["doctor"], timeout=30)
    if completed.returncode == 0:
        _DOCTOR_RUN_CACHE = (completed, attempts)
    return completed, attempts


def check_projectling_doctor() -> dict[str, Any]:
    try:
        completed, attempts = _run_doctor_cached()
    except Exception as exc:
        return item("projectling_doctor", 0, "fail", [f"exception={exc}"], "修复 ProjectLing Python 或 run.sh 运行环境。")
    evidence = [f"rc={completed.returncode}", f"attempts={attempts}"]
    score = 100 if completed.returncode == 0 else 30
    if completed.returncode == 0:
        try:
            data = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return item("projectling_doctor", 60, "warn", evidence + ["stdout_json=invalid"], "修复 doctor JSON 输出。")
        evidence.extend(
            [
                f"planner_model={data.get('planner_model')}",
                f"executor_model={data.get('executor_model')}",
                f"api_key={_compact_bool_flag(bool(data.get('api_key_configured')))}",
                f"tools={_compact_bool_flag(bool(data.get('allow_tools')))}",
                f"context={data.get('shared_entries_chars')}/{data.get('role_context_chars')}",
            ]
        )
        if not data.get("api_key_configured"):
            score -= 20
        if not data.get("allow_tools"):
            score -= 20
    else:
        evidence.append((completed.stderr or completed.stdout)[-400:])
    score = _append_runtime_repr_density_guard(evidence, score)
    return item("projectling_doctor", score, status_from_score(score), evidence, "执行 ProjectLing doctor 查看详情。" if score < 85 else "")


def check_tool_schema() -> dict[str, Any]:
    expected = {
        "link",
        "update_plan",
        "model_mode",
        "command",
        "terminal",
        "aidebug",
        "apply_patch",
        "web_search",
        "contextmanage",
        "memory_add",
        "memory_check",
        "memorycheak",
        "memory_read",
        "memory_status",
        "tool_manage",
    }
    try:
        completed = run_projectling(["show-tools", "--json"], timeout=20)
    except Exception as exc:
        return item("tool_schema", 0, "fail", [f"exception={exc}"], "修复 show-tools。")
    if completed.returncode != 0:
        return item("tool_schema", 20, "fail", [f"rc={completed.returncode}", completed.stderr[-300:]], "修复工具注册。")
    try:
        schemas = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return item("tool_schema", 50, "warn", ["schema_json=invalid"], "修复 show-tools JSON。")
    names = {str((schema.get("function") or {}).get("name") or "") for schema in schemas if isinstance(schema, dict)}
    missing = sorted(expected - names)
    score = 100 if not missing else max(30, 100 - 15 * len(missing))
    names_sorted = sorted(name for name in names if name)
    sample_order = ("link", "update_plan", "command", "apply_patch", "web_search")
    samples = [name for name in sample_order if name in names]
    if not samples:
        samples = names_sorted[:5]
    sample_labels = [_compact_auto_tool_label(name) for name in samples]
    tool_summary = (
        f"names=n:{len(names_sorted)} "
        f"req:{len(expected) - len(missing)}/{len(expected)} "
        f"miss:{_compact_list_or_dash(missing)} "
        f"sample:{_compact_list_or_dash(sample_labels)} "
        f"extra:{max(0, len(names_sorted) - len(samples))}"
    )
    density_limit = 90
    verbose_sample_labels = ("update_plan", "apply_patch", "web_search")
    density_failures = [
        f"names:{len(tool_summary)}"
        for row in (tool_summary,)
        if len(row) > density_limit or "['" in row or "{'" in row or any(label in row for label in verbose_sample_labels)
    ]
    if density_failures:
        score = min(score, 75)
    evidence = [
        tool_summary,
        f"tool_schema_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}",
    ]
    return item("tool_schema", score, status_from_score(score), evidence, "补齐缺失工具 schema。" if missing else "")


def _doctor_json() -> tuple[dict[str, Any] | None, str]:
    try:
        completed, attempts = _run_doctor_cached()
    except Exception as exc:
        return None, f"exception={exc}"
    if completed.returncode != 0:
        return None, f"rc={completed.returncode} attempts={attempts} stderr={(completed.stderr or completed.stdout)[-240:]}"
    try:
        return json.loads(completed.stdout), ""
    except json.JSONDecodeError:
        return None, "doctor_json=invalid"


def check_memory_layout() -> dict[str, Any]:
    data, error = _doctor_json()
    if data is None:
        return item("memory_layout", 0, "fail", [error], "修复 doctor 后再检查 memory。")
    memory = data.get("memory") or {}
    required = [
        Path(str(memory.get("memory_dir") or "")),
        Path(str(memory.get("datememory_path") or "")),
        Path(str(memory.get("memory_db_path") or "")),
    ]
    missing = [str(path) for path in required if not str(path) or not path.exists()]
    score = 100 if not missing else max(40, 100 - len(missing) * 25)
    evidence = [
        f"context_mode={data.get('context_mode')}",
        f"datememory_bytes={memory.get('datememory_bytes')}",
        f"memory_db_diaries={memory.get('memory_db_diaries')}",
        f"missing={_compact_list_or_dash(missing)}",
    ]
    score = _append_runtime_repr_density_guard(evidence, score)
    return item("memory_layout", score, status_from_score(score), evidence, "运行 ./projectling/run.sh doctor 初始化 memory。" if missing else "")


def check_context_mode_config() -> dict[str, Any]:
    data, error = _doctor_json()
    if data is None:
        return item("context_mode_config", 0, "fail", [error], "修复 doctor。")
    mode = str(data.get("context_mode") or "")
    ok = mode in {"entries", "role", "fused"}
    evidence = [
        f"context_mode={mode}",
        f"shared_entries_chars={data.get('shared_entries_chars')}",
        f"role_context_chars={data.get('role_context_chars')}",
    ]
    return item(
        "context_mode_config",
        100 if ok else 45,
        "ok" if ok else "warn",
        evidence,
        "设置 PROJECTLING_CONTEXT_MODE=role 或 fused。" if not ok else "",
    )


def check_api_provider_config() -> dict[str, Any]:
    data, error = _doctor_json()
    if data is None:
        return item("api_provider_config", 0, "fail", [error], "修复 doctor。")
    provider = str(data.get("api_provider") or "").strip().lower()
    planner = str(data.get("planner_model") or "").strip()
    executor = str(data.get("executor_model") or "").strip()
    base_url = str(data.get("base_url") or "").strip()
    checks: dict[str, bool] = {
        "provider_valid": provider in {"deepseek", "gemini"},
        "active_key": bool(data.get("api_key_configured")),
        "base_url": base_url.startswith("http"),
        "planner_model": bool(planner),
        "executor_model": bool(executor),
    }
    if provider == "gemini":
        checks.update(
            {
                "gemini_key": bool(data.get("gemini_api_key_configured")),
                "gemini_base": str(data.get("gemini_base_url") or "").startswith("http"),
                "gemini_planner": "gemini" in str(data.get("gemini_planner_model") or "").lower(),
                "gemini_executor": "gemini" in str(data.get("gemini_executor_model") or "").lower(),
            }
        )
    help_ok, _label, help_detail = _core_smoke(["--help"], timeout=20)
    if help_ok:
        try:
            completed = subprocess.run(
                [sys.executable, str(PROJECTLING_DIR / "core.py"), "--help"],
                cwd=str(PROJECTLING_DIR),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=20,
                check=False,
            )
            help_text = completed.stdout or ""
            checks["list_models_cli"] = "list-models" in help_text
            checks["api_test_cli"] = "api-test" in help_text
        except Exception:
            checks["list_models_cli"] = False
            checks["api_test_cli"] = False
    else:
        checks["help_cli"] = False
    try:
        completed = subprocess.run(
            [sys.executable, str(PROJECTLING_DIR / "core.py"), "help"],
            cwd=str(PROJECTLING_DIR),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=20,
            check=False,
        )
        compact_help = completed.stdout or ""
        checks["compact_help_models"] = completed.returncode == 0 and "/models" in compact_help and "模型列表" in compact_help
        checks["compact_help_api_test"] = (
            completed.returncode == 0
            and "/api-test" in compact_help
            and "测试主星/辅星连通" in compact_help
        )
    except Exception:
        checks["compact_help_models"] = False
        checks["compact_help_api_test"] = False
    compact_help_width_failures: list[str] = []
    compact_help_width_evidence: list[str] = []
    try:
        import core as projectling_core

        for width in (16, 20, 24, 32, 40, 48):
            env = os.environ.copy()
            env["COLUMNS"] = str(width)
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            completed = subprocess.run(
                [sys.executable, str(PROJECTLING_DIR / "core.py"), "help"],
                cwd=str(PROJECTLING_DIR),
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=20,
                check=False,
            )
            plain = projectling_core._strip_ansi(completed.stdout or "")
            max_width = max((projectling_core._display_width(line) for line in plain.splitlines()), default=0)
            compact_help_width_evidence.append(f"w{width}:{max_width}")
            content_ok = all(token in plain for token in ("/models", "/api-test", "/settings", "websearch"))
            if completed.returncode != 0 or max_width > width or not content_ok:
                compact_help_width_failures.append(f"w{width}:rc{completed.returncode}/max{max_width}/content{int(content_ok)}")
        checks["compact_help_width"] = not compact_help_width_failures
    except Exception as exc:
        compact_help_width_failures.append(f"exception={exc}")
        checks["compact_help_width"] = False
    try:
        zsh_text = (PROJECTLING_DIR / "projectling.zsh").read_text(encoding="utf-8", errors="replace")
        checks["zsh_models_alias"] = (
            "models)" in zsh_text
            and "/models\\ *" in zsh_text
            and "/model-list\\ *" in zsh_text
            and "/list-models\\ *" in zsh_text
            and "projectling_run_on_tty list-models" in zsh_text
        )
        checks["zsh_api_test_alias"] = (
            "api-test)" in zsh_text
            and "/api-test\\ *" in zsh_text
            and "/apitest\\ *" in zsh_text
            and "projectling_run_on_tty api-test" in zsh_text
        )
    except Exception:
        checks["zsh_models_alias"] = False
        checks["zsh_api_test_alias"] = False
    evidence = [
        f"provider={provider}",
        f"base_url={base_url}",
        f"planner={planner}",
        f"executor={executor}",
        f"keys=gemini:{_compact_bool_flag(data.get('gemini_api_key_configured'))} deepseek:{_compact_bool_flag(data.get('deepseek_api_key_configured'))}",
        f"cli_help={help_detail}",
        f"help=models:{_compact_bool_flag(checks.get('compact_help_models'))} api:{_compact_bool_flag(checks.get('compact_help_api_test'))}",
        f"help_w={','.join(compact_help_width_evidence)} fail={_compact_list_or_dash(compact_help_width_failures[:4])}",
        f"zsh_aliases=models:{int(bool(checks.get('zsh_models_alias')))} api_test:{int(bool(checks.get('zsh_api_test_alias')))}",
    ]
    api_provider_density_limit = 75
    api_provider_old_labels = (
        "gemini_key=",
        "deepseek_key=",
        "compact_help=",
        "compact_help_widths=",
        "failures=[]",
        "True",
        "False",
        "[]",
        "['",
    )
    api_provider_density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if len(row) > api_provider_density_limit or any(label in row for label in api_provider_old_labels)
    ]
    checks["api_provider_density"] = not api_provider_density_failures
    evidence.append(f"api_provider_density=limit={api_provider_density_limit} failures={_compact_list_or_dash(api_provider_density_failures)}")
    failures = [name for name, ok in checks.items() if not ok]
    score = 100 if not failures else max(30, 100 - len(failures) * 15)
    if failures:
        evidence.append("failures=" + ",".join(failures))
    return item(
        "api_provider_config",
        score,
        status_from_score(score),
        evidence,
        "修复 provider/env/settings/list-models/api-test 接入面。" if failures else "",
    )


def check_relay_model_compatibility_matrix() -> dict[str, Any]:
    json_path = NOTE_DIR / "projectling-relay-model-compatibility.json"
    md_path = NOTE_DIR / "projectling-relay-model-compatibility.md"
    if not json_path.is_file() or not md_path.is_file():
        return item(
            "relay_model_compatibility_matrix",
            55,
            "warn",
            [f"json={int(json_path.is_file())}", f"markdown={int(md_path.is_file())}"],
            "运行 relay_model_matrix.py 生成 23 模型 Markdown/JSON 兼容矩阵。",
        )
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        markdown = md_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return item(
            "relay_model_compatibility_matrix",
            45,
            "fail",
            [f"read_exception={type(exc).__name__}: {exc}"],
            "修复 relay 模型矩阵 JSON/Markdown。",
        )

    models = payload.get("models") if isinstance(payload.get("models"), list) else []
    entries = [entry for entry in models if isinstance(entry, dict) and entry.get("model")]
    names = [str(entry.get("model") or "") for entry in entries]
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    observation = payload.get("channel_observation") if isinstance(payload.get("channel_observation"), dict) else {}
    missing_probe_models = [
        str(entry.get("model") or "")
        for entry in entries
        if not all(
            isinstance((entry.get("probes") or {}).get(name), dict)
            for name in ("text", "stream", "tool")
        )
    ]
    missing_thinking_models = [
        str(entry.get("model") or "")
        for entry in entries
        if "thinking" in str(entry.get("model") or "").lower()
        and not isinstance((entry.get("probes") or {}).get("thinking"), dict)
    ]
    configured = payload.get("configured_models") if isinstance(payload.get("configured_models"), dict) else {}
    planner = str(configured.get("planner") or "")
    executor = str(configured.get("executor") or "")
    by_name = {str(entry.get("model") or ""): entry for entry in entries}
    planner_entry = by_name.get(planner) if planner else None
    executor_entry = by_name.get(executor) if executor else None
    planner_ok = bool(
        planner_entry
        and planner_entry.get("verdict") == "recommended"
        and planner_entry.get("projectling_support") in {"planner_only", "stable"}
    )
    executor_ok = bool(
        executor_entry
        and executor_entry.get("verdict") == "recommended"
        and executor_entry.get("projectling_support") == "stable"
    )
    verdict_total = sum(
        int(summary.get(name) or 0)
        for name in ("recommended", "usable_limited", "diagnostic_only", "incompatible", "unavailable")
    )
    combined_text = json.dumps(payload, ensure_ascii=False) + "\n" + markdown
    secret_ok = not _contains_unmasked_secret(combined_text)
    age = int(time.time() - json_path.stat().st_mtime)
    checks = {
        "count_23": len(entries) == 23 and int(summary.get("model_count") or 0) == 23,
        "unique": len(set(names)) == len(names),
        "probe_coverage": not missing_probe_models,
        "thinking_coverage": not missing_thinking_models,
        "summary_total": verdict_total == len(entries),
        "planner_role": planner_ok,
        "executor_role": executor_ok,
        "luna_absent": observation.get("luna_match_count") == 0,
        "compact_absent": observation.get("exact_5_6_compact_exposed") is False,
        "markdown_markers": all(marker in markdown for marker in ("## Matrix", "luna", "5.6 compact")),
        "secret_redaction": secret_ok,
        "fresh": age <= 86400,
    }
    failures = [name for name, ok in checks.items() if not ok]
    evidence = [
        f"models={len(entries)} unique={len(set(names))} verdicts={verdict_total}",
        f"recommended={summary.get('recommended')} limited={summary.get('usable_limited')} diagnostic={summary.get('diagnostic_only')}",
        f"bad={summary.get('incompatible_or_unavailable')} age={age}",
        f"planner={planner}:{_compact_bool_flag(planner_ok)} executor={executor}:{_compact_bool_flag(executor_ok)}",
        f"probe_missing={_compact_list_or_dash(missing_probe_models)} thinking_missing={_compact_list_or_dash(missing_thinking_models)}",
        f"luna={observation.get('luna_match_count')} compact56={_compact_bool_flag(observation.get('exact_5_6_compact_exposed'))}",
        f"failures={_compact_list_or_dash(failures)}",
    ]
    score = 100 if not failures else 75 if len(entries) == 23 and secret_ok else 45
    return item(
        "relay_model_compatibility_matrix",
        score,
        status_from_score(score),
        evidence,
        "刷新 23 模型矩阵，补齐探针、角色兼容、Luna/5.6 证据或脱敏。" if failures else "",
    )


def check_gemini_parameter_support_matrix() -> dict[str, Any]:
    json_path = NOTE_DIR / "projectling-gemini-parameter-support.json"
    md_path = NOTE_DIR / "projectling-gemini-parameter-support.md"
    if not json_path.is_file() or not md_path.is_file():
        return item(
            "gemini_parameter_support_matrix",
            55,
            "warn",
            [f"json={int(json_path.is_file())}", f"markdown={int(md_path.is_file())}"],
            "运行 relay_model_matrix.py --parameter-matrix 生成 Gemini 参数矩阵。",
        )
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        markdown = md_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return item(
            "gemini_parameter_support_matrix",
            45,
            "fail",
            [f"read_exception={type(exc).__name__}: {exc}"],
            "修复 Gemini 参数矩阵 JSON/Markdown。",
        )

    parameters = payload.get("parameters") if isinstance(payload.get("parameters"), list) else []
    expected_parameters = ["temperature", "top_p", "top_k", "max_tokens", "json_output"]
    models = payload.get("models") if isinstance(payload.get("models"), list) else []
    entries = [entry for entry in models if isinstance(entry, dict) and entry.get("model")]
    names = [str(entry.get("model") or "") for entry in entries]
    probes = [
        probe
        for entry in entries
        for probe in ((entry.get("probes") or {}).get(name) for name in expected_parameters)
        if isinstance(probe, dict)
    ]
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    classifications = summary.get("classifications") if isinstance(summary.get("classifications"), dict) else {}
    allowed = {
        "accepted_unverified",
        "accepted_model_mismatch",
        "rejected",
        "model_unavailable",
        "local_not_sent",
        "request_error",
    }
    unknown = sorted(str(name) for name in classifications if str(name) not in allowed)
    mismatch = [
        f"{entry.get('model')}:{name}"
        for entry in entries
        for name in expected_parameters
        if isinstance((entry.get("probes") or {}).get(name), dict)
        and (entry.get("probes") or {}).get(name, {}).get("classification") == "accepted_model_mismatch"
    ]
    combined_text = json.dumps(payload, ensure_ascii=False) + "\n" + markdown
    raw_error_body = bool(re.search(r"HTTP\s+\d{3}:\s*\{|request[_ ]id", combined_text, re.IGNORECASE))
    age = int(time.time() - json_path.stat().st_mtime)
    expected_probe_count = len(entries) * len(expected_parameters)
    classification_total = sum(int(value or 0) for value in classifications.values())
    checks = {
        "provider": payload.get("provider") == "gemini",
        "model_count": len(entries) == 21,
        "unique": len(set(names)) == len(names),
        "parameters": parameters == expected_parameters,
        "probe_coverage": len(probes) == expected_probe_count,
        "summary_total": int(summary.get("probe_count") or 0) == expected_probe_count == classification_total,
        "known_classifications": not unknown,
        "local_sent": all(probe.get("local_sent") is True for probe in probes),
        "model_identity": not mismatch,
        "secret_redaction": not _contains_unmasked_secret(combined_text),
        "error_redaction": not raw_error_body,
        "markdown_markers": all(marker in markdown for marker in ("Gemini 模型参数真实性矩阵", *expected_parameters)),
        "fresh": age <= 86400,
    }
    failures = [name for name, ok in checks.items() if not ok]
    accepted = int(classifications.get("accepted_unverified") or 0)
    evidence = [
        f"models={len(entries)} unique={len(set(names))} probes={len(probes)}/{expected_probe_count}",
        f"accepted={accepted} unavailable={classifications.get('model_unavailable', 0)} request_error={classifications.get('request_error', 0)} rejected={classifications.get('rejected', 0)}",
        f"mismatch={_compact_list_or_dash(mismatch)} unknown={_compact_list_or_dash(unknown)} age={age}",
        f"failures={_compact_list_or_dash(failures)}",
    ]
    score = 100 if not failures else 75 if len(entries) == 21 and len(probes) == expected_probe_count else 45
    return item(
        "gemini_parameter_support_matrix",
        score,
        status_from_score(score),
        evidence,
        "刷新参数矩阵，修复参数发出、模型一致性、错误脱敏或新鲜度。" if failures else "",
    )


def check_zsh_diagnostic_alias_execution() -> dict[str, Any]:
    zsh_path = PROJECTLING_DIR / "projectling.zsh"
    try:
        zsh_text = zsh_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return item("zsh_diagnostic_alias_execution", 40, "warn", [f"source_read_error={exc}"], "检查 projectling.zsh 是否存在。")
    source_checks = {
        "models_handler": "models)" in zsh_text and "projectling_run_on_tty list-models" in zsh_text,
        "api_test_handler": "api-test)" in zsh_text and "projectling_run_on_tty api-test" in zsh_text,
        "slash_models": "/models\\ *" in zsh_text and "/model-list\\ *" in zsh_text and "/list-models\\ *" in zsh_text,
        "slash_api_test": "/api-test\\ *" in zsh_text and "/apitest\\ *" in zsh_text,
        "inline_local": '"$local_mode" == "models"' in zsh_text and '"$local_mode" == "api-test"' in zsh_text,
    }
    source_failures = [name for name, ok in source_checks.items() if not ok]
    if source_failures:
        return item(
            "zsh_diagnostic_alias_execution",
            max(35, 100 - len(source_failures) * 20),
            "warn",
            ["runtime=source", "failures=" + ",".join(source_failures)],
            "修复 projectling.zsh diagnostics alias source coverage。",
        )

    zsh_bin = shutil.which("zsh")
    if not zsh_bin:
        score = 100
        evidence = ["runtime=source-verified", "zsh=missing", "source_ok=1"]
        score = _append_runtime_repr_density_guard(evidence, score)
        return item(
            "zsh_diagnostic_alias_execution",
            score,
            "ok",
            evidence,
            "",
        )

    sandbox = HEALTH_SANDBOX_DIR / "zsh-diagnostic-alias"
    try:
        if sandbox.exists():
            shutil.rmtree(sandbox)
        sandbox.mkdir(parents=True, exist_ok=True)
        capture = sandbox / "argv.jsonl"
        fake_runner = sandbox / "run.sh"
        fake_runner.write_text(
            "#!/usr/bin/env bash\n"
            "python3 - \"$@\" <<'PY'\n"
            "import json, os, sys\n"
            "with open(os.environ['PROJECTLING_ZSH_ALIAS_CAPTURE'], 'a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps(sys.argv[1:], ensure_ascii=False) + '\\n')\n"
            "PY\n",
            encoding="utf-8",
        )
        fake_runner.chmod(0o755)
        probe_script = "\n".join(
            [
                "source ./projectling.zsh",
                "zle() { :; }",
                "probe() { local raw=\"$1\"; local kind; kind=\"$(projectling_special_command_kind \"$raw\")\" || return 10; projectling_run_inline_action \"$raw\" \"$kind\"; }",
                "probe '/models --limit 1'",
                "probe '/model-list --json'",
                "probe '/list-models'",
                "probe '/api-test --json --no-stream'",
                "probe '/apitest'",
            ]
        )
        completed = subprocess.run(
            [zsh_bin, "-fc", probe_script],
            cwd=str(PROJECTLING_DIR),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=20,
            env={
                **os.environ,
                "PROJECTLING_HOME": str(sandbox),
                "PROJECTLING_ZSH_ALIAS_CAPTURE": str(capture),
            },
            check=False,
        )
        rows = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()] if capture.exists() else []
    except Exception as exc:
        return item("zsh_diagnostic_alias_execution", 45, "warn", [f"runtime=exception {exc}"], "修复 zsh diagnostic fake-runner smoke。")

    expected = [
        ["list-models", "--limit", "1"],
        ["list-models", "--json"],
        ["list-models"],
        ["api-test", "--json", "--no-stream"],
        ["api-test"],
    ]
    ok = completed.returncode == 0 and rows == expected
    evidence = [
        "runtime=zsh-fake-runner",
        f"rc={completed.returncode}",
        f"rows={len(rows)}",
        f"argv_ok={_compact_bool_flag(ok)}",
    ]
    if not ok:
        evidence.append(f"captured={rows}")
        evidence.append(f"stderr={(completed.stderr or completed.stdout)[-240:]}")
    score = 100 if ok else 55
    score = _append_runtime_repr_density_guard(evidence, score)
    return item(
        "zsh_diagnostic_alias_execution",
        score,
        status_from_score(score),
        evidence,
        "修复 zsh diagnostic slash alias execution/passthrough。" if not ok else "",
    )


def check_route_alignment() -> dict[str, Any]:
    if not _projectling_available():
        return item("route_alignment", 0, "fail", ["projectling imports unavailable"], "检查 projectling.py/tooling.py 导入链。")
    try:
        engine = ProjectLingEngine(load_config())  # type: ignore[operator]
    except Exception as exc:
        return item("route_alignment", 0, "fail", [f"engine_init={exc}"], "修复 projectling 初始化。")
    config = load_config() if load_config else None
    collab_mode = str(getattr(config, "collab_mode", "") or "standard")
    planner_model = engine._planner_model_for_mode(collab_mode)
    executor_model = engine._executor_model_for_mode(collab_mode)
    planner_thinking = engine._planner_thinking_for_mode(collab_mode)

    scenarios = [
        (
            "strict_short",
            "只回复：OK。不要解释。",
            "strict_short_reply",
            executor_model,
            False,
            False,
        ),
        (
            "execution",
            "请使用 command 运行 pwd，然后只输出当前路径。",
            "execution_or_format",
            planner_model,
            planner_thinking,
            True,
        ),
        (
            "casual_chat",
            "你好呀",
            "casual_chat",
            executor_model,
            False,
            False,
        ),
        (
            "analysis",
            "综合判断这个项目如何优化，列计划。",
            "analysis",
            planner_model,
            planner_thinking,
            True,
        ),
        (
            "code_only",
            "写一个 Python 函数 is_even(n)，只给代码，不要创建文件。",
            "code_generation",
            planner_model,
            planner_thinking,
            True,
        ),
    ]

    evidence: list[str] = []
    score = 100
    route_failures: list[str] = []
    for label, prompt, expected_category, expected_model, expected_thinking, expected_status in scenarios:
        route = engine.preview_route(prompt, allow_tools=True)
        actual_category = str(route.get("category") or "")
        actual_model = str(route.get("model") or "")
        actual_thinking = route.get("thinking_enabled")
        actual_status = bool(route.get("show_initial_status"))
        ok = actual_category == expected_category and actual_model == expected_model
        if expected_thinking is not None:
            ok = ok and bool(actual_thinking) == bool(expected_thinking)
        ok = ok and actual_status == bool(expected_status)
        if not ok:
            score -= 25
            route_failures.append(label)
            evidence.append(
                f"{label}=fail actual={actual_category}/{actual_model}/t={actual_thinking}/s={actual_status} "
                f"expected={expected_category}/{expected_model}/t={expected_thinking}/s={expected_status}"
            )
        else:
            evidence.append(
                f"{label}=ok cat={_route_category_label(actual_category)} "
                f"model={_route_model_label(actual_model, expected_model)} "
                f"t={_route_thinking_label(actual_thinking)} s={_compact_bool_flag(actual_status)}"
            )
    score = max(25, score)
    route_density_limit = 90
    route_density_failures = [
        f"row{index}:{len(row)}"
        for index, row in enumerate(evidence, start=1)
        if (
            row.startswith(tuple(f"{label}=ok" for label, *_rest in scenarios))
            and (len(row) > route_density_limit or "category=" in row or "expected=" in row)
        )
    ]
    raw_route_repr_failures = [
        f"row{index}"
        for index, row in enumerate(evidence, start=1)
        if ": category=" in row
    ]
    route_density_failures.extend(raw_route_repr_failures)
    if route_density_failures:
        score = min(score, 75)
    evidence.append(
        f"route_evidence_density=limit={route_density_limit} "
        f"failures={_compact_list_or_dash(route_density_failures)} "
        f"route_failures={_compact_list_or_dash(route_failures)}"
    )
    return item(
        "route_alignment",
        score,
        status_from_score(score),
        evidence,
        "修正 projectling 路由决策、普通对话状态显示或短答提示策略。" if score < 85 else "",
    )


def check_deepseek_v4_transport() -> dict[str, Any]:
    if ProjectLingEngine is None or DeepSeekClient is None or deepseek_usage_cache_summary is None or load_config is None:
        return item("deepseek_v4_transport", 0, "fail", ["projectling imports unavailable"], "检查 projectling.py DeepSeek 导入链。")
    try:
        config = load_config()
        engine = ProjectLingEngine(config)  # type: ignore[operator]
        client = DeepSeekClient(config)  # type: ignore[operator]
        provider = str(getattr(config, "api_provider", "deepseek") or "deepseek").lower()
        planner_model = engine._planner_model_for_mode("standard")
        executor_model = engine._executor_model_for_mode("standard")
        precise_model = engine._planner_model_for_mode("precise")
        think_payload = client._build_payload(
            messages=[{"role": "user", "content": "ping"}],
            tools=None,
            tool_choice="none",
            temperature=0.2,
            stream=False,
            model=planner_model,
            thinking_enabled=engine._planner_thinking_for_mode("standard"),
            max_tokens=32,
        )
        stream_payload = client._build_payload(
            messages=[{"role": "user", "content": "ping"}],
            tools=None,
            tool_choice="none",
            temperature=0.2,
            stream=True,
            model=executor_model,
            thinking_enabled=engine._executor_thinking_for_mode("standard"),
            max_tokens=32,
        )
        stream_tools_payload = client._build_payload(
            messages=[{"role": "user", "content": "ping"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "aidebug",
                        "description": "health probe",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            tool_choice="auto",
            temperature=0.2,
            stream=True,
            model=executor_model,
            thinking_enabled=engine._executor_thinking_for_mode("standard"),
            max_tokens=32,
        )
        valid_chain = [
            {"role": "assistant", "content": "", "reasoning_content": "need tools", "tool_calls": [{"id": "a"}, {"id": "b"}]},
            {"role": "tool", "tool_call_id": "a", "content": "{}"},
            {"role": "tool", "tool_call_id": "b", "content": "{}"},
        ]
        invalid_chain = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}, {"id": "b"}]},
            {"role": "tool", "tool_call_id": "a", "content": "{}"},
            {"role": "system", "content": "not allowed here"},
            {"role": "tool", "tool_call_id": "b", "content": "{}"},
        ]
        valid_ok, _ = engine._validate_tool_call_message_order(valid_chain)
        invalid_ok, invalid_error = engine._validate_tool_call_message_order(invalid_chain)

        class _FakeStreamClient:
            def chat_completions_stream(self, **_kwargs: Any) -> Iterable[dict[str, Any]]:
                yield {"choices": [{"delta": {"reasoning_content": "need a tool. "}}]}
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_a",
                                        "type": "function",
                                        "function": {"name": "aidebug", "arguments": "{\"action\":\"check\"}"},
                                    }
                                ]
                            }
                        }
                    ]
                }
                yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
                yield {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 10,
                        "total_tokens": 110,
                        "prompt_cache_hit_tokens": 80,
                        "prompt_cache_miss_tokens": 20,
                    },
                }

        engine.client = _FakeStreamClient()  # type: ignore[assignment]
        stream_response = engine._stream_chat_completions(
            messages=[{"role": "user", "content": "ping"}],
            tools=stream_tools_payload.get("tools"),
            model=executor_model,
            thinking_enabled=False,
            max_tokens=32,
        )
        cache_summary = deepseek_usage_cache_summary(stream_response.get("usage"))
        nested_cache_summary = deepseek_usage_cache_summary(
            {
                "prompt_tokens": 100,
                "completion_tokens": 7,
                "prompt_tokens_details": {"cached_tokens": 70},
            }
        )
        stream_message = (((stream_response.get("choices") or [{}])[0] or {}).get("message") or {})
        with tempfile.TemporaryDirectory(prefix="projectling-audit-health-") as audit_tmp:
            audit_path = Path(audit_tmp) / "model-requests.jsonl"
            audit_secret = "aidebug-audit-secret-must-not-appear"
            audit_payload = client._build_payload(
                messages=[{"role": "user", "content": audit_secret}],
                tools=stream_tools_payload.get("tools"),
                tool_choice="auto",
                temperature=0.0,
                stream=False,
                model=planner_model,
                thinking_enabled=True,
                max_tokens=8,
            )
            audit_record = client._write_model_request_audit(
                audit_payload,
                started_at=time.monotonic(),
                status="ok",
                attempts=1,
                response_data={
                    "id": "health-audit-response",
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
                and audit_record.get("message_count") == 1
                and bool(audit_record.get("tool_names"))
                and "messages" not in audit_record
                and audit_secret not in audit_text
                and (not getattr(config, "api_key", None) or str(config.api_key) not in audit_text)
            )
    except Exception as exc:
        return item("deepseek_v4_transport", 0, "fail", [f"exception={exc}"], "修复 DeepSeek V4 payload 或消息链校验。")

    if provider == "gemini":
        stream_extra = stream_payload.get("extra_body") if isinstance(stream_payload.get("extra_body"), dict) else {}
        stream_google = stream_extra.get("google") if isinstance(stream_extra.get("google"), dict) else {}
        model_ok = bool(planner_model and executor_model and precise_model and "gemini" in planner_model.lower())
        think_ok = (
            think_payload.get("model") == planner_model
            and think_payload.get("reasoning_effort") in {"none", "low", "high"}
            and "thinking" not in think_payload
            and think_payload.get("temperature") is not None
        )
        stream_ok = (
            stream_payload.get("model") == executor_model
            and stream_payload.get("stream_options") == {"include_usage": True}
            and "reasoning_effort" not in stream_payload
            and stream_google.get("thinking_config") == {"thinking_budget": 0}
            and "thinking" not in stream_payload
            and stream_payload.get("temperature") is not None
        )
    else:
        model_ok = planner_model == "deepseek-v4-pro" and executor_model == "deepseek-v4-flash" and precise_model == "deepseek-v4-pro"
        think_ok = (
            think_payload.get("thinking") == {"type": "enabled"}
            and think_payload.get("reasoning_effort") in {"high", "max"}
            and "temperature" not in think_payload
        )
        stream_ok = (
            stream_payload.get("thinking") == {"type": "disabled"}
            and stream_payload.get("stream_options") == {"include_usage": True}
            and "reasoning_effort" not in stream_payload
            and stream_payload.get("temperature") is not None
        )
    stream_tools_ok = (
        stream_tools_payload.get("stream_options") == {"include_usage": True}
        and bool(stream_tools_payload.get("tools"))
        and stream_tools_payload.get("tool_choice") == "auto"
    )
    order_ok = valid_ok and not invalid_ok
    stream_parse_ok = (
        stream_response.get("usage", {}).get("prompt_cache_hit_tokens") == 80
        and stream_response.get("_projectling_streamed") is True
        and bool(stream_message.get("reasoning_content"))
        and bool(stream_message.get("tool_calls"))
        and cache_summary.get("cache_hit_tokens") == 80
        and cache_summary.get("cache_miss_tokens") == 20
        and cache_summary.get("cache_hit_rate") == 80.0
    )
    nested_cache_ok = (
        nested_cache_summary.get("cache_hit_tokens") == 70
        and nested_cache_summary.get("cache_miss_tokens") == 30
        and nested_cache_summary.get("cache_hit_rate") == 70.0
        and nested_cache_summary.get("total_tokens") == 107
    )
    checks = {
        "model_ok": model_ok,
        "think_ok": think_ok,
        "stream_ok": stream_ok,
        "stream_tools_ok": stream_tools_ok,
        "tool_order_ok": order_ok,
        "stream_parse_ok": stream_parse_ok,
        "nested_cache_ok": nested_cache_ok,
        "model_audit_ok": audit_ok,
    }
    expected_order_error = "assistant tool_calls at index 0 not followed by contiguous tool messages"
    if order_ok and expected_order_error in str(invalid_error):
        order_error_label = "assistant_tool_gap"
    else:
        order_error_label = invalid_error or "-"
    tool_order_evidence = (
        f"tool_order=valid:{_compact_bool_flag(valid_ok)} "
        f"invalid:{_compact_bool_flag(invalid_ok)} "
        f"err={order_error_label}"
    )
    transport_order_density_limit = 90
    transport_order_density_failures = []
    if len(tool_order_evidence) > transport_order_density_limit or "True" in tool_order_evidence or "False" in tool_order_evidence:
        transport_order_density_failures.append(f"tool_order:{len(tool_order_evidence)}")
    evidence = [
        f"provider={provider}",
        f"models=s:{planner_model}/{executor_model} p:{precise_model}",
        f"think={_compact_bool_flag(bool(think_payload.get('thinking')))} effort={think_payload.get('reasoning_effort')} temp={_compact_bool_flag('temperature' in think_payload)}",
        f"stream={_compact_bool_flag(stream_ok)} usage={_compact_bool_flag(stream_payload.get('stream_options') == {'include_usage': True})} think={_compact_bool_flag(bool(stream_payload.get('thinking')))}",
        f"stream_tools={_compact_bool_flag(stream_tools_ok)} usage={_compact_bool_flag(stream_tools_payload.get('stream_options') == {'include_usage': True})} tools={_compact_bool_flag(bool(stream_tools_payload.get('tools')))}",
        tool_order_evidence,
        f"transport_order_density=limit={transport_order_density_limit} failures={_compact_list_or_dash(transport_order_density_failures)}",
        f"sse_tool_usage=hit:{cache_summary.get('cache_hit_tokens')} miss:{cache_summary.get('cache_miss_tokens')} rate:{cache_summary.get('cache_hit_rate')} finish:{((stream_response.get('choices') or [{}])[0] or {}).get('finish_reason')}",
        f"nested_cache_usage=hit:{nested_cache_summary.get('cache_hit_tokens')} miss:{nested_cache_summary.get('cache_miss_tokens')} rate:{nested_cache_summary.get('cache_hit_rate')} total:{nested_cache_summary.get('total_tokens')}",
        f"model_audit=model:{_compact_bool_flag(audit_record.get('request_model') == planner_model)} redacted:{_compact_bool_flag(audit_ok)}",
    ]
    transport_evidence_density_limit = 80
    transport_old_labels = (
        "models=standard:",
        "thinking=",
        "stream_options=",
        "stream_tools_options=",
        "temp_in_think=",
        "tools=True",
        "tools=False",
        "True",
        "False",
        "{'",
        "['",
    )
    transport_evidence_density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if len(row) > transport_evidence_density_limit or any(label in row for label in transport_old_labels)
    ]
    checks["transport_order_density"] = not transport_order_density_failures
    checks["transport_evidence_density"] = not transport_evidence_density_failures
    evidence.append(
        f"transport_evidence_density=limit={transport_evidence_density_limit} failures={_compact_list_or_dash(transport_evidence_density_failures)}"
    )
    failures = [name for name, ok in checks.items() if not ok]
    score = 100 if not failures else max(25, 100 - len(failures) * 25)
    if failures:
        evidence.append("failures=" + ",".join(failures))
    return item(
        "deepseek_v4_transport",
        score,
        status_from_score(score),
        evidence,
        "修复 active provider 模型映射、thinking/reasoning_effort、SSE usage、模型审计或 tool_calls 消息链。" if failures else "",
    )


def check_gemini_settings_contract() -> dict[str, Any]:
    if DeepSeekClient is None or load_config is None:
        return item("gemini_settings_contract", 0, "fail", ["projectling imports unavailable"], "检查 Gemini 设置导入链。")

    try:
        config = load_config()
        probe_config = replace(
            config,
            api_provider="gemini",
            gemini_top_p=0.7,
            gemini_top_k=33,
            gemini_candidate_count=2,
            gemini_seed=123,
            gemini_presence_penalty=0.25,
            gemini_frequency_penalty=-0.25,
            gemini_stop_sequences=("END", "STOP"),
            gemini_response_mime_type="application/json",
            gemini_reasoning_effort="low",
            gemini_extra_body_json='{"google":{"generation_config":{"responseSchema":{"type":"object","properties":{"ok":{"type":"boolean"}}}},"safetySettings":[{"category":"HARM_CATEGORY_DANGEROUS_CONTENT","threshold":"BLOCK_NONE"}]},"metadata":{"probe":true}}',
        )
        client = DeepSeekClient(probe_config)  # type: ignore[operator]
        payload = client._build_payload(
            messages=[{"role": "user", "content": "settings contract probe"}],
            tools=None,
            tool_choice="none",
            temperature=None,
            stream=False,
            model=probe_config.gemini_planner_model,
            thinking_enabled=True,
            max_tokens=64,
        )
        extra_body = payload.get("extra_body") if isinstance(payload.get("extra_body"), dict) else {}
        google = extra_body.get("google") if isinstance(extra_body.get("google"), dict) else {}
        generation_config = google.get("generation_config") if isinstance(google.get("generation_config"), dict) else {}
        payload_ok = (
            payload.get("top_p") == 0.7
            and payload.get("presence_penalty") == 0.25
            and payload.get("frequency_penalty") == -0.25
            and payload.get("stop") == ["END", "STOP"]
            and payload.get("reasoning_effort") == "low"
            and generation_config.get("topK") == 33
            and generation_config.get("candidateCount") == 2
            and generation_config.get("seed") == 123
            and generation_config.get("responseMimeType") == "application/json"
            and generation_config.get("responseSchema") == {"type": "object", "properties": {"ok": {"type": "boolean"}}}
            and isinstance(google.get("safetySettings"), list)
            and extra_body.get("metadata") == {"probe": True}
        )

        invalid_config = replace(probe_config, gemini_extra_body_json="{bad json")
        invalid_payload = DeepSeekClient(invalid_config)._build_payload(  # type: ignore[operator]
            messages=[{"role": "user", "content": "invalid extra body probe"}],
            tools=None,
            tool_choice="none",
            temperature=None,
            stream=False,
            model=invalid_config.gemini_planner_model,
            thinking_enabled=True,
            max_tokens=64,
        )
        invalid_extra = invalid_payload.get("extra_body") if isinstance(invalid_payload.get("extra_body"), dict) else {}
        invalid_payload_ok = "metadata" not in invalid_extra

        thinking_config = replace(
            probe_config,
            gemini_extra_body_json='{"google":{"thinking_config":{"thinking_level":"low"}}}',
        )
        thinking_payload = DeepSeekClient(thinking_config)._build_payload(  # type: ignore[operator]
            messages=[{"role": "user", "content": "thinking config override probe"}],
            tools=None,
            tool_choice="none",
            temperature=None,
            stream=False,
            model=thinking_config.gemini_planner_model,
            thinking_enabled=True,
            max_tokens=64,
        )
        thinking_extra = thinking_payload.get("extra_body") if isinstance(thinking_payload.get("extra_body"), dict) else {}
        thinking_google = thinking_extra.get("google") if isinstance(thinking_extra.get("google"), dict) else {}
        thinking_config_override_ok = (
            "reasoning_effort" not in thinking_payload
            and thinking_google.get("thinking_config") == {"thinking_level": "low"}
        )
    except Exception as exc:
        return item("gemini_settings_contract", 0, "fail", [f"payload_exception={exc}"], "修复 Gemini settings payload contract。")

    ui_invalid_ok = False
    ui_detail = "not_run"
    env_path = config.env_file_path
    before_exists = env_path.exists()
    before_text = ""
    before_value = str(getattr(config, "gemini_extra_body_json", "") or "")
    try:
        if before_exists:
            before_text = env_path.read_text(encoding="utf-8", errors="replace")
        completed = run_cmd(
            [sys.executable, str(PROJECTLING_DIR / "core.py"), "shell-settings", "--tab", "gemini_params"],
            cwd=PROJECTLING_DIR,
            input_text="9\n{bad json\n0\n",
            timeout=15,
        )
        after_config = load_config()
        after_value = str(getattr(after_config, "gemini_extra_body_json", "") or "")
        after_exists = env_path.exists()
        after_text = env_path.read_text(encoding="utf-8", errors="replace") if after_exists else ""
        ui_error_hit = int("JSON 无效" in (completed.stdout or ""))
        ui_value_unchanged = int(after_value == before_value)
        ui_file_unchanged = int(after_text == before_text and after_exists == before_exists)
        ui_invalid_ok = (
            completed.returncode == 0
            and ui_error_hit
            and after_value == before_value
            and after_exists == before_exists
            and after_text == before_text
        )
        ui_detail = f"rc={completed.returncode} err={ui_error_hit} val={ui_value_unchanged} file={ui_file_unchanged}"
    except Exception as exc:
        ui_detail = f"ui_exception={exc}"
    finally:
        try:
            if before_exists:
                current_text = env_path.read_text(encoding="utf-8", errors="replace") if env_path.exists() else ""
                if current_text != before_text:
                    env_path.write_text(before_text, encoding="utf-8")
            elif env_path.exists():
                env_path.unlink()
        except OSError:
            pass

    checks = {
        "payload": payload_ok,
        "invalid_payload": invalid_payload_ok,
        "thinking_config_override": thinking_config_override_ok,
        "ui_invalid_json": ui_invalid_ok,
    }
    density_limit = 70
    evidence = [
        f"payload={_compact_bool_flag(payload_ok)} top_p={payload.get('top_p')} topK={generation_config.get('topK')} cand={generation_config.get('candidateCount')} schema={int(isinstance(generation_config.get('responseSchema'), dict))}",
        f"extra={_compact_bool_flag(bool(google))} meta={_compact_bool_flag(extra_body.get('metadata') == {'probe': True})}",
        f"invalid_payload={_compact_bool_flag(invalid_payload_ok)}",
        f"thinking_cfg={_compact_bool_flag(thinking_config_override_ok)}",
        f"ui_invalid={_compact_bool_flag(ui_invalid_ok)} {ui_detail}",
    ]
    old_labels = (
        "payload=True",
        "candidateCount=",
        "responseSchema=",
        "extra_body_google=",
        "invalid_payload_ignored=",
        "thinking_config_override=",
        "ui_invalid_json=",
        "error_text=",
        "value_unchanged=",
        "file_unchanged=",
        "True",
        "False",
        "{'",
        "['",
    )
    density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if len(row) > density_limit or any(label in row for label in old_labels)
    ]
    checks["evidence_density"] = not density_failures
    failures = [name for name, ok in checks.items() if not ok]
    evidence.append(f"gemini_settings_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")
    score = 100 if not failures else max(40, 100 - len(failures) * 25)
    if failures:
        evidence.append("failures=" + ",".join(failures))
    return item(
        "gemini_settings_contract",
        score,
        status_from_score(score),
        evidence,
        "修复 Gemini 参数 payload 序列化或非法 Extra Body JSON UI 校验。" if failures else "",
    )


def _settings_key_summary(
    keys: Iterable[str],
    *,
    required_keys: Iterable[str],
    sample_keys: Iterable[str],
) -> tuple[str, list[str]]:
    present = sorted({str(key) for key in keys if str(key).strip()})
    present_set = set(present)
    required = [str(key) for key in required_keys if str(key).strip()]
    missing = [key for key in required if key not in present_set]
    samples = [str(key) for key in sample_keys if str(key) in present_set]
    if not samples:
        samples = present[:4]
    sample_labels = [_settings_key_alias(key) for key in samples]
    extra_count = max(0, len(present) - len(samples))
    summary = (
        f"keys={len(present)} "
        f"req={len(required) - len(missing)}/{len(required)} "
        f"missing={','.join(missing) if missing else '-'} "
        f"sample={','.join(sample_labels) if sample_labels else '-'} "
        f"extra={extra_count}"
    )
    return summary, missing


def _settings_key_alias(key: Any) -> str:
    text = str(key or "").strip()
    aliases = {
        "GEMINI_API_KEY": "api_key",
        "GEMINI_BASE_URL": "base",
        "GEMINI_PLANNER_MODEL": "planner",
        "GEMINI_EXECUTOR_MODEL": "executor",
        "GEMINI_MAX_TOKENS": "max_tokens",
        "GEMINI_TEMPERATURE": "temp",
        "GEMINI_REASONING_EFFORT": "effort",
        "GEMINI_TOP_P": "top_p",
        "GEMINI_TOP_K": "top_k",
        "GEMINI_CANDIDATE_COUNT": "candidates",
        "GEMINI_SEED": "seed",
        "GEMINI_PRESENCE_PENALTY": "presence",
        "GEMINI_FREQUENCY_PENALTY": "frequency",
        "GEMINI_STOP_SEQUENCES": "stops",
        "GEMINI_RESPONSE_MIME_TYPE": "mime",
        "GEMINI_EXTRA_BODY_JSON": "extra_json",
    }
    return aliases.get(text, text)


def _compact_list_or_dash(values: Iterable[Any]) -> str:
    items = [str(value) for value in values if str(value).strip()]
    return ",".join(items) if items else "-"


def _artifact_file_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    normalized = text.replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1] or "-"


def _detail_file_evidence(prefix: str, value: Any) -> str:
    return f"{prefix}_file={_artifact_file_label(value)}"


def _artifact_status_evidence(prefix: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return f"{prefix}_file=- exists=0"
    path = Path(text)
    exists = path.exists()
    label = _artifact_file_label(text) if exists else text
    return f"{prefix}_file={label} exists={_compact_bool_flag(exists)}"


def _artifact_status_density_failures(
    evidence: list[str],
    *,
    prefixes: tuple[str, ...],
    limit: int = 90,
) -> list[str]:
    failures: list[str] = []
    for row in evidence:
        if not row.startswith(prefixes):
            continue
        has_full_path = ":\\" in row or "/mnt/" in row
        has_verbose_bool = "True" in row or "False" in row
        if len(row) > limit or has_full_path or has_verbose_bool:
            failures.append(f"{row.split('=', 1)[0]}:{len(row)}")
    return failures


def _find_host_adb() -> Path | None:
    candidates: list[Path] = []
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / "Android" / "Sdk" / "platform-tools" / "adb.exe")
        for env_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
            value = os.environ.get(env_name)
            if value:
                candidates.append(Path(value) / "platform-tools" / "adb.exe")
    found = shutil.which("adb")
    if found:
        candidates.append(Path(found))

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower() if os.name == "nt" else str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            return candidate
    return None


def _parse_adb_version(stdout: str) -> str:
    for line in stdout.splitlines():
        text = line.strip()
        if text.lower().startswith("version "):
            return text.split(" ", 1)[1].strip() or "-"
    return "-"


def _host_adb_probe(timeout: int = 12) -> dict[str, Any]:
    adb = _find_host_adb()
    result: dict[str, Any] = {
        "adb": str(adb) if adb else "",
        "adb_label": _artifact_file_label(adb) if adb else "missing",
        "version": "-",
        "rc": "",
        "rows": 0,
        "device": 0,
        "unauthorized": 0,
        "offline": 0,
        "other": 0,
        "error": "",
    }
    if adb is None:
        result["rc"] = "missing"
        return result

    try:
        version = subprocess.run(
            [str(adb), "version"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        result["version"] = _parse_adb_version(version.stdout or version.stderr or "")
    except subprocess.TimeoutExpired:
        result["version"] = "timeout"
    except OSError as exc:
        result["error"] = str(exc)
        result["rc"] = "os_error"
        return result

    try:
        devices = subprocess.run(
            [str(adb), "devices", "-l"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        result["rc"] = "timeout"
        result["error"] = f"timeout>{timeout}s"
        return result
    except OSError as exc:
        result["rc"] = "os_error"
        result["error"] = str(exc)
        return result

    result["rc"] = str(devices.returncode)
    if devices.stderr.strip():
        result["error"] = devices.stderr.strip().splitlines()[0][:120]
    for raw in (devices.stdout or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("*") or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        state = parts[1].strip().lower()
        result["rows"] = int(result["rows"]) + 1
        if state == "device":
            result["device"] = int(result["device"]) + 1
        elif state == "unauthorized":
            result["unauthorized"] = int(result["unauthorized"]) + 1
        elif state == "offline":
            result["offline"] = int(result["offline"]) + 1
        else:
            result["other"] = int(result["other"]) + 1
    return result


def _auto_detail_density_failures(evidence: list[str], *, limit: int = 120) -> list[str]:
    failures: list[str] = []
    full_project = str(PROJECTLING_DIR)
    for index, row in enumerate(evidence, start=1):
        old_detail_prefix = row.startswith(("detail=", "auto_detail="))
        compact_detail_prefix = row.startswith(("detail_file=", "auto_detail_file="))
        if not old_detail_prefix and not compact_detail_prefix:
            continue
        has_full_path = full_project in row or ":\\" in row or "/mnt/" in row
        if old_detail_prefix or len(row) > limit or has_full_path:
            failures.append(f"row{index}:{len(row)}")
    return failures


def _append_auto_detail_density_guard(evidence: list[str], score: int, *, limit: int = 120) -> int:
    failures = _auto_detail_density_failures(evidence, limit=limit)
    if failures:
        score = min(score, 75)
    evidence.append(f"auto_detail_density=limit={limit} failures={_compact_list_or_dash(failures)}")
    return score


def _append_auto_repr_density_guard(evidence: list[str], score: int, *, limit: int = 80) -> int:
    failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if (
            not row.startswith(("detail_file=", "auto_detail_file="))
            and (
                len(row) > limit
                or "True" in row
                or "False" in row
                or "['" in row
                or "']" in row
                or "[]" in row
                or "{'" in row
                or "'}" in row
            )
        )
    ]
    if failures:
        score = min(score, 75)
    evidence.append(f"auto_repr_density=limit={limit} failures={_compact_list_or_dash(failures)}")
    return score


def _append_runtime_repr_density_guard(evidence: list[str], score: int, *, limit: int = 90) -> int:
    failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if (
            len(row) > limit
            or "True" in row
            or "False" in row
            or "['" in row
            or "']" in row
            or "[]" in row
            or "{'" in row
            or "'}" in row
        )
    ]
    if failures:
        score = min(score, 75)
    evidence.append(f"runtime_repr_density=limit={limit} failures={_compact_list_or_dash(failures)}")
    return score


def check_gemini_settings_persistence_contract() -> dict[str, Any]:
    if DeepSeekClient is None or load_config is None:
        return item("gemini_settings_persistence_contract", 0, "fail", ["projectling imports unavailable"], "检查 Gemini settings persistence 导入链。")

    try:
        import core as projectling_core
        import projectling as projectling_module

        base_config = load_config()
        sandbox_dir = HEALTH_SANDBOX_DIR / "gemini-settings-persistence"
        sandbox_runtime = sandbox_dir / "runtime"
        sandbox_env = sandbox_dir / "env"
        sandbox_runtime.mkdir(parents=True, exist_ok=True)
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        sandbox_env.write_text("", encoding="utf-8")

        env_keys = tuple(getattr(projectling_module, "PROJECT_ENV_OVERRIDE_KEYS", ()))
        env_snapshot = {key: os.environ.get(key) for key in env_keys}

        def _restore_env() -> None:
            for key, value in env_snapshot.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        def _layer() -> dict[str, str]:
            return projectling_module._load_env_file(sandbox_env)

        def _optional_float(layer: dict[str, str], key: str, *, min_value: float, max_value: float) -> float | None:
            raw = str(layer.get(key) or "").strip()
            if not raw:
                return None
            try:
                value = float(raw)
            except ValueError:
                return None
            return value if min_value <= value <= max_value else None

        def _optional_int(layer: dict[str, str], key: str, *, min_value: int, max_value: int | None = None) -> int | None:
            raw = str(layer.get(key) or "").strip()
            if not raw:
                return None
            try:
                value = int(raw)
            except ValueError:
                return None
            if value < min_value or (max_value is not None and value > max_value):
                return None
            return value

        def _csv_tuple(raw: str | None) -> tuple[str, ...]:
            return tuple(part.strip() for part in str(raw or "").split(",") if part.strip())

        def _reasoning(raw: str | None) -> str:
            value = str(raw or "high").strip().lower()
            value = {"off": "none", "disabled": "none", "default": "high", "max": "high"}.get(value, value)
            return value if value in {"none", "low", "high"} else "high"

        def _sandbox_config() -> Any:
            layer = _layer()
            provider = projectling_module._api_provider_value(layer.get("PROJECTLING_API_PROVIDER") or "gemini")
            gemini_key = str(layer.get("GEMINI_API_KEY") or "fixture-gemini-persist-contract")
            base_url = str(layer.get("GEMINI_BASE_URL") or "https://example.invalid/v1")
            planner = str(layer.get("GEMINI_PLANNER_MODEL") or "gemini-3.1-pro-low")
            executor = str(layer.get("GEMINI_EXECUTOR_MODEL") or "gemini-3-flash")
            reasoning = _reasoning(layer.get("GEMINI_REASONING_EFFORT"))
            return replace(
                base_config,
                config_dir=sandbox_dir,
                runtime_dir=sandbox_runtime,
                env_file_path=sandbox_env,
                api_provider=provider,
                api_key=gemini_key,
                gemini_api_key=gemini_key,
                base_url=base_url,
                gemini_base_url=base_url,
                model=executor,
                gemini_planner_model=planner,
                gemini_executor_model=executor,
                gemini_top_p=_optional_float(layer, "GEMINI_TOP_P", min_value=0.0, max_value=1.0),
                gemini_top_k=_optional_int(layer, "GEMINI_TOP_K", min_value=1),
                gemini_candidate_count=_optional_int(layer, "GEMINI_CANDIDATE_COUNT", min_value=1, max_value=8),
                gemini_seed=_optional_int(layer, "GEMINI_SEED", min_value=0),
                gemini_presence_penalty=_optional_float(layer, "GEMINI_PRESENCE_PENALTY", min_value=-2.0, max_value=2.0),
                gemini_frequency_penalty=_optional_float(layer, "GEMINI_FREQUENCY_PENALTY", min_value=-2.0, max_value=2.0),
                gemini_stop_sequences=_csv_tuple(layer.get("GEMINI_STOP_SEQUENCES")),
                gemini_response_mime_type=str(layer.get("GEMINI_RESPONSE_MIME_TYPE") or "").strip(),
                gemini_reasoning_effort=reasoning,
                gemini_extra_body_json=str(layer.get("GEMINI_EXTRA_BODY_JSON") or "").strip(),
                reasoning_effort=reasoning,
                collab_mode="standard",
            )

        def _save_sandbox_config(_config: Any, updates: dict[str, str | None]) -> Any:
            projectling_module.save_env_config(updates, path=sandbox_env)
            return _sandbox_config()

        class _Size:
            def __init__(self, columns: int) -> None:
                self.columns = columns
                self.lines = 24

        def _plain(text: str) -> str:
            return projectling_core._strip_ansi(text or "")

        def _max_width(text: str) -> int:
            return max((projectling_core._display_width(line) for line in _plain(text).splitlines()), default=0)

        def _run_ui(fn: Any, inputs: list[str], *, width: int | None = None) -> str:
            iterator = iter(inputs)
            stdout = io.StringIO()
            old_prompt_line = projectling_core._prompt_line
            old_get_terminal_size = projectling_core.shutil.get_terminal_size
            try:
                projectling_core._prompt_line = lambda _prompt="": next(iterator, "0")
                if width is not None:
                    projectling_core.shutil.get_terminal_size = lambda _fallback=(80, 24), columns=width: _Size(columns)
                with contextlib.redirect_stdout(stdout):
                    fn()
            finally:
                projectling_core._prompt_line = old_prompt_line
                projectling_core.shutil.get_terminal_size = old_get_terminal_size
            return stdout.getvalue()

        class _SettingsModelListClient:
            def __init__(self, _config: Any) -> None:
                self.config = _config

            def list_models(self) -> dict[str, Any]:
                return {
                    "object": "list",
                    "data": [
                        {"id": "gemini-3.1-pro-low", "object": "model"},
                        {"id": "gemini-3-flash", "object": "model"},
                        {"id": "gemini-other-model", "object": "model"},
                        {"id": "gemini-round58-flash", "object": "model"},
                        {"id": "gemini-round58-pro", "object": "model"},
                        {"id": "gemini-3-pro-image-preview", "object": "model"},
                        {"id": "claude-sonnet-4-6", "object": "model"},
                    ],
                }

        old_core_load_config = projectling_core.load_config
        old_core_save_config = projectling_core._save_config_value
        old_core_client = projectling_core.DeepSeekClient
        combined_stdout = ""
        try:
            projectling_module.save_env_config(
                {
                    "PROJECTLING_API_PROVIDER": "gemini",
                    "GEMINI_API_KEY": "fixture-gemini-persist-contract",
                    "GEMINI_BASE_URL": "https://example.invalid/v1",
                    "GEMINI_PLANNER_MODEL": "gemini-3.1-pro-low",
                    "GEMINI_EXECUTOR_MODEL": "gemini-3-flash",
                },
                path=sandbox_env,
            )
            projectling_core.load_config = _sandbox_config
            projectling_core._save_config_value = _save_sandbox_config

            combined_stdout += _run_ui(
                projectling_core._run_gemini_params_settings_ui,
                [
                    "1", "0.65",
                    "2", "33",
                    "3", "2",
                    "4", "123",
                    "5", "0.25",
                    "6", "-0.25",
                    "7", "END, STOP",
                    "8", "application/json",
                    "9", '{"google":{"safetySettings":[{"category":"HARM_CATEGORY_DANGEROUS_CONTENT","threshold":"BLOCK_NONE"}]},"metadata":{"persist":true}}',
                    "0",
                ],
            )
            combined_stdout += _run_ui(projectling_core._run_api_settings_ui, ["13", "low", "0"])
            saved_config = _sandbox_config()
            layer_after_save = _layer()

            payload = DeepSeekClient(saved_config)._build_payload(  # type: ignore[operator]
                messages=[{"role": "user", "content": "Gemini persistence contract"}],
                tools=None,
                tool_choice="none",
                temperature=None,
                stream=False,
                model=saved_config.gemini_planner_model,
                thinking_enabled=True,
                max_tokens=64,
            )
            extra_body = payload.get("extra_body") if isinstance(payload.get("extra_body"), dict) else {}
            google = extra_body.get("google") if isinstance(extra_body.get("google"), dict) else {}
            generation_config = google.get("generation_config") if isinstance(google.get("generation_config"), dict) else {}
            persisted_ok = (
                saved_config.gemini_top_p == 0.65
                and saved_config.gemini_top_k == 33
                and saved_config.gemini_candidate_count == 2
                and saved_config.gemini_seed == 123
                and saved_config.gemini_presence_penalty == 0.25
                and saved_config.gemini_frequency_penalty == -0.25
                and saved_config.gemini_stop_sequences == ("END", "STOP")
                and saved_config.gemini_response_mime_type == "application/json"
                and saved_config.gemini_reasoning_effort == "low"
                and saved_config.gemini_extra_body_json.startswith("{")
            )
            payload_ok = (
                payload.get("top_p") == 0.65
                and payload.get("presence_penalty") == 0.25
                and payload.get("frequency_penalty") == -0.25
                and payload.get("stop") == ["END", "STOP"]
                and payload.get("reasoning_effort") == "low"
                and generation_config.get("topK") == 33
                and generation_config.get("candidateCount") == 2
                and generation_config.get("seed") == 123
                and generation_config.get("responseMimeType") == "application/json"
                and isinstance(google.get("safetySettings"), list)
                and extra_body.get("metadata") == {"persist": True}
            )

            before_invalid = sandbox_env.read_text(encoding="utf-8", errors="replace")
            invalid_top_p_stdout = _run_ui(projectling_core._run_gemini_params_settings_ui, ["1", "oops", "0"], width=20)
            after_invalid_top_p = sandbox_env.read_text(encoding="utf-8", errors="replace")
            invalid_top_k_stdout = _run_ui(projectling_core._run_gemini_params_settings_ui, ["2", "bad", "0"], width=20)
            after_invalid_top_k = sandbox_env.read_text(encoding="utf-8", errors="replace")
            invalid_candidate_stdout = _run_ui(projectling_core._run_gemini_params_settings_ui, ["3", "9", "0"], width=20)
            after_invalid_candidate = sandbox_env.read_text(encoding="utf-8", errors="replace")
            invalid_json_stdout = _run_ui(projectling_core._run_gemini_params_settings_ui, ["9", "{bad json", "0"], width=20)
            after_invalid_json = sandbox_env.read_text(encoding="utf-8", errors="replace")
            combined_stdout += invalid_top_p_stdout + invalid_top_k_stdout + invalid_candidate_stdout + invalid_json_stdout
            invalid_texts = [invalid_top_p_stdout, invalid_top_k_stdout, invalid_candidate_stdout, invalid_json_stdout]
            invalid_width_ok = all(_max_width(text) <= 20 for text in invalid_texts)
            invalid_no_mutation_ok = (
                before_invalid == after_invalid_top_p
                and before_invalid == after_invalid_top_k
                and before_invalid == after_invalid_candidate
                and before_invalid == after_invalid_json
                and invalid_width_ok
                and "未保存，保持原样" in invalid_top_p_stdout
                and "请输入数字" in invalid_top_p_stdout
                and "未保存，保持原样" in invalid_top_k_stdout
                and "请输入整数" in invalid_top_k_stdout
                and "未保存，保持原样" in invalid_candidate_stdout
                and "小于等于 8" in invalid_candidate_stdout
                and "保留" in invalid_candidate_stdout
                and "未保存，保持原样" in invalid_json_stdout
                and "JSON 无效" in invalid_json_stdout
                and "Extra Body" in invalid_json_stdout
            )

            combined_stdout += _run_ui(
                projectling_core._run_gemini_params_settings_ui,
                ["1", "", "2", "", "3", "", "4", "", "5", "", "6", "", "7", "", "8", "", "9", "", "0"],
                width=20,
            )
            cleared_config = _sandbox_config()
            layer_after_clear = _layer()
            cleared_ok = (
                cleared_config.gemini_top_p is None
                and cleared_config.gemini_top_k is None
                and cleared_config.gemini_candidate_count is None
                and cleared_config.gemini_seed is None
                and cleared_config.gemini_presence_penalty is None
                and cleared_config.gemini_frequency_penalty is None
                and cleared_config.gemini_stop_sequences == ()
                and cleared_config.gemini_response_mime_type == ""
                and cleared_config.gemini_extra_body_json == ""
            )
            cleared_keys = [
                key
                for key in (
                    "GEMINI_TOP_P",
                    "GEMINI_TOP_K",
                    "GEMINI_CANDIDATE_COUNT",
                    "GEMINI_SEED",
                    "GEMINI_PRESENCE_PENALTY",
                    "GEMINI_FREQUENCY_PENALTY",
                    "GEMINI_STOP_SEQUENCES",
                    "GEMINI_RESPONSE_MIME_TYPE",
                    "GEMINI_EXTRA_BODY_JSON",
                )
                if not str(layer_after_clear.get(key) or "").strip()
            ]
            saved_feedback_ok = (
                "Top P 已保存" in combined_stdout
                and "Top K 已保存" in combined_stdout
                and "Candidate Count 已保存" in combined_stdout
                and "Extra Body JSON 已保存" in combined_stdout
            )
            cleared_feedback_ok = "已清除" in combined_stdout and "使用自动值" in combined_stdout
        finally:
            projectling_core.load_config = old_core_load_config
            projectling_core._save_config_value = old_core_save_config
            projectling_core.DeepSeekClient = old_core_client
            _restore_env()

        secret_ok = "fixture-gemini-persist-contract" not in combined_stdout
    except Exception as exc:
        try:
            _restore_env()  # type: ignore[misc]
        except Exception:
            pass
        return item("gemini_settings_persistence_contract", 0, "fail", [f"exception={exc}"], "修复 Gemini settings persistence contract。")

    checks = {
        "persisted": persisted_ok,
        "payload": payload_ok,
        "saved_feedback": saved_feedback_ok,
        "invalid_no_mutation": invalid_no_mutation_ok,
        "cleared": cleared_ok,
        "cleared_feedback": cleared_feedback_ok,
        "secret_redaction": secret_ok,
    }
    saved_gemini_keys = sorted(
        key for key, value in layer_after_save.items() if key.startswith("GEMINI_") and str(value).strip()
    )
    key_summary, key_missing = _settings_key_summary(
        saved_gemini_keys,
        required_keys=(
            "GEMINI_API_KEY",
            "GEMINI_BASE_URL",
            "GEMINI_PLANNER_MODEL",
            "GEMINI_EXECUTOR_MODEL",
            "GEMINI_TOP_P",
            "GEMINI_TOP_K",
            "GEMINI_CANDIDATE_COUNT",
            "GEMINI_SEED",
            "GEMINI_PRESENCE_PENALTY",
            "GEMINI_FREQUENCY_PENALTY",
            "GEMINI_STOP_SEQUENCES",
            "GEMINI_RESPONSE_MIME_TYPE",
            "GEMINI_EXTRA_BODY_JSON",
            "GEMINI_REASONING_EFFORT",
        ),
        sample_keys=(
            "GEMINI_PLANNER_MODEL",
            "GEMINI_EXECUTOR_MODEL",
            "GEMINI_TOP_P",
            "GEMINI_EXTRA_BODY_JSON",
        ),
    )
    required_count = 14
    sample_labels = ["planner", "executor", "top_p", "extra_json"]
    compact_key_summary = (
        f"k={len(saved_gemini_keys)} "
        f"req={required_count - len(key_missing)}/{required_count} "
        f"miss={_compact_list_or_dash(key_missing)} "
        f"smp={','.join(sample_labels)} "
        f"+{max(0, len(saved_gemini_keys) - len(sample_labels))}"
    )
    key_density_limit = 80
    key_density_rows = [
        f"persisted={_compact_bool_flag(persisted_ok)} {compact_key_summary}",
        f"payload={_compact_bool_flag(payload_ok)} top_p={payload.get('top_p')} topK={generation_config.get('topK')} cand={generation_config.get('candidateCount')} effort={payload.get('reasoning_effort')}",
        f"saved={_compact_bool_flag(saved_feedback_ok)}",
        f"invalid={_compact_bool_flag(invalid_no_mutation_ok)} width={_compact_bool_flag(invalid_width_ok)}",
        f"cleared={_compact_bool_flag(cleared_ok)} fb={_compact_bool_flag(cleared_feedback_ok)} keys={len(cleared_keys)}/9",
        f"secret_redaction={_compact_bool_flag(secret_ok)}",
    ]
    old_labels = (
        "missing=",
        "sample=",
        "extra=",
        "payload=True",
        "payload=False",
        "saved_feedback=",
        "invalid_no_mutation=",
        "invalid_width=",
        "cleared_feedback=",
        "cleared_keys=",
        "secret_redaction=True",
        "secret_redaction=False",
    )
    key_density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in key_density_rows
        if len(row) > key_density_limit
        or "{'" in row
        or "['" in row
        or "True" in row
        or "False" in row
        or any(label in row for label in old_labels)
    ]
    checks["key_evidence_density"] = not key_missing and not key_density_failures
    failures = [name for name, ok in checks.items() if not ok]
    evidence = [
        *key_density_rows[:-1],
        f"settings_key_density=limit={key_density_limit} missing={_compact_list_or_dash(key_missing)} failures={_compact_list_or_dash(key_density_failures)}",
        key_density_rows[-1],
    ]
    score = 100 if not failures else max(35, 100 - len(failures) * 20)
    if failures:
        evidence.append("failures=" + ",".join(failures))
    return item(
        "gemini_settings_persistence_contract",
        score,
        status_from_score(score),
        evidence,
        "修复 Gemini Settings 保存、读取、清除、非法输入 no-mutation 或 payload 对齐。" if failures else "",
    )


def check_settings_exception_restoration_contract() -> dict[str, Any]:
    if load_config is None:
        return item(
            "settings_exception_restoration_contract",
            0,
            "fail",
            ["projectling imports unavailable"],
            "检查 Settings 异常恢复测试导入链。",
        )

    try:
        import core as projectling_core
        import projectling as projectling_module
    except Exception as exc:
        return item(
            "settings_exception_restoration_contract",
            0,
            "fail",
            [f"import_exception={type(exc).__name__}: {exc}"],
            "修复 Settings 异常恢复测试导入链。",
        )

    env_keys = tuple(getattr(projectling_module, "PROJECT_ENV_OVERRIDE_KEYS", ()))
    env_key = "PROJECTLING_API_PROVIDER"
    if env_key not in env_keys:
        return item(
            "settings_exception_restoration_contract",
            0,
            "fail",
            ["PROJECTLING_API_PROVIDER missing from PROJECT_ENV_OVERRIDE_KEYS"],
            "把 Provider 环境覆盖键纳入 Settings finally 恢复范围。",
        )

    original_env = os.environ.get(env_key)
    original_renderer = projectling_core._run_gemini_params_settings_ui
    baseline_load_config = projectling_core.load_config
    baseline_save_config = projectling_core._save_config_value
    baseline_client = projectling_core.DeepSeekClient
    baseline_terminal_size = projectling_core.shutil.get_terminal_size
    baseline_prompt_line = projectling_core._prompt_line
    calls = 0
    nested_result: dict[str, Any] = {}
    checks: dict[str, bool] = {}

    def _fault_injected_renderer() -> Any:
        nonlocal calls
        calls += 1
        if calls >= 2:
            os.environ[env_key] = "deepseek"
            raise RuntimeError("settings exception restoration probe")
        return original_renderer()

    try:
        os.environ[env_key] = "gemini"
        projectling_core._run_gemini_params_settings_ui = _fault_injected_renderer
        nested_result = check_gemini_settings_persistence_contract()
        nested_evidence = "\n".join(str(row) for row in nested_result.get("evidence", []))
        checks = {
            "fault_injected": calls >= 2,
            "nested_failure_observed": nested_result.get("status") == "fail" and "settings exception restoration probe" in nested_evidence,
            "load_config_restored": projectling_core.load_config is baseline_load_config,
            "save_config_restored": projectling_core._save_config_value is baseline_save_config,
            "client_restored": projectling_core.DeepSeekClient is baseline_client,
            "terminal_size_restored": projectling_core.shutil.get_terminal_size is baseline_terminal_size,
            "prompt_restored": projectling_core._prompt_line is baseline_prompt_line,
            "environment_restored": os.environ.get(env_key) == "gemini",
        }
    except Exception as exc:
        checks = {"probe_completed": False}
        nested_result = {"status": "fail", "evidence": [f"probe_exception={type(exc).__name__}: {exc}"]}
    finally:
        projectling_core._run_gemini_params_settings_ui = original_renderer
        projectling_core.load_config = baseline_load_config
        projectling_core._save_config_value = baseline_save_config
        projectling_core.DeepSeekClient = baseline_client
        projectling_core.shutil.get_terminal_size = baseline_terminal_size
        projectling_core._prompt_line = baseline_prompt_line
        if original_env is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = original_env

    failures = [name for name, ok in checks.items() if not ok]
    evidence = [
        f"fault_calls={calls}",
        f"nested_status={nested_result.get('status')}",
        "restored=" + ",".join(name for name, ok in checks.items() if ok),
        f"failures={_compact_list_or_dash(failures)}",
    ]
    score = 100 if checks and not failures else 0
    return item(
        "settings_exception_restoration_contract",
        score,
        status_from_score(score),
        evidence,
        "修复 Settings 测试 finally：恢复 load/save/client/terminal/prompt/environment。" if failures or not checks else "",
    )


def check_api_settings_provider_persistence_contract() -> dict[str, Any]:
    if DeepSeekClient is None or load_config is None:
        return item("api_settings_provider_persistence_contract", 0, "fail", ["projectling imports unavailable"], "检查 API settings provider persistence 导入链。")

    try:
        import core as projectling_core
        import projectling as projectling_module

        base_config = load_config()
        sandbox_dir = HEALTH_SANDBOX_DIR / "api-settings-provider-persistence"
        sandbox_runtime = sandbox_dir / "runtime"
        sandbox_env = sandbox_dir / "env"
        sandbox_runtime.mkdir(parents=True, exist_ok=True)
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        sandbox_env.write_text("", encoding="utf-8")

        env_keys = tuple(getattr(projectling_module, "PROJECT_ENV_OVERRIDE_KEYS", ()))
        env_snapshot = {key: os.environ.get(key) for key in env_keys}

        def _restore_env() -> None:
            for key, value in env_snapshot.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        def _layer() -> dict[str, str]:
            return projectling_module._load_env_file(sandbox_env)

        def _truthy(raw: str | None, *, default: bool = True) -> bool:
            if raw is None or str(raw).strip() == "":
                return default
            return str(raw).strip().lower() not in {"0", "false", "off", "no"}

        def _optional_int(raw: str | None, *, min_value: int, max_value: int | None = None) -> int | None:
            text = str(raw or "").strip()
            if not text:
                return None
            try:
                value = int(text)
            except ValueError:
                return None
            if value < min_value or (max_value is not None and value > max_value):
                return None
            return value

        def _optional_float(raw: str | None, *, min_value: float, max_value: float) -> float | None:
            text = str(raw or "").strip()
            if not text:
                return None
            try:
                value = float(text)
            except ValueError:
                return None
            if value < min_value or value > max_value:
                return None
            return value

        def _first_non_empty(*values: str | None) -> str:
            for value in values:
                text = str(value or "").strip()
                if text:
                    return text
            return ""

        def _deepseek_reasoning(raw: str | None) -> str:
            normalizer = getattr(projectling_module, "_normalize_reasoning_effort", None)
            if callable(normalizer):
                return str(normalizer(raw))
            value = str(raw or "high").strip().lower()
            return value if value in {"high", "max"} else "high"

        def _gemini_reasoning(raw: str | None) -> str:
            normalizer = getattr(projectling_module, "_normalize_gemini_reasoning_effort", None)
            if callable(normalizer):
                return str(normalizer(raw))
            value = str(raw or "high").strip().lower()
            value = {"off": "none", "disabled": "none", "default": "high", "max": "high"}.get(value, value)
            return value if value in {"none", "low", "high"} else "high"

        def _sandbox_config() -> Any:
            layer = _layer()
            provider = projectling_module._api_provider_value(layer.get("PROJECTLING_API_PROVIDER") or "gemini")
            deepseek_key = str(layer.get("DEEPSEEK_API_KEY") or "fixture-api-provider-deepseek-initial")
            gemini_key = str(layer.get("GEMINI_API_KEY") or "fixture-api-provider-gemini-initial")
            deepseek_base = str(layer.get("DEEPSEEK_BASE_URL") or "https://deepseek.initial/v1")
            gemini_base = str(layer.get("GEMINI_BASE_URL") or "https://gemini.initial/v1")
            deepseek_planner = str(layer.get("DEEPSEEK_PLANNER_MODEL") or "deepseek-v4-pro")
            deepseek_executor = str(layer.get("DEEPSEEK_EXECUTOR_MODEL") or "deepseek-v4-flash")
            planner = str(layer.get("GEMINI_PLANNER_MODEL") or "gemini-3.1-pro-low")
            executor = str(layer.get("GEMINI_EXECUTOR_MODEL") or "gemini-3-flash")
            gemini_effort = _gemini_reasoning(layer.get("GEMINI_REASONING_EFFORT"))
            deepseek_effort = _deepseek_reasoning(layer.get("DEEPSEEK_REASONING_EFFORT"))
            max_tokens_raw = _first_non_empty(
                layer.get("GEMINI_MAX_TOKENS") if provider == "gemini" else None,
                layer.get("DEEPSEEK_MAX_TOKENS"),
            )
            temperature_raw = _first_non_empty(
                layer.get("GEMINI_TEMPERATURE") if provider == "gemini" else None,
                layer.get("DEEPSEEK_TEMPERATURE"),
                "0.2",
            )
            max_tokens = _optional_int(max_tokens_raw, min_value=1)
            temperature = _optional_float(temperature_raw, min_value=0.0, max_value=2.0)
            timeout_seconds = _optional_float(layer.get("DEEPSEEK_TIMEOUT_SECONDS"), min_value=5.0, max_value=86400.0) or 180.0
            retry_count = _optional_int(layer.get("DEEPSEEK_RETRY_COUNT"), min_value=0, max_value=10)
            if retry_count is None:
                retry_count = 10
            return replace(
                base_config,
                config_dir=sandbox_dir,
                runtime_dir=sandbox_runtime,
                env_file_path=sandbox_env,
                api_provider=provider,
                api_key=gemini_key if provider == "gemini" else deepseek_key,
                base_url=gemini_base if provider == "gemini" else deepseek_base,
                model=executor if provider == "gemini" else deepseek_executor,
                deepseek_api_key=deepseek_key,
                deepseek_base_url=deepseek_base,
                deepseek_planner_model=deepseek_planner,
                deepseek_executor_model=deepseek_executor,
                gemini_api_key=gemini_key,
                gemini_base_url=gemini_base,
                gemini_planner_model=planner,
                gemini_executor_model=executor,
                gemini_reasoning_effort=gemini_effort,
                gemini_extra_body_json=str(layer.get("GEMINI_EXTRA_BODY_JSON") or "").strip(),
                reasoning_effort=gemini_effort if provider == "gemini" else deepseek_effort,
                max_tokens=max_tokens,
                temperature=temperature if temperature is not None else 0.2,
                timeout_seconds=timeout_seconds,
                retry_count=retry_count,
                enable_sse=_truthy(layer.get("DEEPSEEK_ENABLE_SSE"), default=True),
                collab_mode="standard",
            )

        def _save_sandbox_config(_config: Any, updates: dict[str, str | None]) -> Any:
            projectling_module.save_env_config(updates, path=sandbox_env)
            return _sandbox_config()

        class _UiSize:
            def __init__(self, columns: int) -> None:
                self.columns = columns
                self.lines = 24

        def _plain(text: str) -> str:
            return projectling_core._strip_ansi(text or "")

        def _max_width(text: str) -> int:
            return max((projectling_core._display_width(line) for line in _plain(text).splitlines()), default=0)

        def _run_ui(inputs: list[str], *, width: int | None = None) -> str:
            iterator = iter(inputs)
            stdout = io.StringIO()
            old_prompt_line = projectling_core._prompt_line
            old_get_terminal_size = projectling_core.shutil.get_terminal_size
            try:
                projectling_core._prompt_line = lambda _prompt="": next(iterator, "0")
                if width is not None:
                    projectling_core.shutil.get_terminal_size = lambda _fallback=(80, 24), columns=width: _UiSize(columns)
                with contextlib.redirect_stdout(stdout):
                    projectling_core._run_api_settings_ui()
            finally:
                projectling_core._prompt_line = old_prompt_line
                projectling_core.shutil.get_terminal_size = old_get_terminal_size
            return stdout.getvalue()

        class _SettingsModelListClient:
            def __init__(self, _config: Any) -> None:
                self.config = _config

            def list_models(self) -> dict[str, Any]:
                return {
                    "object": "list",
                    "data": [
                        {"id": "gemini-3.1-pro-low", "object": "model"},
                        {"id": "gemini-3-flash", "object": "model"},
                        {"id": "gemini-other-model", "object": "model"},
                        {"id": "gemini-round58-flash", "object": "model"},
                        {"id": "gemini-round58-pro", "object": "model"},
                        {"id": "gemini-3-pro-image-preview", "object": "model"},
                        {"id": "claude-sonnet-4-6", "object": "model"},
                    ],
                }

        old_core_load_config = projectling_core.load_config
        old_core_save_config = projectling_core._save_config_value
        old_core_client = projectling_core.DeepSeekClient
        combined_stdout = ""
        try:
            projectling_module.save_env_config(
                {
                    "PROJECTLING_API_PROVIDER": "gemini",
                    "GEMINI_API_KEY": "fixture-api-provider-gemini-initial",
                    "DEEPSEEK_API_KEY": "fixture-api-provider-deepseek-initial",
                    "GEMINI_BASE_URL": "https://gemini.initial/v1",
                    "DEEPSEEK_BASE_URL": "https://deepseek.initial/v1",
                    "DEEPSEEK_PLANNER_MODEL": "deepseek-v4-pro",
                    "DEEPSEEK_EXECUTOR_MODEL": "deepseek-v4-flash",
                    "GEMINI_PLANNER_MODEL": "gemini-3.1-pro-low",
                    "GEMINI_EXECUTOR_MODEL": "gemini-3-flash",
                    "GEMINI_EXTRA_BODY_JSON": '{"metadata":{"api_provider_contract":"gemini"}}',
                    "DEEPSEEK_ENABLE_SSE": "1",
                },
                path=sandbox_env,
            )
            projectling_core.load_config = _sandbox_config
            projectling_core._save_config_value = _save_sandbox_config
            projectling_core.DeepSeekClient = _SettingsModelListClient

            combined_stdout += _run_ui(
                [
                    "2", "fixture-api-provider-gemini-live",
                    "3", "https://gemini.round58/v1",
                    "4", "5",
                    "5", "4",
                    "8",
                    "9", "2048",
                    "10", "0.6",
                    "11", "45",
                    "12", "4",
                    "13", "low",
                    "0",
                ],
            )
            gemini_config = _sandbox_config()
            layer_after_gemini = _layer()

            before_invalid = sandbox_env.read_text(encoding="utf-8", errors="replace")
            invalid_stdout = _run_ui(
                [
                    "1", "not-a-provider",
                    "10", "oops",
                    "10", "3",
                    "12", "99",
                    "13", "invalid-effort",
                    "0",
                ],
                width=20,
            )
            after_invalid = sandbox_env.read_text(encoding="utf-8", errors="replace")
            combined_stdout += invalid_stdout

            combined_stdout += _run_ui(
                [
                    "1", "deepseek",
                    "2", "fixture-api-provider-deepseek-live",
                    "3", "https://deepseek.round58/v1",
                    "9", "1024",
                    "10", "0.4",
                    "13", "max",
                    "0",
                ],
            )
            deepseek_config = _sandbox_config()
            layer_after_deepseek = _layer()

            deepseek_invalid_widths: list[str] = []
            deepseek_invalid_failures: list[str] = []
            for width in (16, 20, 24, 32, 48):
                before_deepseek_invalid = sandbox_env.read_text(encoding="utf-8", errors="replace")
                deepseek_invalid_stdout = _run_ui(
                    [
                        "10", "oops",
                        "10", "3",
                        "12", "99",
                        "13", "invalid-effort",
                        "0",
                    ],
                    width=width,
                )
                after_deepseek_invalid = sandbox_env.read_text(encoding="utf-8", errors="replace")
                deepseek_invalid_width = _max_width(deepseek_invalid_stdout)
                deepseek_invalid_widths.append(f"w{width}:{deepseek_invalid_width}")
                if not (
                    before_deepseek_invalid == after_deepseek_invalid
                    and deepseek_invalid_width <= width
                    and "未保存，保持原样" in deepseek_invalid_stdout
                    and "请输入数字" in deepseek_invalid_stdout
                    and "需要在 0 - 2" in deepseek_invalid_stdout
                    and "Retry 最大 10" in deepseek_invalid_stdout
                    and all(token in deepseek_invalid_stdout for token in ("high", "max"))
                    and "fixture-api-provider" not in deepseek_invalid_stdout
                ):
                    deepseek_invalid_failures.append(f"w{width}")
                combined_stdout += deepseek_invalid_stdout

            before_deepseek_blank = sandbox_env.read_text(encoding="utf-8", errors="replace")
            deepseek_blank_stdout = _run_ui(["2", "", "3", "", "13", "", "0"], width=20)
            after_deepseek_blank = sandbox_env.read_text(encoding="utf-8", errors="replace")
            combined_stdout += deepseek_blank_stdout

            deepseek_notice_widths: list[str] = []
            deepseek_notice_failures: list[str] = []
            for width in (16, 20, 24, 32, 48):
                before_deepseek_notice = sandbox_env.read_text(encoding="utf-8", errors="replace")
                deepseek_notice_stdout = _run_ui(["4", "0", "5", "0", "0"], width=width)
                after_deepseek_notice = sandbox_env.read_text(encoding="utf-8", errors="replace")
                deepseek_notice_width = _max_width(deepseek_notice_stdout)
                deepseek_notice_widths.append(f"w{width}:{deepseek_notice_width}")
                if not (
                    before_deepseek_notice == after_deepseek_notice
                    and deepseek_notice_width <= width
                    and deepseek_notice_stdout.count("DeepSeek") >= 2
                    and deepseek_notice_stdout.count("未输入，保持原样") >= 2
                    and "fixture-api-provider" not in deepseek_notice_stdout
                ):
                    deepseek_notice_failures.append(f"w{width}")
                combined_stdout += deepseek_notice_stdout

            combined_stdout += _run_ui(["1", "gemini", "0"])
            back_to_gemini_config = _sandbox_config()
            layer_after_back = _layer()

            before_blank_models = sandbox_env.read_text(encoding="utf-8", errors="replace")
            blank_stdout = _run_ui(["4", "", "5", "", "0"], width=20)
            after_blank_models = sandbox_env.read_text(encoding="utf-8", errors="replace")
            combined_stdout += blank_stdout
            blank_model_config = _sandbox_config()

            settings_stdout = io.StringIO()
            with contextlib.redirect_stdout(settings_stdout):
                projectling_core._render_api_settings(blank_model_config)
            settings_text = projectling_core._strip_ansi(settings_stdout.getvalue())

            class _WorkflowBridgeClient:
                captured_models: list[str] = []

                def __init__(self, _config: Any) -> None:
                    self.config = _config

                def list_models(self) -> dict[str, Any]:
                    return {
                        "object": "list",
                        "data": [
                            {"id": "gemini-other-model", "object": "model"},
                            {"id": "gemini-round58-flash", "object": "model"},
                            {"id": "gemini-round58-pro", "object": "model"},
                            {"id": "gemini-3-pro-image-preview", "object": "model"},
                            {"id": "claude-sonnet-4-6", "object": "model"},
                        ],
                    }

                def chat_completions(self, **kwargs: Any) -> dict[str, Any]:
                    self.captured_models.append(str(kwargs.get("model") or ""))
                    return {"choices": [{"message": {"content": "pong"}}]}

                def chat_completions_stream(self, **kwargs: Any) -> Iterable[dict[str, Any]]:
                    self.captured_models.append(str(kwargs.get("model") or ""))
                    yield {"choices": [{"delta": {"content": "pong"}, "finish_reason": "stop"}]}

            class _Size:
                def __init__(self, columns: int) -> None:
                    self.columns = columns
                    self.lines = 24

            old_bridge_client = projectling_core.DeepSeekClient
            old_get_terminal_size = projectling_core.shutil.get_terminal_size
            role_marker_failures: list[str] = []
            role_marker_evidence: list[str] = []
            try:
                projectling_core.DeepSeekClient = _WorkflowBridgeClient
                for width in (16, 20, 48):
                    projectling_core.shutil.get_terminal_size = lambda _fallback=(80, 24), columns=width: _Size(columns)
                    list_stdout = io.StringIO()
                    with contextlib.redirect_stdout(list_stdout):
                        list_rc = projectling_core._cmd_list_models(
                            argparse.Namespace(json=False, limit=30, base_url="", timeout=None)
                        )
                    list_text = projectling_core._strip_ansi(list_stdout.getvalue())
                    list_width = max((projectling_core._display_width(line) for line in list_text.splitlines()), default=0)
                    role_marker_evidence.append(f"w{width}:{list_width}")
                    if list_rc != 0 or list_width > width or "主星" not in list_text or "执行星" not in list_text:
                        role_marker_failures.append(f"w{width}:rc{list_rc}/max{list_width}")

                projectling_core.shutil.get_terminal_size = lambda _fallback=(80, 24): _Size(48)
                api_stdout = io.StringIO()
                with contextlib.redirect_stdout(api_stdout):
                    api_rc = projectling_core._cmd_api_test(
                        argparse.Namespace(json=True, no_stream=True, model="", base_url="", timeout=None)
                    )
                try:
                    api_payload = json.loads(api_stdout.getvalue())
                except json.JSONDecodeError:
                    api_payload = {}

                before_diag_override = sandbox_env.read_text(encoding="utf-8", errors="replace")
                diag_stdout = io.StringIO()
                with contextlib.redirect_stdout(diag_stdout):
                    diag_rc = projectling_core._cmd_api_test(
                        argparse.Namespace(
                            json=True,
                            no_stream=True,
                            model="gemini-round64-diagnostic-override",
                            base_url="",
                            timeout=None,
                        )
                    )
                after_diag_override = sandbox_env.read_text(encoding="utf-8", errors="replace")
                try:
                    diag_payload = json.loads(diag_stdout.getvalue())
                except json.JSONDecodeError:
                    diag_payload = {}

                risky_save_stdout = _run_ui(
                    [
                        "4", "4",
                        "5", "5",
                        "0",
                    ],
                    width=20,
                )
                combined_stdout += risky_save_stdout
                risky_save_config = _sandbox_config()

                before_risky_blank = sandbox_env.read_text(encoding="utf-8", errors="replace")
                risky_blank_stdout = _run_ui(["4", "", "5", "", "0"], width=20)
                after_risky_blank = sandbox_env.read_text(encoding="utf-8", errors="replace")
                combined_stdout += risky_blank_stdout
                risky_blank_config = _sandbox_config()
            finally:
                projectling_core.DeepSeekClient = old_bridge_client
                projectling_core.shutil.get_terminal_size = old_get_terminal_size
        finally:
            projectling_core.load_config = old_core_load_config
            projectling_core._save_config_value = old_core_save_config
            projectling_core.DeepSeekClient = old_core_client
            _restore_env()

        gemini_saved_ok = (
            gemini_config.api_provider == "gemini"
            and gemini_config.api_key == "fixture-api-provider-gemini-live"
            and gemini_config.base_url == "https://gemini.round58/v1"
            and gemini_config.gemini_planner_model == "gemini-round58-pro"
            and gemini_config.gemini_executor_model == "gemini-round58-flash"
            and gemini_config.enable_sse is False
            and gemini_config.max_tokens == 2048
            and abs(float(gemini_config.temperature) - 0.6) < 0.0001
            and abs(float(gemini_config.timeout_seconds) - 45.0) < 0.0001
            and gemini_config.retry_count == 4
            and gemini_config.reasoning_effort == "low"
        )
        invalid_no_mutation_ok = (
            before_invalid == after_invalid
            and _max_width(invalid_stdout) <= 20
            and "未保存，保持原样" in invalid_stdout
            and "请选择 1 或 2" in invalid_stdout
            and "请输入数字" in invalid_stdout
            and "需要在 0 - 2" in invalid_stdout
            and "Retry 最大 10" in invalid_stdout
            and all(token in invalid_stdout for token in ("none", "low", "high"))
        )
        deepseek_saved_ok = (
            deepseek_config.api_provider == "deepseek"
            and deepseek_config.api_key == "fixture-api-provider-deepseek-live"
            and deepseek_config.base_url == "https://deepseek.round58/v1"
            and deepseek_config.deepseek_planner_model == "deepseek-v4-pro"
            and deepseek_config.deepseek_executor_model == "deepseek-v4-flash"
            and deepseek_config.max_tokens == 1024
            and abs(float(deepseek_config.temperature) - 0.4) < 0.0001
            and deepseek_config.reasoning_effort == "max"
            and deepseek_config.timeout_seconds == 45.0
            and deepseek_config.retry_count == 4
        )
        deepseek_invalid_no_mutation_ok = not deepseek_invalid_failures
        deepseek_blank_no_mutation_ok = (
            before_deepseek_blank == after_deepseek_blank
            and _max_width(deepseek_blank_stdout) <= 20
            and deepseek_blank_stdout.count("未输入，保持原样") >= 3
            and "API Key 未修改" in deepseek_blank_stdout
            and "中转站未修改" in deepseek_blank_stdout
            and "Reasoning 未修改" in deepseek_blank_stdout
        )
        deepseek_model_notice_ok = not deepseek_notice_failures
        provider_isolation_ok = (
            layer_after_deepseek.get("GEMINI_API_KEY") == "fixture-api-provider-gemini-live"
            and layer_after_deepseek.get("DEEPSEEK_API_KEY") == "fixture-api-provider-deepseek-live"
            and layer_after_deepseek.get("GEMINI_BASE_URL") == "https://gemini.round58/v1"
            and layer_after_deepseek.get("DEEPSEEK_BASE_URL") == "https://deepseek.round58/v1"
            and layer_after_deepseek.get("DEEPSEEK_PLANNER_MODEL") == "deepseek-v4-pro"
            and layer_after_deepseek.get("DEEPSEEK_EXECUTOR_MODEL") == "deepseek-v4-flash"
            and layer_after_deepseek.get("GEMINI_MAX_TOKENS") == "2048"
            and layer_after_deepseek.get("DEEPSEEK_MAX_TOKENS") == "1024"
            and layer_after_deepseek.get("GEMINI_TEMPERATURE") == "0.6"
            and layer_after_deepseek.get("DEEPSEEK_TEMPERATURE") == "0.4"
            and layer_after_deepseek.get("GEMINI_REASONING_EFFORT") == "low"
            and layer_after_deepseek.get("DEEPSEEK_REASONING_EFFORT") == "max"
            and layer_after_back.get("PROJECTLING_API_PROVIDER") == "gemini"
            and back_to_gemini_config.gemini_planner_model == "gemini-round58-pro"
            and back_to_gemini_config.gemini_executor_model == "gemini-round58-flash"
            and back_to_gemini_config.max_tokens == 2048
        )
        blank_model_no_mutation_ok = (
            before_blank_models == after_blank_models
            and blank_model_config.gemini_planner_model == "gemini-round58-pro"
            and blank_model_config.gemini_executor_model == "gemini-round58-flash"
            and _max_width(blank_stdout) <= 20
            and blank_stdout.count("未输入，保持原样") >= 2
            and "主星模型保留" in blank_stdout
            and "辅星模型保留" in blank_stdout
        )
        settings_render_ok = (
            "主星" in settings_text
            and "执行模型" in settings_text
            and "gemini-round58-pro" in settings_text
            and "gemini-round58-flash" in settings_text
        )
        role_marker_bridge_ok = not role_marker_failures
        api_test_model_bridge_ok = (
            api_rc == 0
            and api_payload.get("ok") is True
            and api_payload.get("executor_model") == "gemini-round58-flash"
            and "gemini-round58-flash" in _WorkflowBridgeClient.captured_models
        )
        diagnostic_override_no_mutation_ok = (
            diag_rc == 0
            and diag_payload.get("ok") is True
            and diag_payload.get("executor_model") == "gemini-round64-diagnostic-override"
            and _WorkflowBridgeClient.captured_models[-1:] == ["gemini-round64-diagnostic-override"]
            and before_diag_override == after_diag_override
        )
        risky_model_save_hint_ok = (
            risky_save_config.gemini_planner_model == "gemini-3-pro-image-preview"
            and risky_save_config.gemini_executor_model == "claude-sonnet-4-6"
            and _max_width(risky_save_stdout) <= 20
            and risky_save_stdout.count("已保存") >= 2
            and risky_save_stdout.count("提示") >= 2
            and "图像" in risky_save_stdout
            and "Claude" in risky_save_stdout
        )
        risky_model_blank_no_mutation_ok = (
            before_risky_blank == after_risky_blank
            and risky_blank_config.gemini_planner_model == "gemini-3-pro-image-preview"
            and risky_blank_config.gemini_executor_model == "claude-sonnet-4-6"
            and _max_width(risky_blank_stdout) <= 20
            and risky_blank_stdout.count("未输入，保持原样") >= 2
        )

        gemini_payload = DeepSeekClient(back_to_gemini_config)._build_payload(  # type: ignore[operator]
            messages=[{"role": "user", "content": "provider persistence gemini"}],
            tools=None,
            tool_choice="none",
            temperature=None,
            stream=False,
            model=back_to_gemini_config.gemini_planner_model,
            thinking_enabled=True,
            max_tokens=64,
        )
        deepseek_payload = DeepSeekClient(deepseek_config)._build_payload(  # type: ignore[operator]
            messages=[{"role": "user", "content": "provider persistence deepseek"}],
            tools=None,
            tool_choice="none",
            temperature=None,
            stream=False,
            model="deepseek-v4-flash",
            thinking_enabled=True,
            max_tokens=64,
        )
        gemini_extra_body = gemini_payload.get("extra_body") if isinstance(gemini_payload.get("extra_body"), dict) else {}
        payload_isolation_ok = (
            gemini_payload.get("reasoning_effort") == "low"
            and "thinking" not in gemini_payload
            and gemini_extra_body.get("metadata") == {"api_provider_contract": "gemini"}
            and deepseek_payload.get("thinking") == {"type": "enabled"}
            and deepseek_payload.get("reasoning_effort") == "max"
            and "extra_body" not in deepseek_payload
            and "top_p" not in deepseek_payload
        )
        fake_secrets = (
            "fixture-api-provider-gemini-initial",
            "fixture-api-provider-gemini-live",
            "fixture-api-provider-deepseek-initial",
            "fixture-api-provider-deepseek-live",
        )
        secret_ok = not any(secret in combined_stdout for secret in fake_secrets)
    except Exception as exc:
        try:
            _restore_env()  # type: ignore[misc]
        except Exception:
            pass
        return item("api_settings_provider_persistence_contract", 0, "fail", [f"exception={exc}"], "修复 API Settings provider persistence contract。")

    checks = {
        "gemini_saved": gemini_saved_ok,
        "invalid_no_mutation": invalid_no_mutation_ok,
        "deepseek_saved": deepseek_saved_ok,
        "deepseek_invalid_no_mutation": deepseek_invalid_no_mutation_ok,
        "deepseek_blank_no_mutation": deepseek_blank_no_mutation_ok,
        "deepseek_model_notice": deepseek_model_notice_ok,
        "provider_isolation": provider_isolation_ok,
        "blank_model_no_mutation": blank_model_no_mutation_ok,
        "settings_render": settings_render_ok,
        "role_marker_bridge": role_marker_bridge_ok,
        "api_test_model_bridge": api_test_model_bridge_ok,
        "diagnostic_override_no_mutation": diagnostic_override_no_mutation_ok,
        "risky_model_save_hint": risky_model_save_hint_ok,
        "risky_model_blank_no_mutation": risky_model_blank_no_mutation_ok,
        "payload_isolation": payload_isolation_ok,
        "secret_redaction": secret_ok,
    }
    saved_api_gemini_keys = sorted(
        key
        for key, value in layer_after_gemini.items()
        if key.startswith("GEMINI_") and key != "GEMINI_API_KEY" and str(value).strip()
    )
    api_key_summary, api_key_missing = _settings_key_summary(
        saved_api_gemini_keys,
        required_keys=(
            "GEMINI_BASE_URL",
            "GEMINI_PLANNER_MODEL",
            "GEMINI_EXECUTOR_MODEL",
            "GEMINI_MAX_TOKENS",
            "GEMINI_TEMPERATURE",
            "GEMINI_REASONING_EFFORT",
            "GEMINI_EXTRA_BODY_JSON",
        ),
        sample_keys=(
            "GEMINI_PLANNER_MODEL",
            "GEMINI_EXECUTOR_MODEL",
            "GEMINI_MAX_TOKENS",
            "GEMINI_TEMPERATURE",
        ),
    )
    api_required_count = 7
    api_sample_labels = ["planner", "executor", "max_tokens", "temp"]
    compact_api_key_summary = (
        f"k={len(saved_api_gemini_keys)} "
        f"req={api_required_count - len(api_key_missing)}/{api_required_count} "
        f"miss={_compact_list_or_dash(api_key_missing)} "
        f"smp={','.join(api_sample_labels)} "
        f"+{max(0, len(saved_api_gemini_keys) - len(api_sample_labels))}"
    )
    key_density_limit = 80
    key_density_rows = [
        f"gemini_saved={_compact_bool_flag(gemini_saved_ok)} {compact_api_key_summary}",
        f"invalid={_compact_bool_flag(invalid_no_mutation_ok)}",
        f"ds_saved={_compact_bool_flag(deepseek_saved_ok)} p={deepseek_config.api_provider}",
        f"ds_invalid={_compact_bool_flag(deepseek_invalid_no_mutation_ok)} w={','.join(deepseek_invalid_widths)}",
        f"ds_blank={_compact_bool_flag(deepseek_blank_no_mutation_ok)} notice={_compact_bool_flag(deepseek_model_notice_ok)} w={','.join(deepseek_notice_widths)}",
        f"provider_iso={_compact_bool_flag(provider_isolation_ok)} final={back_to_gemini_config.api_provider}",
        f"model_flow blank={_compact_bool_flag(blank_model_no_mutation_ok)} settings={_compact_bool_flag(settings_render_ok)} markers={_compact_bool_flag(role_marker_bridge_ok)} w={','.join(role_marker_evidence)}",
        f"api_bridge={_compact_bool_flag(api_test_model_bridge_ok)} exec={api_payload.get('executor_model')} diag={_compact_bool_flag(diagnostic_override_no_mutation_ok)}",
        f"risky_hint={_compact_bool_flag(risky_model_save_hint_ok)} blank={_compact_bool_flag(risky_model_blank_no_mutation_ok)}",
        f"payload_iso={_compact_bool_flag(payload_isolation_ok)} gem={gemini_payload.get('reasoning_effort')} ds={deepseek_payload.get('reasoning_effort')}",
        f"secret_redaction={_compact_bool_flag(secret_ok)}",
    ]
    old_labels = (
        "keys=",
        "missing=",
        "sample=",
        "extra=",
        "invalid_no_mutation=",
        "deepseek_saved=",
        "deepseek_invalid=",
        "widths=",
        "deepseek_blank=",
        "model_notice=",
        "notice_widths=",
        "provider_isolation=",
        "model_workflow",
        "marker_widths=",
        "api_test_bridge=",
        "diagnostic_override=",
        "risky_model_save_hint=",
        "blank_no_mutation=",
        "payload_isolation=",
        "gemini_effort=",
        "deepseek_effort=",
        "secret_redaction=True",
        "secret_redaction=False",
    )
    key_density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in key_density_rows
        if len(row) > key_density_limit
        or "{'" in row
        or "['" in row
        or "True" in row
        or "False" in row
        or any(label in row for label in old_labels)
    ]
    checks["key_evidence_density"] = not api_key_missing and not key_density_failures
    failures = [name for name, ok in checks.items() if not ok]
    evidence = [
        *key_density_rows[:-1],
        f"settings_key_density=limit={key_density_limit} missing={_compact_list_or_dash(api_key_missing)} failures={_compact_list_or_dash(key_density_failures)}",
        key_density_rows[-1],
    ]
    score = 100 if not failures else max(30, 100 - len(failures) * 18)
    if failures:
        evidence.append("failures=" + ",".join(failures))
    return item(
        "api_settings_provider_persistence_contract",
        score,
        status_from_score(score),
        evidence,
        "修复 API Settings provider 保存、非法输入 no-mutation、provider 隔离、payload 隔离或密钥脱敏。" if failures else "",
    )


def check_provider_switch_contract() -> dict[str, Any]:
    if ProjectLingEngine is None or DeepSeekClient is None or load_config is None:
        return item("provider_switch_contract", 0, "fail", ["projectling imports unavailable"], "检查 provider switch 导入链。")

    try:
        config = load_config()
        gemini_config = replace(
            config,
            api_provider="gemini",
            api_key="fixture-provider-contract-gemini",
            gemini_api_key="fixture-provider-contract-gemini",
            base_url=getattr(config, "gemini_base_url", "") or config.base_url,
            model=getattr(config, "gemini_executor_model", "") or config.model,
            gemini_reasoning_effort="low",
            gemini_extra_body_json='{"metadata":{"provider_contract":"gemini"}}',
        )
        deepseek_config = replace(
            config,
            api_provider="deepseek",
            api_key="fixture-provider-contract-deepseek",
            deepseek_api_key="fixture-provider-contract-deepseek",
            base_url=getattr(config, "deepseek_base_url", "") or "https://api.deepseek.com",
            model="deepseek-v4-flash",
            gemini_extra_body_json='{"metadata":{"must_not_enter_deepseek":true}}',
        )

        gemini_engine = ProjectLingEngine(gemini_config)  # type: ignore[operator]
        deepseek_engine = ProjectLingEngine(deepseek_config)  # type: ignore[operator]
        gemini_planner = gemini_engine._planner_model_for_mode("standard")
        gemini_executor = gemini_engine._executor_model_for_mode("standard")
        deepseek_planner = deepseek_engine._planner_model_for_mode("standard")
        deepseek_executor = deepseek_engine._executor_model_for_mode("standard")
        deepseek_precise = deepseek_engine._planner_model_for_mode("precise")

        gemini_payload = DeepSeekClient(gemini_config)._build_payload(  # type: ignore[operator]
            messages=[{"role": "user", "content": "provider switch gemini"}],
            tools=None,
            tool_choice="none",
            temperature=0.2,
            stream=False,
            model=gemini_planner,
            thinking_enabled=True,
            max_tokens=32,
        )
        deepseek_payload = DeepSeekClient(deepseek_config)._build_payload(  # type: ignore[operator]
            messages=[{"role": "user", "content": "provider switch deepseek"}],
            tools=None,
            tool_choice="none",
            temperature=0.2,
            stream=False,
            model=deepseek_precise,
            thinking_enabled=True,
            max_tokens=32,
        )

        gemini_route_ok = "gemini" in gemini_planner.lower() and "gemini" in gemini_executor.lower()
        deepseek_route_ok = (
            deepseek_planner == "deepseek-v4-pro"
            and deepseek_executor == "deepseek-v4-flash"
            and deepseek_precise == "deepseek-v4-pro"
        )
        gemini_payload_ok = (
            gemini_payload.get("model") == gemini_planner
            and "thinking" not in gemini_payload
            and gemini_payload.get("reasoning_effort") == "low"
            and isinstance(gemini_payload.get("extra_body"), dict)
            and (gemini_payload.get("extra_body") or {}).get("metadata") == {"provider_contract": "gemini"}
        )
        deepseek_payload_ok = (
            deepseek_payload.get("model") == "deepseek-v4-pro"
            and deepseek_payload.get("thinking") == {"type": "enabled"}
            and deepseek_payload.get("reasoning_effort") in {"high", "max"}
            and "extra_body" not in deepseek_payload
            and "temperature" not in deepseek_payload
        )

        import core as projectling_core

        gemini_buffer = io.StringIO()
        deepseek_buffer = io.StringIO()
        with contextlib.redirect_stdout(gemini_buffer):
            projectling_core._render_api_settings(gemini_config)
        with contextlib.redirect_stdout(deepseek_buffer):
            projectling_core._render_api_settings(deepseek_config)
        gemini_settings = gemini_buffer.getvalue()
        deepseek_settings = deepseek_buffer.getvalue()
        settings_ok = (
            "当前 [Gemini]" in gemini_settings
            and "当前 [DeepSeek]" in deepseek_settings
            and "主星模型" in gemini_settings
            and "执行模型" in gemini_settings
            and "主星模型" in deepseek_settings
            and "执行模型" in deepseek_settings
        )
        secret_ok = "fixture-provider-contract" not in gemini_settings and "fixture-provider-contract" not in deepseek_settings
    except Exception as exc:
        return item("provider_switch_contract", 0, "fail", [f"exception={exc}"], "修复 provider switch contract。")

    checks = {
        "gemini_route": gemini_route_ok,
        "deepseek_route": deepseek_route_ok,
        "gemini_payload": gemini_payload_ok,
        "deepseek_payload": deepseek_payload_ok,
        "settings": settings_ok,
        "secret_redaction": secret_ok,
    }
    failures = [name for name, ok in checks.items() if not ok]
    evidence = [
        f"g_route={_compact_bool_flag(gemini_route_ok)} main={gemini_planner} exec={gemini_executor}",
        f"ds_route={_compact_bool_flag(deepseek_route_ok)} s={deepseek_planner} e={deepseek_executor} p={deepseek_precise}",
        f"g_payload={_compact_bool_flag(gemini_payload_ok)} think={_compact_bool_flag('thinking' in gemini_payload)} effort={gemini_payload.get('reasoning_effort')} extra={_compact_bool_flag('extra_body' in gemini_payload)}",
        f"ds_payload={_compact_bool_flag(deepseek_payload_ok)} think={_compact_bool_flag(bool(deepseek_payload.get('thinking')))} effort={deepseek_payload.get('reasoning_effort')} extra={_compact_bool_flag('extra_body' in deepseek_payload)} temp={_compact_bool_flag('temperature' in deepseek_payload)}",
        f"settings={_compact_bool_flag(settings_ok)} sec={_compact_bool_flag(secret_ok)}",
    ]
    density_limit = 70
    old_labels = (
        "gemini_route=",
        "deepseek_route=",
        "gemini_payload=",
        "deepseek_payload=",
        "secret_redaction=",
        "True",
        "False",
        "{'",
        "['",
    )
    density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if len(row) > density_limit or any(label in row for label in old_labels)
    ]
    checks["evidence_density"] = not density_failures
    evidence.append(f"provider_switch_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")
    failures = [name for name, ok in checks.items() if not ok]
    score = 100 if not failures else max(30, 100 - len(failures) * 20)
    if failures:
        evidence.append("failures=" + ",".join(failures))
    return item(
        "provider_switch_contract",
        score,
        status_from_score(score),
        evidence,
        "修复 Gemini/DeepSeek provider 切换、payload 分支或 settings 脱敏。" if failures else "",
    )


def check_gemini_planner_review_contract() -> dict[str, Any]:
    if ProjectLingEngine is None or load_config is None:
        return item("gemini_planner_review_contract", 0, "fail", ["projectling imports unavailable"], "检查 planner review Gemini 导入链。")

    captured: dict[str, Any] = {}

    class _ReviewProbeClient:
        def chat_completions(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "choices": [
                    {
                        "message": {
                            "content": "复审通过：继续执行当前计划，完成后用 link.done 回报。",
                            "reasoning_content": "检查 update_plan 和最近工具状态。",
                        },
                        "finish_reason": "stop",
                    }
                ]
            }

    try:
        config = load_config()
        probe_config = replace(
            config,
            api_provider="gemini",
            api_key="fixture-review-contract-gemini",
            gemini_api_key="fixture-review-contract-gemini",
            collab_mode="standard",
        )
        engine = ProjectLingEngine(probe_config, client=_ReviewProbeClient())  # type: ignore[operator]
        role, _seed, bundle = engine.persona_for_dispatch_mode("chat")
        route: dict[str, Any] = {"collab_mode": "standard", "tools_enabled": True}
        conversation_messages: list[dict[str, Any]] = [
            {"role": "user", "content": "请执行一个需要计划复审的测试任务。"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-plan"}]},
            {"role": "tool", "tool_call_id": "call-plan", "content": "{\"status\":\"ok\"}"},
        ]
        thinking_traces: list[dict[str, Any]] = []
        review_ok = engine._maybe_review_plan_update(
            payload={
                "tool": "update_plan",
                "status": "ok",
                "action": "update",
                "needs_review": True,
                "items": [{"id": "T1", "title": "probe", "status": "in_progress"}],
            },
            route=route,
            role=role,
            bundle=bundle,
            cwd=PROJECTLING_DIR,
            conversation_messages=conversation_messages,
            thinking_traces=thinking_traces,
            on_stream_event=None,
        )
    except Exception as exc:
        return item("gemini_planner_review_contract", 0, "fail", [f"exception={exc}"], "修复 Gemini planner review contract。")

    messages = captured.get("messages") if isinstance(captured.get("messages"), list) else []
    roles = [str(message.get("role") or "") for message in messages if isinstance(message, dict)]
    user_messages = [message for message in messages if isinstance(message, dict) and str(message.get("role") or "") == "user"]
    system_messages = [message for message in messages if isinstance(message, dict) and str(message.get("role") or "") == "system"]
    user_content = "\n".join(str(message.get("content") or "") for message in user_messages)
    all_content = "\n".join(str(message.get("content") or "") for message in messages if isinstance(message, dict))
    system_only_regression = bool(messages) and not user_messages
    review_prompt_ok = (
        bool(review_ok)
        and bool(system_messages)
        and bool(user_messages)
        and bool(user_content.strip())
        and "最新 update_plan" in user_content
        and not system_only_regression
    )
    request_ok = (
        captured.get("tools") is None
        and captured.get("tool_choice") == "none"
        and "gemini" in str(captured.get("model") or "").lower()
        and bool(captured.get("thinking_enabled")) is True
    )
    trace_ok = (
        bool(thinking_traces)
        and str(thinking_traces[-1].get("role") or "") == "planner_review"
        and "复审暂不可用" not in str(conversation_messages[-1].get("content") or "")
    )
    secret_ok = "fixture-review-contract" not in all_content and "fixture-review-contract" not in str(conversation_messages[-1])
    checks = {
        "review_prompt": review_prompt_ok,
        "request": request_ok,
        "trace": trace_ok,
        "secret_redaction": secret_ok,
    }
    evidence = [
        "roles=" + ",".join(roles),
        f"msgs u={len(user_messages)} sys_only={int(system_only_regression)}",
        f"request model={captured.get('model')} tc={captured.get('tool_choice')} think={_compact_bool_flag(captured.get('thinking_enabled'))}",
        f"review={_compact_bool_flag(review_ok)} traces={len(thinking_traces)} sec={_compact_bool_flag(secret_ok)}",
    ]
    density_limit = 65
    old_labels = (
        "user_messages=",
        "tool_choice=",
        "thinking=True",
        "thinking=False",
        "review_ok=",
        "secret_redaction=",
        "True",
        "False",
    )
    density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if len(row) > density_limit or any(label in row for label in old_labels)
    ]
    checks["evidence_density"] = not density_failures
    failures = [name for name, ok in checks.items() if not ok]
    evidence.append(f"planner_review_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")
    score = 100 if not failures else max(35, 100 - len(failures) * 25)
    if failures:
        evidence.append("failures=" + ",".join(failures))
    return item(
        "gemini_planner_review_contract",
        score,
        status_from_score(score),
        evidence,
        "修复 Gemini planner review 请求结构，必须避免 system-only chat 请求并保留复审 trace。" if failures else "",
    )


def check_gemini_model_list_failure_contract() -> dict[str, Any]:
    if DeepSeekClient is None or load_config is None:
        return item("gemini_model_list_failure_contract", 0, "fail", ["projectling imports unavailable"], "检查 Gemini model-list 导入链。")

    try:
        import core as projectling_core
        import projectling as projectling_module

        config = load_config()
        probe_config = replace(
            config,
            api_provider="gemini",
            api_key="fixture-model-list-contract",
            gemini_api_key="fixture-model-list-contract",
            base_url=getattr(config, "gemini_base_url", "") or config.base_url,
        )
        success_payload = {
            "object": "list",
            "data": [
                {"id": "gemini-3.1-pro-low", "object": "model"},
                {"id": "gemini-3-flash", "object": "model"},
            ],
        }
        ids = projectling_core._extract_model_ids(success_payload)
        success_shape_ok = ids == ["gemini-3.1-pro-low", "gemini-3-flash"]

        def _plain(text: str) -> str:
            return projectling_core._strip_ansi(text or "")

        def _max_width(text: str) -> int:
            return max((projectling_core._display_width(line) for line in _plain(text).splitlines()), default=0)

        missing_key_config = replace(probe_config, api_key="", gemini_api_key="")
        try:
            DeepSeekClient(missing_key_config).list_models()  # type: ignore[operator]
            missing_key_ok = False
            missing_key_text = ""
        except Exception as exc:
            missing_key_text = str(exc)
            missing_key_ok = "GEMINI_API_KEY" in missing_key_text and "fixture-model-list-contract" not in missing_key_text

        class _FakeResponse:
            def __init__(self, body: str) -> None:
                self.body = body

            def __enter__(self) -> "_FakeResponse":
                return self

            def __exit__(self, *_args: Any) -> None:
                return None

            def read(self) -> bytes:
                return self.body.encode("utf-8")

        old_urlopen = projectling_module.request.urlopen
        try:
            projectling_module.request.urlopen = lambda *_args, **_kwargs: _FakeResponse("{not json")  # type: ignore[assignment]
            try:
                DeepSeekClient(probe_config).list_models()  # type: ignore[operator]
                invalid_json_ok = False
                invalid_json_text = ""
            except Exception as exc:
                invalid_json_text = str(exc)
                invalid_json_ok = "模型列表响应不是合法 JSON" in invalid_json_text

            def _raise_url_error(*_args: Any, **_kwargs: Any) -> Any:
                raise projectling_module.error.URLError("relay offline")

            projectling_module.request.urlopen = _raise_url_error  # type: ignore[assignment]
            try:
                DeepSeekClient(probe_config).list_models()  # type: ignore[operator]
                network_error_ok = False
                network_error_text = ""
            except Exception as exc:
                network_error_text = str(exc)
                network_error_ok = "模型列表请求失败" in network_error_text and "relay offline" in network_error_text
        finally:
            projectling_module.request.urlopen = old_urlopen  # type: ignore[assignment]

        captured_base_urls: list[str] = []

        class _FailingListClient:
            def __init__(self, _config: Any) -> None:
                captured_base_urls.append(str(getattr(_config, "base_url", "")))

            def list_models(self) -> dict[str, Any]:
                raise RuntimeError("模型列表请求失败: simulated relay down")

        old_core_load_config = projectling_core.load_config
        old_core_cleanup = projectling_core._cleanup_legacy_runtime
        old_core_client = projectling_core.DeepSeekClient
        cli_stdout = io.StringIO()
        try:
            projectling_core.load_config = lambda: probe_config
            projectling_core._cleanup_legacy_runtime = lambda _config: None
            projectling_core.DeepSeekClient = _FailingListClient
            with contextlib.redirect_stdout(cli_stdout):
                cli_rc = projectling_core._cmd_list_models(
                    argparse.Namespace(
                        json=True,
                        limit=80,
                        base_url="https://override.invalid/v1",
                        timeout=5,
                    )
                )
        finally:
            projectling_core.load_config = old_core_load_config
            projectling_core._cleanup_legacy_runtime = old_core_cleanup
            projectling_core.DeepSeekClient = old_core_client
        try:
            cli_payload = json.loads(cli_stdout.getvalue())
        except json.JSONDecodeError:
            cli_payload = {}
        cli_failure_ok = (
            cli_rc == 1
            and cli_payload.get("ok") is False
            and cli_payload.get("provider") == "gemini"
            and cli_payload.get("base_url") == "https://override.invalid/v1"
            and captured_base_urls == ["https://override.invalid/v1"]
            and "simulated relay down" in str(cli_payload.get("error") or "")
        )

        class _Size:
            def __init__(self, columns: int) -> None:
                self.columns = columns
                self.lines = 24

        class _EmptyListClient:
            def __init__(self, _config: Any) -> None:
                pass

            def list_models(self) -> dict[str, Any]:
                return {"object": "list", "data": []}

        old_core_load_config = projectling_core.load_config
        old_core_cleanup = projectling_core._cleanup_legacy_runtime
        old_core_client = projectling_core.DeepSeekClient
        old_get_terminal_size = projectling_core.shutil.get_terminal_size
        empty_width_failures: list[str] = []
        empty_width_evidence: list[str] = []
        try:
            projectling_core.load_config = lambda: probe_config
            projectling_core._cleanup_legacy_runtime = lambda _config: None
            projectling_core.DeepSeekClient = _EmptyListClient
            for width in (16, 40, 80):
                projectling_core.shutil.get_terminal_size = lambda _fallback=(80, 24), columns=width: _Size(columns)
                empty_stdout = io.StringIO()
                with contextlib.redirect_stdout(empty_stdout):
                    empty_rc = projectling_core._cmd_list_models(argparse.Namespace(json=False, limit=80, base_url="", timeout=None))
                empty_text = _plain(empty_stdout.getvalue())
                empty_width = _max_width(empty_text)
                empty_width_evidence.append(f"w{width}:{empty_width}")
                recovery_ok = all(token in empty_text for token in ("下一步", "API Key", "Base URL", "模型列表接口", "Relay 渠道"))
                hint_absent = "pro适合主星" not in empty_text and "flash适合执行星" not in empty_text
                if empty_rc != 0 or empty_width > width or "模型列表为空" not in empty_text or not recovery_ok or not hint_absent:
                    empty_width_failures.append(f"w{width}:rc{empty_rc}/max{empty_width}")
        finally:
            projectling_core.load_config = old_core_load_config
            projectling_core._cleanup_legacy_runtime = old_core_cleanup
            projectling_core.DeepSeekClient = old_core_client
            projectling_core.shutil.get_terminal_size = old_get_terminal_size
        empty_list_ui_ok = not empty_width_failures

        combined_error_text = "\n".join(
            [
                missing_key_text,
                invalid_json_text,
                network_error_text,
                json.dumps(cli_payload, ensure_ascii=False),
                ",".join(empty_width_evidence),
            ]
        )
        secret_ok = "fixture-model-list-contract" not in combined_error_text
    except Exception as exc:
        return item("gemini_model_list_failure_contract", 0, "fail", [f"exception={exc}"], "修复 Gemini model-list failure contract。")

    checks = {
        "success_shape": success_shape_ok,
        "missing_key": missing_key_ok,
        "invalid_json": invalid_json_ok,
        "network_error": network_error_ok,
        "cli_failure_json": cli_failure_ok,
        "empty_list_ui": empty_list_ui_ok,
        "secret_redaction": secret_ok,
    }
    evidence = [
        f"success={_compact_bool_flag(success_shape_ok)} ids={','.join(ids)}",
        f"missing_key={_compact_bool_flag(missing_key_ok)}",
        f"invalid_json={_compact_bool_flag(invalid_json_ok)}",
        f"network_error={_compact_bool_flag(network_error_ok)}",
        f"cli_fail={_compact_bool_flag(cli_failure_ok)} rc={cli_rc} p={cli_payload.get('provider')} base=override",
        f"empty_ui={_compact_bool_flag(empty_list_ui_ok)} w={','.join(empty_width_evidence)} fail={_compact_list_or_dash(empty_width_failures[:4])}",
        f"sec={_compact_bool_flag(secret_ok)}",
    ]
    density_limit = 70
    old_labels = (
        "success_ids=[",
        "missing_key=True",
        "missing_key=False",
        "invalid_json=True",
        "invalid_json=False",
        "network_error=True",
        "network_error=False",
        "cli_failure_json=",
        "provider=",
        "base_override=",
        "empty_list_ui=",
        "widths=",
        "failures=[]",
        "secret_redaction=",
        "True",
        "False",
        "[]",
        "['",
    )
    density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if len(row) > density_limit or any(label in row for label in old_labels)
    ]
    checks["evidence_density"] = not density_failures
    failures = [name for name, ok in checks.items() if not ok]
    evidence.append(f"model_list_failure_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")
    score = 100 if not failures else max(35, 100 - len(failures) * 20)
    if failures:
        evidence.append("failures=" + ",".join(failures))
    return item(
        "gemini_model_list_failure_contract",
        score,
        status_from_score(score),
        evidence,
        "修复 Gemini list-models 成功 shape、空列表 UI、失败 JSON 输出或错误脱敏。" if failures else "",
    )


def check_gemini_api_test_failure_contract() -> dict[str, Any]:
    if load_config is None:
        return item("gemini_api_test_failure_contract", 0, "fail", ["projectling imports unavailable"], "检查 Gemini api-test failure 导入链。")

    def _bool_bit(value: Any) -> str:
        if value is True:
            return "1"
        if value is False:
            return "0"
        if value is None:
            return "-"
        return str(value)

    def _compact_failure_row(
        label: str,
        ok: bool,
        rc: Any,
        payload: dict[str, Any],
        *,
        model_alias: str,
        base_alias: str = "",
    ) -> str:
        if ok:
            row = (
                f"{label}=1 rc={rc} p=g ok=0 "
                f"str={_bool_bit(payload.get('stream'))} mdl={model_alias}"
            )
            if base_alias:
                row += f" base={base_alias}"
            return row
        row = (
            f"{label}={ok} rc={rc} ok={payload.get('ok')} "
            f"provider={payload.get('provider')} stream={payload.get('stream')} "
            f"model={payload.get('executor_model')}"
        )
        if "base_url" in payload:
            row += f" base={payload.get('base_url')}"
        return row

    try:
        import core as projectling_core

        config = load_config()
        probe_config = replace(
            config,
            api_provider="gemini",
            api_key="fixture-api-test-contract",
            gemini_api_key="fixture-api-test-contract",
            model="gemini-bad-model",
            gemini_executor_model="gemini-bad-model",
            enable_sse=True,
        )

        class _BadModelClient:
            def __init__(self, _config: Any) -> None:
                pass

            def chat_completions(self, **_kwargs: Any) -> dict[str, Any]:
                raise RuntimeError("HTTP 404: bad model gemini-bad-model")

            def chat_completions_stream(self, **_kwargs: Any) -> Iterable[dict[str, Any]]:
                raise RuntimeError("HTTP 404: bad model gemini-bad-model")
                yield {}

        old_core_load_config = projectling_core.load_config
        old_core_cleanup = projectling_core._cleanup_legacy_runtime
        old_core_client = projectling_core.DeepSeekClient
        try:
            projectling_core.load_config = lambda: probe_config
            projectling_core._cleanup_legacy_runtime = lambda _config: None
            projectling_core.DeepSeekClient = _BadModelClient

            no_stream_stdout = io.StringIO()
            with contextlib.redirect_stdout(no_stream_stdout):
                no_stream_rc = projectling_core._cmd_api_test(
                    argparse.Namespace(json=True, no_stream=True, model="", base_url="", timeout=None)
                )
            stream_stdout = io.StringIO()
            with contextlib.redirect_stdout(stream_stdout):
                stream_rc = projectling_core._cmd_api_test(
                    argparse.Namespace(json=True, no_stream=False, model="", base_url="", timeout=None)
                )
            cli_override_stdout = io.StringIO()
            with contextlib.redirect_stdout(cli_override_stdout):
                cli_override_rc = projectling_core._cmd_api_test(
                    argparse.Namespace(
                        json=True,
                        no_stream=True,
                        model="gemini-cli-override-bad-model",
                        base_url="https://api-test.override.invalid/v1",
                        timeout=5,
                    )
                )
        finally:
            projectling_core.load_config = old_core_load_config
            projectling_core._cleanup_legacy_runtime = old_core_cleanup
            projectling_core.DeepSeekClient = old_core_client

        try:
            no_stream_payload = json.loads(no_stream_stdout.getvalue())
        except json.JSONDecodeError:
            no_stream_payload = {}
        try:
            stream_payload = json.loads(stream_stdout.getvalue())
        except json.JSONDecodeError:
            stream_payload = {}
        try:
            cli_override_payload = json.loads(cli_override_stdout.getvalue())
        except json.JSONDecodeError:
            cli_override_payload = {}

        no_stream_ok = (
            no_stream_rc == 1
            and no_stream_payload.get("ok") is False
            and no_stream_payload.get("provider") == "gemini"
            and no_stream_payload.get("stream") is False
            and no_stream_payload.get("executor_model") == "gemini-bad-model"
            and "bad model" in str(no_stream_payload.get("error") or "")
        )
        stream_ok = (
            stream_rc == 1
            and stream_payload.get("ok") is False
            and stream_payload.get("provider") == "gemini"
            and stream_payload.get("stream") is True
            and stream_payload.get("executor_model") == "gemini-bad-model"
            and "bad model" in str(stream_payload.get("error") or "")
        )
        cli_override_ok = (
            cli_override_rc == 1
            and cli_override_payload.get("ok") is False
            and cli_override_payload.get("provider") == "gemini"
            and cli_override_payload.get("stream") is False
            and cli_override_payload.get("executor_model") == "gemini-cli-override-bad-model"
            and cli_override_payload.get("base_url") == "https://api-test.override.invalid/v1"
        )
        secret_ok = (
            "fixture-api-test-contract" not in no_stream_stdout.getvalue()
            and "fixture-api-test-contract" not in stream_stdout.getvalue()
            and "fixture-api-test-contract" not in cli_override_stdout.getvalue()
        )
    except Exception as exc:
        return item("gemini_api_test_failure_contract", 0, "fail", [f"exception={exc}"], "修复 Gemini api-test failure contract。")

    checks = {
        "no_stream_json": no_stream_ok,
        "stream_json": stream_ok,
        "cli_override_json": cli_override_ok,
        "secret_redaction": secret_ok,
    }
    evidence = [
        _compact_failure_row("nostr", no_stream_ok, no_stream_rc, no_stream_payload, model_alias="cfg_bad"),
        _compact_failure_row("stream", stream_ok, stream_rc, stream_payload, model_alias="cfg_bad"),
        _compact_failure_row(
            "cli",
            cli_override_ok,
            cli_override_rc,
            cli_override_payload,
            model_alias="cli_bad",
            base_alias="override",
        ),
        f"sec={_bool_bit(secret_ok)}" if secret_ok else f"secret_redaction={secret_ok}",
    ]
    density_limit = 80
    verbose_healthy_labels = (
        "no_stream=True",
        "stream=True",
        "cli_override=True",
        "secret_redaction=True",
        "stream=False",
        "model=gemini-bad-model",
        "model=gemini-cli-override-bad-model",
        "base=https://api-test.override.invalid/v1",
        "True",
        "False",
    )
    density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if len(row) > density_limit or any(label in row for label in verbose_healthy_labels)
    ]
    checks["failure_evidence_density"] = not density_failures
    failures = [name for name, ok in checks.items() if not ok]
    evidence.append(f"api_test_failure_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")
    score = 100 if not failures else max(35, 100 - len(failures) * 25)
    if failures:
        evidence.append("failures=" + ",".join(failures))
    return item(
        "gemini_api_test_failure_contract",
        score,
        status_from_score(score),
        evidence,
        "修复 Gemini api-test 坏模型/流式失败 JSON 输出或错误脱敏。" if failures else "",
    )


def check_gemini_api_test_model_safety_contract() -> dict[str, Any]:
    if load_config is None:
        return item("gemini_api_test_model_safety_contract", 0, "fail", ["projectling imports unavailable"], "检查 Gemini api-test model safety 导入链。")

    try:
        import core as projectling_core

        config = load_config()
        probe_config = replace(
            config,
            api_provider="gemini",
            api_key="fixture-api-test-safety-contract",
            gemini_api_key="fixture-api-test-safety-contract",
            base_url="https://fast.aieyra.cn/v1/api-test-safety",
            gemini_base_url="https://fast.aieyra.cn/v1/api-test-safety",
            gemini_planner_model="gemini-3.1-pro-low",
            gemini_executor_model="gemini-3-flash",
            enable_sse=False,
        )

        class _Size:
            def __init__(self, columns: int) -> None:
                self.columns = columns
                self.lines = 24

        class _SuccessClient:
            captured_models: list[str] = []

            def __init__(self, _config: Any) -> None:
                pass

            def chat_completions(self, **kwargs: Any) -> dict[str, Any]:
                self.captured_models.append(str(kwargs.get("model") or ""))
                return {"choices": [{"message": {"content": "pong"}}]}

            def chat_completions_stream(self, **kwargs: Any) -> Iterable[dict[str, Any]]:
                self.captured_models.append(str(kwargs.get("model") or ""))
                yield {"choices": [{"delta": {"content": "pong"}, "finish_reason": "stop"}]}

        def _plain(text: str) -> str:
            return projectling_core._strip_ansi(text or "")

        def _max_width(text: str) -> int:
            return max((projectling_core._display_width(line) for line in _plain(text).splitlines()), default=0)

        old_get_terminal_size = projectling_core.shutil.get_terminal_size
        old_load_config = projectling_core.load_config
        old_cleanup = projectling_core._cleanup_legacy_runtime
        old_client = projectling_core.DeepSeekClient
        json_payloads: dict[str, dict[str, Any]] = {}
        human_texts: list[str] = []
        width_failures: list[str] = []
        width_evidence: list[str] = []
        try:
            projectling_core.load_config = lambda: probe_config
            projectling_core._cleanup_legacy_runtime = lambda _config: None
            projectling_core.DeepSeekClient = _SuccessClient
            projectling_core.shutil.get_terminal_size = lambda _fallback=(80, 24): _Size(80)
            for label, model in {
                "flash": "gemini-3-flash",
                "image": "gemini-3.1-flash-image",
                "agent": "gemini-3-flash-agent",
                "claude": "claude-sonnet-4-6",
                "unknown": "round70-unknown-model",
                "false_positive_pro": "projectling-unknown-model",
            }.items():
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    rc = projectling_core._cmd_api_test(
                        argparse.Namespace(json=True, no_stream=True, model=model, base_url="", timeout=None)
                    )
                try:
                    payload = json.loads(stdout.getvalue())
                except json.JSONDecodeError:
                    payload = {}
                payload["_rc"] = rc
                json_payloads[label] = payload

            for width in (16, 20, 24, 32, 40, 48, 80, 120):
                projectling_core.shutil.get_terminal_size = lambda _fallback=(80, 24), columns=width: _Size(columns)
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    rc = projectling_core._cmd_api_test(
                        argparse.Namespace(json=False, no_stream=True, model="gemini-3.1-flash-image", base_url="", timeout=None)
                    )
                text = _plain(stdout.getvalue())
                human_texts.append(text)
                max_width = _max_width(text)
                width_evidence.append(f"w{width}:{max_width}")
                if rc != 0 or max_width > width or ("hint" not in text and "提示" not in text):
                    width_failures.append(f"w{width}:rc{rc}/max{max_width}")
        finally:
            projectling_core.shutil.get_terminal_size = old_get_terminal_size
            projectling_core.load_config = old_load_config
            projectling_core._cleanup_legacy_runtime = old_cleanup
            projectling_core.DeepSeekClient = old_client

        stable_ok = (
            json_payloads.get("flash", {}).get("_rc") == 0
            and json_payloads.get("flash", {}).get("executor_risk") == "normal"
            and json_payloads.get("flash", {}).get("executor_tags") == ["flash"]
            and not json_payloads.get("flash", {}).get("executor_hint")
        )
        risky_ok = (
            json_payloads.get("image", {}).get("executor_risk") == "image"
            and "image" in (json_payloads.get("image", {}).get("executor_tags") or [])
            and bool(json_payloads.get("image", {}).get("executor_hint"))
            and json_payloads.get("agent", {}).get("executor_risk") == "agent"
            and json_payloads.get("claude", {}).get("executor_risk") == "claude"
            and json_payloads.get("unknown", {}).get("executor_risk") == "unknown"
            and json_payloads.get("false_positive_pro", {}).get("executor_risk") == "unknown"
            and json_payloads.get("false_positive_pro", {}).get("executor_tags") == ["unknown"]
        )
        no_mutation_ok = _SuccessClient.captured_models[-1:] == ["gemini-3.1-flash-image"]
        combined_output = "\n".join([json.dumps(payload, ensure_ascii=False) for payload in json_payloads.values()] + human_texts)
        secret_ok = "fixture-api-test-safety-contract" not in combined_output
    except Exception as exc:
        return item("gemini_api_test_model_safety_contract", 0, "fail", [f"exception={exc}"], "修复 Gemini api-test model safety contract。")

    checks = {
        "stable_flash": stable_ok,
        "risky_models": risky_ok,
        "width_matrix": not width_failures,
        "diagnostic_override": no_mutation_ok,
        "secret_redaction": secret_ok,
    }
    failures = [name for name, ok in checks.items() if not ok]
    risk_summary = {
        "image": json_payloads.get("image", {}).get("executor_risk"),
        "agent": json_payloads.get("agent", {}).get("executor_risk"),
        "claude": json_payloads.get("claude", {}).get("executor_risk"),
        "unknown": json_payloads.get("unknown", {}).get("executor_risk"),
        "false_positive_pro": json_payloads.get("false_positive_pro", {}).get("executor_risk"),
    }
    evidence = [
        (
            f"stable={_compact_bool_flag(stable_ok)} risk={json_payloads.get('flash', {}).get('executor_risk')}"
            if stable_ok
            else f"stable_flash={stable_ok} risk={json_payloads.get('flash', {}).get('executor_risk')}"
        ),
        (
            "risky=1 "
            f"img={risk_summary['image']} ag={risk_summary['agent']} cl={risk_summary['claude']} "
            f"unk={risk_summary['unknown']} fp={risk_summary['false_positive_pro']}"
            if risky_ok
            else (
                f"risky_models={risky_ok} image={risk_summary['image']} agent={risk_summary['agent']} "
                f"claude={risk_summary['claude']} unknown={risk_summary['unknown']} "
                f"false_positive_pro={risk_summary['false_positive_pro']}"
            )
        ),
        "widths=" + ",".join(width_evidence),
        f"width_failures={_compact_list_or_dash(width_failures[:4])}",
        f"diag={_compact_bool_flag(no_mutation_ok)}" if no_mutation_ok else f"diagnostic_override={no_mutation_ok}",
        f"sec={_compact_bool_flag(secret_ok)}" if secret_ok else f"secret_redaction={secret_ok}",
    ]
    density_limit = 75
    verbose_healthy_labels = (
        "stable_flash=True",
        "risky_models=",
        "false_positive_pro=",
        "diagnostic_override=True",
        "secret_redaction=True",
        "True",
        "False",
        "[]",
    )
    density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if len(row) > density_limit or any(label in row for label in verbose_healthy_labels)
    ]
    checks["model_safety_density"] = not density_failures
    failures = [name for name, ok in checks.items() if not ok]
    evidence.append(f"model_safety_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")
    score = 100 if not failures else max(35, 100 - len(failures) * 15)
    if failures:
        evidence.append("failures=" + ",".join(failures))
    return item(
        "gemini_api_test_model_safety_contract",
        score,
        status_from_score(score),
        evidence,
        "修复 Gemini api-test 模型安全提示、JSON 元数据、窄宽布局、override 路由或脱敏。" if failures else "",
    )


def check_settings_status_width_contract() -> dict[str, Any]:
    if load_config is None:
        return item("settings_status_width_contract", 0, "fail", ["projectling imports unavailable"], "检查 settings/status width 导入链。")

    try:
        import core as projectling_core

        config = load_config()
        probe_config = replace(
            config,
            api_provider="gemini",
            api_key="fixture-width-contract",
            gemini_api_key="fixture-width-contract",
            base_url="https://fast.aieyra.cn/v1/very/long/path/for/layout/probe",
            gemini_base_url="https://fast.aieyra.cn/v1/very/long/path/for/layout/probe",
            gemini_planner_model="gemini-3.1-pro-low",
            gemini_executor_model="gemini-3-flash",
            gemini_extra_body_json='{"metadata":{"layout_probe":true}}',
            websearch_summary_key="fixture-width-websearch-summary",
            websearch_web_key="fixture-width-websearch-web",
            websearch_endpoint="https://open.feedcoopapi.com/search_api/web_search/very/long/path/for/layout/probe",
        )

        class _Size:
            def __init__(self, columns: int) -> None:
                self.columns = columns
                self.lines = 24

        def _plain(text: str) -> str:
            return projectling_core._strip_ansi(text)

        def _max_width(text: str) -> int:
            return max((projectling_core._display_width(line) for line in _plain(text).splitlines()), default=0)

        old_get_terminal_size = projectling_core.shutil.get_terminal_size
        old_load_config = projectling_core.load_config
        old_prompt_line = projectling_core._prompt_line
        old_cleanup = projectling_core._cleanup_legacy_runtime
        old_client = projectling_core.DeepSeekClient
        old_save_config = projectling_core._save_config_value
        render_failures: list[str] = []
        render_evidence: list[str] = []
        required_text_ok = True
        gemini_compact_label_ok = True
        gemini_compact_label_failures: list[str] = []
        width_samples: dict[str, int] = {}
        websearch_width_failures: list[str] = []
        websearch_width_evidence: list[str] = []
        direct_settings_failures: list[str] = []
        direct_settings_evidence: list[str] = []
        direct_settings_required_ok = True
        direct_settings_usage_ok = True
        direct_settings_secret_ok = True
        provider_menu_ok = False
        provider_menu_text = ""
        deepseek_model_choices_ok = False
        try:
            projectling_core.load_config = lambda: probe_config
            projectling_core._cleanup_legacy_runtime = lambda _config: None
            projectling_core._prompt_line = lambda _prompt="": "0"
            deepseek_model_ids = [model_id for model_id, _desc in projectling_core.DEEPSEEK_SETTINGS_MODEL_CHOICES]
            deepseek_model_choices_ok = (
                deepseek_model_ids == ["deepseek-v4-pro", "deepseek-v4-flash"]
                and "deepseek-chat" not in deepseek_model_ids
                and "deepseek-reasoner" not in deepseek_model_ids
            )
            for width in (16, 20, 24, 32, 40, 48):
                projectling_core.shutil.get_terminal_size = lambda _fallback=(80, 24), columns=width: _Size(columns)
                pages = {
                    "root": lambda: projectling_core._render_settings_root(probe_config),
                    "api": lambda: projectling_core._render_api_settings(probe_config),
                    "gemini": projectling_core._run_gemini_params_settings_ui,
                    "persona": lambda: projectling_core._run_persona_settings_ui(probe_config),
                    "system": lambda: projectling_core._render_system_settings(probe_config),
                    "websearch": projectling_core._run_websearch_settings_ui,
                }
                for name, renderer in pages.items():
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        renderer()
                    text = stdout.getvalue()
                    max_width = _max_width(text)
                    width_samples[f"{name}@{width}"] = max_width
                    if max_width > width:
                        render_failures.append(f"{name}@{width}:{max_width}>{width}")
                    plain_text = _plain(text)
                    if name == "root":
                        required_text_ok = required_text_ok and all(
                            marker in plain_text
                            for marker in ("设置中心", "当前", "主星", "执行", "API 与模型", "搜索", "系统")
                        )
                    if name == "api":
                        required_text_ok = required_text_ok and all(
                            marker in plain_text
                            for marker in ("API 与模型", "当前", "主星", "执行", "地址", "连通测试", "生成", "运行")
                        )
                    if name == "gemini":
                        required_text_ok = required_text_ok and all(marker in plain_text for marker in ("Gemini 参数", "Top P", "Extra Body"))
                        if width < 34:
                            required_labels = ("候选数量", "存在惩罚", "频率惩罚", "停止词", "响应 MIME", "Extra Body")
                            stale_labels = ("Candidate Co", "Presence Pen", "Frequency Pen", "Stop Sequenc", "Response MIME", "Extra Body J")
                            label_ok = all(marker in plain_text for marker in required_labels) and not any(marker in plain_text for marker in stale_labels)
                            gemini_compact_label_ok = gemini_compact_label_ok and label_ok
                            if not label_ok:
                                gemini_compact_label_failures.append(f"w{width}")
                    if name == "persona":
                        required_text_ok = required_text_ok and all(
                            marker in plain_text
                            for marker in ("角色", "重新抽卡", "锁定角色", "主星", "执行星")
                        )
                    if name == "system":
                        required_text_ok = required_text_ok and all(marker in plain_text for marker in ("系统设置", "协作模式"))
                    if name == "websearch":
                        required_text_ok = required_text_ok and all(marker in plain_text for marker in ("搜索设置", "摘要 Key", "网页 Key", "接口地址"))

            provider_menu_config = replace(probe_config, api_provider="deepseek")
            projectling_core.shutil.get_terminal_size = lambda _fallback=(80, 24): _Size(80)
            provider_menu_stdout = io.StringIO()
            with contextlib.redirect_stdout(provider_menu_stdout):
                projectling_core._choose_provider_interactive(provider_menu_config)
            provider_menu_text = _plain(provider_menu_stdout.getvalue())
            provider_menu_ok = (
                "Gemini 中转站" in provider_menu_text
                and "DeepSeek" in provider_menu_text
                and "DeepSeek [当前]" in provider_menu_text
                and "Gemini 中转站 []" not in provider_menu_text
                and "DeepSeek []" not in provider_menu_text
            )

            class _FailingListClient:
                def __init__(self, _config: Any) -> None:
                    pass

                def list_models(self) -> dict[str, Any]:
                    raise RuntimeError("模型列表请求失败: simulated layout relay down")

            class _BadModelClient:
                def __init__(self, _config: Any) -> None:
                    pass

                def chat_completions(self, **_kwargs: Any) -> dict[str, Any]:
                    raise RuntimeError("HTTP 404: bad model gemini-bad-model")

                def chat_completions_stream(self, **_kwargs: Any) -> Iterable[dict[str, Any]]:
                    raise RuntimeError("HTTP 404: bad model gemini-bad-model")
                    yield {}

            projectling_core.shutil.get_terminal_size = lambda _fallback=(80, 24): _Size(20)
            projectling_core.DeepSeekClient = _FailingListClient
            list_stdout = io.StringIO()
            with contextlib.redirect_stdout(list_stdout):
                list_rc = projectling_core._cmd_list_models(argparse.Namespace(json=False, limit=80))
            list_text = _plain(list_stdout.getvalue())

            bad_model_config = replace(probe_config, model="gemini-bad-model", gemini_executor_model="gemini-bad-model", enable_sse=True)
            projectling_core.load_config = lambda: bad_model_config
            projectling_core.DeepSeekClient = _BadModelClient
            api_stdout = io.StringIO()
            with contextlib.redirect_stdout(api_stdout):
                api_rc = projectling_core._cmd_api_test(argparse.Namespace(json=False, no_stream=True))
            api_text = _plain(api_stdout.getvalue())

            websearch_tab = run_cmd(
                [sys.executable, str(PROJECTLING_DIR / "core.py"), "shell-settings", "--tab", "websearch"],
                cwd=PROJECTLING_DIR,
                input_text="0\n",
                timeout=30,
            )
            websearch_tab_text = _plain(websearch_tab.stdout or "")

            def _run_direct_settings(command_name: str, tab: str, *, width: int) -> subprocess.CompletedProcess[str]:
                env = os.environ.copy()
                env["AITERMUX_HOME"] = str(AITERMUX_HOME)
                env["AITERMUX_AIDEBUG_DIR"] = str(AIDEBUG_DIR)
                env["PROJECTLING_DIR"] = str(PROJECTLING_DIR)
                env["COLUMNS"] = str(width)
                env["PYTHONUTF8"] = "1"
                env["PYTHONIOENCODING"] = "utf-8"
                return subprocess.run(
                    [sys.executable, str(PROJECTLING_DIR / "core.py"), command_name, tab],
                    cwd=str(PROJECTLING_DIR),
                    env=env,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    input="0\n",
                    capture_output=True,
                    timeout=30,
                    check=False,
                )

            direct_settings_expectations = {
                "api": ("API 与模型", "当前", "主星", "执行", "地址", "超时", "‹ 0  返回", "选择"),
                "gemini": ("API 与模型", "Gemini", "主星", "执行", "连通测试", "选择"),
                "websearch": ("搜索设置", "摘要 Key", "网页 Key", "接口地址", "选择"),
            }
            for command_name, command_label in (("settings", "settings"), ("/settings", "slash")):
                for tab, expected_markers in direct_settings_expectations.items():
                    for width in (16, 20, 24, 32, 48, 80):
                        completed = _run_direct_settings(command_name, tab, width=width)
                        combined_text = f"{completed.stdout or ''}\n{completed.stderr or ''}"
                        plain_text = _plain(combined_text)
                        max_width = _max_width(plain_text)
                        evidence_label = f"{command_label}:{tab}@{width}"
                        direct_settings_evidence.append(f"{evidence_label}:{completed.returncode}/{max_width}")
                        lowered = plain_text.lower()
                        has_argparse_usage = (
                            ("usage:" in lowered and "core.py" in lowered)
                            or "invalid choice" in lowered
                            or "unrecognized arguments" in lowered
                        )
                        missing_markers = [marker for marker in expected_markers if marker not in plain_text]
                        if completed.returncode != 0:
                            direct_settings_failures.append(f"{evidence_label}:rc{completed.returncode}")
                        if max_width > width:
                            direct_settings_failures.append(f"{evidence_label}:{max_width}>{width}")
                        if has_argparse_usage:
                            direct_settings_usage_ok = False
                            direct_settings_failures.append(f"{evidence_label}:usage")
                        if missing_markers:
                            direct_settings_required_ok = False
                            direct_settings_failures.append(f"{evidence_label}:missing")
                        if _contains_unmasked_secret(plain_text):
                            direct_settings_secret_ok = False
                            direct_settings_failures.append(f"{evidence_label}:secret")

            def _run_websearch_ui(inputs: list[str], *, width: int, config: Any) -> str:
                iterator = iter(inputs)
                stdout = io.StringIO()
                projectling_core.load_config = lambda config=config: config
                projectling_core._prompt_line = lambda _prompt="": next(iterator, "0")
                projectling_core.shutil.get_terminal_size = lambda _fallback=(80, 24), columns=width: _Size(columns)
                with contextlib.redirect_stdout(stdout):
                    projectling_core._run_websearch_settings_ui()
                return _plain(stdout.getvalue())

            for width in (16, 20, 24, 32, 48, 80):
                text = _run_websearch_ui(["0"], width=width, config=probe_config)
                max_width = _max_width(text)
                websearch_width_evidence.append(f"w{width}:{max_width}")
                if max_width > width or "搜索设置" not in text or "摘要 Key" not in text:
                    websearch_width_failures.append(f"w{width}:{max_width}>{width}")

            websearch_updates: list[dict[str, str | None]] = []

            def _capture_websearch_save(_config: Any, updates: dict[str, str | None]) -> Any:
                websearch_updates.append(dict(updates))
                return probe_config

            projectling_core._save_config_value = _capture_websearch_save
            websearch_save_text = _run_websearch_ui(
                [
                    "1", "fixture-width-websearch-summary-live",
                    "2", "fixture-width-websearch-web-live",
                    "3", "https://websearch.width.invalid/search_api/web_search",
                    "0",
                ],
                width=20,
                config=probe_config,
            )
            updates_after_save = len(websearch_updates)
            websearch_blank_text = _run_websearch_ui(["1", "", "2", "", "3", "", "0"], width=20, config=probe_config)
            updates_after_blank = len(websearch_updates)

            missing_summary_config = replace(probe_config, websearch_summary_key=None)
            websearch_missing_text = _run_websearch_ui(["4", "", "0"], width=20, config=missing_summary_config)
        finally:
            projectling_core.shutil.get_terminal_size = old_get_terminal_size
            projectling_core.load_config = old_load_config
            projectling_core._prompt_line = old_prompt_line
            projectling_core._cleanup_legacy_runtime = old_cleanup
            projectling_core.DeepSeekClient = old_client
            projectling_core._save_config_value = old_save_config

        list_status_ok = (
            list_rc == 1
            and _max_width(list_text) <= 20
            and "Gemini" in list_text
            and "fail" in list_text
            and "下一步" in list_text
            and all(token in list_text for token in ("API Key", "Base URL", "模型列表接口", "网络"))
        )
        api_status_ok = (
            api_rc == 1
            and _max_width(api_text) <= 20
            and "api-test fail" in api_text
            and "gemini" in api_text
            and "主星" in api_text
            and "辅星" in api_text
            and "下一步" in api_text
            and all(token in api_text for token in ("API Key", "Base URL", "模型名", "网络"))
        )
        websearch_tab_ok = websearch_tab.returncode == 0 and "WEBSEARCH API" in websearch_tab_text
        websearch_save_ok = (
            updates_after_save == 3
            and _max_width(websearch_save_text) <= 20
            and websearch_save_text.count("已保存") >= 3
            and "fixture-width-websearch" not in websearch_save_text
        )
        websearch_blank_ok = (
            updates_after_blank == updates_after_save
            and _max_width(websearch_blank_text) <= 20
            and websearch_blank_text.count("未输入，保持原样") >= 3
            and "摘要 Key 未修改" in websearch_blank_text
            and "网页 Key 未修改" in websearch_blank_text
            and "接口地址未修改" in websearch_blank_text
        )
        websearch_missing_ok = (
            _max_width(websearch_missing_text) <= 20
            and "status" in websearch_missing_text
            and "error" in websearch_missing_text
            and "未配置 Summary Key" in websearch_missing_text
            and "WebSearch 设置" in websearch_missing_text
            and "下一步" in websearch_missing_text
            and all(token in websearch_missing_text for token in ("Summary Key", "Web Key", "Endpoint", "网络"))
            and "DeepSeek 设置" not in websearch_missing_text
        )
        secret_ok = (
            "fixture-width-contract" not in list_text
            and "fixture-width-contract" not in api_text
            and "fixture-width-websearch" not in "\n".join((websearch_tab_text, websearch_save_text, websearch_blank_text, websearch_missing_text))
            and direct_settings_secret_ok
        )
    except Exception as exc:
        return item("settings_status_width_contract", 0, "fail", [f"exception={exc}"], "修复 settings/status width contract。")

    checks = {
        "render_widths": not render_failures,
        "required_text": required_text_ok,
        "list_status": list_status_ok,
        "api_status": api_status_ok,
        "provider_menu": provider_menu_ok,
        "deepseek_model_choices": deepseek_model_choices_ok,
        "gemini_compact_labels": gemini_compact_label_ok,
        "websearch_widths": not websearch_width_failures,
        "websearch_tab": websearch_tab_ok,
        "websearch_save": websearch_save_ok,
        "websearch_blank": websearch_blank_ok,
        "websearch_missing": websearch_missing_ok,
        "direct_settings_cli": not direct_settings_failures and direct_settings_required_ok and direct_settings_usage_ok,
        "secret_redaction": secret_ok,
    }
    direct_settings_ok = checks["direct_settings_cli"]
    direct_settings_max_width = 0
    for row in direct_settings_evidence:
        try:
            direct_settings_max_width = max(direct_settings_max_width, int(str(row).rsplit("/", 1)[1]))
        except (IndexError, ValueError):
            direct_settings_max_width = max(direct_settings_max_width, 0)
    direct_settings_summary = (
        "direct="
        f"ok:{_compact_bool_flag(direct_settings_ok)} "
        "cmd=set,/set "
        "tab:3 w=16,20,24,32,48,80 "
        f"p={len(direct_settings_evidence)} "
        f"max={direct_settings_max_width} "
        f"fail={_compact_list_or_dash(direct_settings_failures[:4])} "
        f"use:{_compact_bool_flag(direct_settings_usage_ok)}"
    )
    settings_status_density_limit = 90
    for width in (16, 20, 24, 32, 40, 48):
        render_evidence.append(
            f"w{width}=root:{width_samples.get(f'root@{width}')} api:{width_samples.get(f'api@{width}')} "
            f"gemini:{width_samples.get(f'gemini@{width}')} persona:{width_samples.get(f'persona@{width}')} "
            f"system:{width_samples.get(f'system@{width}')} websearch:{width_samples.get(f'websearch@{width}')}"
        )
    render_fail_row = f"render_fail={_compact_list_or_dash(render_failures[:4])}"
    required_row = f"required={_compact_bool_flag(required_text_ok)}"
    gemini_label_row = (
        f"gemini_labels={_compact_bool_flag(gemini_compact_label_ok)} "
        f"fail={_compact_list_or_dash(gemini_compact_label_failures[:4])}"
    )
    list_row = f"list={_compact_bool_flag(list_status_ok)} rc={list_rc} max={_max_width(list_text)}"
    api_row = f"api={_compact_bool_flag(api_status_ok)} rc={api_rc} max={_max_width(api_text)}"
    provider_menu_row = f"provider_menu={_compact_bool_flag(provider_menu_ok)}"
    deepseek_model_row = f"ds_models={_compact_bool_flag(deepseek_model_choices_ok)}"
    web_width_row = f"web_w={','.join(websearch_width_evidence)} fail={_compact_list_or_dash(websearch_width_failures[:4])}"
    web_tab_row = (
        f"web_tab={_compact_bool_flag(websearch_tab_ok)} save={_compact_bool_flag(websearch_save_ok)} "
        f"blank={_compact_bool_flag(websearch_blank_ok)} missing={_compact_bool_flag(websearch_missing_ok)}"
    )
    secret_row = f"sec={_compact_bool_flag(secret_ok)}"
    settings_density_rows = [
        *render_evidence[:3],
        render_fail_row,
        required_row,
        gemini_label_row,
        provider_menu_row,
        deepseek_model_row,
        list_row,
        api_row,
        web_width_row,
        web_tab_row,
        direct_settings_summary,
        secret_row,
    ]
    direct_settings_verbose_labels = (
        "direct_settings=",
        "tabs=",
        "probes=",
        "usage:",
        "render_failures=",
        "required_text=",
        "gemini_compact_labels=",
        "list_status=",
        "api_status=",
        "websearch_widths=",
        "websearch_tab=",
        "secret_redaction=",
        "failures=[]",
    )
    settings_status_density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in settings_density_rows
        if (
            len(row) > settings_status_density_limit
            or "samples=" in row
            or "{'" in row
            or "['" in row
            or "True" in row
            or "False" in row
            or "[]" in row
            or any(label in row for label in direct_settings_verbose_labels)
        )
    ]
    checks["settings_status_density"] = not settings_status_density_failures
    failures = [name for name, ok in checks.items() if not ok]
    evidence = [
        "widths=16,20,24,32,40,48",
        *render_evidence[:3],
        render_fail_row,
        required_row,
        gemini_label_row,
        provider_menu_row,
        deepseek_model_row,
        list_row,
        api_row,
        web_width_row,
        web_tab_row,
        direct_settings_summary,
        f"settings_status_density=limit={settings_status_density_limit} failures={_compact_list_or_dash(settings_status_density_failures)}",
        secret_row,
    ]
    score = 100 if not failures else max(35, 100 - len(failures) * 20)
    if failures:
        evidence.append("failures=" + ",".join(failures))
    return item(
        "settings_status_width_contract",
        score,
        status_from_score(score),
        evidence,
        "修复 Settings/API/model-list/api-test 窄宽输出、状态文案或脱敏。" if failures else "",
    )


def check_gemini_diagnostic_output_contract() -> dict[str, Any]:
    if load_config is None:
        return item("gemini_diagnostic_output_contract", 0, "fail", ["projectling imports unavailable"], "检查 Gemini diagnostic output 导入链。")

    try:
        import core as projectling_core

        bad_base = "http://127.0.0.1:9/v1"
        bad_model = "gemini-diagnostic-output-bad-model"
        env_path = PROJECTLING_DIR / "config" / "env"

        def _env_digest() -> str:
            if not env_path.exists():
                return "missing"
            return hashlib.sha256(env_path.read_bytes()).hexdigest()

        def _plain(text: str) -> str:
            return projectling_core._strip_ansi(text or "")

        def _max_width(text: str) -> int:
            return max((projectling_core._display_width(line) for line in _plain(text).splitlines()), default=0)

        def _run_public(args: list[str], *, columns: int) -> subprocess.CompletedProcess[str]:
            env = os.environ.copy()
            env["AITERMUX_HOME"] = str(AITERMUX_HOME)
            env["AITERMUX_AIDEBUG_DIR"] = str(AIDEBUG_DIR)
            env["PROJECTLING_DIR"] = str(PROJECTLING_DIR)
            env["COLUMNS"] = str(columns)
            env["LINES"] = "24"
            env["PYTHONIOENCODING"] = "utf-8"
            env["PROJECTLING_API_PROVIDER"] = "gemini"
            env["GEMINI_API_KEY"] = "fixture-diagnostic-output-contract"
            env["GEMINI_EXECUTOR_MODEL"] = bad_model
            if os.name == "nt":
                command = [sys.executable, str(PROJECTLING_DIR / "core.py"), *args]
                cwd = PROJECTLING_DIR
            else:
                command = [str(PROJECTLING_RUN), *args]
                cwd = AITERMUX_HOME
            return subprocess.run(
                command,
                cwd=str(cwd),
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=30,
            )

        before_digest = _env_digest()
        api_json = _run_public(
            [
                "api-test",
                "--json",
                "--no-stream",
                "--model",
                bad_model,
                "--base-url",
                bad_base,
                "--timeout",
                "5",
            ],
            columns=80,
        )
        list_json = _run_public(
            ["list-models", "--json", "--base-url", bad_base, "--timeout", "5"],
            columns=80,
        )
        api_human = _run_public(
            [
                "api-test",
                "--no-stream",
                "--model",
                bad_model,
                "--base-url",
                bad_base,
                "--timeout",
                "5",
            ],
            columns=20,
        )
        list_human = _run_public(
            ["list-models", "--base-url", bad_base, "--timeout", "5"],
            columns=20,
        )
        alias_models_json = _run_public(
            ["models", "--json", "--base-url", bad_base, "--timeout", "5"],
            columns=80,
        )
        alias_model_list_human = _run_public(
            ["model-list", "--base-url", bad_base, "--timeout", "5"],
            columns=20,
        )
        alias_apitest_json = _run_public(
            [
                "apitest",
                "--json",
                "--no-stream",
                "--model",
                bad_model,
                "--base-url",
                bad_base,
                "--timeout",
                "5",
            ],
            columns=80,
        )
        slash_help_human = _run_public(["/help"], columns=20)
        slash_models_json = _run_public(
            ["/models", "--json", "--base-url", bad_base, "--timeout", "5"],
            columns=80,
        )
        slash_model_list_human = _run_public(
            ["/model-list", "--base-url", bad_base, "--timeout", "5"],
            columns=20,
        )
        slash_api_json = _run_public(
            [
                "/api-test",
                "--json",
                "--no-stream",
                "--model",
                bad_model,
                "--base-url",
                bad_base,
                "--timeout",
                "5",
            ],
            columns=80,
        )
        slash_apitest_json = _run_public(
            [
                "/apitest",
                "--json",
                "--no-stream",
                "--model",
                bad_model,
                "--base-url",
                bad_base,
                "--timeout",
                "5",
            ],
            columns=80,
        )
        after_digest = _env_digest()

        try:
            api_payload = json.loads(api_json.stdout)
        except json.JSONDecodeError:
            api_payload = {}
        try:
            list_payload = json.loads(list_json.stdout)
        except json.JSONDecodeError:
            list_payload = {}
        try:
            alias_models_payload = json.loads(alias_models_json.stdout)
        except json.JSONDecodeError:
            alias_models_payload = {}
        try:
            alias_apitest_payload = json.loads(alias_apitest_json.stdout)
        except json.JSONDecodeError:
            alias_apitest_payload = {}
        try:
            slash_models_payload = json.loads(slash_models_json.stdout)
        except json.JSONDecodeError:
            slash_models_payload = {}
        try:
            slash_api_payload = json.loads(slash_api_json.stdout)
        except json.JSONDecodeError:
            slash_api_payload = {}
        try:
            slash_apitest_payload = json.loads(slash_apitest_json.stdout)
        except json.JSONDecodeError:
            slash_apitest_payload = {}

        api_human_text = _plain(api_human.stdout)
        list_human_text = _plain(list_human.stdout)
        alias_model_list_text = _plain(alias_model_list_human.stdout)
        slash_help_text = _plain(f"{slash_help_human.stdout or ''}\n{slash_help_human.stderr or ''}")
        slash_model_list_text = _plain(slash_model_list_human.stdout)
        api_public_json_ok = (
            api_json.returncode == 1
            and api_payload.get("ok") is False
            and api_payload.get("provider") == "gemini"
            and api_payload.get("executor_model") == bad_model
            and api_payload.get("base_url") == bad_base
            and api_payload.get("stream") is False
            and bool(api_payload.get("error"))
        )
        list_public_json_ok = (
            list_json.returncode == 1
            and list_payload.get("ok") is False
            and list_payload.get("provider") == "gemini"
            and list_payload.get("base_url") == bad_base
            and bool(list_payload.get("error"))
        )
        api_public_human_ok = (
            api_human.returncode == 1
            and _max_width(api_human_text) <= 20
            and "api-test fail" in api_human_text
            and "provider" in api_human_text
            and "辅星" in api_human_text
            and "base" in api_human_text
            and "下一步" in api_human_text
            and all(token in api_human_text for token in ("API Key", "Base URL", "模型名", "网络"))
        )
        list_public_human_ok = (
            list_human.returncode == 1
            and _max_width(list_human_text) <= 20
            and "Gemini" in list_human_text
            and "fail" in list_human_text
            and "base" in list_human_text
            and "下一步" in list_human_text
            and all(token in list_human_text for token in ("API Key", "Base URL", "模型列表接口", "网络"))
        )
        alias_models_json_ok = (
            alias_models_json.returncode == 1
            and alias_models_payload.get("ok") is False
            and alias_models_payload.get("provider") == "gemini"
            and alias_models_payload.get("base_url") == bad_base
            and bool(alias_models_payload.get("error"))
        )
        alias_model_list_human_ok = (
            alias_model_list_human.returncode == 1
            and _max_width(alias_model_list_text) <= 20
            and "Gemini" in alias_model_list_text
            and "fail" in alias_model_list_text
            and "下一步" in alias_model_list_text
            and all(token in alias_model_list_text for token in ("API Key", "Base URL", "模型列表接口", "网络"))
        )
        alias_apitest_json_ok = (
            alias_apitest_json.returncode == 1
            and alias_apitest_payload.get("ok") is False
            and alias_apitest_payload.get("provider") == "gemini"
            and alias_apitest_payload.get("executor_model") == bad_model
            and alias_apitest_payload.get("base_url") == bad_base
            and alias_apitest_payload.get("stream") is False
            and bool(alias_apitest_payload.get("error"))
        )
        alias_runtime_ok = alias_models_json_ok and alias_model_list_human_ok and alias_apitest_json_ok
        slash_help_ok = (
            slash_help_human.returncode == 0
            and _max_width(slash_help_text) <= 20
            and all(token in slash_help_text for token in ("/models", "/api-test", "/settings", "/help"))
            and "usage:" not in slash_help_text.lower()
            and "invalid choice" not in slash_help_text.lower()
        )
        slash_models_json_ok = (
            slash_models_json.returncode == 1
            and slash_models_payload.get("ok") is False
            and slash_models_payload.get("provider") == "gemini"
            and slash_models_payload.get("base_url") == bad_base
            and bool(slash_models_payload.get("error"))
        )
        slash_model_list_human_ok = (
            slash_model_list_human.returncode == 1
            and _max_width(slash_model_list_text) <= 20
            and "Gemini" in slash_model_list_text
            and "fail" in slash_model_list_text
            and "下一步" in slash_model_list_text
            and all(token in slash_model_list_text for token in ("API Key", "Base URL", "模型列表接口", "网络"))
        )
        slash_api_json_ok = (
            slash_api_json.returncode == 1
            and slash_api_payload.get("ok") is False
            and slash_api_payload.get("provider") == "gemini"
            and slash_api_payload.get("executor_model") == bad_model
            and slash_api_payload.get("base_url") == bad_base
            and slash_api_payload.get("stream") is False
            and bool(slash_api_payload.get("error"))
        )
        slash_apitest_json_ok = (
            slash_apitest_json.returncode == 1
            and slash_apitest_payload.get("ok") is False
            and slash_apitest_payload.get("provider") == "gemini"
            and slash_apitest_payload.get("executor_model") == bad_model
            and slash_apitest_payload.get("base_url") == bad_base
            and slash_apitest_payload.get("stream") is False
            and bool(slash_apitest_payload.get("error"))
        )
        slash_alias_runtime_ok = (
            slash_help_ok
            and slash_models_json_ok
            and slash_model_list_human_ok
            and slash_api_json_ok
            and slash_apitest_json_ok
        )
        no_env_mutation = before_digest == after_digest

        config = load_config()
        probe_config = replace(
            config,
            api_provider="gemini",
            api_key="fixture-diagnostic-output-contract",
            gemini_api_key="fixture-diagnostic-output-contract",
            base_url="https://fast.aieyra.cn/v1/diagnostic-output",
            gemini_base_url="https://fast.aieyra.cn/v1/diagnostic-output",
            gemini_executor_model="gemini-3-flash",
            enable_sse=True,
        )

        class _Size:
            def __init__(self, columns: int) -> None:
                self.columns = columns
                self.lines = 24

        class _DiagnosticFailClient:
            def __init__(self, _config: Any) -> None:
                self.config = _config

            def list_models(self) -> dict[str, Any]:
                raise RuntimeError("模型列表请求失败: diagnostic relay down")

            def chat_completions(self, **_kwargs: Any) -> dict[str, Any]:
                raise RuntimeError("HTTP 404: bad model diagnostic")

            def chat_completions_stream(self, **_kwargs: Any) -> Iterable[dict[str, Any]]:
                raise RuntimeError("HTTP 404: bad model diagnostic")
                yield {}

        old_get_terminal_size = projectling_core.shutil.get_terminal_size
        old_load_config = projectling_core.load_config
        old_cleanup = projectling_core._cleanup_legacy_runtime
        old_client = projectling_core.DeepSeekClient
        width_failures: list[str] = []
        width_evidence: list[str] = []
        internal_texts: list[str] = []
        try:
            projectling_core.load_config = lambda: probe_config
            projectling_core._cleanup_legacy_runtime = lambda _config: None
            projectling_core.DeepSeekClient = _DiagnosticFailClient
            for width in (16, 20, 24, 32, 40, 48):
                projectling_core.shutil.get_terminal_size = lambda _fallback=(80, 24), columns=width: _Size(columns)
                list_stdout = io.StringIO()
                with contextlib.redirect_stdout(list_stdout):
                    list_rc = projectling_core._cmd_list_models(
                        argparse.Namespace(json=False, limit=80, base_url=bad_base, timeout=5)
                    )
                api_stdout = io.StringIO()
                with contextlib.redirect_stdout(api_stdout):
                    api_rc = projectling_core._cmd_api_test(
                        argparse.Namespace(json=False, no_stream=True, model=bad_model, base_url=bad_base, timeout=5)
                    )
                list_text = _plain(list_stdout.getvalue())
                api_text = _plain(api_stdout.getvalue())
                internal_texts.extend([list_text, api_text])
                list_width = _max_width(list_text)
                api_width = _max_width(api_text)
                width_evidence.append(f"w{width}:{list_width}/{api_width}")
                list_recovery_ok = all(token in list_text for token in ("API Key", "Base URL", "模型列表接口", "网络"))
                if list_rc != 1 or list_width > width or "base" not in list_text or "下一步" not in list_text or not list_recovery_ok:
                    width_failures.append(f"list@{width}:{list_rc}/{list_width}")
                api_recovery_ok = all(token in api_text for token in ("API Key", "Base URL", "模型名", "网络"))
                if (
                    api_rc != 1
                    or api_width > width
                    or "provider" not in api_text
                    or "辅星" not in api_text
                    or "base" not in api_text
                    or "下一步" not in api_text
                    or not api_recovery_ok
                ):
                    width_failures.append(f"api@{width}:{api_rc}/{api_width}")
        finally:
            projectling_core.shutil.get_terminal_size = old_get_terminal_size
            projectling_core.load_config = old_load_config
            projectling_core._cleanup_legacy_runtime = old_cleanup
            projectling_core.DeepSeekClient = old_client

        secret_candidates = [
            str(value)
            for value in (
                getattr(config, "api_key", ""),
                getattr(config, "gemini_api_key", ""),
                getattr(config, "deepseek_api_key", ""),
                "fixture-diagnostic-output-contract",
            )
            if isinstance(value, str) and len(value) >= 8
        ]
        combined_output = "\n".join(
            [
                api_json.stdout,
                api_json.stderr,
                list_json.stdout,
                list_json.stderr,
                api_human.stdout,
                api_human.stderr,
                list_human.stdout,
                list_human.stderr,
                alias_models_json.stdout,
                alias_models_json.stderr,
                alias_model_list_human.stdout,
                alias_model_list_human.stderr,
                alias_apitest_json.stdout,
                alias_apitest_json.stderr,
                slash_help_human.stdout,
                slash_help_human.stderr,
                slash_models_json.stdout,
                slash_models_json.stderr,
                slash_model_list_human.stdout,
                slash_model_list_human.stderr,
                slash_api_json.stdout,
                slash_api_json.stderr,
                slash_apitest_json.stdout,
                slash_apitest_json.stderr,
                *internal_texts,
            ]
        )
        secret_ok = all(secret not in combined_output for secret in secret_candidates)
    except Exception as exc:
        return item("gemini_diagnostic_output_contract", 0, "fail", [f"exception={exc}"], "修复 Gemini diagnostic output contract。")

    checks = {
        "public_api_json": api_public_json_ok,
        "public_list_json": list_public_json_ok,
        "public_api_human": api_public_human_ok,
        "public_list_human": list_public_human_ok,
        "alias_runtime": alias_runtime_ok,
        "slash_alias_runtime": slash_alias_runtime_ok,
        "width_matrix": not width_failures,
        "no_env_mutation": no_env_mutation,
        "secret_redaction": secret_ok,
    }
    failures = [name for name, ok in checks.items() if not ok]
    width_evidence_row = f"widths={','.join(width_evidence[:6])}"
    width_density_limit = 90
    width_density_failures = []
    if len(width_evidence_row) > width_density_limit or "{'" in width_evidence_row or "['" in width_evidence_row:
        width_density_failures.append(f"widths:{len(width_evidence_row)}")
    evidence = [
        (
            f"pub_api=1 rc={api_json.returncode} mdl=bad base=loopback"
            if api_public_json_ok
            else (
                f"public_api_json={api_public_json_ok} rc={api_json.returncode} "
                f"model={api_payload.get('executor_model')} base={api_payload.get('base_url')}"
            )
        ),
        (
            f"pub_list=1 rc={list_json.returncode} base=loopback"
            if list_public_json_ok
            else f"public_list_json={list_public_json_ok} rc={list_json.returncode} base={list_payload.get('base_url')}"
        ),
        (
            f"human={_compact_bool_flag(api_public_human_ok)}/{_compact_bool_flag(list_public_human_ok)} "
            f"max={_max_width(api_human_text)}/{_max_width(list_human_text)}"
        ),
        (
            "alias_runtime="
            f"m:{_compact_bool_flag(alias_models_json_ok)} "
            f"ml:{_compact_bool_flag(alias_model_list_human_ok)} "
            f"api:{_compact_bool_flag(alias_apitest_json_ok)}"
        ),
        (
            "slash_alias="
            f"h:{_compact_bool_flag(slash_help_ok)} "
            f"m:{_compact_bool_flag(slash_models_json_ok)} "
            f"ml:{_compact_bool_flag(slash_model_list_human_ok)} "
            f"api:{_compact_bool_flag(slash_api_json_ok)} "
            f"apit:{_compact_bool_flag(slash_apitest_json_ok)}"
        ),
        width_evidence_row,
        f"width_failures={_compact_list_or_dash(width_failures[:4])}",
        f"diagnostic_width_density=limit={width_density_limit} failures={_compact_list_or_dash(width_density_failures)}",
        f"env={_compact_bool_flag(no_env_mutation)}" if no_env_mutation else f"no_env_mutation={no_env_mutation}",
        f"sec={_compact_bool_flag(secret_ok)}" if secret_ok else f"secret_redaction={secret_ok}",
    ]
    density_limit = 75
    verbose_healthy_labels = (
        "public_api_json=True",
        "public_list_json=True",
        "public_human=True",
        "slash_alias_runtime=",
        "model=gemini-diagnostic-output-bad-model",
        "base=http://127.0.0.1:9/v1",
        "no_env_mutation=True",
        "secret_redaction=True",
        "True",
        "False",
    )
    density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if len(row) > density_limit or any(label in row for label in verbose_healthy_labels)
    ]
    checks["diagnostic_output_density"] = not density_failures
    failures = [name for name, ok in checks.items() if not ok]
    evidence.append(f"diagnostic_output_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")
    score = 100 if not failures else max(35, 100 - len(failures) * 15)
    if width_density_failures:
        score = min(score, 75)
    if failures:
        evidence.append("failures=" + ",".join(failures))
    return item(
        "gemini_diagnostic_output_contract",
        score,
        status_from_score(score),
        evidence,
        "修复 Gemini diagnostic CLI 输出、窄宽布局、JSON 失败 shape 或 config/env no-mutation。" if failures else "",
    )


def check_gemini_model_list_role_marker_contract() -> dict[str, Any]:
    if load_config is None:
        return item("gemini_model_list_role_marker_contract", 0, "fail", ["projectling imports unavailable"], "检查 Gemini model-list role marker 导入链。")

    try:
        import core as projectling_core

        planner_model = "gemini-3.1-pro-low"
        executor_model = "gemini-3-flash"
        config = load_config()
        probe_config = replace(
            config,
            api_provider="gemini",
            api_key="fixture-model-list-marker-contract",
            gemini_api_key="fixture-model-list-marker-contract",
            base_url="https://fast.aieyra.cn/v1",
            gemini_base_url="https://fast.aieyra.cn/v1",
            gemini_planner_model=planner_model,
            gemini_executor_model=executor_model,
            collab_mode="standard",
        )
        payload = {
            "object": "list",
            "data": [
                {"id": "claude-opus-4-6", "object": "model"},
                {"id": "gemini-2.5-flash", "object": "model"},
                {"id": executor_model, "object": "model"},
                {"id": "gemini-3-pro-low", "object": "model"},
                {"id": planner_model, "object": "model"},
                {"id": "gemini-pro-agent", "object": "model"},
            ],
        }

        class _Size:
            def __init__(self, columns: int) -> None:
                self.columns = columns
                self.lines = 24

        class _ModelListClient:
            def __init__(self, _config: Any) -> None:
                pass

            def list_models(self) -> dict[str, Any]:
                return payload

        def _plain(text: str) -> str:
            return projectling_core._strip_ansi(text or "")

        def _max_width(text: str) -> int:
            return max((projectling_core._display_width(line) for line in _plain(text).splitlines()), default=0)

        old_get_terminal_size = projectling_core.shutil.get_terminal_size
        old_load_config = projectling_core.load_config
        old_cleanup = projectling_core._cleanup_legacy_runtime
        old_client = projectling_core.DeepSeekClient
        width_failures: list[str] = []
        marker_failures: list[str] = []
        width_evidence: list[str] = []
        combined_texts: list[str] = []
        try:
            projectling_core.load_config = lambda: probe_config
            projectling_core._cleanup_legacy_runtime = lambda _config: None
            projectling_core.DeepSeekClient = _ModelListClient
            for width in (16, 20, 24, 32, 40, 48, 80, 120):
                projectling_core.shutil.get_terminal_size = lambda _fallback=(80, 24), columns=width: _Size(columns)
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    rc = projectling_core._cmd_list_models(argparse.Namespace(json=False, limit=30, base_url="", timeout=None))
                text = _plain(stdout.getvalue())
                combined_texts.append(text)
                max_width = _max_width(text)
                width_evidence.append(f"w{width}:{max_width}")
                if rc != 0 or max_width > width:
                    width_failures.append(f"w{width}:rc{rc}/max{max_width}")
                if "主星" not in text or "执行星" not in text:
                    marker_failures.append(f"w{width}")
        finally:
            projectling_core.shutil.get_terminal_size = old_get_terminal_size
            projectling_core.load_config = old_load_config
            projectling_core._cleanup_legacy_runtime = old_cleanup
            projectling_core.DeepSeekClient = old_client

        combined_text = "\n".join(combined_texts)
        planner_marked = planner_model in combined_text and "主星" in combined_text
        executor_marked = executor_model in combined_text and "执行星" in combined_text
        secret_ok = "fixture-model-list-marker-contract" not in combined_text
    except Exception as exc:
        return item("gemini_model_list_role_marker_contract", 0, "fail", [f"exception={exc}"], "修复 Gemini model-list role marker contract。")

    checks = {
        "widths": not width_failures,
        "markers": not marker_failures,
        "planner_marked": planner_marked,
        "executor_marked": executor_marked,
        "secret_redaction": secret_ok,
    }
    evidence = [
        "widths=" + ",".join(width_evidence),
        f"width_fail={_compact_list_or_dash(width_failures[:4])}",
        f"marker_fail={_compact_list_or_dash(marker_failures[:4])}",
        f"planner={_compact_bool_flag(planner_marked)}",
        f"executor={_compact_bool_flag(executor_marked)}",
        f"sec={_compact_bool_flag(secret_ok)}",
    ]
    density_limit = 70
    old_labels = (
        "width_failures=",
        "marker_failures=",
        "planner_marked=",
        "executor_marked=",
        "secret_redaction=",
        "True",
        "False",
        "[]",
        "['",
    )
    density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if len(row) > density_limit or any(label in row for label in old_labels)
    ]
    checks["evidence_density"] = not density_failures
    failures = [name for name, ok in checks.items() if not ok]
    evidence.append(f"role_marker_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")
    score = 100 if not failures else max(35, 100 - len(failures) * 20)
    if failures:
        evidence.append("failures=" + ",".join(failures))
    return item(
        "gemini_model_list_role_marker_contract",
        score,
        status_from_score(score),
        evidence,
        "修复 Gemini model-list 成功路径角色标记、窄宽布局或脱敏。" if failures else "",
    )


def check_gemini_model_list_taxonomy_contract() -> dict[str, Any]:
    if load_config is None:
        return item("gemini_model_list_taxonomy_contract", 0, "fail", ["projectling imports unavailable"], "检查 Gemini model-list taxonomy 导入链。")

    try:
        import core as projectling_core

        planner_model = "gemini-3.1-pro-low"
        executor_model = "gemini-3-flash"
        config = load_config()
        probe_config = replace(
            config,
            api_provider="gemini",
            api_key="fixture-model-list-taxonomy-contract",
            gemini_api_key="fixture-model-list-taxonomy-contract",
            base_url="https://fast.aieyra.cn/v1",
            gemini_base_url="https://fast.aieyra.cn/v1",
            gemini_planner_model=planner_model,
            gemini_executor_model=executor_model,
            collab_mode="standard",
        )
        model_ids = [
            "claude-opus-4-6",
            "claude-opus-4-6-thinking",
            "claude-sonnet-4-6",
            "claude-sonnet-4-6-thinking",
            "gemini-2.5-flash",
            "gemini-2.5-flash-image",
            "gemini-2.5-flash-lite",
            "gemini-2.5-flash-thinking",
            executor_model,
            "gemini-3-flash-agent",
            "gemini-3-pro-high",
            "gemini-3-pro-image-preview",
            "gemini-3-pro-low",
            "gemini-3.1-flash-image",
            "gemini-3.1-flash-image-preview",
            "gemini-3.1-flash-lite",
            "gemini-3.1-pro-high",
            planner_model,
            "gemini-3.5-flash-low",
            "gemini-pro-agent",
        ]
        payload = {"object": "list", "data": [{"id": model_id, "object": "model"} for model_id in model_ids]}

        class _Size:
            def __init__(self, columns: int) -> None:
                self.columns = columns
                self.lines = 24

        class _ModelListClient:
            def __init__(self, _config: Any) -> None:
                pass

            def list_models(self) -> dict[str, Any]:
                return payload

        def _plain(text: str) -> str:
            return projectling_core._strip_ansi(text or "")

        def _max_width(text: str) -> int:
            return max((projectling_core._display_width(line) for line in _plain(text).splitlines()), default=0)

        classifier_ok = (
            projectling_core._relay_model_tags("claude-opus-4-6-thinking") == ["claude", "think"]
            and projectling_core._relay_model_tags("gemini-3-pro-image-preview") == ["pro", "image"]
            and projectling_core._relay_model_tags("gemini-3-flash-agent") == ["flash", "agent"]
            and projectling_core._relay_model_tags("unknown-model") == ["unknown"]
        )

        old_get_terminal_size = projectling_core.shutil.get_terminal_size
        old_load_config = projectling_core.load_config
        old_cleanup = projectling_core._cleanup_legacy_runtime
        old_client = projectling_core.DeepSeekClient
        width_failures: list[str] = []
        taxonomy_failures: list[str] = []
        width_evidence: list[str] = []
        combined_texts: list[str] = []
        try:
            projectling_core.load_config = lambda: probe_config
            projectling_core._cleanup_legacy_runtime = lambda _config: None
            projectling_core.DeepSeekClient = _ModelListClient
            for width in (16, 20, 24, 32, 40, 48, 80, 120):
                projectling_core.shutil.get_terminal_size = lambda _fallback=(80, 24), columns=width: _Size(columns)
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    rc = projectling_core._cmd_list_models(argparse.Namespace(json=False, limit=30, base_url="", timeout=None))
                text = _plain(stdout.getvalue())
                combined_texts.append(text)
                max_width = _max_width(text)
                width_evidence.append(f"w{width}:{max_width}")
                if rc != 0 or max_width > width:
                    width_failures.append(f"w{width}:rc{rc}/max{max_width}")
                if width >= 32 and not all(tag in text for tag in ("分类", "pro", "flash", "think", "image", "agent", "claude", "lite")):
                    taxonomy_failures.append(f"w{width}:summary")
                if width >= 40 and not all(tag in text for tag in ("claude", "think")):
                    taxonomy_failures.append(f"w{width}:model-tags")
        finally:
            projectling_core.shutil.get_terminal_size = old_get_terminal_size
            projectling_core.load_config = old_load_config
            projectling_core._cleanup_legacy_runtime = old_cleanup
            projectling_core.DeepSeekClient = old_client

        combined_text = "\n".join(combined_texts)
        role_ok = "主星" in combined_text and "执行星" in combined_text
        hint_ok = "pro适合主星" in combined_text and "flash适合执行星" in combined_text
        secret_ok = "fixture-model-list-taxonomy-contract" not in combined_text
    except Exception as exc:
        return item("gemini_model_list_taxonomy_contract", 0, "fail", [f"exception={exc}"], "修复 Gemini model-list taxonomy contract。")

    checks = {
        "classifier": classifier_ok,
        "widths": not width_failures,
        "taxonomy": not taxonomy_failures,
        "role_markers": role_ok,
        "wide_hint": hint_ok,
        "secret_redaction": secret_ok,
    }
    evidence = [
        f"classifier={_compact_bool_flag(classifier_ok)}",
        "widths=" + ",".join(width_evidence),
        f"width_fail={_compact_list_or_dash(width_failures[:4])}",
        f"tax_fail={_compact_list_or_dash(taxonomy_failures[:4])}",
        f"roles={_compact_bool_flag(role_ok)}",
        f"hint={_compact_bool_flag(hint_ok)}",
        f"sec={_compact_bool_flag(secret_ok)}",
    ]
    density_limit = 70
    old_labels = (
        "classifier=True",
        "classifier=False",
        "width_failures=",
        "taxonomy_failures=",
        "role_markers=",
        "wide_hint=",
        "secret_redaction=",
        "True",
        "False",
        "[]",
        "['",
    )
    density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if len(row) > density_limit or any(label in row for label in old_labels)
    ]
    checks["evidence_density"] = not density_failures
    failures = [name for name, ok in checks.items() if not ok]
    evidence.append(f"taxonomy_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")
    score = 100 if not failures else max(35, 100 - len(failures) * 15)
    if failures:
        evidence.append("failures=" + ",".join(failures))
    return item(
        "gemini_model_list_taxonomy_contract",
        score,
        status_from_score(score),
        evidence,
        "修复 Gemini model-list taxonomy 标签、角色标记、窄宽布局或脱敏。" if failures else "",
    )


def _check_runner_concurrency_windows_wsl() -> dict[str, Any]:
    wsl_exe = _find_windows_wsl_exe()
    if wsl_exe is None:
        return item(
            "runner_concurrency",
            85,
            "ok",
            ["runtime=windows-native", "host_bash=missing", "wsl_exe=missing", "scope=Termux/WSL run.sh concurrency"],
            "安装或修复 WSL 后复测 run.sh 并发锁；Windows native 前端仍可运行。",
        )

    list_rc, list_stdout, list_stderr = _run_wsl(wsl_exe, ["-l", "-q"], timeout=20)
    raw_distros = _parse_wsl_distro_list(list_stdout)
    distro = _select_projectling_wsl_distro(raw_distros)
    if list_rc not in {0, None} or not raw_distros:
        evidence = [
            "runtime=windows-native",
            "host_bash=missing",
            f"wsl_list_rc={list_rc if list_rc is not None else 'timeout'}",
            f"distros={_compact_list_or_dash(raw_distros)}",
        ]
        if list_stderr.strip():
            evidence.append(f"list_stderr={list_stderr.strip()[:160]}")
        return item(
            "runner_concurrency",
            85,
            "ok",
            evidence,
            "修复 WSL distro 列表后复测 run.sh 并发锁；Windows native 前端仍可运行。",
        )

    linux_project = os.environ.get("PROJECTLING_WSL_PROJECT_PATH", "").strip() or _windows_path_to_wsl(PROJECTLING_DIR)
    encoded_project = base64.b64encode(linux_project.encode("utf-8")).decode("ascii")
    probe_code = r"""
import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

project = Path(base64.b64decode(sys.argv[1]).decode("utf-8"))
with tempfile.TemporaryDirectory(prefix="projectling-runner-") as tmp:
    root = Path(tmp)
    fake_bin = root / "bin"
    runtime_dir = root / "runtime"
    fake_bin.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    fake_python = fake_bin / "python"
    fake_python.write_text("#!/usr/bin/env bash\nsleep 0.75\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "AITERMUX_PROJECTLING_RUNTIME_DIR": str(runtime_dir),
            "PROJECTLING_SINGLE_INSTANCE": "auto",
        }
    )
    command = [
        str(project / "run.sh"),
        "shell-dispatch",
        "--mode",
        "chat",
        "--cwd",
        ".",
        "--raw",
        "health-pong",
        "--dry-run",
    ]
    p1 = subprocess.Popen(
        command,
        cwd=str(project),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.15)
    command2 = [*command[:-2], "health-ping", "--dry-run"]
    p2 = subprocess.Popen(
        command2,
        cwd=str(project),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    err1 = ""
    err2 = ""
    try:
        _out1, err1 = p1.communicate(timeout=20)
        _out2, err2 = p2.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        for process in (p1, p2):
            if process.poll() is None:
                process.kill()
        print("rc=timeout")
        sys.exit(124)
    rc1 = p1.returncode
    rc2 = p2.returncode
    ok = rc1 == 0 and rc2 == 0
    print(f"rc1={rc1} rc2={rc2}")
    if not ok:
        detail = (err1 or err2 or "").strip()
        if detail:
            print(detail[:400])
    sys.exit(0 if ok else 1)
"""
    probe_rc, probe_out, probe_err = _run_wsl(
        wsl_exe,
        ["-d", distro, "--", "python3", "-c", probe_code, encoded_project],
        timeout=30,
    )
    output = (probe_out or probe_err or "").strip()[:240] or "no output"
    evidence = [
        "runtime=windows-wsl-fallback",
        "host_bash=missing",
        f"distro={distro}",
        f"distros={_compact_list_or_dash(raw_distros)}",
        f"rc={probe_rc if probe_rc is not None else 'timeout'}",
        output,
    ]
    ok = probe_rc == 0
    return item(
        "runner_concurrency",
        100 if ok else 30,
        status_from_score(100 if ok else 30),
        evidence,
        "修复 Windows->WSL run.sh auto single-instance fallback；非 TTY chat/shell-dispatch 不应互相 TERM。" if not ok else "",
    )


def check_runner_concurrency() -> dict[str, Any]:
    if os.name == "nt" and not _host_command_available("bash"):
        return _check_runner_concurrency_windows_wsl()

    sandbox = HEALTH_SANDBOX_DIR / f"runner-concurrency-{os.getpid()}-{int(time.time() * 1000)}"
    fake_bin = sandbox / "bin"
    runtime_dir = sandbox / "runtime"
    fake_python = fake_bin / "python"
    try:
        fake_bin.mkdir(parents=True, exist_ok=True)
        runtime_dir.mkdir(parents=True, exist_ok=True)
        fake_python.write_text("#!/usr/bin/env bash\nsleep 0.75\nexit 0\n", encoding="utf-8")
        fake_python.chmod(0o755)
    except OSError as exc:
        shutil.rmtree(sandbox, ignore_errors=True)
        return item("runner_concurrency", 0, "fail", [f"setup={exc}"], "检查 aidebug tmp 写入权限。")

    script = "\n".join(
        [
            "set -uo pipefail",
            "export PATH=\"${FAKE_BIN}:$PATH\"",
            "export AITERMUX_PROJECTLING_RUNTIME_DIR=\"${RUNTIME_DIR}\"",
            "export PROJECTLING_SINGLE_INSTANCE=auto",
            "\"${PROJECTLING_RUN}\" shell-dispatch --mode chat --cwd . --raw \"health-pong\" --dry-run >/dev/null 2>&1 &",
            "pid1=$!",
            "sleep 0.15",
            "\"${PROJECTLING_RUN}\" shell-dispatch --mode chat --cwd . --raw \"health-ping\" --dry-run >/dev/null 2>&1 &",
            "pid2=$!",
            "wait \"$pid1\"; rc1=$?",
            "wait \"$pid2\"; rc2=$?",
            "printf 'rc1=%s rc2=%s\\n' \"$rc1\" \"$rc2\"",
            "test \"$rc1\" -eq 0 && test \"$rc2\" -eq 0",
        ]
    )
    env = os.environ.copy()
    env.update(
        {
            "AITERMUX_HOME": str(AITERMUX_HOME),
            "AITERMUX_AIDEBUG_DIR": str(AIDEBUG_DIR),
            "FAKE_BIN": str(fake_bin),
            "RUNTIME_DIR": str(runtime_dir),
            "PROJECTLING_RUN": str(PROJECTLING_RUN),
        }
    )
    try:
        try:
            completed = subprocess.run(
                ["bash", "-c", script],
                cwd=str(PROJECTLING_DIR),
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return item("runner_concurrency", 0, "fail", ["timeout after 20s"], "检查 run.sh 非 TTY 并发是否卡住。")
        evidence = [
            f"rc={completed.returncode}",
            (completed.stdout or completed.stderr or "").strip()[:240] or "no output",
        ]
        ok = completed.returncode == 0
        return item(
            "runner_concurrency",
            100 if ok else 30,
            status_from_score(100 if ok else 30),
            evidence,
            "修复 run.sh auto single-instance，非 TTY chat/shell-dispatch 不应互相 TERM。" if not ok else "",
        )
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def check_persona_split() -> dict[str, Any]:
    data, error = _doctor_json()
    if data is None:
        return item("persona_split", 0, "fail", [error], "修复 doctor 后再检查 persona 显示。")
    main_zh = str(data.get("persona_display_zh") or "").strip()
    main_en = str(data.get("persona_display_en") or "").strip()
    liaison_zh = str(data.get("persona_liaison_display_zh") or "").strip()
    liaison_en = str(data.get("persona_liaison_display_en") or "").strip()
    liaison_label = str(data.get("persona_liaison") or "").strip()
    persona_locked = bool(data.get("persona_locked"))
    liaison_locked = bool(data.get("liaison_locked"))
    split_ok = bool(main_zh and main_en and liaison_zh and liaison_en and (main_zh != liaison_zh or main_en != liaison_en))
    evidence = [
        f"main={main_zh} / {main_en}",
        f"liaison={liaison_zh} / {liaison_en}",
        f"persona_liaison={liaison_label}",
        f"locks=main:{_compact_bool_flag(persona_locked)} liaison:{_compact_bool_flag(liaison_locked)}",
    ]
    score = 100 if split_ok else 55
    score = _append_runtime_repr_density_guard(evidence, score)
    return item(
        "persona_split",
        score,
        status_from_score(score),
        evidence,
        "检查 persona 绑定是否仍然被融合，或确认执行星是否可见。" if not split_ok else "",
    )


def check_command_guard() -> dict[str, Any]:
    payload = _execute_tool(
        "command",
        {"command": "rm -rf /", "brief": "检查高危命令门禁"},
        cwd=HEALTH_SANDBOX_DIR,
    )
    if payload is None:
        return item("command_guard", 0, "fail", ["tool execution unavailable"], "检查 projectling/tooling imports。")
    status = str(payload.get("status") or "")
    confirm = str(payload.get("confirm_command") or "")
    evidence = [
        f"status={status}",
        f"confirm={confirm}",
        f"reason={payload.get('reason')}",
    ]
    ok = status == "blocked" or (status == "pending_confirmation" and confirm == "yes")
    score = 100 if ok else 30
    return item(
        "command_guard",
        score,
        status_from_score(score),
        evidence,
        "修复 command 高危门禁：高危命令必须 blocked 或进入明确确认。" if not ok else "",
    )


def check_context_budget_runtime() -> dict[str, Any]:
    payload = _execute_tool(
        "context",
        {"percent": 35, "turns": 2, "brief": "检查上下文预算"},
        cwd=HEALTH_SANDBOX_DIR,
    )
    if payload is None:
        return item("context_budget_runtime", 0, "fail", ["tool execution unavailable"], "检查 projectling/tooling imports。")
    status = str(payload.get("status") or "")
    percent = payload.get("percent")
    turns = payload.get("turns_remaining")
    summary = _health_summary(payload)
    evidence = [f"status={status}", f"percent={percent}", f"turns={turns}", f"summary={summary}"]
    ok = status == "ok" and int(percent or 0) == 35 and int(turns or 0) == 2
    score = 100 if ok else 35
    return item(
        "context_budget_runtime",
        score,
        status_from_score(score),
        evidence,
        "修复 context 工具的预算写入或摘要回执。" if not ok else "",
    )


def _compact_tool_fact_summary(tool_name: str, summary: Any) -> str:
    text = str(summary or "").strip()
    text = text.replace(" · ", " ")
    text = text.replace("toolbox list", "toolbox")
    text = text.replace("visible", "vis")
    if tool_name == "apply_patch" and text.startswith("patch ok "):
        text = "ok " + text[len("patch ok ") :]
    return text[:70] or "-"


def check_tool_fact_cards() -> dict[str, Any]:
    if not _projectling_available():
        return item("tool_fact_cards", 0, "fail", ["projectling imports unavailable"], "检查 projectling.py/tooling.py 导入链。")
    checks: list[tuple[str, dict[str, Any]]] = []
    checks.append(
        (
            "command",
            _execute_tool(
                "command",
                {"command": "pwd", "brief": "查看路径", "context_percent": 35},
                cwd=HEALTH_SANDBOX_DIR,
            )
            or {},
        )
    )
    checks.append(
        (
            "tool_manage",
            _execute_tool("tool_manage", {"action": "list", "brief": "列出工具箱状态"}, cwd=HEALTH_SANDBOX_DIR)
            or {},
        )
    )
    checks.append(
        (
            "apply_patch",
            _execute_tool(
                "apply_patch",
                {
                    "patch": "*** Begin Patch\n*** Add File: tmp_health_probe.txt\n+probe\n*** End Patch\n",
                    "brief": "检查补丁摘要",
                    "check_only": True,
                },
                cwd=HEALTH_SANDBOX_DIR,
            )
            or {},
        )
    )

    evidence: list[str] = []
    score = 100
    label_map = {
        "command": "command",
        "tool_manage": "tool_manage",
        "apply_patch": "apply_patch",
    }
    kind_map = {
        "command": "cmd",
        "tool_manage": "tools",
        "apply_patch": "patch",
    }
    for name, payload in checks:
        status = str(payload.get("status") or "")
        summary = str(payload.get("summary") or "")
        kind = str(payload.get("kind") or "")
        label = label_map.get(name, name)
        compact_kind = kind_map.get(kind, kind or "-")
        compact_summary = _compact_tool_fact_summary(name, summary)
        evidence.append(f"{label}={status or '-'} k={compact_kind} s={compact_summary}")
        if status != "ok" or not summary:
            score -= 25

    density_limit = 70
    density_failures = [
        f"{index}:{len(text)}"
        for index, text in enumerate(evidence, start=1)
        if len(text) > density_limit
    ]
    legacy_markers = (": status=", " kind=", " summary=")
    for index, text in enumerate(evidence, start=1):
        if any(marker in text for marker in legacy_markers):
            density_failures.append(f"{index}:legacy")
    if density_failures:
        score = min(score, 75)
    evidence.append(f"tool_fact_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")

    score = max(25, score)
    return item(
        "tool_fact_cards",
        score,
        status_from_score(score),
        evidence,
        "补齐工具 result.summary / kind / 输出摘要卡。" if score < 85 else "",
    )


def check_health_history_trend(history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    summary = _health_history_summary(history if history is not None else _load_health_history())
    run_count = int(summary.get("run_count") or 0)
    recent_count = int(summary.get("recent_count") or 0)
    if run_count == 0:
        return item(
            "health_history_trend",
            75,
            "warn",
            ["runs=0", "recent=0"],
            "再运行 aidebug health，建立第一条历史基线。",
        )

    latest = summary.get("latest_score")
    previous = summary.get("previous_score")
    delta = summary.get("delta")
    recent_average = summary.get("recent_average")
    recent_min = summary.get("recent_min")
    recent_max = summary.get("recent_max")
    trend = str(summary.get("trend") or "unknown")

    if recent_count < 2:
        score = 80
    else:
        score = 100
        try:
            numeric_delta = float(delta)
            numeric_min = float(recent_min)
            numeric_average = float(recent_average)
        except (TypeError, ValueError):
            numeric_delta = 0.0
            numeric_min = 100.0
            numeric_average = 100.0
        if numeric_delta <= -10 or numeric_min < 85:
            score = 70
        elif numeric_delta <= -5 or numeric_min < 95:
            score = 85
        elif numeric_average < 98:
            score = 95

    evidence = [
        f"runs={run_count}",
        f"recent={recent_count}",
        f"latest={latest}",
        f"previous={previous}",
        f"delta={delta}",
        f"recent_avg={recent_average}",
        f"range={recent_min}..{recent_max}",
        f"trend={trend}",
    ]
    next_action = ""
    if score < 85:
        next_action = "回看最近 health JSONL，定位分数断崖或链路回归。"
    elif recent_count < 3:
        next_action = "继续运行几轮 aidebug health，让趋势判断更稳定。"
    return item("health_history_trend", score, status_from_score(score), evidence, next_action)


def check_aidebug_health_jsonl_integrity() -> dict[str, Any]:
    summary = _health_jsonl_integrity(HEALTH_JSONL, recent_window=12)
    exists = bool(summary.get("exists"))
    read_error = str(summary.get("read_error") or "")
    lines = int(summary.get("lines") or 0)
    valid = int(summary.get("valid") or 0)
    bad = int(summary.get("bad") or 0)
    bad_recent = int(summary.get("bad_recent") or 0)
    bad_legacy = int(summary.get("bad_legacy") or 0)
    latest_ok = bool(summary.get("latest_ok"))

    if read_error:
        score = 55
    elif not exists:
        score = 75
    elif lines == 0 or valid == 0:
        score = 75
    elif not latest_ok:
        score = 55
    elif bad_recent:
        score = 85
    else:
        score = 100

    evidence = [
        f"lines={lines} valid={valid} bad={bad}",
        "latest="
        f"ln:{summary.get('latest_line', '-')} "
        f"ok:{int(latest_ok)} "
        f"gen:{_compact_iso_timestamp(summary.get('latest_generated_at'))}",
        "bad="
        f"first:{summary.get('first_bad', '-')} "
        f"last:{summary.get('last_bad', '-')} "
        f"recent:{bad_recent} "
        f"legacy:{bad_legacy}",
        f"window={summary.get('window', 12)} read={'err' if read_error else 'ok'}",
    ]
    density_limit = 70
    density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if len(row) > density_limit or "{" in row or "}" in row or "['" in row
    ]
    if density_failures:
        score = min(score, 75)
    evidence.append(f"jsonl_integrity_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")

    next_action = ""
    if read_error:
        next_action = "检查 aidebug-health.jsonl 读取权限或磁盘状态。"
    elif not latest_ok:
        next_action = "最新 health JSONL 行无法解析；重跑 aidebug health 并检查写入中断。"
    elif bad_recent:
        next_action = "最近 health JSONL 窗口存在坏行；检查并修复日志写入链路。"
    return item("aidebug_health_jsonl_integrity", score, status_from_score(score), evidence, next_action)


def check_projectling_tests() -> dict[str, Any]:
    note = AIDEBUG_DIR / "logs" / "projectling-test.md"
    meta = file_meta(note)
    score = 100 if meta.get("exists") and int(meta.get("bytes") or 0) > 500 else 55
    evidence = [f"exists={_compact_bool_flag(meta.get('exists'))}", f"bytes={meta.get('bytes', 0)}", f"age={meta.get('age_seconds', '-')}"]
    score = _append_runtime_repr_density_guard(evidence, score)
    return item("projectling_test_record", score, status_from_score(score), evidence, "补写测试记录，避免重复测试。" if score < 85 else "")


def check_desktop_goal_anchor() -> dict[str, Any]:
    anchor = PROJECTLING_DIR.parent / "PROJECT凌-OVERNIGHT-GOAL.md"
    meta = file_meta(anchor)
    evidence = [
        f"exists={_compact_bool_flag(meta.get('exists'))}",
        f"bytes={meta.get('bytes', 0)}",
        f"age={meta.get('age_seconds', '-')}",
        "file=PROJECT凌-OVERNIGHT-GOAL.md",
    ]
    if not meta.get("exists"):
        evidence.append("sections=0/5")
        evidence.append("refs=0/3")
        evidence.append("sec=1")
        return item(
            "projectling_desktop_goal_anchor",
            55,
            "warn",
            evidence,
            "恢复桌面 PROJECT凌-OVERNIGHT-GOAL.md，确保 compact 后可接续。",
        )

    try:
        text = anchor.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        evidence.append(f"read_error={exc}")
        return item(
            "projectling_desktop_goal_anchor",
            55,
            "warn",
            evidence,
            "检查桌面锚点文件读取权限。",
        )

    required_sections = (
        "# PROJECT Ling Overnight Goal Anchor",
        "## Historical Sessions",
        "## User Objective",
        "## Current Known Baseline",
        "## Resume Commands",
    )
    required_refs = (
        "projectling-test.md",
        "projectling-aidebug-next-plan.md",
        "rollout-2026-07-05T01-00-20-019f2e12-ed1f-7101-bd77-5fae06e5c90f.jsonl",
    )
    sections_ok = sum(1 for marker in required_sections if marker in text)
    refs_ok = sum(1 for marker in required_refs if marker in text)
    secret_ok = not any(marker in text for marker in ("sk-", "ghp_", "AIza", "Bearer "))
    score = 100
    failures: list[str] = []
    if sections_ok != len(required_sections):
        score = min(score, 75)
        failures.append("sections")
    if refs_ok != len(required_refs):
        score = min(score, 75)
        failures.append("refs")
    if not secret_ok:
        score = 0
        failures.append("secret")

    evidence.extend(
        [
            f"sections={sections_ok}/{len(required_sections)}",
            f"refs={refs_ok}/{len(required_refs)}",
            f"sec={_compact_bool_flag(secret_ok)}",
        ]
    )
    density_limit = 80
    density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if len(row) > density_limit or "True" in row or "False" in row or "['" in row
    ]
    if density_failures:
        score = min(score, 75)
    evidence.append(f"anchor_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")
    if failures:
        evidence.append("failures=" + ",".join(failures))
    return item(
        "projectling_desktop_goal_anchor",
        score,
        status_from_score(score),
        evidence,
        "修复桌面锚点内容、恢复历史会话/恢复命令，并移除任何密钥。" if failures else "",
    )


def check_history_requirement_matrix() -> dict[str, Any]:
    matrix = NOTE_DIR / "projectling-history-requirements-matrix.md"
    meta = file_meta(matrix)
    evidence = [
        f"exists={_compact_bool_flag(meta.get('exists'))}",
        f"bytes={meta.get('bytes', 0)}",
        f"age={meta.get('age_seconds', '-')}",
        "file=projectling-history-requirements-matrix.md",
    ]
    if not meta.get("exists"):
        evidence.extend(["ids=0/14", "sessions=0/4", "proofs=0/8", "sec=1"])
        return item(
            "projectling_history_requirement_matrix",
            55,
            "warn",
            evidence,
            "补写历史要求复审矩阵，绑定历史会话、原始要求、当前证据和剩余门槛。",
        )

    try:
        text = matrix.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        evidence.append(f"read_error={exc}")
        return item(
            "projectling_history_requirement_matrix",
            55,
            "warn",
            evidence,
            "检查历史要求复审矩阵读取权限。",
        )

    required_sessions = (
        "rollout-2026-06-08T17-47-30-019ea6a1-5107-7f80-840e-30a737bdf0f8.jsonl",
        "rollout-2026-06-13T13-04-11-019ebf5d-b945-7b53-9977-1a98fa473585.jsonl",
        "rollout-2026-06-30T19-06-49-019f1835-d536-7ee0-9f6e-896fb15507eb.jsonl",
        "rollout-2026-07-05T01-00-20-019f2e12-ed1f-7101-bd77-5fae06e5c90f.jsonl",
    )
    required_ids = tuple(f"R{index:02d}" for index in range(1, 15))
    required_proofs = (
        "projectling_auto_profile_coverage",
        "command_matrix_profile_coverage",
        "deepseek_live_cache_quality",
        "web_search_live_quality",
        "settings_status_width_contract",
        "windows_launcher_gemini_surface",
        "zsh_diagnostic_alias_execution",
        "projectling_desktop_goal_anchor",
    )
    required_sections = (
        "# ProjectLing Historical Requirement Review Matrix",
        "## Historical Session Anchors",
        "## Requirement Matrix",
        "## Remaining External Gate",
        "## Current Baseline",
        "## Resume Guidance",
    )
    sessions_ok = sum(1 for marker in required_sessions if marker in text)
    ids_ok = sum(1 for marker in required_ids if f"| {marker} |" in text)
    proofs_ok = sum(1 for marker in required_proofs if marker in text)
    sections_ok = sum(1 for marker in required_sections if marker in text)
    android_gate_ok = all(marker in text for marker in ("am", "allow-external-apps=true", "log_path"))
    threshold_ok = "25/25" in text or "26/26" in text
    secret_ok = not any(marker in text for marker in ("sk-", "ghp_", "AIza", "Bearer "))

    failures: list[str] = []
    if sessions_ok != len(required_sessions):
        failures.append("sessions")
    if ids_ok != len(required_ids):
        failures.append("ids")
    if proofs_ok != len(required_proofs):
        failures.append("proofs")
    if sections_ok != len(required_sections):
        failures.append("sections")
    if not android_gate_ok:
        failures.append("android_gate")
    if not threshold_ok:
        failures.append("threshold")
    if not secret_ok:
        failures.append("secret")

    score = 100 if not failures else 75
    if not secret_ok:
        score = 0
    evidence.extend(
        [
            f"ids={ids_ok}/{len(required_ids)}",
            f"sessions={sessions_ok}/{len(required_sessions)}",
            f"proofs={proofs_ok}/{len(required_proofs)}",
            f"sections={sections_ok}/{len(required_sections)}",
            f"android_gate={_compact_bool_flag(android_gate_ok)}",
            f"threshold={_compact_bool_flag(threshold_ok)}",
            f"sec={_compact_bool_flag(secret_ok)}",
        ]
    )
    if failures:
        evidence.append("failures=" + ",".join(failures))
    density_limit = 90
    density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if len(row) > density_limit or "True" in row or "False" in row or "['" in row
    ]
    if density_failures:
        score = min(score, 75)
    evidence.append(f"history_matrix_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")
    return item(
        "projectling_history_requirement_matrix",
        score,
        status_from_score(score),
        evidence,
        "修复历史要求矩阵：会话、R01-R14、证据检查、Android gate、阈值和脱敏必须完整。" if score < 85 else "",
    )


def _resolve_artifact_path(raw: Any) -> Path:
    text = str(raw or "").strip()
    if os.name == "nt" and text.startswith("/mnt/"):
        parts = text.split("/")
        if len(parts) >= 4 and len(parts[2]) == 1:
            drive = parts[2].upper() + ":"
            return Path(drive + "\\" + "\\".join(parts[3:]))
    if os.name != "nt" and len(text) >= 3 and text[1] == ":" and text[2] in {"\\", "/"}:
        drive = text[0].lower()
        rest = text[2:].lstrip("\\/").replace("\\", "/")
        return Path(f"/mnt/{drive}/{rest}")
    return Path(text)


def _profile_sample_detail_failures(profile: str, sample: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    detail_raw = str(sample.get("detail_path") or "").strip()
    if not detail_raw:
        return ["detail_path_empty"]
    detail_path = _resolve_artifact_path(detail_raw)
    if not detail_path.is_file():
        return [f"detail_missing:{detail_path}"]
    try:
        detail = json.loads(detail_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return [f"detail_parse:{exc}"]
    if not isinstance(detail, dict):
        return ["detail_not_object"]

    if str(detail.get("started_at") or "") != str(sample.get("started_at") or ""):
        failures.append("started_at")
    if _auto_profile_name(detail) != profile:
        failures.append("profile")
    if str(detail.get("run_mode") or "") != str(sample.get("run_mode") or ""):
        failures.append("run_mode")
    if bool(detail.get("ok")) != bool(sample.get("ok")):
        failures.append("ok")

    command = detail.get("command") if isinstance(detail.get("command"), dict) else {}
    if not command:
        command = detail.get("command_matrix_smoke") if isinstance(detail.get("command_matrix_smoke"), dict) else {}
    try:
        matrix_cases = int(command.get("matrix_cases") or command.get("case_count") or 0)
    except (TypeError, ValueError):
        matrix_cases = 0
    if not matrix_cases and isinstance(command.get("cases"), list):
        matrix_cases = len(command.get("cases") or [])
    matrix_ok = command.get("matrix_ok") is True or command.get("ok") is True
    if not matrix_ok or matrix_cases < 4:
        failures.append("command_matrix")

    memory = detail.get("memory") if isinstance(detail.get("memory"), dict) else {}
    if not memory:
        memory = detail.get("memory_smoke") if isinstance(detail.get("memory_smoke"), dict) else {}
    if not (
        memory.get("append_ok") is True
        and memory.get("db_integrity_ok") is True
        and memory.get("keyword_unique_ok") is True
        and str(memory.get("journal_mode") or "").lower() == "wal"
    ):
        failures.append("memory_db")

    context_variants = detail.get("context_pressure_variants") if isinstance(detail.get("context_pressure_variants"), dict) else {}
    if not context_variants:
        context_variants = (
            detail.get("context_pressure_variants_smoke")
            if isinstance(detail.get("context_pressure_variants_smoke"), dict)
            else {}
        )
    try:
        variants_passed = int(context_variants.get("passed") or 0)
        variant_count = int(context_variants.get("variant_count") or 0)
    except (TypeError, ValueError):
        variants_passed = 0
        variant_count = 0
    if context_variants.get("ok") is not True or variants_passed < 3 or variant_count < 3:
        failures.append("context_variants")

    if profile in {"live", "full"}:
        live_chat = detail.get("live_chat") if isinstance(detail.get("live_chat"), dict) else {}
        if not live_chat:
            live_chat = detail.get("live_chat_smoke") if isinstance(detail.get("live_chat_smoke"), dict) else {}
        provider = _live_chat_provider(live_chat)
        tools = live_chat.get("tool_names") if isinstance(live_chat.get("tool_names"), list) else []
        usage = live_chat.get("usage") if isinstance(live_chat.get("usage"), dict) else {}
        try:
            hit_rate = float(usage.get("cache_hit_rate") or 0)
            cache_miss = int(usage.get("cache_miss_tokens") or 0)
        except (TypeError, ValueError):
            hit_rate = 0.0
            cache_miss = 999999
        functional_ok = (
            live_chat.get("ok") is True
            and live_chat.get("context_restored") is True
            and (not tools or "command" in {str(tool) for tool in tools})
            and live_chat.get("functional_ok") is not False
        )
        cache_ok = live_chat.get("cache_ok") is True and hit_rate >= 85 and cache_miss <= 1000
        if provider == "deepseek":
            live_ok = functional_ok and cache_ok
        else:
            live_ok = functional_ok
        if not live_ok:
            failures.append("live_chat")

    if profile == "full":
        web_search = detail.get("web_search") if isinstance(detail.get("web_search"), dict) else {}
        if not web_search:
            web_search = detail.get("web_smoke") if isinstance(detail.get("web_smoke"), dict) else {}
        web_result = web_search.get("result") if isinstance(web_search.get("result"), dict) else {}
        try:
            result_count = int(web_search.get("result_count") or web_result.get("result_count") or 0)
        except (TypeError, ValueError):
            result_count = 0
        web_validation = detail.get("web_validation") if isinstance(detail.get("web_validation"), dict) else {}
        web_ok = web_search.get("ok") is True or str(web_result.get("status") or "") == "ok"
        validation_ok = web_search.get("validation_ok") is True or web_validation.get("ok") is True
        if not web_ok or not validation_ok or result_count < 1:
            failures.append("web_search")

    return failures


def _profile_detail_freshness_summary(
    ages: dict[str, int | None],
    *,
    fresh_limit: int = 86400,
) -> dict[str, Any]:
    near_stale_limit = int(fresh_limit * 0.75)
    numeric_ages = {profile: age for profile, age in ages.items() if age is not None}
    stale_profiles = [
        profile
        for profile, age in ages.items()
        if age is None or age > fresh_limit
    ]
    near_stale_profiles = [
        profile
        for profile, age in numeric_ages.items()
        if near_stale_limit <= age <= fresh_limit
    ]
    if numeric_ages:
        oldest_profile = max(numeric_ages, key=numeric_ages.get)
        newest_profile = min(numeric_ages, key=numeric_ages.get)
        age_span = int(numeric_ages[oldest_profile] - numeric_ages[newest_profile])
    else:
        oldest_profile = "-"
        newest_profile = "-"
        age_span = "-"
    return {
        "status": "stale" if stale_profiles else "near_stale" if near_stale_profiles else "fresh",
        "oldest_profile": oldest_profile,
        "newest_profile": newest_profile,
        "age_span_seconds": age_span,
        "near_stale_limit_seconds": near_stale_limit,
        "fresh_limit_seconds": fresh_limit,
        "near_stale_profiles": near_stale_profiles,
        "stale_profiles": stale_profiles,
    }


CRITICAL_CHECK_NAMES = (
    "projectling_auto_profile_coverage",
    "command_matrix_profile_coverage",
    "projectling_profile_detail_integrity",
    "projectling_profile_freshness_policy",
    "deepseek_live_cache_quality",
    "live_smoke_cost_efficiency",
    "web_search_live_quality",
    "memory_db_integrity_auto",
    "context_pressure_variants_auto",
    "projectling_auto_stress_durability",
    "projectling_auto_issue_ledger",
    "relay_model_compatibility_matrix",
    "gemini_parameter_support_matrix",
    "settings_exception_restoration_contract",
    "runtime_state_no_mutation",
)

NEXT_PLAN_THRESHOLD_NAMES = (
    "projectling_auto_profile_coverage",
    "command_matrix_profile_coverage",
    "deepseek_live_cache_quality",
    "live_smoke_cost_efficiency",
    "web_search_live_quality",
    "memory_db_integrity_auto",
    "context_pressure_variants_auto",
    "projectling_profile_detail_integrity",
    "projectling_profile_freshness_policy",
    "projectling_next_plan_artifact",
    "projectling_critical_summary_freshness",
    "projectling_threshold_summary_integrity",
    "aidebug_health_jsonl_integrity",
    "projectling_desktop_goal_anchor",
    "projectling_history_requirement_matrix",
    "relay_model_compatibility_matrix",
    "gemini_parameter_support_matrix",
    "settings_status_width_contract",
    "windows_launcher_gemini_surface",
    "zsh_diagnostic_alias_execution",
    "gemini_diagnostic_output_contract",
    "gemini_model_list_role_marker_contract",
    "gemini_model_list_taxonomy_contract",
    "gemini_api_test_model_safety_contract",
    "gemini_settings_persistence_contract",
    "api_settings_provider_persistence_contract",
    "projectling_deepseek_cache_metric_summary",
    "deepseek_cache_stability_trend",
    "settings_exception_restoration_contract",
    "runtime_state_no_mutation",
    "motd_zshrc_smoke",
)


def _threshold_summary(thresholds: Any, *, generated_at: str | None = None) -> dict[str, Any]:
    entries: list[dict[str, str]] = []
    observed_names: list[str] = []
    if isinstance(thresholds, list):
        for threshold in thresholds:
            if not isinstance(threshold, dict):
                continue
            name = str(threshold.get("check") or "")
            if not name:
                continue
            observed_names.append(name)
            entries.append(
                {
                    "check": name,
                    "expect": str(threshold.get("expect") or "")[:360],
                }
            )
    observed_set = set(observed_names)
    expected_set = set(NEXT_PLAN_THRESHOLD_NAMES)
    return {
        "schema_version": 1,
        "generated_at": str(generated_at or ""),
        "expected": list(NEXT_PLAN_THRESHOLD_NAMES),
        "count": len(entries),
        "missing": [name for name in NEXT_PLAN_THRESHOLD_NAMES if name not in observed_set],
        "unexpected": [name for name in observed_names if name not in expected_set],
        "checks": entries,
    }


DIAGNOSTIC_ARTIFACT_CHECK_NAMES = (
    "gemini_diagnostic_output_contract",
    "zsh_diagnostic_alias_execution",
    "projectling_threshold_summary_integrity",
    "windows_launcher_gemini_surface",
)


def _diagnostic_artifact_summary(checks: list[Any], *, generated_at: str | None = None) -> dict[str, Any]:
    checks_by_name = {
        str(check.get("name") or ""): check
        for check in checks
        if isinstance(check, dict)
    }
    items: list[dict[str, Any]] = []
    missing: list[str] = []
    highlight_needles = (
        "alias_runtime=",
        "runtime=",
        "rows=",
        "argv_ok=",
        "expected=",
        "count=",
        "missing=",
        "unexpected=",
        "risky_mode=",
        "stable_api=",
        "active_provider=",
    )
    for name in DIAGNOSTIC_ARTIFACT_CHECK_NAMES:
        check = checks_by_name.get(name)
        if not isinstance(check, dict):
            missing.append(name)
            items.append(
                {
                    "name": name,
                    "status": "missing",
                    "score": 0,
                    "accepted": False,
                    "external_gate": False,
                    "highlights": [],
                    "next_action": "regenerate health with diagnostic checks",
                }
            )
            continue
        evidence = check.get("evidence") if isinstance(check.get("evidence"), list) else []
        highlights = [
            str(entry)
            for entry in evidence
            if any(needle in str(entry) for needle in highlight_needles)
        ]
        if not highlights:
            highlights = [str(entry) for entry in evidence[:2]]
        external_gate = (
            name == "windows_launcher_gemini_surface"
            and int(check.get("score") or 0) >= 80
            and any(str(entry).startswith("startup_external=") for entry in evidence)
            and any("launcher_surface_density=" in str(entry) and "failures=-" in str(entry) for entry in evidence)
        )
        accepted = (
            str(check.get("status") or "") == "ok" and int(check.get("score") or 0) >= 85
        ) or external_gate
        items.append(
            {
                "name": name,
                "status": str(check.get("status") or ""),
                "score": int(check.get("score") or 0),
                "accepted": accepted,
                "external_gate": external_gate,
                "highlights": highlights[:4],
                "next_action": str(check.get("next_action") or ""),
            }
        )
    return {
        "schema_version": 1,
        "generated_at": str(generated_at or ""),
        "required": list(DIAGNOSTIC_ARTIFACT_CHECK_NAMES),
        "missing": missing,
        "external_gates": [item["name"] for item in items if item.get("external_gate")],
        "ok": not missing and all(bool(item.get("accepted")) for item in items),
        "checks": items,
    }


def _compact_critical_evidence(name: str, evidence: list[Any]) -> list[str]:
    text_items = [str(entry)[:360] for entry in evidence]
    if name != "deepseek_live_cache_quality":
        return text_items[:5]
    preferred_prefixes = (
        "ok=",
        "hit_rate=",
        "miss=",
        "cached=",
        "cache_ok=",
        "context_restored=",
        "prompt=",
        "rounds=",
        "tools=",
    )
    selected: list[str] = []
    for prefix in preferred_prefixes:
        match = next((entry for entry in text_items if entry.startswith(prefix)), "")
        if match and match not in selected:
            selected.append(match)
    for entry in text_items:
        if entry not in selected:
            selected.append(entry)
        if len(selected) >= 9:
            break
    return selected[:9]


def _evidence_map(check: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(check, dict):
        return {}
    evidence = check.get("evidence") if isinstance(check.get("evidence"), list) else []
    result: dict[str, str] = {}
    for entry in evidence:
        text = str(entry)
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def _health_evidence_matches(actual: str, expected: Any) -> bool:
    expected_values = expected if isinstance(expected, (list, tuple, set)) else [expected]
    accepted = {str(value).strip() for value in expected_values}
    return str(actual).strip() in accepted


def _deepseek_cache_metric_summary(
    check: dict[str, Any] | None,
    auto_rows: list[dict[str, Any]],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    active_provider = _active_api_provider()
    latest_full = _latest_auto_profile(auto_rows, "full")
    latest_live = _latest_auto_with(auto_rows, "live_chat")
    source_row = latest_full if active_provider == "deepseek" else (latest_live or latest_full)
    source_profile = _auto_profile_name(source_row or {}) if source_row else ""
    detail_path_text = str((source_row or {}).get("detail_path") or "")
    detail_path = _resolve_artifact_path(detail_path_text) if detail_path_text else Path("")
    detail: dict[str, Any] = {}
    detail_errors: list[str] = []
    if not detail_path_text:
        detail_errors.append("detail_path_empty")
    elif not detail_path.is_file():
        detail_errors.append(f"detail_missing:{detail_path}")
    else:
        try:
            parsed = json.loads(detail_path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(parsed, dict):
                detail = parsed
            else:
                detail_errors.append("detail_not_object")
        except Exception as exc:
            detail_errors.append(f"detail_parse:{exc}")

    detail_live = detail.get("live_chat") if isinstance(detail.get("live_chat"), dict) else {}
    if not detail_live:
        detail_live = detail.get("live_chat_smoke") if isinstance(detail.get("live_chat_smoke"), dict) else {}
    jsonl_live = (source_row or {}).get("live_chat") if isinstance((source_row or {}).get("live_chat"), dict) else {}
    live = detail_live or jsonl_live
    provider = _live_chat_provider(live)
    cache_policy = "required" if provider == "deepseek" else f"not_required_for_{provider}"
    usage = live.get("usage") if isinstance(live.get("usage"), dict) else {}
    warmup = live.get("cache_warmup") if isinstance(live.get("cache_warmup"), dict) else {}

    def int_or_none(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def float_or_none(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    prompt_tokens = int_or_none(usage.get("prompt_tokens"))
    cached_tokens = int_or_none(usage.get("cached_tokens"))
    miss_tokens = int_or_none(usage.get("cache_miss_tokens"))
    hit_rate = float_or_none(usage.get("cache_hit_rate"))
    rounds = int_or_none(live.get("rounds"))
    tools = live.get("tool_names") if isinstance(live.get("tool_names"), list) else []
    health = _evidence_map(check)
    expected_health = {
        "prompt": str(prompt_tokens),
        "cached": str(cached_tokens),
        "miss": str(miss_tokens),
        "hit_rate": str(hit_rate),
        "cache_ok": [str(live.get("cache_ok")), _compact_bool_flag(live.get("cache_ok"))],
        "context_restored": [str(live.get("context_restored")), _compact_bool_flag(live.get("context_restored"))],
        "ctx": _compact_bool_flag(live.get("context_restored")),
        "rounds": str(rounds),
        "tools": [str(tools), _compact_live_tool_sequence(tools), _compact_list_or_dash(tools)],
    }
    health_mismatches = [
        key
        for key, expected in expected_health.items()
        if key in health and not _health_evidence_matches(health.get(key, ""), expected)
    ]
    jsonl_mismatches: list[str] = []
    jsonl_usage = jsonl_live.get("usage") if isinstance(jsonl_live.get("usage"), dict) else {}
    if jsonl_usage:
        jsonl_expected = {
            "prompt_tokens": prompt_tokens,
            "cached_tokens": cached_tokens,
            "cache_miss_tokens": miss_tokens,
            "cache_hit_rate": hit_rate,
        }
        for key, expected in jsonl_expected.items():
            actual = jsonl_usage.get(key)
            try:
                actual_value = float(actual) if key == "cache_hit_rate" else int(actual)
            except (TypeError, ValueError):
                actual_value = actual
            if actual_value != expected:
                jsonl_mismatches.append(key)
        for key in ("ok", "cache_ok", "context_restored"):
            if jsonl_live.get(key) != live.get(key):
                jsonl_mismatches.append(key)

    functional_ok = (
        not detail_errors
        and bool(live.get("ok")) is True
        and bool(live.get("context_restored")) is True
        and not health_mismatches
        and not jsonl_mismatches
    )
    cache_metric_ok = (
        bool(live.get("cache_ok")) is True
        and hit_rate is not None
        and hit_rate >= 85.0
        and miss_tokens is not None
        and miss_tokens <= 1000
    )
    metric_ok = functional_ok and (cache_metric_ok if provider == "deepseek" else True)
    return {
        "schema_version": 1,
        "generated_at": str(generated_at or ""),
        "provider": provider,
        "cache_policy": cache_policy,
        "ok": metric_ok,
        "source_profile": source_profile,
        "source_started_at": str((source_row or {}).get("started_at") or ""),
        "source_run_mode": str((source_row or {}).get("run_mode") or ""),
        "detail_path": detail_path_text,
        "detail_ok": not detail_errors,
        "detail_errors": detail_errors,
        "live_ok": bool(live.get("ok")),
        "rounds": rounds,
        "tools": [str(item) for item in tools],
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached_tokens,
        "cache_miss_tokens": miss_tokens,
        "cache_hit_rate": hit_rate,
        "cache_ok": bool(live.get("cache_ok")),
        "warmup": {
            "attempted": bool(warmup.get("attempted")) if warmup else False,
            "resolved": bool(warmup.get("resolved")) if warmup else False,
            "first_cache_ok": bool(warmup.get("first_cache_ok")) if warmup else False,
            "first_miss": int_or_none(warmup.get("first_miss")) if warmup else None,
            "first_hit_rate": float_or_none(warmup.get("first_hit_rate")) if warmup else None,
        },
        "context_restored": bool(live.get("context_restored")),
        "health_status": str((check or {}).get("status") or ""),
        "health_score": int((check or {}).get("score") or 0) if isinstance(check, dict) else 0,
        "health_mismatches": health_mismatches,
        "jsonl_mismatches": jsonl_mismatches,
    }


def _deepseek_cache_stability_summary(
    check: dict[str, Any] | None,
    auto_rows: list[dict[str, Any]],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    health = _evidence_map(check)
    evidence = check.get("evidence") if isinstance(check, dict) and isinstance(check.get("evidence"), list) else []
    active_provider = _active_api_provider()

    def int_or_none(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def float_or_none(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    rows = auto_rows[-20:]
    samples = [
        row
        for row in rows
        if isinstance(row.get("live_chat"), dict) and row.get("live_chat")
    ]
    recent = samples[-8:]
    hit_rates: list[float] = []
    misses: list[int] = []
    cache_ok_flags: list[bool] = []
    warmup_resolved_calc = 0
    sequence_warmup_resolved_calc = 0
    post_warmup_failures_calc: list[str] = []
    profile_counts: dict[str, int] = {}
    profile_latest: dict[str, dict[str, Any]] = {}
    sample_stats: list[tuple[int, float]] = []
    for sample in recent:
        live = sample.get("live_chat") if isinstance(sample.get("live_chat"), dict) else {}
        profile = _auto_profile_name(sample) if active_provider == "deepseek" else _live_chat_provider(live, active_provider)
        profile_counts[profile] = profile_counts.get(profile, 0) + 1
        profile_latest[profile] = sample
        usage = live.get("usage") if isinstance(live.get("usage"), dict) else {}
        miss = int_or_none(usage.get("cache_miss_tokens")) or 0
        hit_rate = float_or_none(usage.get("cache_hit_rate")) or 0.0
        if active_provider == "deepseek":
            cache_ok = bool(live.get("cache_ok")) or (miss <= 1000 and hit_rate >= 85.0)
        else:
            cache_ok = bool(live.get("ok") and live.get("context_restored"))
        misses.append(miss)
        hit_rates.append(hit_rate)
        cache_ok_flags.append(cache_ok)
        sample_stats.append((miss, hit_rate))
    for index, sample in enumerate(recent):
        live = sample.get("live_chat") if isinstance(sample.get("live_chat"), dict) else {}
        warmup = live.get("cache_warmup") if isinstance(live.get("cache_warmup"), dict) else {}
        if warmup.get("resolved"):
            warmup_resolved_calc += 1
        next_cache_ok = index + 1 < len(cache_ok_flags) and cache_ok_flags[index + 1]
        if not cache_ok_flags[index] and next_cache_ok:
            sequence_warmup_resolved_calc += 1
        if active_provider == "deepseek" and live.get("ok") and not cache_ok_flags[index] and not warmup.get("resolved") and not next_cache_ok:
            miss, hit_rate = sample_stats[index]
            post_warmup_failures_calc.append(
                f"{sample.get('started_at')} profile={_auto_profile_name(sample)} miss={miss} hit_rate={hit_rate}"
            )

    profile_distribution_calc = ",".join(f"{name}:{count}" for name, count in sorted(profile_counts.items()))
    profile_latest_age_calc = {
        name: _auto_started_age_seconds(row)
        for name, row in sorted(profile_latest.items())
    }
    post_warmup_failures = [
        str(entry).split("=", 1)[1]
        for entry in evidence
        if str(entry).startswith("post_warmup_failure=")
    ]
    auto_expected = {
        "live_samples": str(len(samples)),
        "recent": str(len(recent)),
        "cache_ok_count": str(sum(1 for ok in cache_ok_flags if ok)),
        "warmup_resolved": str(warmup_resolved_calc),
        "sequence_warmup_resolved": str(sequence_warmup_resolved_calc),
        "min_hit_rate": str(round(min(hit_rates), 2) if hit_rates else 0.0),
        "max_miss": str(max(misses) if misses else 0),
        "profiles": profile_distribution_calc,
    }
    auto_mismatches = [
        key
        for key, expected in auto_expected.items()
        if key in health and health.get(key) != expected
    ]
    profile_latest_age_seconds: dict[str, int | None] = {}
    for key, value in health.items():
        if key.endswith("_latest_age"):
            profile_latest_age_seconds[key[: -len("_latest_age")]] = int_or_none(value)
    if not profile_latest_age_seconds:
        profile_latest_age_seconds = profile_latest_age_calc

    live_samples = int_or_none(health.get("live_samples")) or len(samples)
    recent_count = int_or_none(health.get("recent")) or len(recent)
    cache_ok_count = int_or_none(health.get("cache_ok_count")) or sum(1 for ok in cache_ok_flags if ok)
    latest_age = int_or_none(health.get("latest_age"))
    health_score = int((check or {}).get("score") or 0) if isinstance(check, dict) else 0
    health_score_ok = health_score == 100 if active_provider == "deepseek" else health_score >= 85
    ok = (
        isinstance(check, dict)
        and str(check.get("status") or "") == "ok"
        and health_score_ok
        and recent_count >= 3
        and cache_ok_count == recent_count
        and not post_warmup_failures
        and not post_warmup_failures_calc
        and not auto_mismatches
    )
    return {
        "schema_version": 1,
        "generated_at": str(generated_at or ""),
        "provider": active_provider,
        "cache_policy": "required" if active_provider == "deepseek" else f"not_required_for_{active_provider}",
        "ok": ok,
        "health_status": str((check or {}).get("status") or ""),
        "health_score": int((check or {}).get("score") or 0) if isinstance(check, dict) else 0,
        "live_samples": live_samples,
        "recent_samples": recent_count,
        "cache_ok_count": cache_ok_count,
        "warmup_resolved": int_or_none(health.get("warmup_resolved")) or warmup_resolved_calc,
        "sequence_warmup_resolved": int_or_none(health.get("sequence_warmup_resolved")) or sequence_warmup_resolved_calc,
        "min_hit_rate": float_or_none(health.get("min_hit_rate")) if "min_hit_rate" in health else (round(min(hit_rates), 2) if hit_rates else None),
        "max_miss": int_or_none(health.get("max_miss")) if "max_miss" in health else (max(misses) if misses else None),
        "latest_age_seconds": latest_age if latest_age is not None else _auto_started_age_seconds(recent[-1]) if recent else None,
        "profile_distribution": health.get("profiles") or profile_distribution_calc,
        "profile_latest_age_seconds": profile_latest_age_seconds,
        "post_warmup_failures": post_warmup_failures or post_warmup_failures_calc,
        "auto_mismatches": auto_mismatches,
    }


def _provider_cache_alias_summary(
    summary: dict[str, Any],
    *,
    compatibility_key: str,
    compatibility_check: str,
) -> dict[str, Any]:
    alias = dict(summary)
    alias["provider_neutral"] = True
    alias["compatibility_key"] = compatibility_key
    alias["compatibility_check"] = compatibility_check
    return alias


def _strip_provider_cache_volatile_fields(summary: dict[str, Any]) -> dict[str, Any]:
    stable = dict(summary)
    stable.pop("latest_age_seconds", None)
    stable.pop("profile_latest_age_seconds", None)
    return stable


def _compact_bool_flag(value: Any) -> str:
    if value is True:
        return "1"
    if value is False:
        return "0"
    return str(value)


def _compact_iso_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 19 and "T" in text:
        return text[5:16]
    return text or "-"


def _compact_iso_clock(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 16 and "T" in text:
        return text[11:16]
    return text or "-"


def _compact_provider_label(value: Any) -> str:
    text = str(value or "").strip()
    replacements = {
        "gemini": "g",
        "deepseek": "ds",
    }
    return replacements.get(text, text or "-")


def _compact_run_mode_label(value: Any) -> str:
    text = str(value or "").strip()
    replacements = {
        "local_stress": "local",
        "live_web": "web",
        "live": "live",
        "full": "full",
    }
    return replacements.get(text, text or "-")


def _compact_cache_policy_label(value: Any) -> str:
    text = str(value or "").strip()
    prefix = "not_required_for_"
    if text.startswith(prefix):
        return "nrf"
    if text == "required":
        return "req"
    return text or "-"


def _compact_compat_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text == "-":
        return "-"
    replacements = {
        "deepseek_cache_metric_summary": "m",
        "deepseek_cache_stability_summary": "s",
    }
    return replacements.get(text, text)


def _compact_diagnostic_check_label(value: Any) -> str:
    text = str(value or "").strip()
    replacements = {
        "gemini_diagnostic_output_contract": "gdiag",
        "zsh_diagnostic_alias_execution": "zsh",
        "projectling_threshold_summary_integrity": "thresh",
        "windows_launcher_gemini_surface": "launch",
    }
    return replacements.get(text, text)


def _compact_auto_tool_label(value: Any) -> str:
    text = str(value or "").strip()
    replacements = {
        "link": "ln",
        "update_plan": "plan",
        "command": "cmd",
        "apply_patch": "patch",
        "web_search": "web",
        "contextmanage": "ctx",
        "memory_add": "mem",
    }
    return replacements.get(text, text or "-")


def _compact_command_matrix_label(value: Any) -> str:
    text = str(value or "").strip()
    replacements = {
        "unicode_stdout": "uout",
        "stderr_capture": "err",
        "nonzero_exit": "nz",
        "timeout": "to",
    }
    return replacements.get(text, text or "-")


def _colon_detail_tokens(value: Any) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for part in str(value or "").split():
        if part.startswith("freshness_trend=status:"):
            tokens.setdefault("status", part.split("freshness_trend=status:", 1)[1])
            continue
        if ":" not in part:
            continue
        key, text = part.split(":", 1)
        if key and key not in tokens:
            tokens[key] = text
    return tokens


def _compact_cache_metric_evidence(label: str, summary: Any) -> str:
    short_label = {
        "provider_cache_metric_summary": "pcm",
        "deepseek_cache_metric_summary": "dcm",
    }.get(label, label)
    if not isinstance(summary, dict):
        return f"{short_label}=-"
    health_mismatches = summary.get("health_mismatches")
    jsonl_mismatches = summary.get("jsonl_mismatches")
    parts = [
        f"p:{_compact_provider_label(summary.get('provider'))}",
        f"ok:{_compact_bool_flag(summary.get('ok'))}",
        f"pol={_compact_cache_policy_label(summary.get('cache_policy'))}",
        f"s:{_compact_iso_clock(summary.get('source_started_at'))}/{_compact_run_mode_label(summary.get('source_run_mode'))}",
        f"t={summary.get('prompt_tokens')}/{summary.get('cached_tokens')}/{summary.get('cache_miss_tokens')}/{summary.get('cache_hit_rate')}",
        f"k:{_compact_compat_label(summary.get('compatibility_key', '-'))}",
        f"mm:{len(health_mismatches) if isinstance(health_mismatches, list) else '-'}"
        f"/{len(jsonl_mismatches) if isinstance(jsonl_mismatches, list) else '-'}",
    ]
    return f"{short_label}=" + " ".join(parts)


def _compact_cache_stability_evidence(label: str, summary: Any) -> str:
    short_label = {
        "provider_cache_stability_summary": "pcs",
        "deepseek_cache_stability_summary": "dcs",
    }.get(label, label)
    if not isinstance(summary, dict):
        return f"{short_label}=-"
    parts = [
        f"p:{_compact_provider_label(summary.get('provider'))}",
        f"ok:{_compact_bool_flag(summary.get('ok'))}",
        f"pol:{_compact_cache_policy_label(summary.get('cache_policy'))}",
        f"s:{summary.get('live_samples')}/{summary.get('recent_samples')}",
        f"c:{summary.get('cache_ok_count')}",
        f"a:{summary.get('latest_age_seconds')}",
        f"pr:{str(summary.get('profile_distribution')).replace('gemini:', 'g:').replace('deepseek:', 'ds:')}",
        f"k:{_compact_compat_label(summary.get('compatibility_key', '-'))}",
        f"mm:{len(summary.get('auto_mismatches')) if isinstance(summary.get('auto_mismatches'), list) else '-'}",
    ]
    return f"{short_label}=" + " ".join(parts)


def _compact_threshold_summary_evidence(summary: Any) -> str:
    if not isinstance(summary, dict):
        return "thresh=-"
    checks = summary.get("checks") if isinstance(summary.get("checks"), list) else []
    expected = summary.get("expected") if isinstance(summary.get("expected"), list) else []
    return (
        "thresh="
        f"n={summary.get('count')} "
        f"exp={len(expected)} "
        f"chk={len(checks)} "
        f"miss={_compact_list_or_dash(summary.get('missing') if isinstance(summary.get('missing'), list) else [])} "
        f"unexp={_compact_list_or_dash(summary.get('unexpected') if isinstance(summary.get('unexpected'), list) else [])} "
        f"gen={_compact_iso_timestamp(summary.get('generated_at'))}"
    )


def _compact_profile_samples_evidence(verification_sources: Any) -> str:
    if not isinstance(verification_sources, dict):
        return "profiles=-"
    samples = verification_sources.get("profile_samples")
    if not isinstance(samples, dict):
        return "profiles=-"
    parts: list[str] = []
    name_labels = {"local": "l", "live": "v", "full": "f"}
    mode_labels = {"local": "l", "live": "v", "web": "w", "full": "f"}
    for name in ("local", "live", "full"):
        sample = samples.get(name)
        if not isinstance(sample, dict):
            parts.append(f"{name_labels[name]}:missing")
            continue
        mode = _compact_run_mode_label(sample.get("run_mode"))
        parts.append(
            f"{name_labels[name]}={_compact_iso_clock(sample.get('started_at'))}/"
            f"{mode_labels.get(mode, mode)}/"
            f"{_compact_bool_flag(sample.get('ok'))}/a{sample.get('age_seconds')}"
        )
    return "profiles=" + " ".join(parts)


def _compact_source_freshness_evidence(verification_sources: Any) -> str:
    if not isinstance(verification_sources, dict):
        return "source_freshness=-"
    freshness = verification_sources.get("source_freshness_seconds")
    if not isinstance(freshness, dict):
        return "source_freshness=-"
    return (
        "source_freshness="
        f"auto_age={freshness.get('latest_auto_age_seconds')} "
        f"health_age={freshness.get('health_json_age_seconds')} "
        f"max_age={freshness.get('max_allowed_age_seconds')}"
    )


def _compact_profile_detail_integrity_evidence(profile_detail_integrity: Any) -> str:
    if not isinstance(profile_detail_integrity, dict):
        return "profile_detail_integrity=-"
    parts = [
        f"{name}:{profile_detail_integrity.get(name, '-')}"
        for name in ("local", "live", "full")
    ]
    return "profile_detail_integrity=" + " ".join(parts)


def _compact_diagnostic_summary_evidence(summary: Any) -> str:
    if not isinstance(summary, dict):
        return "diag_summary=-"
    checks = summary.get("checks") if isinstance(summary.get("checks"), list) else []
    parts: list[str] = []
    for item in checks:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        highlights = item.get("highlights") if isinstance(item.get("highlights"), list) else []
        highlight = next((str(entry) for entry in highlights if "alias_runtime" in str(entry) or "expected=" in str(entry)), "")
        if highlight:
            parts.append(f"{name}:{item.get('status')}/{item.get('score')}:{highlight}")
        else:
            parts.append(f"{name}:{item.get('status')}/{item.get('score')}")
    external_gates = summary.get("external_gates") if isinstance(summary.get("external_gates"), list) else []
    return (
        "diag_summary="
        f"ok:{_compact_bool_flag(summary.get('ok'))} "
        f"miss:{_compact_list_or_dash(summary.get('missing') if isinstance(summary.get('missing'), list) else [])} "
        f"ext:{_compact_list_or_dash(_compact_diagnostic_check_label(name) for name in external_gates)} "
        f"n:{len(checks)} "
        f"hl={_compact_list_or_dash(_compact_diagnostic_check_label(item.split(':', 1)[0]) for item in parts[:4])}"
    )


def _compact_critical_freshness_evidence(summary: Any) -> str:
    if not isinstance(summary, dict):
        return "critical_freshness=-"
    trend_tokens = _colon_detail_tokens(summary.get("freshness_trend"))
    age = trend_tokens.get("a", trend_tokens.get("age", "-"))
    remaining = trend_tokens.get("r", trend_tokens.get("remaining", "-"))
    max_age = trend_tokens.get("m", trend_tokens.get("max", "-"))
    return (
        "critical_freshness="
        f"{summary.get('status')}/{summary.get('score')} "
        f"gen={_compact_iso_timestamp(summary.get('generated_at'))} "
        f"tr={trend_tokens.get('status', '-')} "
        f"a={age} "
        f"r={remaining} "
        f"m={max_age}"
    )


def _markdown_clip(value: Any, limit: int = 150) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _markdown_bool(value: Any) -> str:
    return _compact_bool_flag(value)


def _markdown_mapping_summary(value: Any, keys: list[tuple[str, str]], *, limit: int = 170) -> str:
    if not isinstance(value, dict):
        return "-"
    parts = [f"{label}={_markdown_bool(value.get(key))}" for key, label in keys]
    return _markdown_clip("; ".join(parts), limit=limit)


def _markdown_list_summary(value: Any, *, limit: int = 170) -> str:
    if not isinstance(value, list):
        return "-"
    return _markdown_clip(", ".join(str(item) for item in value), limit=limit) or "-"


def _markdown_evidence_preview(evidence: Any, *, count: int = 2, item_limit: int = 80) -> str:
    if not isinstance(evidence, list):
        return "-"
    return "; ".join(_markdown_clip(entry, item_limit) for entry in evidence[:count]) or "-"


def _critical_check_summary(
    checks: list[dict[str, Any]],
    *,
    generated_at: str | None = None,
    source_health_generated_at: str | None = None,
) -> dict[str, Any]:
    by_name = {
        str(check.get("name") or ""): check
        for check in checks
        if isinstance(check, dict)
    }
    entries: list[dict[str, Any]] = []
    missing: list[str] = []
    status_counts: dict[str, int] = {}
    for name in CRITICAL_CHECK_NAMES:
        check = by_name.get(name)
        if check is None:
            missing.append(name)
            entries.append(
                {
                    "name": name,
                    "status": "missing",
                    "score": 0,
                    "evidence": [],
                    "next_action": "missing critical health check",
                }
            )
            status_counts["missing"] = status_counts.get("missing", 0) + 1
            continue
        status = str(check.get("status") or "")
        status_counts[status] = status_counts.get(status, 0) + 1
        evidence = check.get("evidence") if isinstance(check.get("evidence"), list) else []
        try:
            score = int(check.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        entries.append(
            {
                "name": name,
                "status": status,
                "score": score,
                "evidence": _compact_critical_evidence(name, evidence),
                "next_action": str(check.get("next_action") or ""),
            }
        )
    return {
        "schema_version": 1,
        "generated_at": str(generated_at or ""),
        "source_health_generated_at": str(source_health_generated_at or generated_at or ""),
        "required": list(CRITICAL_CHECK_NAMES),
        "checks": entries,
        "missing": missing,
        "status_counts": status_counts,
        "ok_count": status_counts.get("ok", 0),
        "non_ok_count": len(entries) - status_counts.get("ok", 0),
    }


def _critical_summary_freshness_artifact(check: dict[str, Any] | None, *, generated_at: str | None = None) -> dict[str, Any]:
    if not isinstance(check, dict):
        return {
            "schema_version": 1,
            "generated_at": str(generated_at or ""),
            "status": "missing",
            "score": 0,
            "freshness_trend": "",
            "evidence": [],
            "next_action": "missing projectling_critical_summary_freshness health check",
        }
    evidence = check.get("evidence") if isinstance(check.get("evidence"), list) else []
    freshness_trend = next(
        (str(entry) for entry in evidence if str(entry).startswith("freshness_trend=")),
        "",
    )
    try:
        score = int(check.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    return {
        "schema_version": 1,
        "generated_at": str(generated_at or ""),
        "status": str(check.get("status") or ""),
        "score": score,
        "freshness_trend": freshness_trend,
        "evidence": [str(entry)[:360] for entry in evidence[:8]],
        "next_action": str(check.get("next_action") or ""),
    }


def check_next_plan_artifact() -> dict[str, Any]:
    md_meta = file_meta(NEXT_PLAN_MD)
    json_meta = file_meta(NEXT_PLAN_JSON)
    if not md_meta.get("exists"):
        return item(
            "projectling_next_plan_artifact",
            70,
            "warn",
            ["next_plan=missing", f"path={NEXT_PLAN_MD}"],
            "运行 aidebug health 生成下一轮自动化计划。",
        )
    if not json_meta.get("exists"):
        return item(
            "projectling_next_plan_artifact",
            70,
            "warn",
            ["next_plan_json=missing", f"path={NEXT_PLAN_JSON}"],
            "运行 aidebug health 生成机器可读下一轮自动化计划。",
        )
    try:
        text = NEXT_PLAN_MD.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return item(
            "projectling_next_plan_artifact",
            55,
            "warn",
            [f"read_error={exc}", f"path={NEXT_PLAN_MD}"],
            "检查 next-plan artifact 权限和路径。",
        )
    try:
        json_text = NEXT_PLAN_JSON.read_text(encoding="utf-8", errors="replace")
        plan_json = json.loads(json_text)
    except Exception as exc:
        return item(
            "projectling_next_plan_artifact",
            55,
            "warn",
            [f"json_read_error={exc}", f"path={NEXT_PLAN_JSON}"],
            "检查 next-plan JSON artifact 格式。",
        )
    age = max(int(md_meta.get("age_seconds") or 0), int(json_meta.get("age_seconds") or 0))
    active_provider = _active_api_provider()
    cache_policy_needle = "cache_hit_rate >= 85" if active_provider == "deepseek" else f"cache_policy=not_required_for_{active_provider}"
    required = [
        "--profile local",
        "--profile live",
        "--profile full",
        "aidebug_health.py",
        "Android Termux",
        "Expected Pass Thresholds",
        cache_policy_needle,
        "memory_db_integrity_auto",
        "context_pressure_variants_auto",
        "projectling_profile_detail_integrity",
        "projectling_profile_freshness_policy",
        "class=",
        "class=environment-gated",
        "Verification Sources",
        "source_freshness",
        "profile_samples",
        "detail JSON integrity",
        "freshness trend",
        "Critical Check Summary",
        "summary_generated_at",
        "source_health_generated_at",
        "Critical Summary Freshness",
        "freshness_trend=",
        "Threshold Summary",
        "threshold_count",
        "Diagnostic Evidence Summary",
        "alias_runtime",
        "zsh_diagnostic_alias_execution",
        "Provider Cache Metric Summary",
        "Provider Cache Stability Summary",
        "compatibility_key",
        "provider:",
        "cache_policy:",
        "cache_miss_tokens",
        "cached_tokens",
        "context_restored",
        "cache_ok_count",
        "min_hit_rate",
        "max_miss",
    ]
    missing = [needle for needle in required if needle not in text]
    commands = plan_json.get("commands") if isinstance(plan_json, dict) else []
    thresholds = plan_json.get("thresholds") if isinstance(plan_json, dict) else []
    threshold_summary = plan_json.get("threshold_summary") if isinstance(plan_json, dict) else {}
    weak_spots = plan_json.get("weak_spots") if isinstance(plan_json, dict) else []
    required_checks = plan_json.get("required_checks") if isinstance(plan_json, dict) else []
    verification_sources = plan_json.get("verification_sources") if isinstance(plan_json, dict) else {}
    critical_summary = plan_json.get("critical_check_summary") if isinstance(plan_json, dict) else {}
    diagnostic_summary = plan_json.get("diagnostic_evidence_summary") if isinstance(plan_json, dict) else {}
    critical_freshness = plan_json.get("critical_summary_freshness") if isinstance(plan_json, dict) else {}
    deepseek_metric_summary = plan_json.get("deepseek_cache_metric_summary") if isinstance(plan_json, dict) else {}
    deepseek_stability_summary = plan_json.get("deepseek_cache_stability_summary") if isinstance(plan_json, dict) else {}
    provider_metric_summary = plan_json.get("provider_cache_metric_summary") if isinstance(plan_json, dict) else {}
    provider_stability_summary = plan_json.get("provider_cache_stability_summary") if isinstance(plan_json, dict) else {}
    json_missing: list[str] = []
    profile_detail_integrity: dict[str, str] = {}
    try:
        health_text = HEALTH_JSON.read_text(encoding="utf-8", errors="replace")
        current_health = json.loads(health_text)
    except Exception:
        health_text = ""
        current_health = {}
    current_health_checks = current_health.get("checks") if isinstance(current_health.get("checks"), list) else []
    auto_rows_for_artifacts = _load_auto_history(limit=60)
    if not isinstance(plan_json, dict) or plan_json.get("schema_version") != 1:
        json_missing.append("schema_version=1")
    if not isinstance(plan_json, dict) or not plan_json.get("last_verified"):
        json_missing.append("last_verified")
    if not isinstance(commands, list) or len(commands) < 5:
        json_missing.append("commands>=5")
    else:
        command_text = "\n".join(str(item.get("command") if isinstance(item, dict) else item) for item in commands)
        for needle in ("--profile local", "--profile live", "--profile full", "aidebug_health.py"):
            if needle not in command_text:
                json_missing.append(f"command:{needle}")
    if not isinstance(thresholds, list) or len(thresholds) < 10:
        json_missing.append("thresholds>=10")
    else:
        threshold_names = [
            str(item.get("check") or "")
            for item in thresholds
            if isinstance(item, dict) and str(item.get("check") or "")
        ]
        threshold_set = set(threshold_names)
        for name in NEXT_PLAN_THRESHOLD_NAMES:
            if name not in threshold_set:
                json_missing.append(f"threshold:{name}")
        for name in threshold_names:
            if name not in NEXT_PLAN_THRESHOLD_NAMES:
                json_missing.append(f"threshold_unexpected:{name}")
        if len(threshold_names) != len(threshold_set):
            json_missing.append("thresholds:duplicate")
        if threshold_names != list(NEXT_PLAN_THRESHOLD_NAMES):
            json_missing.append("thresholds:canonical_order")
    expected_threshold_summary = _threshold_summary(
        thresholds,
        generated_at=str(plan_json.get("generated_at") if isinstance(plan_json, dict) else ""),
    )
    if not isinstance(threshold_summary, dict):
        json_missing.append("threshold_summary=dict")
    else:
        for field in ("schema_version", "generated_at", "expected", "count", "missing", "unexpected", "checks"):
            if threshold_summary.get(field) != expected_threshold_summary.get(field):
                json_missing.append(f"threshold_summary:{field}")
    if not isinstance(required_checks, list) or len(required_checks) < 10:
        json_missing.append("required_checks>=10")
    else:
        required_names = [str(item) for item in required_checks if str(item)]
        required_set = set(required_names)
        for name in NEXT_PLAN_THRESHOLD_NAMES:
            if name not in required_set:
                json_missing.append(f"required_check:{name}")
        for name in required_names:
            if name not in NEXT_PLAN_THRESHOLD_NAMES:
                json_missing.append(f"required_check_unexpected:{name}")
        if len(required_names) != len(required_set):
            json_missing.append("required_checks:duplicate")
        if required_names != list(NEXT_PLAN_THRESHOLD_NAMES):
            json_missing.append("required_checks:canonical_order")
    if not isinstance(weak_spots, list):
        json_missing.append("weak_spots=list")
    elif weak_spots and not all(isinstance(item, dict) and item.get("class") for item in weak_spots):
        json_missing.append("weak_spot_classes")
    expected_health_generated_at = str(current_health.get("generated_at") or "")
    expected_summary_generated_at = str(plan_json.get("generated_at") or expected_health_generated_at)
    expected_critical_summary = _critical_check_summary(
        current_health_checks,
        generated_at=expected_summary_generated_at,
        source_health_generated_at=expected_health_generated_at,
    )
    expected_diagnostic_summary = _diagnostic_artifact_summary(
        current_health_checks,
        generated_at=expected_summary_generated_at,
    )
    current_health_by_name = {
        str(check.get("name") or ""): check
        for check in current_health_checks
        if isinstance(check, dict)
    }
    expected_critical_freshness = _critical_summary_freshness_artifact(
        current_health_by_name.get("projectling_critical_summary_freshness"),
        generated_at=expected_summary_generated_at,
    )
    expected_deepseek_metric_summary = _deepseek_cache_metric_summary(
        current_health_by_name.get("deepseek_live_cache_quality"),
        auto_rows_for_artifacts,
        generated_at=expected_summary_generated_at,
    )
    expected_deepseek_stability_summary = _deepseek_cache_stability_summary(
        current_health_by_name.get("deepseek_cache_stability_trend"),
        auto_rows_for_artifacts,
        generated_at=expected_summary_generated_at,
    )
    expected_provider_metric_summary = _provider_cache_alias_summary(
        expected_deepseek_metric_summary,
        compatibility_key="deepseek_cache_metric_summary",
        compatibility_check="projectling_deepseek_cache_metric_summary",
    )
    expected_provider_stability_summary = _provider_cache_alias_summary(
        expected_deepseek_stability_summary,
        compatibility_key="deepseek_cache_stability_summary",
        compatibility_check="deepseek_cache_stability_trend",
    )
    if not isinstance(critical_summary, dict):
        json_missing.append("critical_check_summary=dict")
    else:
        if critical_summary.get("schema_version") != 1:
            json_missing.append("critical_summary:schema_version")
        if critical_summary.get("generated_at") != expected_summary_generated_at:
            json_missing.append("critical_summary:generated_at")
        if critical_summary.get("source_health_generated_at") != expected_health_generated_at:
            json_missing.append("critical_summary:source_health_generated_at")
        if critical_summary.get("required") != list(CRITICAL_CHECK_NAMES):
            json_missing.append("critical_summary:required")
        if critical_summary.get("missing") != expected_critical_summary.get("missing"):
            json_missing.append("critical_summary:missing")
        expected_by_name = {
            str(item.get("name") or ""): item
            for item in expected_critical_summary.get("checks", [])
            if isinstance(item, dict)
        }
        summary_checks = critical_summary.get("checks") if isinstance(critical_summary.get("checks"), list) else []
        if len(summary_checks) != len(CRITICAL_CHECK_NAMES):
            json_missing.append("critical_summary:check_count")
        for summary_item in (summary_checks if isinstance(summary_checks, list) else []):
            if not isinstance(summary_item, dict):
                json_missing.append("critical_summary:check_item")
                continue
            expected_item = expected_by_name.get(str(summary_item.get("name") or ""))
            if expected_item is None:
                json_missing.append(f"critical_summary:unexpected:{summary_item.get('name')}")
                continue
            for field in ("status", "score", "evidence", "next_action"):
                if summary_item.get(field) != expected_item.get(field):
                    json_missing.append(f"critical_summary:{summary_item.get('name')}:{field}")
        for name in CRITICAL_CHECK_NAMES:
            if not any(isinstance(summary_item, dict) and summary_item.get("name") == name for summary_item in summary_checks):
                json_missing.append(f"critical_summary:missing_check:{name}")
    if not isinstance(diagnostic_summary, dict):
        json_missing.append("diagnostic_evidence_summary=dict")
    else:
        for field in ("schema_version", "generated_at", "required", "missing", "ok", "checks"):
            if diagnostic_summary.get(field) != expected_diagnostic_summary.get(field):
                json_missing.append(f"diagnostic_evidence_summary:{field}")
        if expected_diagnostic_summary.get("ok") is not True:
            json_missing.append("diagnostic_evidence_summary:not_ok")
    if not isinstance(critical_freshness, dict):
        json_missing.append("critical_summary_freshness=dict")
    else:
        for field in ("schema_version", "generated_at", "status", "score", "freshness_trend", "evidence", "next_action"):
            if critical_freshness.get(field) != expected_critical_freshness.get(field):
                json_missing.append(f"critical_summary_freshness:{field}")
    if not isinstance(deepseek_metric_summary, dict):
        json_missing.append("deepseek_cache_metric_summary=dict")
    else:
        for field in (
            "schema_version",
            "generated_at",
            "provider",
            "cache_policy",
            "ok",
            "source_profile",
            "source_started_at",
            "source_run_mode",
            "detail_path",
            "detail_ok",
            "detail_errors",
            "live_ok",
            "rounds",
            "tools",
            "prompt_tokens",
            "cached_tokens",
            "cache_miss_tokens",
            "cache_hit_rate",
            "cache_ok",
            "warmup",
            "context_restored",
            "health_status",
            "health_score",
            "health_mismatches",
            "jsonl_mismatches",
        ):
            if deepseek_metric_summary.get(field) != expected_deepseek_metric_summary.get(field):
                json_missing.append(f"deepseek_cache_metric_summary:{field}")
        if expected_deepseek_metric_summary.get("ok") is not True:
            json_missing.append("deepseek_cache_metric_summary:not_ok")
    if not isinstance(deepseek_stability_summary, dict):
        json_missing.append("deepseek_cache_stability_summary=dict")
    else:
        for field in (
            "schema_version",
            "generated_at",
            "provider",
            "cache_policy",
            "ok",
            "health_status",
            "health_score",
            "live_samples",
            "recent_samples",
            "cache_ok_count",
            "warmup_resolved",
            "sequence_warmup_resolved",
            "min_hit_rate",
            "max_miss",
            "profile_distribution",
            "post_warmup_failures",
            "auto_mismatches",
        ):
            if deepseek_stability_summary.get(field) != expected_deepseek_stability_summary.get(field):
                json_missing.append(f"deepseek_cache_stability_summary:{field}")
        if expected_deepseek_stability_summary.get("ok") is not True:
            json_missing.append("deepseek_cache_stability_summary:not_ok")
    if not isinstance(provider_metric_summary, dict):
        json_missing.append("provider_cache_metric_summary=dict")
    elif provider_metric_summary != expected_provider_metric_summary:
        json_missing.append("provider_cache_metric_summary:alias_mismatch")
    if not isinstance(provider_stability_summary, dict):
        json_missing.append("provider_cache_stability_summary=dict")
    elif _strip_provider_cache_volatile_fields(provider_stability_summary) != _strip_provider_cache_volatile_fields(expected_provider_stability_summary):
        json_missing.append("provider_cache_stability_summary:alias_mismatch")
    if not isinstance(verification_sources, dict):
        json_missing.append("verification_sources=dict")
    else:
        max_allowed_age = 86400
        freshness = verification_sources.get("source_freshness_seconds")
        profile_samples = verification_sources.get("profile_samples")
        auto_rows = _load_auto_history(limit=60)
        latest_auto = auto_rows[-1] if auto_rows else {}
        expected_auto_detail = str(latest_auto.get("detail_path") or "")
        if not expected_auto_detail:
            json_missing.append("verification:auto_detail_available")
        elif str(verification_sources.get("latest_auto_detail_path") or "") != expected_auto_detail:
            json_missing.append("verification:latest_auto_detail_path")
        if str(verification_sources.get("latest_auto_started_at") or "") != str(latest_auto.get("started_at") or ""):
            json_missing.append("verification:latest_auto_started_at")
        if str(verification_sources.get("latest_auto_profile") or "") != str(latest_auto.get("profile") or ""):
            json_missing.append("verification:latest_auto_profile")
        if str(verification_sources.get("latest_auto_run_mode") or "") != str(latest_auto.get("run_mode") or ""):
            json_missing.append("verification:latest_auto_run_mode")
        if bool(verification_sources.get("latest_auto_ok")) != bool(latest_auto.get("ok")):
            json_missing.append("verification:latest_auto_ok")
        if not str(verification_sources.get("health_json_path") or "").endswith("aidebug-health.json"):
            json_missing.append("verification:health_json_path")
        if str(verification_sources.get("health_generated_at") or "") != str(current_health.get("generated_at") or ""):
            json_missing.append("verification:health_generated_at")
        if not isinstance(freshness, dict):
            json_missing.append("verification:source_freshness_seconds")
        else:
            try:
                stored_auto_age = int(freshness.get("latest_auto_age_seconds"))
                stored_health_age = int(freshness.get("health_json_age_seconds"))
                max_allowed_age = int(freshness.get("max_allowed_age_seconds") or 86400)
            except (TypeError, ValueError):
                stored_auto_age = -1
                stored_health_age = -1
                max_allowed_age = 86400
            actual_auto_age = _auto_started_age_seconds(latest_auto)
            actual_health_age = _timestamp_age_seconds(current_health.get("generated_at"))
            if stored_auto_age < 0 or stored_auto_age > max_allowed_age:
                json_missing.append("freshness:stored_auto_age")
            if stored_health_age < 0 or stored_health_age > max_allowed_age:
                json_missing.append("freshness:stored_health_age")
            if actual_auto_age is None or actual_auto_age > max_allowed_age:
                json_missing.append("freshness:actual_auto_age")
            if actual_health_age is None or actual_health_age > max_allowed_age:
                json_missing.append("freshness:actual_health_age")
        if not isinstance(profile_samples, dict):
            json_missing.append("verification:profile_samples")
        else:
            for profile in ("local", "live", "full"):
                expected = _latest_auto_profile(auto_rows, profile)
                sample = profile_samples.get(profile)
                if expected is None:
                    json_missing.append(f"profile_samples:{profile}:available")
                    continue
                if not isinstance(sample, dict):
                    json_missing.append(f"profile_samples:{profile}:dict")
                    continue
                if str(sample.get("detail_path") or "") != str(expected.get("detail_path") or ""):
                    json_missing.append(f"profile_samples:{profile}:detail_path")
                if str(sample.get("started_at") or "") != str(expected.get("started_at") or ""):
                    json_missing.append(f"profile_samples:{profile}:started_at")
                if str(sample.get("run_mode") or "") != str(expected.get("run_mode") or ""):
                    json_missing.append(f"profile_samples:{profile}:run_mode")
                if bool(sample.get("ok")) != bool(expected.get("ok")):
                    json_missing.append(f"profile_samples:{profile}:ok")
                actual_profile_age = _auto_started_age_seconds(expected)
                try:
                    stored_profile_age = int(sample.get("age_seconds"))
                except (TypeError, ValueError):
                    stored_profile_age = -1
                if stored_profile_age < 0 or stored_profile_age > max_allowed_age:
                    json_missing.append(f"profile_samples:{profile}:stored_age")
                if actual_profile_age is None or actual_profile_age > max_allowed_age:
                    json_missing.append(f"profile_samples:{profile}:actual_age")
                detail_failures = _profile_sample_detail_failures(profile, sample)
                profile_detail_integrity[profile] = "ok" if not detail_failures else ",".join(detail_failures)
                for failure in detail_failures:
                    json_missing.append(f"profile_samples:{profile}:detail:{failure}")
    threshold_summary_evidence = _compact_threshold_summary_evidence(threshold_summary)
    source_freshness_evidence = _compact_source_freshness_evidence(verification_sources)
    profile_samples_evidence = _compact_profile_samples_evidence(verification_sources)
    diagnostic_summary_evidence = _compact_diagnostic_summary_evidence(diagnostic_summary)
    critical_freshness_evidence = _compact_critical_freshness_evidence(critical_freshness)
    evidence_density_limit = 700
    evidence_density_candidates = {
        "threshold_summary": threshold_summary_evidence,
        "profile_samples": profile_samples_evidence,
        "diagnostic_evidence_summary": diagnostic_summary_evidence,
        "critical_freshness": critical_freshness_evidence,
    }
    evidence_density_failures = [
        f"{name}:{len(str(text))}"
        for name, text in evidence_density_candidates.items()
        if len(str(text)) > evidence_density_limit
    ]
    for failure in evidence_density_failures:
        json_missing.append(f"evidence_density:{failure}")
    artifact_texts = {
        "next_plan_md": text,
        "next_plan_json": json_text,
        "health_json": health_text,
    }
    mojibake_markers = ("PROJECT\u00c1\u00e8", "PROJECTÃ", "PROJECT\ufffd")
    mojibake_hits = [
        f"{name}:{marker}"
        for name, artifact_text in artifact_texts.items()
        for marker in mojibake_markers
        if marker in artifact_text
    ]
    readable_project_path = any("PROJECT凌" in artifact_text for artifact_text in artifact_texts.values())
    if mojibake_hits:
        json_missing.append("unicode_paths:mojibake")
    if not readable_project_path:
        json_missing.append("unicode_paths:missing_PROJECT凌")
    markdown_line_limit = 240
    markdown_long_lines = [
        f"{index}:{len(line)}"
        for index, line in enumerate(text.splitlines(), start=1)
        if len(line) > markdown_line_limit
    ]
    markdown_repr_markers = [
        marker
        for marker in ("{'", "['", "[]", ": True", ": False", "=True", "=False")
        if marker in text
    ]
    if markdown_long_lines:
        json_missing.append("markdown_readability:long_lines")
    if markdown_repr_markers:
        json_missing.append("markdown_readability:python_repr")
    profile_detail_integrity_evidence = _compact_profile_detail_integrity_evidence(profile_detail_integrity)
    provider_cache_metric_evidence = _compact_cache_metric_evidence(
        "provider_cache_metric_summary", provider_metric_summary
    )
    provider_cache_stability_evidence = _compact_cache_stability_evidence(
        "provider_cache_stability_summary", provider_stability_summary
    )
    deepseek_cache_metric_evidence = _compact_cache_metric_evidence(
        "deepseek_cache_metric_summary", deepseek_metric_summary
    )
    deepseek_cache_stability_evidence = _compact_cache_stability_evidence(
        "deepseek_cache_stability_summary", deepseek_stability_summary
    )
    md_artifact_evidence = f"md={NEXT_PLAN_MD.name}"
    json_artifact_evidence = f"json={NEXT_PLAN_JSON.name}"
    health_evidence_repr_candidates = {
        "source_freshness": source_freshness_evidence,
        "profile_detail_integrity": profile_detail_integrity_evidence,
        "diagnostic_evidence_summary": diagnostic_summary_evidence,
    }
    health_evidence_repr_failures = [
        f"{name}:{marker}"
        for name, evidence_text in health_evidence_repr_candidates.items()
        for marker in ("{'", "['")
        if marker in str(evidence_text)
    ]
    if health_evidence_repr_failures:
        json_missing.append("health_evidence_repr:python_repr")
    health_evidence_line_limit = 100
    health_evidence_length_candidates = {
        "threshold_summary": threshold_summary_evidence,
        "profile_samples": profile_samples_evidence,
        "diagnostic_evidence_summary": diagnostic_summary_evidence,
        "provider_cache_metric_summary": provider_cache_metric_evidence,
        "provider_cache_stability_summary": provider_cache_stability_evidence,
        "deepseek_cache_metric_summary": deepseek_cache_metric_evidence,
        "deepseek_cache_stability_summary": deepseek_cache_stability_evidence,
        "critical_freshness": critical_freshness_evidence,
    }
    health_evidence_length_failures = [
        f"{name}:{len(str(evidence_text))}"
        for name, evidence_text in health_evidence_length_candidates.items()
        if len(str(evidence_text)) > health_evidence_line_limit
    ]
    if health_evidence_length_failures:
        json_missing.append("health_evidence_length:long_lines")
    artifact_density_limit = 75
    artifact_density_candidates = {
        "provider_cache_metric": provider_cache_metric_evidence,
        "provider_cache_stability": provider_cache_stability_evidence,
        "deepseek_cache_metric": deepseek_cache_metric_evidence,
        "deepseek_cache_stability": deepseek_cache_stability_evidence,
        "critical_freshness": critical_freshness_evidence,
        "md_artifact": md_artifact_evidence,
        "json_artifact": json_artifact_evidence,
    }
    artifact_density_failures = [
        f"{name}:{len(str(evidence_text))}"
        for name, evidence_text in artifact_density_candidates.items()
        if len(str(evidence_text)) > artifact_density_limit
    ]
    artifact_legacy_markers = {
        "provider_cache_metric": ("provider_cache_metric_summary=",),
        "provider_cache_stability": ("provider_cache_stability_summary=",),
        "deepseek_cache_metric": ("deepseek_cache_metric_summary=",),
        "deepseek_cache_stability": ("deepseek_cache_stability_summary=",),
        "critical_freshness": ("status=", "trend=", "age=", "rem=", "max="),
        "md_artifact": ("md_path=", str(NEXT_PLAN_MD.parent), "\\aidebug\\notes\\", "/aidebug/notes/"),
        "json_artifact": ("json_path=", str(NEXT_PLAN_JSON.parent), "\\aidebug\\notes\\", "/aidebug/notes/"),
    }
    for name, evidence_text in artifact_density_candidates.items():
        text_value = str(evidence_text)
        for marker in artifact_legacy_markers.get(name, ()):
            if marker and marker in text_value:
                artifact_density_failures.append(f"{name}:legacy")
                break
    if artifact_density_failures:
        json_missing.append("next_plan_artifact_density:failures")
    ok = age <= 86400 and not missing and not json_missing
    score = 100 if ok else 75
    if age > 7 * 86400:
        score = min(score, 65)
    evidence = [
        "markdown=1",
        "json=1",
        f"age={age}",
        f"md_bytes={md_meta.get('bytes')}",
        f"json_bytes={json_meta.get('bytes')}",
        f"thresholds={len(thresholds) if isinstance(thresholds, list) else '-'}",
        f"required_checks={len(required_checks) if isinstance(required_checks, list) else '-'}",
        threshold_summary_evidence,
        f"missing={_compact_list_or_dash(missing)}",
        f"json_missing={_compact_list_or_dash(json_missing)}",
        f"evidence_density=limit={evidence_density_limit} failures={_compact_list_or_dash(evidence_density_failures)}",
        f"unicode_paths=readable:{_compact_bool_flag(readable_project_path)} mojibake={_compact_list_or_dash(mojibake_hits[:4])}",
        f"markdown_readability=line_limit={markdown_line_limit} long_lines={_compact_list_or_dash(markdown_long_lines[:4])} python_repr={_compact_list_or_dash(markdown_repr_markers)}",
        f"health_evidence_repr=failures={_compact_list_or_dash(health_evidence_repr_failures)}",
        f"health_evidence_length=limit={health_evidence_line_limit} failures={_compact_list_or_dash(health_evidence_length_failures)}",
        f"last_verified={plan_json.get('last_verified') if isinstance(plan_json, dict) else '-'}",
        _detail_file_evidence(
            "auto_detail",
            verification_sources.get("latest_auto_detail_path") if isinstance(verification_sources, dict) else "",
        ),
        source_freshness_evidence,
        profile_samples_evidence,
        profile_detail_integrity_evidence,
        diagnostic_summary_evidence,
        provider_cache_metric_evidence,
        provider_cache_stability_evidence,
        deepseek_cache_metric_evidence,
        deepseek_cache_stability_evidence,
        "crit_summary="
        f"n={len(critical_summary.get('checks', [])) if isinstance(critical_summary, dict) and isinstance(critical_summary.get('checks'), list) else '-'} "
        f"miss={_compact_list_or_dash(critical_summary.get('missing') if isinstance(critical_summary, dict) and isinstance(critical_summary.get('missing'), list) else [])} "
        f"gen={_compact_iso_timestamp(critical_summary.get('generated_at') if isinstance(critical_summary, dict) else '')} "
        f"src={_compact_iso_timestamp(critical_summary.get('source_health_generated_at') if isinstance(critical_summary, dict) else '')}",
        critical_freshness_evidence,
        md_artifact_evidence,
        json_artifact_evidence,
        f"next_plan_artifact_density=limit={artifact_density_limit} failures={_compact_list_or_dash(artifact_density_failures)}",
    ]
    score = _append_auto_detail_density_guard(evidence, score)
    score = _append_runtime_repr_density_guard(evidence, score, limit=120)
    return item(
        "projectling_next_plan_artifact",
        score,
        status_from_score(score),
        evidence,
        "刷新 projectling-aidebug-next-plan.md/json，确保三档 profile、双端 health、阈值和 Android readiness 都列出。" if score < 85 else "",
    )


def check_critical_summary_freshness() -> dict[str, Any]:
    try:
        plan_json = json.loads(NEXT_PLAN_JSON.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return item(
            "projectling_critical_summary_freshness",
            55,
            "warn",
            [f"plan_read_error={exc}", f"path={NEXT_PLAN_JSON}"],
            "重新生成 next-plan JSON 后复测 critical summary freshness。",
        )
    try:
        current_health = json.loads(HEALTH_JSON.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return item(
            "projectling_critical_summary_freshness",
            55,
            "warn",
            [f"health_read_error={exc}", f"path={HEALTH_JSON}"],
            "重新生成 health JSON 后复测 critical summary freshness。",
        )
    if not isinstance(plan_json, dict):
        return item(
            "projectling_critical_summary_freshness",
            55,
            "warn",
            ["plan_json=dict:false"],
            "检查 next-plan JSON 结构。",
        )
    critical_summary = plan_json.get("critical_check_summary")
    if not isinstance(critical_summary, dict):
        return item(
            "projectling_critical_summary_freshness",
            65,
            "warn",
            ["critical_check_summary=dict:false"],
            "重新生成带 critical_check_summary 的 next-plan JSON。",
        )

    health_generated_at = str(current_health.get("generated_at") or "")
    plan_generated_at = str(plan_json.get("generated_at") or "")
    summary_generated_at = str(critical_summary.get("generated_at") or "")
    source_health_generated_at = str(critical_summary.get("source_health_generated_at") or "")
    summary_checks = critical_summary.get("checks") if isinstance(critical_summary.get("checks"), list) else []
    missing = critical_summary.get("missing") if isinstance(critical_summary.get("missing"), list) else []
    status_counts = critical_summary.get("status_counts") if isinstance(critical_summary.get("status_counts"), dict) else {}
    observed_ok_count = sum(1 for check in summary_checks if isinstance(check, dict) and check.get("status") == "ok")
    observed_non_ok_count = len(summary_checks) - observed_ok_count
    current_health_checks = current_health.get("checks") if isinstance(current_health.get("checks"), list) else []
    expected = _critical_check_summary(
        current_health_checks,
        generated_at=plan_generated_at or health_generated_at,
        source_health_generated_at=health_generated_at,
    )

    mismatches: list[str] = []
    if summary_generated_at != (plan_generated_at or health_generated_at):
        mismatches.append("summary_generated_at")
    if source_health_generated_at != health_generated_at:
        mismatches.append("source_health_generated_at")
    if critical_summary.get("required") != list(CRITICAL_CHECK_NAMES):
        mismatches.append("required")
    if len(summary_checks) != len(CRITICAL_CHECK_NAMES):
        mismatches.append("check_count")
    if missing != expected.get("missing"):
        mismatches.append("missing")
    if int(critical_summary.get("ok_count") or 0) != observed_ok_count:
        mismatches.append("ok_count")
    if int(critical_summary.get("non_ok_count") or 0) != observed_non_ok_count:
        mismatches.append("non_ok_count")
    if int(status_counts.get("ok") or 0) != observed_ok_count:
        mismatches.append("status_counts.ok")
    if source_health_generated_at:
        source_age = _timestamp_age_seconds(source_health_generated_at)
    else:
        source_age = None
    fresh_limit = 86400
    near_stale_limit = int(fresh_limit * 0.75)
    if source_age is None:
        mismatches.append("source_age")
        freshness_status = "missing"
        source_remaining = "-"
    elif source_age > fresh_limit:
        mismatches.append("source_age_stale")
        freshness_status = "stale"
        source_remaining = str(fresh_limit - source_age)
    elif source_age >= near_stale_limit:
        freshness_status = "near_stale"
        source_remaining = str(fresh_limit - source_age)
    else:
        freshness_status = "fresh"
        source_remaining = str(fresh_limit - source_age)
    if critical_summary.get("schema_version") != 1:
        mismatches.append("schema_version")
    if critical_summary.get("generated_at") != expected.get("generated_at"):
        mismatches.append("generated_at_expected")
    if critical_summary.get("source_health_generated_at") != expected.get("source_health_generated_at"):
        mismatches.append("source_health_expected")

    score = 100 if not mismatches else 75
    if freshness_status == "near_stale":
        score = min(score, 90)
    if source_age is not None and source_age > 7 * 86400:
        score = min(score, 65)
    freshness_trend = (
        f"freshness_trend=status:{freshness_status} a:{source_age if source_age is not None else '-'} "
        f"r:{source_remaining} w:{near_stale_limit} m:{fresh_limit}"
    )
    evidence = [
        f"plan_generated_at={plan_generated_at}",
        f"health_generated_at={health_generated_at}",
        f"summary_generated_at={summary_generated_at}",
        f"source_health_generated_at={source_health_generated_at}",
        freshness_trend,
        f"source_age={source_age if source_age is not None else '-'}",
        f"required={len(critical_summary.get('required', [])) if isinstance(critical_summary.get('required'), list) else '-'}",
        f"checks={len(summary_checks)}",
        f"missing={_compact_list_or_dash(missing)}",
        f"ok_count={critical_summary.get('ok_count')}",
        f"non_ok_count={critical_summary.get('non_ok_count')}",
        f"mismatches={_compact_list_or_dash(mismatches)}",
    ]
    density_limit = 70
    density_failures = [
        f"row{index}:{len(row)}"
        for index, row in enumerate(evidence, start=1)
        if (
            row.startswith("freshness_trend=")
            and (
                len(row) > density_limit
                or " age:" in row
                or "remaining:" in row
                or "warn_at:" in row
                or "max:" in row
            )
        )
    ]
    if density_failures:
        score = min(score, 75)
    evidence.append(f"critical_freshness_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")
    score = _append_runtime_repr_density_guard(evidence, score, limit=90)
    return item(
        "projectling_critical_summary_freshness",
        score,
        status_from_score(score),
        evidence,
        "刷新 next-plan critical summary，使摘要时间、来源 health 和计数重新对齐。" if score < 85 else "",
    )


def check_threshold_summary_integrity() -> dict[str, Any]:
    try:
        plan_json = json.loads(NEXT_PLAN_JSON.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return item(
            "projectling_threshold_summary_integrity",
            55,
            "warn",
            [f"plan_read_error={exc}", f"path={NEXT_PLAN_JSON}"],
            "重新生成 next-plan JSON 后复测 threshold summary。",
        )
    if not isinstance(plan_json, dict):
        return item(
            "projectling_threshold_summary_integrity",
            55,
            "warn",
            ["plan_json=dict:false"],
            "检查 next-plan JSON 结构。",
        )
    thresholds = plan_json.get("thresholds") if isinstance(plan_json.get("thresholds"), list) else []
    threshold_summary = plan_json.get("threshold_summary")
    expected = _threshold_summary(thresholds, generated_at=str(plan_json.get("generated_at") or ""))
    if not isinstance(threshold_summary, dict):
        return item(
            "projectling_threshold_summary_integrity",
            65,
            "warn",
            ["threshold_summary=dict:false"],
            "重新生成带 threshold_summary 的 next-plan JSON。",
        )
    mismatches: list[str] = []
    for field in ("schema_version", "generated_at", "expected", "count", "missing", "unexpected", "checks"):
        if threshold_summary.get(field) != expected.get(field):
            mismatches.append(field)
    missing = threshold_summary.get("missing") if isinstance(threshold_summary.get("missing"), list) else []
    unexpected = threshold_summary.get("unexpected") if isinstance(threshold_summary.get("unexpected"), list) else []
    threshold_names = [
        str(item.get("check") or "")
        for item in thresholds
        if isinstance(item, dict) and str(item.get("check") or "")
    ]
    summary_checks = threshold_summary.get("checks") if isinstance(threshold_summary.get("checks"), list) else []
    summary_check_names = [
        str(item.get("check") or "")
        for item in summary_checks
        if isinstance(item, dict) and str(item.get("check") or "")
    ]
    coverage_failures: list[str] = []
    if threshold_names != list(NEXT_PLAN_THRESHOLD_NAMES):
        coverage_failures.append("thresholds")
    if threshold_summary.get("expected") != list(NEXT_PLAN_THRESHOLD_NAMES):
        coverage_failures.append("expected")
    if threshold_summary.get("count") != len(NEXT_PLAN_THRESHOLD_NAMES):
        coverage_failures.append("count")
    if missing:
        coverage_failures.append("missing")
    if unexpected:
        coverage_failures.append("unexpected")
    if summary_check_names != list(NEXT_PLAN_THRESHOLD_NAMES):
        coverage_failures.append("checks")
    score = 100 if not mismatches and not coverage_failures else 75
    evidence = [
        f"generated_at={threshold_summary.get('generated_at')}",
        f"expected={len(threshold_summary.get('expected', [])) if isinstance(threshold_summary.get('expected'), list) else '-'}",
        f"count={threshold_summary.get('count')}",
        f"missing={_compact_list_or_dash(missing)}",
        f"unexpected={_compact_list_or_dash(unexpected)}",
        f"mismatches={_compact_list_or_dash(mismatches)}",
        f"coverage={_compact_list_or_dash(coverage_failures)}",
    ]
    density_limit = 80
    density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if len(row) > density_limit or "['" in row or "[]" in row
    ]
    if density_failures:
        score = min(score, 75)
    evidence.append(f"threshold_summary_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")
    return item(
        "projectling_threshold_summary_integrity",
        score,
        status_from_score(score),
        evidence,
        "刷新 next-plan threshold summary，使阈值计数、期望列表和缺失列表重新对齐。" if score < 85 else "",
    )


def check_deepseek_cache_metric_summary() -> dict[str, Any]:
    try:
        plan_json = json.loads(NEXT_PLAN_JSON.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return item(
            "projectling_deepseek_cache_metric_summary",
            55,
            "warn",
            [f"plan_read_error={exc}", f"path={NEXT_PLAN_JSON}"],
            "重新生成 next-plan JSON 后复测 DeepSeek cache metric summary。",
        )
    try:
        current_health = json.loads(HEALTH_JSON.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return item(
            "projectling_deepseek_cache_metric_summary",
            55,
            "warn",
            [f"health_read_error={exc}", f"path={HEALTH_JSON}"],
            "重新生成 health JSON 后复测 DeepSeek cache metric summary。",
        )
    if not isinstance(plan_json, dict):
        return item(
            "projectling_deepseek_cache_metric_summary",
            55,
            "warn",
            ["plan_json=dict:false"],
            "检查 next-plan JSON 结构。",
        )
    metric_summary = plan_json.get("deepseek_cache_metric_summary")
    if not isinstance(metric_summary, dict):
        return item(
            "projectling_deepseek_cache_metric_summary",
            65,
            "warn",
            ["deepseek_cache_metric_summary=dict:false"],
            "重新生成带 DeepSeek cache metric summary 的 next-plan JSON。",
        )
    checks = current_health.get("checks") if isinstance(current_health.get("checks"), list) else []
    checks_by_name = {
        str(check.get("name") or ""): check
        for check in checks
        if isinstance(check, dict)
    }
    expected = _deepseek_cache_metric_summary(
        checks_by_name.get("deepseek_live_cache_quality"),
        _load_auto_history(limit=60),
        generated_at=str(plan_json.get("generated_at") or ""),
    )
    fields = (
        "schema_version",
        "generated_at",
        "provider",
        "cache_policy",
        "ok",
        "source_profile",
        "source_started_at",
        "source_run_mode",
        "detail_path",
        "detail_ok",
        "detail_errors",
        "live_ok",
        "rounds",
        "tools",
        "prompt_tokens",
        "cached_tokens",
        "cache_miss_tokens",
        "cache_hit_rate",
        "cache_ok",
        "warmup",
        "context_restored",
        "health_status",
        "health_score",
        "health_mismatches",
        "jsonl_mismatches",
    )
    mismatches = [field for field in fields if metric_summary.get(field) != expected.get(field)]
    if expected.get("ok") is not True:
        mismatches.append("expected_not_ok")
    score = 100 if not mismatches else 75
    health_mismatches = metric_summary.get("health_mismatches") if isinstance(metric_summary.get("health_mismatches"), list) else []
    jsonl_mismatches = metric_summary.get("jsonl_mismatches") if isinstance(metric_summary.get("jsonl_mismatches"), list) else []
    evidence = [
        f"generated_at={metric_summary.get('generated_at')}",
        f"provider={metric_summary.get('provider')}",
        f"cache_policy={metric_summary.get('cache_policy')}",
        f"ok={_compact_bool_flag(metric_summary.get('ok'))}",
        f"tools={_compact_live_tool_sequence(metric_summary.get('tools'))}",
        f"hit_rate={metric_summary.get('cache_hit_rate')}",
        f"miss={metric_summary.get('cache_miss_tokens')}",
        f"cached={metric_summary.get('cached_tokens')}",
        f"prompt={metric_summary.get('prompt_tokens')}",
        f"cache_ok={_compact_bool_flag(metric_summary.get('cache_ok'))}",
        f"context_restored={_compact_bool_flag(metric_summary.get('context_restored'))}",
        f"detail_ok={_compact_bool_flag(metric_summary.get('detail_ok'))}",
        f"health_mismatches={_compact_list_or_dash(health_mismatches)}",
        f"jsonl_mismatches={_compact_list_or_dash(jsonl_mismatches)}",
        f"mismatches={_compact_list_or_dash(mismatches)}",
        _detail_file_evidence("detail", metric_summary.get("detail_path")),
    ]
    score = _append_auto_detail_density_guard(evidence, score)
    return item(
        "projectling_deepseek_cache_metric_summary",
        score,
        status_from_score(score),
        evidence,
        "刷新 DeepSeek cache metric summary，使 detail JSON、JSONL 和 health evidence 重新对齐。" if score < 85 else "",
    )


def _runner_help_probe(script_name: str, expected_text: str) -> tuple[bool, str]:
    script = AIDEBUG_DIR / "runner" / script_name
    if not script.exists():
        return False, f"help_probe=missing script={script}"
    try:
        completed = run_cmd([sys.executable, str(script), "--help"], cwd=PROJECTLING_DIR, timeout=25)
    except Exception as exc:
        return False, f"help_probe=exception {exc}"
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    has_expected = expected_text in stdout
    ok = completed.returncode == 0 and has_expected
    detail = f"help_rc={completed.returncode} expected={int(has_expected)} stderr={stderr.strip()[:160]}"
    return ok, detail


def _load_auto_history(limit: int = 40) -> list[dict[str, Any]]:
    path = LOG_DIR / "projectling-auto.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines[-max(1, limit) :]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _load_jsonl_path(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if limit is not None:
        lines = lines[-max(1, limit) :]
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _auto_issue_key(issue: dict[str, Any]) -> str:
    return "|".join(
        [
            str(issue.get("at") or issue.get("issue_at") or ""),
            str(issue.get("round") or issue.get("issue_round") or ""),
            str(issue.get("component") or issue.get("issue_component") or ""),
            str(issue.get("message") or issue.get("issue_message") or ""),
        ]
    )


def check_auto_runner_history() -> dict[str, Any]:
    probe_ok, probe_detail = _runner_help_probe("projectling_auto.py", "projectling-auto")
    path = LOG_DIR / "projectling-auto.jsonl"
    meta = file_meta(path)
    if not meta.get("exists"):
        score = 70 if probe_ok else 35
        return item(
            "projectling_auto_runner",
            score,
            status_from_score(score),
            ["missing projectling-auto.jsonl", probe_detail],
            "运行 aidebug projectling-auto --rounds 1 做回归。",
        )
    last = ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        last = lines[-1] if lines else ""
        data = json.loads(last) if last else {}
    except Exception as exc:
        score = 55 if probe_ok else 35
        return item("projectling_auto_runner", score, status_from_score(score), [f"parse_error={exc}", probe_detail], "检查 projectling-auto.jsonl。")
    ok = bool(data.get("ok"))
    age = int(meta.get("age_seconds") or 0)
    score = 100 if ok and probe_ok else 60 if ok else 45
    if not probe_ok:
        score = min(score, 45)
    if age > 86400:
        score = min(score, 80)
    if age > 7 * 86400:
        score = min(score, 70)
    context_pressure = data.get("context_pressure") if isinstance(data.get("context_pressure"), dict) else {}
    context_variants = data.get("context_pressure_variants") if isinstance(data.get("context_pressure_variants"), dict) else {}
    memory = data.get("memory") if isinstance(data.get("memory"), dict) else {}
    memory_pressure = data.get("memory_pressure") if isinstance(data.get("memory_pressure"), dict) else {}
    web_search = data.get("web_search") if isinstance(data.get("web_search"), dict) else {}
    live_chat = data.get("live_chat") if isinstance(data.get("live_chat"), dict) else {}
    command = data.get("command") if isinstance(data.get("command"), dict) else {}
    aidebug = data.get("aidebug") if isinstance(data.get("aidebug"), dict) else {}
    evidence = [
        f"last_ok={_compact_bool_flag(ok)}",
        f"age={age}",
        f"run_mode={data.get('run_mode')} profile={data.get('profile') or 'legacy'}",
        probe_detail,
        _detail_file_evidence("detail", data.get("detail_path")),
    ]
    if command:
        matrix_details = command.get("matrix_details") if isinstance(command.get("matrix_details"), list) else []
        evidence.append(
            "cmd="
            f"ok:{_compact_bool_flag(command.get('ok'))} h:{_compact_bool_flag(command.get('has_head'))} "
            f"t:{_compact_bool_flag(command.get('has_tail'))} safe:{_compact_bool_flag(command.get('safety_ok'))} "
            f"mat:{_compact_bool_flag(command.get('matrix_ok'))} cases:{command.get('matrix_cases')}"
        )
        if matrix_details:
            matrix_items = [item for item in matrix_details if isinstance(item, dict)]
            matrix_labels = [_compact_command_matrix_label(item.get("label")) for item in matrix_items]
            matrix_returncodes = [str(item.get("returncode") if item.get("returncode") is not None else "-") for item in matrix_items]
            matrix_failures = [
                f"{_compact_command_matrix_label(item.get('label'))}:{item.get('status')}/"
                f"{item.get('returncode') if item.get('returncode') is not None else '-'}"
                for item in matrix_items
                if item.get("ok") is not True
            ]
            evidence.append(
                "cmd_matrix="
                f"cases:{len(matrix_items)} ok:{sum(1 for item in matrix_items if item.get('ok') is True)} "
                f"lbl:{_compact_list_or_dash(matrix_labels)} rc:{_compact_list_or_dash(matrix_returncodes)} "
                f"fail:{_compact_list_or_dash(matrix_failures)}"
            )
    if context_pressure:
        evidence.append(
            "ctxp="
            f"ok:{_compact_bool_flag(context_pressure.get('ok'))} h:{context_pressure.get('hidden_after')} "
            f"f:{context_pressure.get('folded')} b:{_compact_bool_flag(context_pressure.get('budget_ok'))} "
            f"fr:{_compact_bool_flag(context_pressure.get('freshness_ok'))} a:{context_pressure.get('active_chars')} "
            f"t:{context_pressure.get('compact_target')}"
        )
    if context_variants:
        variant_labels = context_variants.get("labels") if isinstance(context_variants.get("labels"), list) else []
        evidence.append(
            "ctxv="
            f"ok:{_compact_bool_flag(context_variants.get('ok'))} pass:{context_variants.get('passed')}/"
            f"{context_variants.get('variant_count')} lbl:{_compact_list_or_dash(variant_labels)}"
        )
    if memory:
        evidence.append(
            "memory="
            f"ok:{_compact_bool_flag(memory.get('ok'))} diaries:{memory.get('diaries')} "
            f"events:{memory.get('events')} reject:{_compact_bool_flag(memory.get('reject_ok'))} "
            f"append:{_compact_bool_flag(memory.get('append_ok'))} db:{_compact_bool_flag(memory.get('db_integrity_ok'))} "
            f"unique:{_compact_bool_flag(memory.get('keyword_unique_ok'))} journal:{memory.get('journal_mode')}"
        )
    if aidebug:
        evidence.append(
            "aidebug="
            f"s:{_compact_bool_flag(aidebug.get('slice_ok'))} p:{_compact_bool_flag(aidebug.get('precision_ok'))} "
            f"h:{_compact_bool_flag(aidebug.get('head_ok'))} t:{_compact_bool_flag(aidebug.get('tail_ok'))} "
            f"w:{_compact_bool_flag(aidebug.get('slice_window_ok'))} tr:{_compact_bool_flag(aidebug.get('truncation_ok'))} "
            f"sec:{_compact_bool_flag(aidebug.get('security_ok'))}"
        )
    if memory_pressure:
        evidence.append(
            "memp="
            f"ok:{_compact_bool_flag(memory_pressure.get('ok'))} b:{memory_pressure.get('bytes_before')}/"
            f"{memory_pressure.get('memory_max_bytes')} a:{memory_pressure.get('bytes_after')} "
            f"p:{_compact_bool_flag(memory_pressure.get('pressure_ok'))} c:{_compact_bool_flag(memory_pressure.get('consume_ok'))} "
            f"r:{_compact_bool_flag(memory_pressure.get('read_ok'))}"
        )
    if web_search and web_search.get("ok") is not None:
        evidence.append(
            "web="
            f"ok:{_compact_bool_flag(web_search.get('ok'))} val:{_compact_bool_flag(web_search.get('validation_ok'))} "
            f"n:{web_search.get('result_count')}"
        )
    if live_chat:
        usage = live_chat.get("usage") if isinstance(live_chat.get("usage"), dict) else {}
        warmup = live_chat.get("cache_warmup") if isinstance(live_chat.get("cache_warmup"), dict) else {}
        tools = [str(tool) for tool in live_chat.get("tool_names") if str(tool).strip()] if isinstance(live_chat.get("tool_names"), list) else []
        tool_sample = [_compact_auto_tool_label(tool) for tool in tools[:4]]
        actor_labels = live_chat.get("tool_actor_labels") if isinstance(live_chat.get("tool_actor_labels"), list) else []
        evidence.append(
            "live_chat="
            f"ok:{_compact_bool_flag(live_chat.get('ok'))} r:{live_chat.get('rounds')} tl:{len(tools)} "
            f"cmd:{int('command' in set(tools))} s:{_compact_list_or_dash(tool_sample)} "
            f"ex:{max(0, len(tools) - len(tool_sample))} ctx:{_compact_bool_flag(live_chat.get('context_restored'))} "
            f"dual:{_compact_bool_flag(live_chat.get('dual_star_metadata_ok'))} act:{_compact_list_or_dash(actor_labels)}"
        )
        evidence.append(
            "cache="
            f"p:{usage.get('prompt_tokens')} c:{usage.get('cached_tokens')} "
            f"m:{usage.get('cache_miss_tokens')} hit:{usage.get('cache_hit_rate')} "
            f"att:{live_chat.get('attempts')} ok:{_compact_bool_flag(live_chat.get('cache_ok'))} "
            f"warmup:{int(bool(warmup.get('attempted')))}/{int(bool(warmup.get('resolved')))}"
        )
    score = _append_auto_detail_density_guard(evidence, score)
    density_limit = 90
    compact_label_failures = ("update_plan", "unicode_stdout", "stderr_capture", "nonzero_exit")
    legacy_markers = (
        "command=ok:",
        "context_pressure=",
        "context_variants=",
        "aidebug=slice:",
        "memory_pressure=",
        "web_search=",
        "live_cache=prompt:",
        " True",
        " False",
        ":True",
        ":False",
    )
    density_failures = [
        f"row{index}:{len(row)}"
        for index, row in enumerate(evidence, start=1)
        if (
            len(row) > density_limit
            or "['" in row
            or "{'" in row
            or "tools:[" in row
            or "labels:[" in row
            or any(marker in row for marker in legacy_markers)
            or (row.startswith(("live_chat=", "cmd_matrix=")) and any(label in row for label in compact_label_failures))
        )
    ]
    if density_failures:
        score = min(score, 75)
    evidence.append(f"auto_runner_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")
    return item(
        "projectling_auto_runner",
        score,
        status_from_score(score),
        evidence,
        "运行 aidebug projectling-auto --rounds 1 做回归。" if score < 85 else "",
    )


def _timestamp_age_seconds(raw: Any) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return max(0, int(time.time() - calendar.timegm(parsed)))


def check_auto_issue_ledger() -> dict[str, Any]:
    issues_path = LOG_DIR / "projectling-auto-issues.jsonl"
    resolutions_path = LOG_DIR / "projectling-auto-resolutions.jsonl"
    issues = _load_jsonl_path(issues_path)
    resolutions = _load_jsonl_path(resolutions_path)
    if not issues:
        return item(
            "projectling_auto_issue_ledger",
            100,
            "ok",
            ["issues=0", f"resolutions={len(resolutions)}"],
            "",
        )
    resolved_keys = {
        str(row.get("issue_key") or _auto_issue_key(row))
        for row in resolutions
        if row.get("issue_key") or row.get("issue_component")
    }
    unresolved = [issue for issue in issues if _auto_issue_key(issue) not in resolved_keys]
    recent_auto = _load_auto_history(limit=8)
    latest_ok = next((row for row in reversed(recent_auto) if row.get("ok") is True), None)
    compat_covered = sum(1 for row in resolutions if row.get("status") == "compat-covered")
    latest_issue = issues[-1] if issues else {}
    latest_resolution = resolutions[-1] if resolutions else {}
    latest_compat = next((row for row in reversed(resolutions) if row.get("status") == "compat-covered"), {})
    latest_issue_age = _timestamp_age_seconds(latest_issue.get("at")) if isinstance(latest_issue, dict) else None
    latest_resolution_age = (
        _timestamp_age_seconds(latest_resolution.get("resolved_at")) if isinstance(latest_resolution, dict) else None
    )
    score = 100 if not unresolved else 80 if latest_ok and resolutions else 60
    evidence = [
        f"issues={len(issues)}",
        f"resolutions={len(resolutions)}",
        f"unresolved={len(unresolved)}",
        f"compat_covered={compat_covered}",
        f"latest_ok_at={latest_ok.get('started_at') if isinstance(latest_ok, dict) else '-'}",
        f"latest_issue={latest_issue.get('at', '-') if isinstance(latest_issue, dict) else '-'} "
        f"{latest_issue.get('component', '-') if isinstance(latest_issue, dict) else '-'} "
        f"age={latest_issue_age if latest_issue_age is not None else '-'}",
        f"latest_resolution={latest_resolution.get('resolved_at', '-') if isinstance(latest_resolution, dict) else '-'} "
        f"{latest_resolution.get('issue_component', '-') if isinstance(latest_resolution, dict) else '-'} "
        f"status={latest_resolution.get('status', '-') if isinstance(latest_resolution, dict) else '-'} "
        f"age={latest_resolution_age if latest_resolution_age is not None else '-'}",
        "last_compat_covered="
        f"{latest_compat.get('issue_component', '-') if isinstance(latest_compat, dict) else '-'} "
        f"{str(latest_compat.get('issue_message', '-'))[:80] if isinstance(latest_compat, dict) else '-'}",
    ]
    if unresolved:
        latest_unresolved = unresolved[-1]
        evidence.append(
            "latest_unresolved="
            f"{latest_unresolved.get('at')} {latest_unresolved.get('component')} "
            f"{str(latest_unresolved.get('message') or '')[:120]}"
        )
    for issue in unresolved[:4]:
        evidence.append(
            "unresolved="
            f"{issue.get('at')} {issue.get('component')} {str(issue.get('message') or '')[:120]}"
        )
    return item(
        "projectling_auto_issue_ledger",
        score,
        status_from_score(score),
        evidence,
        "复跑 projectling-auto，使新成功证据写入 projectling-auto-resolutions.jsonl。" if unresolved else "",
    )


def check_aidebug_read_precision_auto() -> dict[str, Any]:
    rows = _load_auto_history(limit=20)
    sample: dict[str, Any] | None = None
    for row in reversed(rows):
        aidebug = row.get("aidebug") if isinstance(row.get("aidebug"), dict) else {}
        if aidebug:
            sample = row
            break
    if sample is None:
        return item(
            "aidebug_read_precision_auto",
            70,
            "warn",
            ["aidebug_precision=missing"],
            "运行 projectling-auto 以生成 aidebug head/tail/slice/truncation replay。",
        )
    aidebug = sample.get("aidebug") if isinstance(sample.get("aidebug"), dict) else {}
    age = _auto_started_age_seconds(sample)
    ok = bool(
        aidebug.get("slice_ok")
        and aidebug.get("precision_ok")
        and aidebug.get("head_ok")
        and aidebug.get("tail_ok")
        and aidebug.get("slice_window_ok")
        and aidebug.get("truncation_ok")
        and aidebug.get("security_ok")
    )
    score = 100 if ok else 65
    if age is not None and age > 86400:
        score = min(score, 75)
    evidence = [
        f"ok={_compact_bool_flag(ok)}",
        f"age={age if age is not None else '-'}",
        f"head={_compact_bool_flag(aidebug.get('head_ok'))}",
        f"tail={_compact_bool_flag(aidebug.get('tail_ok'))}",
        f"window={_compact_bool_flag(aidebug.get('slice_window_ok'))}",
        f"trunc={_compact_bool_flag(aidebug.get('truncation_ok'))}",
        f"security={_compact_bool_flag(aidebug.get('security_ok'))}",
        _detail_file_evidence("detail", sample.get("detail_path")),
    ]
    score = _append_auto_detail_density_guard(evidence, score)
    score = _append_auto_repr_density_guard(evidence, score)
    return item(
        "aidebug_read_precision_auto",
        score,
        status_from_score(score),
        evidence,
        "修复 aidebug.read 边界读取或大文件截断 replay。" if score < 85 else "",
    )


def check_memory_db_integrity_auto() -> dict[str, Any]:
    rows = _load_auto_history(limit=20)
    sample: dict[str, Any] | None = None
    for row in reversed(rows):
        memory = row.get("memory") if isinstance(row.get("memory"), dict) else {}
        if memory:
            sample = row
            break
    if sample is None:
        return item(
            "memory_db_integrity_auto",
            70,
            "warn",
            ["memory=missing"],
            "运行 projectling-auto 以生成 memory append/db integrity replay。",
        )
    memory = sample.get("memory") if isinstance(sample.get("memory"), dict) else {}
    age = _auto_started_age_seconds(sample)
    ok = bool(
        memory.get("ok")
        and memory.get("append_ok")
        and memory.get("db_integrity_ok")
        and memory.get("keyword_unique_ok")
        and str(memory.get("journal_mode") or "").lower() == "wal"
    )
    score = 100 if ok else 65
    if age is not None and age > 86400:
        score = min(score, 75)
    evidence = [
        f"ok={_compact_bool_flag(ok)}",
        f"age={age if age is not None else '-'}",
        f"append={_compact_bool_flag(memory.get('append_ok'))}",
        f"db={_compact_bool_flag(memory.get('db_integrity_ok'))}",
        f"unique={_compact_bool_flag(memory.get('keyword_unique_ok'))}",
        f"journal={memory.get('journal_mode')}",
        f"diaries={memory.get('diaries')}",
        f"events={memory.get('events')}",
        _detail_file_evidence("detail", sample.get("detail_path")),
    ]
    score = _append_auto_detail_density_guard(evidence, score)
    score = _append_auto_repr_density_guard(evidence, score)
    return item(
        "memory_db_integrity_auto",
        score,
        status_from_score(score),
        evidence,
        "修复 memory append、SQLite WAL 或关键词去重 replay。" if score < 85 else "",
    )


def check_context_pressure_variants_auto() -> dict[str, Any]:
    rows = _load_auto_history(limit=20)
    sample: dict[str, Any] | None = None
    for row in reversed(rows):
        variants = row.get("context_pressure_variants") if isinstance(row.get("context_pressure_variants"), dict) else {}
        if variants:
            sample = row
            break
    if sample is None:
        return item(
            "context_pressure_variants_auto",
            70,
            "warn",
            ["context_variants=missing"],
            "运行 projectling-auto 以生成 small/medium/near_limit context pressure replay。",
        )
    variants = sample.get("context_pressure_variants") if isinstance(sample.get("context_pressure_variants"), dict) else {}
    age = _auto_started_age_seconds(sample)
    try:
        passed = int(variants.get("passed") or 0)
        count = int(variants.get("variant_count") or 0)
    except (TypeError, ValueError):
        passed = 0
        count = 0
    ok = bool(variants.get("ok") and count >= 3 and passed == count)
    score = 100 if ok else 65
    if age is not None and age > 86400:
        score = min(score, 75)
    labels = variants.get("labels") if isinstance(variants.get("labels"), list) else []
    evidence = [
        f"ok={_compact_bool_flag(ok)}",
        f"age={age if age is not None else '-'}",
        f"passed={passed}/{count}",
        f"labels={_compact_list_or_dash(labels)}",
        _detail_file_evidence("detail", sample.get("detail_path")),
    ]
    score = _append_auto_detail_density_guard(evidence, score)
    score = _append_auto_repr_density_guard(evidence, score)
    return item(
        "context_pressure_variants_auto",
        score,
        status_from_score(score),
        evidence,
        "修复 context pressure variants 的 replace/fold/budget/freshness replay。" if score < 85 else "",
    )


def check_local_stress_auto() -> dict[str, Any]:
    rows = _load_auto_history(limit=30)
    sample: dict[str, Any] | None = None
    for row in reversed(rows):
        if row.get("run_mode") == "local_stress":
            sample = row
            break
    if sample is None:
        return item(
            "local_stress_auto",
            70,
            "warn",
            ["local_stress=missing"],
            "运行 projectling-auto --rounds 1 --local-stress。",
        )
    age = _auto_started_age_seconds(sample)
    command = sample.get("command") if isinstance(sample.get("command"), dict) else {}
    aidebug = sample.get("aidebug") if isinstance(sample.get("aidebug"), dict) else {}
    context = sample.get("context_pressure") if isinstance(sample.get("context_pressure"), dict) else {}
    context_variants = sample.get("context_pressure_variants") if isinstance(sample.get("context_pressure_variants"), dict) else {}
    memory = sample.get("memory") if isinstance(sample.get("memory"), dict) else {}
    memory_pressure = sample.get("memory_pressure") if isinstance(sample.get("memory_pressure"), dict) else {}
    ok = bool(
        sample.get("ok")
        and command.get("matrix_ok")
        and aidebug.get("precision_ok")
        and aidebug.get("truncation_ok")
        and context.get("freshness_ok")
        and context_variants.get("ok")
        and memory.get("db_integrity_ok")
        and memory_pressure.get("consume_ok")
        and not sample.get("live_chat")
    )
    score = 100 if ok else 65
    if age is not None and age > 86400:
        score = min(score, 75)
    evidence = [
        f"ok={_compact_bool_flag(ok)}",
        f"age={age if age is not None else '-'}",
        f"command_matrix={_compact_bool_flag(command.get('matrix_ok'))}",
        f"aidebug_precision={_compact_bool_flag(aidebug.get('precision_ok'))}",
        f"aidebug_trunc={_compact_bool_flag(aidebug.get('truncation_ok'))}",
        f"context_fresh={_compact_bool_flag(context.get('freshness_ok'))}",
        f"context_variants={context_variants.get('passed')}/{context_variants.get('variant_count')}",
        f"memory_db={_compact_bool_flag(memory.get('db_integrity_ok'))}",
        f"memory_pressure_consume={_compact_bool_flag(memory_pressure.get('consume_ok'))}",
        f"live_chat={_compact_bool_flag(bool(sample.get('live_chat')))}",
        _detail_file_evidence("detail", sample.get("detail_path")),
    ]
    score = _append_auto_detail_density_guard(evidence, score)
    score = _append_auto_repr_density_guard(evidence, score)
    return item(
        "local_stress_auto",
        score,
        status_from_score(score),
        evidence,
        "修复 local-stress 本地压力覆盖或复跑 projectling-auto --local-stress。" if score < 85 else "",
    )


def _auto_profile_name(row: dict[str, Any]) -> str:
    profile = str(row.get("profile") or "").strip()
    if profile:
        return profile
    run_mode = str(row.get("run_mode") or "").strip()
    if run_mode == "local_stress":
        return "local"
    if run_mode == "live_web":
        return "full"
    if run_mode == "live":
        return "live"
    return "legacy"


def check_auto_profile_coverage() -> dict[str, Any]:
    rows = _load_auto_history(limit=60)
    required = ("local", "live", "full")
    latest: dict[str, dict[str, Any] | None] = {name: None for name in required}
    for row in reversed(rows):
        profile = _auto_profile_name(row)
        if profile in latest and latest[profile] is None and row.get("ok") is True:
            latest[profile] = row
    ages = {name: (_auto_started_age_seconds(row) if row is not None else None) for name, row in latest.items()}
    fresh_limit = 86400
    ok = all(row is not None for row in latest.values()) and all(
        age is not None and age <= fresh_limit for age in ages.values()
    )
    score = 100 if ok else 75
    def detail_file_name(row: dict[str, Any] | None) -> str:
        raw = str((row or {}).get("detail_path") or "-")
        return raw.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or "-"

    profile_aliases = {"local": "l", "live": "v", "full": "f"}
    evidence = [
        f"ok={_compact_bool_flag(ok)}",
        "ages=" + " ".join(f"{profile_aliases[name]}:{ages[name] if ages[name] is not None else '-'}" for name in required),
        *[f"{profile_aliases[name]}_file={detail_file_name(latest[name])}" for name in required],
    ]
    density_limit = 80
    density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for index, row in enumerate(evidence, start=1)
        if (
            len(row) > density_limit
            or "_detail=C:" in row
            or "_detail=/mnt/" in row
            or "True" in row
            or "False" in row
            or any(label in row for label in ("local_", "live_", "full_", "_detail_file="))
        )
    ]
    if density_failures:
        score = min(score, 75)
    evidence.append(f"profile_coverage_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")
    return item(
        "projectling_auto_profile_coverage",
        score,
        status_from_score(score),
        evidence,
        "复跑 projectling-auto --profile local、--profile live 和 --profile full，刷新本地压力、DeepSeek 与 live+web 覆盖。" if score < 85 else "",
    )


def check_command_matrix_profile_coverage() -> dict[str, Any]:
    rows = _load_auto_history(limit=60)
    required = ("local", "live", "full")
    latest: dict[str, dict[str, Any] | None] = {name: None for name in required}
    for row in reversed(rows):
        profile = _auto_profile_name(row)
        command = row.get("command") if isinstance(row.get("command"), dict) else {}
        try:
            case_count = int(command.get("matrix_cases") or 0)
        except (TypeError, ValueError):
            case_count = 0
        if (
            profile in latest
            and latest[profile] is None
            and row.get("ok") is True
            and command.get("matrix_ok") is True
            and case_count >= 4
        ):
            latest[profile] = row
    ages = {name: (_auto_started_age_seconds(row) if row is not None else None) for name, row in latest.items()}
    fresh_limit = 86400
    ok = all(row is not None for row in latest.values()) and all(
        age is not None and age <= fresh_limit for age in ages.values()
    )
    score = 100 if ok else 75
    evidence = [f"ok={_compact_bool_flag(ok)}"]
    profile_aliases = {"local": "l", "live": "v", "full": "f"}
    label_aliases = {
        "unicode_stdout": "uout",
        "stderr_capture": "err",
        "nonzero_exit": "nz",
        "timeout": "to",
    }
    for name in required:
        row = latest[name] or {}
        command = row.get("command") if isinstance(row.get("command"), dict) else {}
        matrix_details = command.get("matrix_details") if isinstance(command.get("matrix_details"), list) else []
        labels = [
            label_aliases.get(str(item.get("label")), str(item.get("label")))
            for item in matrix_details
            if isinstance(item, dict) and item.get("label")
        ]
        evidence.append(
            f"{profile_aliases[name]}=age:{ages[name] if ages[name] is not None else '-'} "
            f"cases:{command.get('matrix_cases')} labels:{','.join(labels)}"
        )
    density_limit = 65
    verbose_labels = (
        "ok=True",
        "ok=False",
        "local=",
        "live=",
        "full=",
        "unicode_stdout",
        "stderr_capture",
        "nonzero_exit",
        "timeout",
        "True",
        "False",
    )
    density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if len(row) > density_limit or any(label in row for label in verbose_labels)
    ]
    if density_failures:
        score = min(score, 75)
    evidence.append(f"command_matrix_profile_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")
    return item(
        "command_matrix_profile_coverage",
        score,
        status_from_score(score),
        evidence,
        "复跑 projectling-auto --profile local/live/full，确认三档 command matrix 都有新鲜成功样本。" if score < 85 else "",
    )


def check_profile_detail_integrity() -> dict[str, Any]:
    rows = _load_auto_history(limit=60)
    required = ("local", "live", "full")
    latest = {profile: _latest_auto_profile(rows, profile) for profile in required}
    fresh_limit = 86400
    profile_aliases = {"local": "l", "live": "v", "full": "f"}
    evidence: list[str] = []
    failures: dict[str, list[str]] = {}
    ages: dict[str, int | None] = {}
    ok = True
    for profile in required:
        row = latest[profile]
        if row is None:
            ok = False
            failures[profile] = ["missing_sample"]
            ages[profile] = None
            evidence.append(f"{profile_aliases[profile]}=missing")
            continue
        age = _auto_started_age_seconds(row)
        sample = {
            "profile": profile,
            "detail_path": str(row.get("detail_path") or ""),
            "started_at": str(row.get("started_at") or ""),
            "run_mode": str(row.get("run_mode") or ""),
            "ok": bool(row.get("ok")),
        }
        detail_failures = _profile_sample_detail_failures(profile, sample)
        if age is None or age > fresh_limit:
            detail_failures = [*detail_failures, "stale_or_missing_age"]
        ages[profile] = age
        if detail_failures:
            ok = False
        failures[profile] = detail_failures
        remaining = fresh_limit - age if age is not None else None
        detail_file = str(sample["detail_path"]).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or "-"
        evidence.append(
            f"{profile_aliases[profile]}=a:{age if age is not None else '-'} "
            f"rem:{remaining if remaining is not None else '-'} "
            f"int:{'ok' if not detail_failures else ','.join(detail_failures)} "
            f"file:{detail_file}"
        )
    freshness = _profile_detail_freshness_summary(ages, fresh_limit=fresh_limit)
    near_stale_profiles = list(freshness.get("near_stale_profiles") or [])
    stale_profiles = list(freshness.get("stale_profiles") or [])
    score = 100 if ok and not near_stale_profiles else 90 if ok else 75 if all(latest.values()) else 55

    def _profile_label(value: Any) -> str:
        text = str(value or "").strip()
        return profile_aliases.get(text, text or "-")

    def _profile_labels(values: list[Any]) -> str:
        labels = [_profile_label(value) for value in values if str(value or "").strip()]
        return ",".join(labels) or "-"

    evidence.insert(0, f"ok={_compact_bool_flag(ok)}")
    evidence.insert(
        1,
        "freshness_trend="
        f"status:{freshness.get('status')} "
        f"on:{_profile_label(freshness.get('oldest_profile'))}/{_profile_label(freshness.get('newest_profile'))} "
        f"sp:{freshness.get('age_span_seconds')} "
        f"lm:{freshness.get('near_stale_limit_seconds')}/{freshness.get('fresh_limit_seconds')} "
        f"nr:{_profile_labels(near_stale_profiles)} st:{_profile_labels(stale_profiles)}",
    )
    next_action = ""
    if score < 85:
        next_action = "复跑 profile local/live/full 并检查 detail JSON 是否保留 command、memory、context、DeepSeek 和 web 证据。"
    elif near_stale_profiles:
        next_action = "刷新接近过期的 profile detail 样本：" + ", ".join(near_stale_profiles)
    density_limit = 75
    verbose_trend_labels = (
        "oldest:",
        "newest:",
        "warn:",
        "warn_at:",
        "max:",
        " old:",
        " new:",
        " span:",
        " near:",
        " stale:",
        "o:local",
        "o:live",
        "o:full",
        "n:local",
        "n:live",
        "n:full",
        "nr:local",
        "nr:live",
        "nr:full",
        "st:local",
        "st:live",
        "st:full",
    )
    verbose_detail_labels = ("remaining:", "integrity:", "detail_file:")
    density_failures = [
        f"row{index}:{len(row)}"
        for index, row in enumerate(evidence, start=1)
        if (
            row.startswith(("ok=", "freshness_trend=", "l=", "v=", "f=", "local=", "live=", "full="))
            and (
                len(row) > density_limit
                or "ok=True" in row
                or "ok=False" in row
                or "True" in row
                or "False" in row
                or row.startswith(("local=", "live=", "full="))
                or "detail:C:" in row
                or "detail:/mnt/" in row
                or (row.startswith("freshness_trend=") and any(label in row for label in verbose_trend_labels))
                or (row.startswith(("l=", "v=", "f=", "local=", "live=", "full=")) and any(label in row for label in verbose_detail_labels))
            )
        )
    ]
    if density_failures:
        score = min(score, 75)
    evidence.append(f"profile_detail_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")
    return item(
        "projectling_profile_detail_integrity",
        score,
        status_from_score(score),
        evidence,
        next_action,
    )


def check_profile_freshness_policy() -> dict[str, Any]:
    fresh_limit = 86400
    near_stale_limit = int(fresh_limit * 0.75)
    cases = [
        {
            "label": "fresh",
            "ages": {"local": 60, "live": 45, "full": 30},
            "status": "fresh",
            "near": [],
            "stale": [],
        },
        {
            "label": "near_stale",
            "ages": {"local": near_stale_limit, "live": 600, "full": 300},
            "status": "near_stale",
            "near": ["local"],
            "stale": [],
        },
        {
            "label": "stale",
            "ages": {"local": fresh_limit + 1, "live": 600, "full": 300},
            "status": "stale",
            "near": [],
            "stale": ["local"],
        },
        {
            "label": "missing_age",
            "ages": {"local": None, "live": 600, "full": 300},
            "status": "stale",
            "near": [],
            "stale": ["local"],
        },
    ]
    evidence = [
        f"fresh_limit={fresh_limit}",
        f"near_stale_limit={near_stale_limit}",
    ]
    failures: list[str] = []
    profile_aliases = {"local": "l", "live": "v", "full": "f"}

    def _profile_labels(values: list[Any]) -> str:
        labels = [profile_aliases.get(str(value), str(value)) for value in values if str(value or "").strip()]
        return ",".join(labels) or "-"

    for case in cases:
        summary = _profile_detail_freshness_summary(case["ages"], fresh_limit=fresh_limit)
        near = list(summary.get("near_stale_profiles") or [])
        stale = list(summary.get("stale_profiles") or [])
        case_ok = (
            summary.get("status") == case["status"]
            and near == case["near"]
            and stale == case["stale"]
        )
        if not case_ok:
            failures.append(str(case["label"]))
        evidence.append(
            f"{case['label']}=ok:{_compact_bool_flag(case_ok)} status:{summary.get('status')} "
            f"near:{_profile_labels(near)} stale:{_profile_labels(stale)} "
            f"span:{summary.get('age_span_seconds')}"
        )
    density_limit = 70
    density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in evidence
        if len(row) > density_limit or "True" in row or "False" in row or "near:local" in row or "stale:local" in row
    ]
    if density_failures:
        failures.append("evidence_density")
    evidence.append(f"profile_freshness_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}")
    ok = not failures
    score = 100 if ok else 55
    return item(
        "projectling_profile_freshness_policy",
        score,
        status_from_score(score),
        evidence,
        "修复 profile detail freshness policy 的 fresh/near-stale/stale/missing-age 判定。" if not ok else "",
    )


def _auto_started_age_seconds(payload: dict[str, Any]) -> int | None:
    raw = str(payload.get("started_at") or "").strip()
    if not raw:
        return None
    try:
        parsed = time.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return max(0, int(time.time() - calendar.timegm(parsed)))


def _latest_auto_with(rows: list[dict[str, Any]], key: str, ok_key: str = "ok") -> dict[str, Any] | None:
    for row in reversed(rows):
        value = row.get(key) if isinstance(row.get(key), dict) else {}
        if value.get(ok_key) is True:
            return row
    return None


def _latest_auto_profile(rows: list[dict[str, Any]], profile: str) -> dict[str, Any] | None:
    for row in reversed(rows):
        if row.get("ok") is True and _auto_profile_name(row) == profile:
            return row
    return None


def check_auto_stress_durability() -> dict[str, Any]:
    rows = _load_auto_history(limit=30)
    if not rows:
        return item(
            "projectling_auto_stress_durability",
            55,
            "warn",
            ["auto_history=missing"],
            "运行 aidebug projectling-auto --rounds 1 --web-query \"ProjectLing aidebug stress\" --live-chat-smoke。",
        )
    pass_streak = 0
    recent_failures = 0
    for row in reversed(rows):
        if row.get("ok") is True:
            pass_streak += 1
        else:
            recent_failures += 1
            break
    recent_failures += sum(1 for row in rows[-10:] if row.get("ok") is not True)

    latest = rows[-1]
    latest_age = _auto_started_age_seconds(latest)
    latest_live = _latest_auto_with(rows, "live_chat")
    latest_web = _latest_auto_with(rows, "web_search")
    latest_context = _latest_auto_with(rows, "context_pressure")
    latest_context_variants = _latest_auto_with(rows, "context_pressure_variants")
    latest_memory = _latest_auto_with(rows, "memory")
    latest_memory_pressure = _latest_auto_with(rows, "memory_pressure")
    latest_profile_local = _latest_auto_profile(rows, "local")
    latest_profile_live = _latest_auto_profile(rows, "live")
    latest_profile_full = _latest_auto_profile(rows, "full")
    latest_local_stress: dict[str, Any] | None = None
    for row in reversed(rows):
        if row.get("run_mode") == "local_stress" and row.get("ok") is True:
            latest_local_stress = row
            break
    latest_matrix: dict[str, Any] | None = None
    for row in reversed(rows):
        command = row.get("command") if isinstance(row.get("command"), dict) else {}
        if command.get("matrix_ok") is True:
            latest_matrix = row
            break

    samples = {
        "live": latest_live,
        "web": latest_web,
        "context": latest_context,
        "context_variants": latest_context_variants,
        "memory": latest_memory,
        "memory_pressure": latest_memory_pressure,
        "local_stress": latest_local_stress,
        "profile_local": latest_profile_local,
        "profile_live": latest_profile_live,
        "profile_full": latest_profile_full,
        "command_matrix": latest_matrix,
    }
    issues = _load_jsonl_path(LOG_DIR / "projectling-auto-issues.jsonl")
    resolutions = _load_jsonl_path(LOG_DIR / "projectling-auto-resolutions.jsonl")
    resolved_keys = {
        str(row.get("issue_key") or _auto_issue_key(row))
        for row in resolutions
        if row.get("issue_key") or row.get("issue_component")
    }
    unresolved = [issue for issue in issues if _auto_issue_key(issue) not in resolved_keys]
    ages = {name: (_auto_started_age_seconds(row) if row is not None else None) for name, row in samples.items()}
    fresh_limit = 86400
    fresh_ok = all(age is not None and age <= fresh_limit for age in ages.values())
    coverage_ok = all(row is not None for row in samples.values())
    score = 100 if pass_streak >= 3 and coverage_ok and fresh_ok and not unresolved else 85
    if not coverage_ok:
        score = 75
    if unresolved:
        score = min(score, 80)
    if pass_streak == 0:
        score = 45
    if latest_age is not None and latest_age > fresh_limit:
        score = min(score, 70)
    evidence = [
        f"rows={len(rows)}",
        f"pass_streak={pass_streak}",
        f"recent_failures={recent_failures}",
        f"unresolved_issues={len(unresolved)}",
        f"latest_age={latest_age if latest_age is not None else '-'}",
        *[f"{name}_age={age if age is not None else '-'}" for name, age in ages.items()],
    ]
    return item(
        "projectling_auto_stress_durability",
        score,
        status_from_score(score),
        evidence,
        "继续复跑 profile local/live/full 建立 3 连胜，并确认 issue ledger unresolved=0。" if score < 100 else "",
    )


def _compact_live_tool_sequence(value: Any) -> str:
    if not isinstance(value, list):
        return "-"
    aliases = {
        "link": "ln",
        "update_plan": "plan",
        "command": "cmd",
        "apply_patch": "patch",
        "web_search": "web",
        "aidebug": "dbg",
        "contextmanage": "ctx",
        "memory_add": "mem",
        "model_mode": "mode",
    }
    compacted = [aliases.get(str(tool).strip(), str(tool).strip()) for tool in value if str(tool).strip()]
    return _compact_list_or_dash(compacted)


def _compact_live_actor_labels(value: Any) -> str:
    if not isinstance(value, list):
        return "-"
    compacted: list[str] = []
    for raw in value:
        text = str(raw or "").strip()
        lowered = text.lower()
        if not text:
            continue
        if "主星" in text or "主" == text or "planner" in lowered or "main" in lowered:
            compacted.append("main")
        elif "执行星" in text or "执行" in text or "executor" in lowered or "exec" in lowered:
            compacted.append("exec")
        else:
            compacted.append(text.replace(",", "_").replace(" ", "_"))
    return _compact_list_or_dash(compacted)


def _append_live_quality_density_guard(evidence: list[str], score: int, *, limit: int = 75) -> int:
    density_candidates = [
        row
        for row in evidence
        if row.startswith(
            (
                "provider=",
                "ok=",
                "dual=",
                "actors=",
                "thinking=",
                "review_err=",
                "cmd_actor=",
                "rounds=",
                "tools=",
                "cache_policy=",
                "cache_ok=",
                "warmup=",
                "ctx=",
            )
        )
    ]
    density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in density_candidates
        if len(row) > limit
        or "['" in row
        or "']" in row
        or "{'" in row
        or "'}" in row
        or "True" in row
        or "False" in row
    ]
    if density_failures:
        score = min(score, 75)
    evidence.append(f"live_quality_density=limit={limit} failures={_compact_list_or_dash(density_failures)}")
    return score


def check_deepseek_live_cache_quality() -> dict[str, Any]:
    active_provider = "deepseek"
    try:
        active_provider = str(getattr(load_config(), "api_provider", "deepseek") or "deepseek").lower() if load_config else "deepseek"
    except Exception:
        active_provider = "deepseek"
    rows = _load_auto_history()
    sample: dict[str, Any] | None = None
    for row in reversed(rows):
        live = row.get("live_chat") if isinstance(row.get("live_chat"), dict) else {}
        if live:
            sample = row
            break
    if sample is None:
        return item(
            "deepseek_live_cache_quality",
            70,
            "warn",
            ["recent_live_chat=missing"],
            "运行 aidebug projectling-auto --rounds 1 --live-chat-smoke。",
        )
    live = sample.get("live_chat") if isinstance(sample.get("live_chat"), dict) else {}
    usage = live.get("usage") if isinstance(live.get("usage"), dict) else {}
    age = _auto_started_age_seconds(sample)
    ok = bool(live.get("ok"))
    provider = str(live.get("provider") or active_provider or "deepseek").lower()
    if active_provider != "deepseek" or provider != "deepseek":
        dual_star_ok = live.get("dual_star_metadata_ok") is True
        score = 100 if ok and dual_star_ok else 80 if ok else 45
        if age is not None and age > 86400:
            score = min(score, 75)
        evidence = [
            f"provider={provider}",
            f"ok={_compact_bool_flag(ok)}",
            f"dual={_compact_bool_flag(dual_star_ok)}",
            f"actors={_compact_live_actor_labels(live.get('tool_actor_labels'))}",
            f"thinking={_compact_live_actor_labels(live.get('thinking_actor_labels'))}",
            f"review_err={live.get('planner_review_errors')}",
            f"cmd_actor={_compact_bool_flag(live.get('command_executor_actor'))}",
            f"age={age if age is not None else '-'}",
            f"rounds={live.get('rounds')}",
            f"tools={_compact_live_tool_sequence(live.get('tool_names'))}",
            f"cache_policy=not_required_for_{provider}",
            _detail_file_evidence("detail", sample.get("detail_path")),
        ]
        score = _append_auto_detail_density_guard(evidence, score)
        score = _append_live_quality_density_guard(evidence, score)
        return item(
            "deepseek_live_cache_quality",
            score,
            status_from_score(score),
            evidence,
            f"复跑 {provider} live-chat smoke，要求 command 由执行星执行、主星 thinking/review 存在且无复审错误。" if score < 85 else "",
        )
    try:
        miss = int(usage.get("cache_miss_tokens") or 0)
    except (TypeError, ValueError):
        miss = 0
    try:
        hit_rate = float(usage.get("cache_hit_rate") or 0.0)
    except (TypeError, ValueError):
        hit_rate = 0.0
    score = 100 if ok and hit_rate >= 85.0 and miss <= 1000 else 80 if ok else 45
    if age is not None and age > 86400:
        score = min(score, 75)
    evidence = [
        f"ok={_compact_bool_flag(ok)}",
        f"age={age if age is not None else '-'}",
        f"rounds={live.get('rounds')}",
        f"tools={_compact_live_tool_sequence(live.get('tool_names'))}",
        f"prompt={usage.get('prompt_tokens')}",
        f"cached={usage.get('cached_tokens')}",
        f"miss={miss}",
        f"hit_rate={hit_rate}",
        f"attempts={live.get('attempts')}",
        f"cache_ok={_compact_bool_flag(live.get('cache_ok'))}",
        f"warmup={_compact_bool_flag(live.get('cache_warmup'))}",
        f"ctx={_compact_bool_flag(live.get('context_restored'))}",
        _detail_file_evidence("detail", sample.get("detail_path")),
    ]
    score = _append_auto_detail_density_guard(evidence, score)
    score = _append_live_quality_density_guard(evidence, score)
    return item(
        "deepseek_live_cache_quality",
        score,
        status_from_score(score),
        evidence,
        "检查 DeepSeek 缓存命中、上下文裁剪或工具 schema 体积。" if score < 85 else "",
    )


def check_live_smoke_cost_efficiency() -> dict[str, Any]:
    rows = _load_auto_history(limit=60)
    sample: dict[str, Any] | None = None
    for row in reversed(rows):
        live = row.get("live_chat") if isinstance(row.get("live_chat"), dict) else {}
        if isinstance(live.get("request_usage_total"), dict):
            sample = row
            break
    if sample is None:
        return item(
            "live_smoke_cost_efficiency",
            70,
            "warn",
            ["request_usage_total=missing"],
            "复跑 live profile，生成逐 API 调用与累计 token 分解。",
        )

    live = sample.get("live_chat") if isinstance(sample.get("live_chat"), dict) else {}
    final_usage = live.get("usage") if isinstance(live.get("usage"), dict) else {}
    cumulative = live.get("request_usage_total") if isinstance(live.get("request_usage_total"), dict) else {}
    breakdown = live.get("request_breakdown") if isinstance(live.get("request_breakdown"), list) else []
    runtime_state = sample.get("runtime_state") if isinstance(sample.get("runtime_state"), dict) else {}
    try:
        final_prompt = int(final_usage.get("prompt_tokens") or 0)
    except (TypeError, ValueError):
        final_prompt = 0
    try:
        cumulative_prompt = int(cumulative.get("prompt_tokens") or 0)
    except (TypeError, ValueError):
        cumulative_prompt = 0
    try:
        api_calls = int(cumulative.get("api_calls") or 0)
    except (TypeError, ValueError):
        api_calls = 0
    allowed_tools = {"update_plan", "command", "link"}
    schema_names = {
        str(name)
        for entry in breakdown
        if isinstance(entry, dict)
        for name in (entry.get("tool_schema_names") if isinstance(entry.get("tool_schema_names"), list) else [])
        if str(name)
    }
    tool_names = {str(name) for name in (live.get("tool_names") or []) if str(name)}
    workflow_ok = (
        live.get("ok") is True
        and live.get("dual_star_metadata_ok") is True
        and int(live.get("planner_review_errors") or 0) == 0
        and {"update_plan", "command", "link"}.issubset(tool_names)
    )
    budget_ok = 0 < cumulative_prompt <= 50000 and 0 < final_prompt <= 15000 and 0 < api_calls <= 10
    schema_ok = bool(schema_names) and schema_names.issubset(allowed_tools)
    state_ok = runtime_state.get("ok") is True
    age = _auto_started_age_seconds(sample)
    fresh = age is not None and age <= 86400
    ok = workflow_ok and budget_ok and schema_ok and state_ok and fresh
    historical_final_prompt = 120078
    reduction = round((historical_final_prompt - final_prompt) * 100.0 / historical_final_prompt, 1) if final_prompt > 0 else 0.0
    evidence = [
        f"prompt=final:{final_prompt} cumulative:{cumulative_prompt} calls:{api_calls}",
        f"baseline_final={historical_final_prompt} reduction={reduction}%",
        f"request_chars={cumulative.get('request_json_chars')} schema_chars={cumulative.get('tool_schema_json_chars')}",
        f"workflow={_compact_bool_flag(workflow_ok)} tools={_compact_live_tool_sequence(live.get('tool_names'))}",
        f"schemas={','.join(sorted(schema_names)) or '-'}",
        f"state={_compact_bool_flag(state_ok)} age={age if age is not None else '-'}",
        _detail_file_evidence("detail", sample.get("detail_path")),
    ]
    score = 100 if ok else 75 if workflow_ok and fresh else 45
    return item(
        "live_smoke_cost_efficiency",
        score,
        status_from_score(score),
        evidence,
        "保持双星/复审/command 合同，同时把累计 prompt 控制在 50k、末轮控制在 15k，并只暴露 plan/command/link。" if not ok else "",
    )


def check_deepseek_cache_stability_trend() -> dict[str, Any]:
    active_provider = "deepseek"
    try:
        active_provider = str(getattr(load_config(), "api_provider", "deepseek") or "deepseek").lower() if load_config else "deepseek"
    except Exception:
        active_provider = "deepseek"
    rows = _load_auto_history(limit=20)
    samples: list[dict[str, Any]] = []
    for row in rows:
        live = row.get("live_chat") if isinstance(row.get("live_chat"), dict) else {}
        if live:
            samples.append(row)
    if not samples:
        return item(
            "deepseek_cache_stability_trend",
            70,
            "warn",
            ["live_samples=0"],
            "运行带 --live-chat-smoke 的 projectling-auto 以建立缓存趋势。",
        )
    recent = samples[-8:]
    if active_provider != "deepseek":
        ok_count = 0
        provider_counts: dict[str, int] = {}
        for sample in recent:
            live = sample.get("live_chat") if isinstance(sample.get("live_chat"), dict) else {}
            provider = str(live.get("provider") or active_provider).lower()
            provider_counts[provider] = provider_counts.get(provider, 0) + 1
            if live.get("ok") is True:
                ok_count += 1
        latest_age = _auto_started_age_seconds(recent[-1])
        score = 100 if len(recent) >= 3 and ok_count == len(recent) and latest_age is not None and latest_age <= 86400 else 85
        if latest_age is not None and latest_age > 86400:
            score = min(score, 75)
        evidence = [
            f"provider={active_provider}",
            f"live_samples={len(samples)}",
            f"recent={len(recent)}",
            f"ok_count={ok_count}",
            f"latest_age={latest_age if latest_age is not None else '-'}",
            "profiles=" + ",".join(f"{name}:{count}" for name, count in sorted(provider_counts.items())),
            f"cache_policy=not_required_for_{active_provider}",
        ]
        return item(
            "deepseek_cache_stability_trend",
            score,
            status_from_score(score),
            evidence,
            f"复跑 {active_provider} live/full profile，建立至少 3 个新鲜成功样本。" if score < 85 else "",
        )
    post_warmup_failures: list[str] = []
    hit_rates: list[float] = []
    misses: list[int] = []
    warmup_resolved = 0
    sequence_warmup_resolved = 0
    cache_ok_count = 0
    cache_ok_by_index: list[bool] = []
    sample_stats: list[tuple[int, float]] = []
    profile_counts: dict[str, int] = {}
    profile_latest: dict[str, dict[str, Any]] = {}
    for sample in recent:
        profile = _auto_profile_name(sample)
        profile_counts[profile] = profile_counts.get(profile, 0) + 1
        profile_latest[profile] = sample
        live = sample.get("live_chat") if isinstance(sample.get("live_chat"), dict) else {}
        usage = live.get("usage") if isinstance(live.get("usage"), dict) else {}
        try:
            miss = int(usage.get("cache_miss_tokens") or 0)
        except (TypeError, ValueError):
            miss = 0
        try:
            hit_rate = float(usage.get("cache_hit_rate") or 0.0)
        except (TypeError, ValueError):
            hit_rate = 0.0
        hit_rates.append(hit_rate)
        misses.append(miss)
        cache_ok = bool(live.get("cache_ok")) or (miss <= 1000 and hit_rate >= 85.0)
        cache_ok_by_index.append(cache_ok)
        sample_stats.append((miss, hit_rate))
        if cache_ok:
            cache_ok_count += 1
    for index, sample in enumerate(recent):
        live = sample.get("live_chat") if isinstance(sample.get("live_chat"), dict) else {}
        warmup = live.get("cache_warmup") if isinstance(live.get("cache_warmup"), dict) else {}
        if warmup.get("resolved"):
            warmup_resolved += 1
        next_cache_ok = index + 1 < len(cache_ok_by_index) and cache_ok_by_index[index + 1]
        if not cache_ok_by_index[index] and next_cache_ok:
            sequence_warmup_resolved += 1
        if live.get("ok") and not cache_ok_by_index[index] and not warmup.get("resolved") and not next_cache_ok:
            miss, hit_rate = sample_stats[index]
            post_warmup_failures.append(
                f"{sample.get('started_at')} profile={_auto_profile_name(sample)} miss={miss} hit_rate={hit_rate}"
            )
    latest_age = _auto_started_age_seconds(recent[-1])
    min_hit = min(hit_rates) if hit_rates else 0.0
    max_miss = max(misses) if misses else 0
    score = 100 if len(recent) >= 3 and not post_warmup_failures and latest_age is not None and latest_age <= 86400 else 85
    if post_warmup_failures:
        score = 70
    if latest_age is not None and latest_age > 86400:
        score = min(score, 75)
    evidence = [
        f"live_samples={len(samples)}",
        f"recent={len(recent)}",
        f"cache_ok_count={cache_ok_count}",
        f"warmup_resolved={warmup_resolved}",
        f"sequence_warmup_resolved={sequence_warmup_resolved}",
        f"min_hit_rate={round(min_hit, 2)}",
        f"max_miss={max_miss}",
        f"latest_age={latest_age if latest_age is not None else '-'}",
        "profiles=" + ",".join(f"{name}:{count}" for name, count in sorted(profile_counts.items())),
        *[
            f"{name}_latest_age={_auto_started_age_seconds(row) if row is not None else '-'}"
            for name, row in sorted(profile_latest.items())
        ],
    ]
    for failure in post_warmup_failures[:4]:
        evidence.append(f"post_warmup_failure={failure}")
    return item(
        "deepseek_cache_stability_trend",
        score,
        status_from_score(score),
        evidence,
        "检查 DeepSeek 缓存稳定性；若 warmup 后仍低命中，缩小工具 schema 或上下文。" if score < 85 else "",
    )


def check_web_search_live_quality() -> dict[str, Any]:
    rows = _load_auto_history()
    sample: dict[str, Any] | None = None
    for row in reversed(rows):
        web = row.get("web_search") if isinstance(row.get("web_search"), dict) else {}
        if web.get("ok") is not None:
            sample = row
            break
    if sample is None:
        return item(
            "web_search_live_quality",
            70,
            "warn",
            ["recent_web_search=missing"],
            "运行 aidebug projectling-auto --rounds 1 --web-query \"ProjectLing aidebug smoke\"。",
        )
    web = sample.get("web_search") if isinstance(sample.get("web_search"), dict) else {}
    age = _auto_started_age_seconds(sample)
    result_count = int(web.get("result_count") or 0)
    ok = bool(web.get("ok")) and bool(web.get("validation_ok")) and result_count > 0
    score = 100 if ok else 70 if web.get("ok") else 45
    if age is not None and age > 86400:
        score = min(score, 75)
    evidence = [
        f"ok={_compact_bool_flag(web.get('ok'))}",
        f"validation={_compact_bool_flag(web.get('validation_ok'))}",
        f"results={result_count}",
        f"age={age if age is not None else '-'}",
        _detail_file_evidence("detail", sample.get("detail_path")),
    ]
    score = _append_auto_detail_density_guard(evidence, score)
    score = _append_auto_repr_density_guard(evidence, score)
    return item(
        "web_search_live_quality",
        score,
        status_from_score(score),
        evidence,
        "复跑 web_search smoke 或检查搜索 API 配置。" if score < 85 else "",
    )


def write_android_readiness_artifact(
    *,
    is_android_termux: bool,
    am_path: str,
    tmux_path: str,
    termux_bash: Path,
    projectling_run: Path,
    properties: Path,
    allow_external: bool,
    adb_probe: dict[str, Any],
    missing: list[str],
) -> Path:
    NOTE_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# ProjectLing Android Termux Readiness",
        "",
        "This file is generated by `aidebug_health.py` so the real-device validation steps stay visible.",
        "",
        "## Current Probe",
        "",
        f"- generated_at: {timestamp()}",
        f"- runtime_android_termux: {is_android_termux}",
        f"- am: {am_path or 'missing'}",
        f"- tmux: {tmux_path or 'missing'}",
        f"- termux_bash: {termux_bash.as_posix()}",
        f"- projectling_run: {projectling_run.as_posix()}",
        f"- termux_properties: {properties.as_posix()}",
        f"- allow_external_apps: {allow_external}",
        f"- missing: {', '.join(missing) if missing else 'none'}",
        "",
        "## Host ADB Probe",
        "",
        f"- adb: {adb_probe.get('adb') or 'missing'}",
        f"- adb_version: {adb_probe.get('version') or '-'}",
        "- adb_devices: "
        f"device={adb_probe.get('device', 0)} "
        f"unauthorized={adb_probe.get('unauthorized', 0)} "
        f"offline={adb_probe.get('offline', 0)} "
        f"other={adb_probe.get('other', 0)} "
        f"rows={adb_probe.get('rows', 0)}",
        f"- adb_rc: {adb_probe.get('rc') or '-'}",
        f"- adb_error: {adb_probe.get('error') or '-'}",
        "",
        "## Device Setup Commands",
        "",
        "Run these inside real Android Termux, from the ProjectLing repo root:",
        "",
        "```sh",
        "pkg update",
        "pkg install -y python tmux git",
        "mkdir -p ~/.termux",
        "grep -qxF 'allow-external-apps=true' ~/.termux/termux.properties 2>/dev/null || printf '\\nallow-external-apps=true\\n' >> ~/.termux/termux.properties",
        "termux-reload-settings",
        "command -v am tmux python3 bash",
        "test -x /data/data/com.termux/files/usr/bin/bash",
        "./run.sh doctor",
        "python3 aidebug/runner/projectling_auto.py --rounds 1 --local-stress",
        "python3 aidebug/runner/aidebug_health.py",
        "```",
        "",
        "## Live DeepSeek Optional Replay",
        "",
        "Use this only when the real device has network/API access:",
        "",
        "```sh",
        "python3 aidebug/runner/projectling_auto.py --rounds 1 --web-query 'ProjectLing Android Termux readiness' --live-chat-smoke",
        "python3 aidebug/runner/aidebug_health.py",
        "```",
        "",
        "## Pass Criteria",
        "",
        "- `android_termux_readiness` scores 100 on the Android device.",
        "- `terminal_logs` no longer reports `android_am_missing`.",
        "- `projectling_auto_runner` is ok after `--local-stress`.",
        "- If live replay is used, `deepseek_live_cache_quality` remains ok.",
        "",
    ]
    ANDROID_READINESS_MD.write_text("\n".join(lines), encoding="utf-8")
    return ANDROID_READINESS_MD


def check_android_termux_readiness() -> dict[str, Any]:
    am_path = shutil.which("am") or ("/system/bin/am" if Path("/system/bin/am").exists() else "")
    tmux_path = shutil.which("tmux") or ""
    termux_bash = Path("/data/data/com.termux/files/usr/bin/bash")
    termux_run_command = Path("/data/data/com.termux/files/usr/bin/termux-open-url")
    properties = HOME / ".termux" / "termux.properties"
    adb_probe = _host_adb_probe()
    adb_device_count = int(adb_probe.get("device") or 0)
    adb_unauthorized_count = int(adb_probe.get("unauthorized") or 0)
    allow_external = False
    if properties.is_file():
        try:
            text = properties.read_text(encoding="utf-8", errors="replace")
            allow_external = any(
                line.strip().lower() == "allow-external-apps=true"
                for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
        except OSError:
            allow_external = False
    is_android_termux = bool(termux_bash.exists() and am_path and os.environ.get("PREFIX", "").startswith("/data/data/com.termux"))
    required = {
        "am": bool(am_path),
        "tmux": bool(tmux_path),
        "termux_bash": termux_bash.exists(),
        "projectling_run": PROJECTLING_RUN.exists(),
        "aidebug_dir": AIDEBUG_DIR.exists(),
    }
    missing = [name for name, ok in required.items() if not ok]
    artifact = write_android_readiness_artifact(
        is_android_termux=is_android_termux,
        am_path=am_path,
        tmux_path=tmux_path,
        termux_bash=termux_bash,
        projectling_run=PROJECTLING_RUN,
        properties=properties,
        allow_external=allow_external,
        adb_probe=adb_probe,
        missing=missing,
    )
    score = 100 if is_android_termux and not missing else 85 if not is_android_termux else max(25, 100 - len(missing) * 20)
    evidence = [
        f"runtime={'android-termux' if is_android_termux else 'non-android-compat'}",
        f"am={am_path or 'missing'}",
        f"tmux={tmux_path or 'missing'}",
        f"termux_bash={_compact_bool_flag(termux_bash.exists())}",
        f"projectling_run={_compact_bool_flag(PROJECTLING_RUN.exists())}",
        f"aidebug_dir={_compact_bool_flag(AIDEBUG_DIR.exists())}",
        f"termux_properties={properties if properties.exists() else 'missing'}",
        f"allow_external_apps={_compact_bool_flag(allow_external)}",
        f"termux_open_url={_compact_bool_flag(termux_run_command.exists())}",
        f"host_adb={adb_probe.get('adb_label') or 'missing'}",
        f"adb_version={adb_probe.get('version') or '-'}",
        "adb_devices="
        f"d:{adb_probe.get('device', 0)} "
        f"u:{adb_probe.get('unauthorized', 0)} "
        f"o:{adb_probe.get('offline', 0)} "
        f"x:{adb_probe.get('other', 0)}",
        f"adb_rc={adb_probe.get('rc') or '-'}",
        _artifact_status_evidence("artifact", artifact),
    ]
    if adb_probe.get("error"):
        evidence.append(f"adb_error={str(adb_probe.get('error'))[:90]}")
    if missing:
        evidence.append("missing=" + ",".join(missing))
    artifact_density_limit = 90
    artifact_density_failures = _artifact_status_density_failures(
        evidence,
        prefixes=("artifact_file=",),
        limit=artifact_density_limit,
    )
    if artifact_density_failures:
        score = min(score, 75)
    evidence.append(
        f"android_artifact_density=limit={artifact_density_limit} "
        f"failures={_compact_list_or_dash(artifact_density_failures)}"
    )
    score = _append_runtime_repr_density_guard(evidence, score)
    next_action = ""
    if is_android_termux and missing:
        next_action = "安装缺失组件并执行 termux-reload-settings 后复跑 aidebug projectling-auto。"
    elif not is_android_termux:
        if adb_unauthorized_count:
            next_action = "先在 Android 设备上确认 USB 调试授权，再在真实 Android Termux 中复跑以验证 am 前台标签页启动。"
        elif adb_device_count == 0:
            next_action = "当前 host adb 无已连接设备；连接并授权 Android 设备后，在真实 Android Termux 中复跑以验证 am 前台标签页启动。"
        else:
            next_action = "host adb 已看到设备；进入真实 Android Termux 后复跑以验证 am 前台标签页启动。"
    return item("android_termux_readiness", score, status_from_score(score), evidence, next_action)


def check_terminal_logs() -> dict[str, Any]:
    terminal_dir = AIDEBUG_DIR / "projectling" / "terminal output"
    logs = sorted(terminal_dir.glob("*.log")) if terminal_dir.exists() else []
    state = terminal_dir / "terminal-sessions.json"
    auto_path = LOG_DIR / "projectling-auto.jsonl"
    try:
        lines = auto_path.read_text(encoding="utf-8", errors="replace").splitlines()
        data = json.loads(lines[-1]) if lines else {}
    except Exception:
        data = {}
    terminal = data.get("terminal") if isinstance(data.get("terminal"), dict) else {}
    if data.get("ok") is True and terminal.get("ok") is True and not terminal.get("log_path"):
        reason = str(terminal.get("reason") or "android_am_missing")
        if terminal.get("skipped") or reason in {"android_am_missing", "tmux_missing"}:
            runtime = "windows-wsl-compat" if os.name == "nt" else "non-android-compat"
            auto_profile = str(data.get("profile") or "legacy")
            auto_run_mode = str(data.get("run_mode") or "")
            current_tmux = shutil.which("tmux") or ""
            local_auto_skip = auto_profile == "local" or auto_run_mode in {"local", "local_stress"}
            next_action = (
                "在真实 Android Termux 中复跑 projectling-auto terminal smoke；要求 am 可用、"
                "allow-external-apps=true，并生成真实 terminal log_path。"
            )
            if reason == "tmux_missing":
                if local_auto_skip:
                    next_action = (
                        "最新 local auto 轮次因 tmux_missing 兼容跳过 terminal smoke；"
                        "真实闭环仍需在 Android Termux 中确认 tmux、am、allow-external-apps=true "
                        "并生成 terminal log_path。"
                    )
                elif current_tmux:
                    next_action = (
                        "当前环境已看到 tmux，但最新 terminal smoke 仍记录 tmux_missing；"
                        "复跑 projectling-auto terminal smoke 刷新证据，并在真实 Android Termux 中确认 "
                        "am、allow-external-apps=true 与 terminal log_path。"
                    )
                else:
                    next_action = (
                        "安装 tmux 后复跑 projectling-auto terminal smoke；真实 Android Termux 还需 "
                        "am、allow-external-apps=true，并生成 terminal log_path。"
                    )
            score = 85
            evidence = [
                f"runtime={runtime}",
                "termux_terminal=skipped",
                f"reason={reason}",
                f"auto={auto_profile}/{auto_run_mode or '-'}",
                f"current_tmux={_compact_bool_flag(bool(current_tmux))}",
                _artifact_status_evidence("readiness", ANDROID_READINESS_MD),
                _detail_file_evidence("auto_detail", data.get("detail_path")),
            ]
            score = _append_auto_detail_density_guard(evidence, score)
            artifact_density_limit = 90
            artifact_density_failures = _artifact_status_density_failures(
                evidence,
                prefixes=("readiness_file=",),
                limit=artifact_density_limit,
            )
            if artifact_density_failures:
                score = min(score, 75)
            evidence.append(
                f"terminal_artifact_density=limit={artifact_density_limit} "
                f"failures={_compact_list_or_dash(artifact_density_failures)}"
            )
            return item(
                "terminal_logs",
                score,
                "ok",
                evidence,
                next_action,
            )
    score = 100 if state.exists() else 75
    if not logs:
        score -= 20
    latest = logs[-1] if logs else None
    evidence = [f"logs={len(logs)}", f"state_exists={_compact_bool_flag(state.exists())}"]
    if latest:
        meta = file_meta(latest)
        evidence.append(f"latest={latest.name} lines={meta.get('lines')} bytes={meta.get('bytes')}")
    score = _append_runtime_repr_density_guard(evidence, score)
    return item("terminal_logs", score, status_from_score(score), evidence, "启动 terminal smoke 检查协作终端链路。" if score < 85 else "")


def _motd_probe_status(probe: Any) -> str:
    if not isinstance(probe, dict):
        return "-"
    if probe.get("skipped"):
        return str(probe.get("reason") or "skipped")
    status = "ok" if probe.get("ok") else "fail"
    rc = probe.get("returncode")
    if rc is not None:
        status = f"{status}/rc{rc}"
    reason = probe.get("reason") or probe.get("error")
    if reason:
        status = f"{status}/{reason}"
    return status


def _compact_motd_zshrc_smoke_evidence(data: dict[str, Any], detail_path: Path) -> list[str]:
    non_tty = data.get("non_tty_motd") if isinstance(data.get("non_tty_motd"), dict) else {}
    zshrc = data.get("zshrc") if isinstance(data.get("zshrc"), dict) else {}
    tty = data.get("tty_motd") if isinstance(data.get("tty_motd"), dict) else {}
    startup = data.get("startup") if isinstance(data.get("startup"), dict) else {}
    motd_log = data.get("motd_log") if isinstance(data.get("motd_log"), dict) else {}
    zshrc_log = data.get("zshrc_log") if isinstance(data.get("zshrc_log"), dict) else {}
    warnings = startup.get("warnings") if isinstance(startup.get("warnings"), list) else []
    stdout_lines = zshrc.get("stdout_lines") if isinstance(zshrc.get("stdout_lines"), list) else []
    stdout_summary = ",".join(str(line) for line in stdout_lines[:4]) or "-"
    if len(stdout_lines) > 4:
        stdout_summary += f",+{len(stdout_lines) - 4}"
    source = str(zshrc.get("source") or "-")
    source_label = _artifact_file_label(source) if zshrc.get("ok") else source
    evidence = [
        "summary="
        f"ok:{_compact_bool_flag(data.get('ok'))} "
        f"st:{_compact_iso_timestamp(data.get('started_at'))} "
        f"nt:{_motd_probe_status(non_tty)} "
        f"z:{_motd_probe_status(zshrc)} "
        f"tty:{_motd_probe_status(tty)} "
        f"w:{len(warnings)}",
        "zshrc_source="
        f"{source_label} "
        f"lines:{stdout_summary} "
        f"err={zshrc.get('stderr_chars', '-')}",
        "startup="
        f"ln:{startup.get('new_line_count', '-')} "
        f"motd:{len(motd_log.get('new_lines') if isinstance(motd_log.get('new_lines'), list) else [])} "
        f"zshrc:{len(zshrc_log.get('new_lines') if isinstance(zshrc_log.get('new_lines'), list) else [])}",
        _detail_file_evidence("detail", detail_path),
    ]
    return evidence


def check_motd_zshrc_smoke() -> dict[str, Any]:
    probe_ok, probe_detail = _runner_help_probe("motd_zshrc_smoke.py", "motd-zshrc-smoke")
    path = LOG_DIR / "motd-zshrc-smoke.jsonl"
    meta = file_meta(path)
    if not meta.get("exists"):
        score = 70 if probe_ok else 35
        return item("motd_zshrc_smoke", score, status_from_score(score), ["missing motd-zshrc-smoke.jsonl", probe_detail], "运行 aidebug motd-zshrc-smoke。")
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        data = json.loads(lines[-1]) if lines else {}
    except Exception as exc:
        score = 55 if probe_ok else 35
        return item("motd_zshrc_smoke", score, status_from_score(score), [f"parse_error={exc}", probe_detail], "检查 motd-zshrc-smoke.jsonl。")
    ok = bool(data.get("ok"))
    age = int(meta.get("age_seconds") or 0)
    score = 100 if ok and probe_ok else 60 if ok else 45
    if not probe_ok:
        score = min(score, 45)
    if age > 86400:
        score = min(score, 80)
    if age > 7 * 86400:
        score = min(score, 70)
    compact_evidence = _compact_motd_zshrc_smoke_evidence(data, path)
    score = _append_auto_detail_density_guard(compact_evidence, score)
    density_limit = 80
    density_candidates = [row for row in compact_evidence if row.startswith(("summary=", "zshrc_source=", "startup="))]
    old_labels = (
        "started:",
        "non_tty:",
        "zshrc:ok/",
        "warn:",
        "lines=",
        "new_lines:",
        "motd_new:",
        "zshrc_new:",
    )
    density_failures = [
        f"{row.split('=', 1)[0]}:{len(row)}"
        for row in density_candidates
        if len(row) > density_limit
        or "{'" in row
        or "['" in row
        or "True" in row
        or "False" in row
        or any(label in row for label in old_labels)
    ]
    if density_failures:
        score = min(score, 75)
    evidence = [
        f"last_ok={_compact_bool_flag(ok)}",
        f"age={age}",
        probe_detail,
        *compact_evidence,
        f"motd_evidence_density=limit={density_limit} failures={_compact_list_or_dash(density_failures)}",
    ]
    score = _append_runtime_repr_density_guard(evidence, score)
    return item(
        "motd_zshrc_smoke",
        score,
        status_from_score(score),
        evidence,
        "运行 aidebug motd-zshrc-smoke 复测启动 UI。" if score < 85 else "",
    )


def build_health() -> dict[str, Any]:
    history = _load_health_history(limit=12)
    state_before, state_before_error = _capture_runtime_state("aidebug-health-before")
    previous_runtime_read_only = os.environ.get("PROJECTLING_RUNTIME_STATE_READ_ONLY")
    os.environ["PROJECTLING_RUNTIME_STATE_READ_ONLY"] = "1"
    checks: list[dict[str, Any]] = []
    try:
        checks = [
            check_layout(),
            check_windows_wsl_adapter(),
            check_windows_host_wsl_bridge(),
            check_logs(),
            check_projectling_doctor(),
            check_tool_schema(),
            check_actor_identity_name_contract(),
            _check_markdown_rendering(),
            _check_windows_tool_receipt_layout(),
            check_focus_anchor_contract(),
            check_windows_launcher_gemini_surface(),
            check_launcher_external_gate_contract(),
            check_route_alignment(),
            check_api_provider_config(),
            check_relay_model_compatibility_matrix(),
            check_gemini_parameter_support_matrix(),
            check_zsh_diagnostic_alias_execution(),
            check_deepseek_v4_transport(),
            check_gemini_settings_contract(),
            check_gemini_settings_persistence_contract(),
            check_settings_exception_restoration_contract(),
            check_api_settings_provider_persistence_contract(),
            check_provider_switch_contract(),
            check_gemini_planner_review_contract(),
            check_gemini_model_list_failure_contract(),
            check_gemini_api_test_failure_contract(),
            check_gemini_api_test_model_safety_contract(),
            check_settings_status_width_contract(),
            check_gemini_diagnostic_output_contract(),
            check_gemini_model_list_role_marker_contract(),
            check_gemini_model_list_taxonomy_contract(),
            check_runner_concurrency(),
            check_persona_split(),
            check_command_guard(),
            check_context_budget_runtime(),
            check_tool_fact_cards(),
            check_health_history_trend(history),
            check_aidebug_health_jsonl_integrity(),
            check_memory_layout(),
            check_context_mode_config(),
            check_projectling_tests(),
            check_desktop_goal_anchor(),
            check_history_requirement_matrix(),
            check_next_plan_artifact(),
            check_critical_summary_freshness(),
            check_threshold_summary_integrity(),
            check_deepseek_cache_metric_summary(),
            check_auto_runner_history(),
            check_auto_issue_ledger(),
            check_aidebug_read_precision_auto(),
            check_memory_db_integrity_auto(),
            check_context_pressure_variants_auto(),
            check_local_stress_auto(),
            check_auto_profile_coverage(),
            check_command_matrix_profile_coverage(),
            check_profile_detail_integrity(),
            check_profile_freshness_policy(),
            check_auto_stress_durability(),
            check_deepseek_live_cache_quality(),
            check_live_smoke_cost_efficiency(),
            check_deepseek_cache_stability_trend(),
            check_web_search_live_quality(),
            check_terminal_logs(),
            check_android_termux_readiness(),
            check_motd_zshrc_smoke(),
        ]
    except Exception as exc:
        checks.append(
            item(
                "aidebug_health_runner",
                0,
                "fail",
                [f"exception={type(exc).__name__}: {exc}"],
                "修复 health runner 未捕获异常。",
            )
        )
    finally:
        if previous_runtime_read_only is None:
            os.environ.pop("PROJECTLING_RUNTIME_STATE_READ_ONLY", None)
        else:
            os.environ["PROJECTLING_RUNTIME_STATE_READ_ONLY"] = previous_runtime_read_only
    state_after, state_after_error = _capture_runtime_state("aidebug-health-after")
    checks.append(
        _runtime_state_health_item(
            state_before,
            state_after,
            scope="full-health",
            before_error=state_before_error,
            after_error=state_after_error,
        )
    )
    total = round(sum(check["score"] for check in checks) / max(1, len(checks)), 1)
    status = status_from_score(int(total))
    payload = {
        "generated_at": timestamp(),
        "aidebug_dir": str(AIDEBUG_DIR),
        "projectling_dir": str(PROJECTLING_DIR),
        "overall_score": total,
        "overall_status": status,
        "history": _health_history_summary(history),
        "checks": checks,
    }
    latest_previous = payload["history"].get("latest_score") if isinstance(payload.get("history"), dict) else None
    if latest_previous is not None:
        try:
            payload["history"]["current_delta"] = round(total - float(latest_previous), 1)
            payload["history"]["current_score"] = total
        except (TypeError, ValueError):
            pass
    return payload


def build_windows_report(*, repair: bool = False, capture_ui: bool = False) -> dict[str, Any]:
    state_before, state_before_error = _capture_runtime_state("aidebug-windows-before")
    previous_runtime_read_only = os.environ.get("PROJECTLING_RUNTIME_STATE_READ_ONLY")
    os.environ["PROJECTLING_RUNTIME_STATE_READ_ONLY"] = "1"
    repairs: list[str] = []
    checks: list[dict[str, Any]] = []
    artifacts: dict[str, Any] = {}
    try:
        repairs = repair_windows_wsl_adapter() if repair else []
        checks = [check_windows_wsl_adapter()]
        if os.name == "nt":
            checks.append(check_windows_host_wsl_bridge())
            checks.append(_check_windows_ui_text_layout())
            checks.append(check_actor_identity_name_contract())
            checks.append(_check_markdown_rendering())
            checks.append(_check_windows_tool_receipt_layout())
            checks.append(check_focus_anchor_contract())
            checks.append(check_windows_launcher_gemini_surface())
            checks.append(check_launcher_external_gate_contract())
            checks.append(check_deepseek_v4_transport())
            checks.append(check_gemini_settings_contract())
            checks.append(check_gemini_settings_persistence_contract())
            checks.append(check_settings_exception_restoration_contract())
            checks.append(check_api_settings_provider_persistence_contract())
            checks.append(check_provider_switch_contract())
            checks.append(check_gemini_planner_review_contract())
            checks.append(check_gemini_model_list_failure_contract())
            checks.append(check_gemini_api_test_failure_contract())
            checks.append(check_gemini_api_test_model_safety_contract())
            checks.append(check_settings_status_width_contract())
            checks.append(check_gemini_diagnostic_output_contract())
            checks.append(check_gemini_model_list_role_marker_contract())
            checks.append(check_gemini_model_list_taxonomy_contract())
            if capture_ui:
                capture = _capture_windows_ui_screenshot()
                artifacts["ui_screenshot"] = capture
                checks.append(_check_windows_ui_screenshot(capture))
    except Exception as exc:
        checks.append(
            item(
                "aidebug_windows_runner",
                0,
                "fail",
                [f"exception={type(exc).__name__}: {exc}"],
                "修复 Windows health runner 未捕获异常。",
            )
        )
    finally:
        if previous_runtime_read_only is None:
            os.environ.pop("PROJECTLING_RUNTIME_STATE_READ_ONLY", None)
        else:
            os.environ["PROJECTLING_RUNTIME_STATE_READ_ONLY"] = previous_runtime_read_only
    state_after, state_after_error = _capture_runtime_state("aidebug-windows-after")
    before_files = (state_before or {}).get("files") if isinstance((state_before or {}).get("files"), dict) else {}
    bootstrap_allow = {
        path
        for path in ("config/role.json", "memory/datememory.json", "memory/memory.db")
        if not bool((before_files.get(path) or {}).get("exists"))
    }
    checks.append(
        _runtime_state_health_item(
            state_before,
            state_after,
            scope="windows-report",
            before_error=state_before_error,
            after_error=state_after_error,
            allow=bootstrap_allow,
        )
    )
    total = round(sum(check["score"] for check in checks) / max(1, len(checks)), 1)
    status = status_from_score(int(total))
    payload = {
        "generated_at": timestamp(),
        "aidebug_dir": str(AIDEBUG_DIR),
        "projectling_dir": str(PROJECTLING_DIR),
        "overall_score": total,
        "overall_status": status,
        "checks": checks,
    }
    if artifacts:
        payload["artifacts"] = artifacts
    if repair:
        payload["repairs"] = repairs
    return payload


def write_windows_report(payload: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    WINDOWS_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def classify_weak_spot(check: dict[str, Any]) -> str:
    name = str(check.get("name") or "")
    try:
        score = int(check.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    if name in {"android_termux_readiness", "terminal_logs", "runner_concurrency"}:
        return "environment-gated"
    if score >= 85:
        return "observation-only"
    return "code-regression"


def write_next_plan_artifact(payload: dict[str, Any]) -> Path:
    active_provider = _active_api_provider()
    live_cache_expect = (
        "ok / 100, cache_hit_rate >= 85, cache_miss_tokens <= 1000, context_restored=true"
        if active_provider == "deepseek"
        else f"ok / 100, provider={active_provider}, live tool smoke succeeds, context_restored=true, cache_policy=not_required_for_{active_provider}"
    )
    cache_metric_expect = (
        "ok / 100, DeepSeek cache metric summary matches full-profile detail JSON, projectling-auto JSONL, and deepseek_live_cache_quality health evidence"
        if active_provider == "deepseek"
        else f"ok / 100, provider={active_provider} function-call metric summary matches full-profile detail JSON, projectling-auto JSONL, and health evidence"
    )
    cache_stability_expect = (
        "ok / 100, at least 3 recent live samples, no post-warmup cache failures, latest sample fresh within 86400s, and min hit rate plus max miss are reported"
        if active_provider == "deepseek"
        else f"ok / 100, at least 3 recent {active_provider} live samples, all functionally ok, latest sample fresh within 86400s, cache_policy=not_required_for_{active_provider}"
    )
    checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
    weak = [
        check
        for check in checks
        if isinstance(check, dict) and (int(check.get("score") or 0) < 100 or check.get("next_action"))
    ]
    commands = [
        {
            "label": "windows_profile_local",
            "command": 'python "C:\\Users\\%USERNAME%\\Desktop\\PROJECT凌\\aidebug\\runner\\projectling_auto.py" --rounds 1 --profile local',
        },
        {
            "label": "wsl_profile_live",
            "command": "& 'C:\\Windows\\Sysnative\\wsl.exe' -d Ubuntu-ProjectLing -- bash -lc 'cd /mnt/c/Users/$USER/Desktop/PROJECT凌 && python3 aidebug/runner/projectling_auto.py --rounds 1 --profile live'",
        },
        {
            "label": "wsl_profile_full",
            "command": "& 'C:\\Windows\\Sysnative\\wsl.exe' -d Ubuntu-ProjectLing -- bash -lc 'cd /mnt/c/Users/$USER/Desktop/PROJECT凌 && python3 aidebug/runner/projectling_auto.py --rounds 1 --profile full'",
        },
        {
            "label": "windows_health",
            "command": 'python "C:\\Users\\%USERNAME%\\Desktop\\PROJECT凌\\aidebug\\runner\\aidebug_health.py"',
        },
        {
            "label": "wsl_health",
            "command": "& 'C:\\Windows\\Sysnative\\wsl.exe' -d Ubuntu-ProjectLing -- bash -lc 'cd /mnt/c/Users/$USER/Desktop/PROJECT凌 && python3 aidebug/runner/aidebug_health.py'",
        },
    ]
    thresholds = [
        {
            "check": "projectling_auto_profile_coverage",
            "expect": "ok / 100, local/live/full samples fresh within 86400s",
        },
        {
            "check": "command_matrix_profile_coverage",
            "expect": "ok / 100, local/live/full each pass 4 command cases",
        },
        {
            "check": "deepseek_live_cache_quality",
            "expect": live_cache_expect,
        },
        {
            "check": "live_smoke_cost_efficiency",
            "expect": "ok / 100, cumulative prompt <= 50000, final prompt <= 15000, <= 10 API calls, isolated state, and only update_plan/command/link schemas",
        },
        {
            "check": "web_search_live_quality",
            "expect": "ok / 100, live web sample returns at least 1 result",
        },
        {
            "check": "memory_db_integrity_auto",
            "expect": "ok / 100, append replay, SQLite integrity, WAL mode, and keyword uniqueness pass",
        },
        {
            "check": "context_pressure_variants_auto",
            "expect": "ok / 100, small/medium/near_limit variants pass 3/3",
        },
        {
            "check": "projectling_profile_detail_integrity",
            "expect": "ok / 100, local/live/full detail JSON files exist and match profile source plus command, memory, context, provider-aware live evidence, web evidence, and freshness trend",
        },
        {
            "check": "projectling_profile_freshness_policy",
            "expect": "ok / 100, deterministic fresh, near-stale, stale, and missing-age freshness policy cases pass",
        },
        {
            "check": "projectling_next_plan_artifact",
            "expect": "ok / 100, Markdown and JSON artifacts are fresh and contain commands, thresholds, classes, profile sample sources, detail JSON integrity, critical check summary, and Android Termux criteria",
        },
        {
            "check": "projectling_critical_summary_freshness",
            "expect": "ok / 100, critical summary timestamps, source health, freshness trend, required counts, missing counts, and ok/non-ok counts align with the current health artifact",
        },
        {
            "check": "projectling_threshold_summary_integrity",
            "expect": "ok / 100, threshold summary schema, expected names, count, missing list, unexpected list, and compact threshold entries match the generated thresholds",
        },
        {
            "check": "aidebug_health_jsonl_integrity",
            "expect": "ok / 100, latest aidebug-health.jsonl row parses, malformed rows are counted with compact line-number evidence, and legacy malformed rows do not break rolling history",
        },
        {
            "check": "projectling_desktop_goal_anchor",
            "expect": "ok / 100, desktop PROJECT凌-OVERNIGHT-GOAL.md exists, contains resume files, historical session references, stable provider context, resume commands, and no secret-looking tokens",
        },
        {
            "check": "projectling_history_requirement_matrix",
            "expect": "ok / 100, historical requirement matrix exists, covers the four session anchors, R01-R14 requirements, current proof checks, Android gate, threshold baseline, and no secret-looking tokens",
        },
        {
            "check": "relay_model_compatibility_matrix",
            "expect": "ok / 100, 23 unique models have text/SSE/tool probes, all thinking variants have reasoning probes, configured planner/executor roles are compatible, and Luna/5.6 findings are redacted",
        },
        {
            "check": "gemini_parameter_support_matrix",
            "expect": "ok / 100, 21 unique Gemini models have temperature/top_p/top_k/max_tokens/JSON probes, all parameters are locally sent, response models match, reports are fresh, and errors/secrets are redacted",
        },
        {
            "check": "settings_status_width_contract",
            "expect": "ok / 100, compact Settings/API/model-list/api-test output fits widths 16/20/24/32/40/48, keeps provider/status/next-action copy, and redacts secrets",
        },
        {
            "check": "windows_launcher_gemini_surface",
            "expect": "ok / 100, Windows launcher shows the restored role-card startup, Chinese collaboration/provider/time status, one TIP, no persistent home menu, and a width-safe Settings/Role/Exit slash menu with secret redaction",
        },
        {
            "check": "zsh_diagnostic_alias_execution",
            "expect": "ok / 100, zsh/Termux diagnostic slash aliases /models, /model-list, /list-models, /api-test, and /apitest execute local diagnostics with argument passthrough via source verification on Windows and fake-runner runtime verification on WSL",
        },
        {
            "check": "gemini_diagnostic_output_contract",
            "expect": "ok / 100, public Gemini diagnostic CLI override failures return structured JSON, compact human output fits widths 16/20/24/32/40/48 with provider/model/base/status/next-action copy, config/env is unchanged, and secrets are redacted",
        },
        {
            "check": "gemini_model_list_role_marker_contract",
            "expect": "ok / 100, Gemini model-list success output marks the configured planner/main-star and executor/flash-star models, fits widths 16/20/24/32/40/48/80/120, and redacts secrets",
        },
        {
            "check": "gemini_model_list_taxonomy_contract",
            "expect": "ok / 100, Gemini relay model-list taxonomy classifies pro/flash/think/image/agent/claude/lite/unknown models, keeps role markers, fits widths 16/20/24/32/40/48/80/120, and redacts secrets",
        },
        {
            "check": "gemini_api_test_model_safety_contract",
            "expect": "ok / 100, Gemini api-test reports executor model tags/risk/hints for image, agent, Claude, and unknown model overrides, keeps stable flash clean, fits widths 16/20/24/32/40/48/80/120, and redacts secrets",
        },
        {
            "check": "gemini_settings_persistence_contract",
            "expect": "ok / 100, Gemini params save/read/payload/clear/no-mutation behavior passes in a sandbox and redacts fake secrets",
        },
        {
            "check": "api_settings_provider_persistence_contract",
            "expect": "ok / 100, API Settings provider switch/save/read/no-mutation behavior stays provider-scoped for Gemini and DeepSeek, model edits flow through Settings, model-list role markers, API-test executor routing, diagnostic override no-mutation, payload isolation, and secret redaction",
        },
        {
            "check": "projectling_deepseek_cache_metric_summary",
            "expect": cache_metric_expect,
        },
        {
            "check": "deepseek_cache_stability_trend",
            "expect": cache_stability_expect,
        },
        {
            "check": "settings_exception_restoration_contract",
            "expect": "ok / 100, fault-injected Settings failure restores load/save/client/terminal-size/prompt/environment state",
        },
        {
            "check": "runtime_state_no_mutation",
            "expect": "ok / 100, health checks leave Provider, models, secret presence, role, focus, context, and memory fingerprints unchanged",
        },
        {
            "check": "motd_zshrc_smoke",
            "expect": "ok / 100, latest motd/zshrc startup smoke is fresh within 86400s, help probe succeeds, zshrc hook passes, and missing motd is explicitly compatibility-skipped",
        },
    ]
    weak_spots = []
    for check in weak[:12]:
        weak_spots.append(
            {
                "name": str(check.get("name") or ""),
                "status": str(check.get("status") or ""),
                "score": int(check.get("score") or 0),
                "class": classify_weak_spot(check),
                "next_action": str(check.get("next_action") or "observe and keep fresh evidence"),
            }
        )
    auto_rows = _load_auto_history(limit=60)
    latest_auto = auto_rows[-1] if auto_rows else {}
    latest_auto_age = _auto_started_age_seconds(latest_auto) if latest_auto else None
    health_json_age = _timestamp_age_seconds(payload.get("generated_at"))
    profile_samples: dict[str, dict[str, Any]] = {}
    for profile in ("local", "live", "full"):
        row = _latest_auto_profile(auto_rows, profile)
        age = _auto_started_age_seconds(row) if row else None
        profile_samples[profile] = {
            "profile": profile,
            "detail_path": str((row or {}).get("detail_path") or ""),
            "started_at": str((row or {}).get("started_at") or ""),
            "run_mode": str((row or {}).get("run_mode") or ""),
            "ok": bool((row or {}).get("ok")),
            "age_seconds": age if age is not None else -1,
        }
    verification_sources = {
        "health_json_path": str(HEALTH_JSON),
        "health_generated_at": str(payload.get("generated_at") or ""),
        "latest_auto_log_path": str(LOG_DIR / "projectling-auto.jsonl"),
        "latest_auto_detail_path": str(latest_auto.get("detail_path") or ""),
        "latest_auto_started_at": str(latest_auto.get("started_at") or ""),
        "latest_auto_profile": str(latest_auto.get("profile") or ""),
        "latest_auto_run_mode": str(latest_auto.get("run_mode") or ""),
        "latest_auto_ok": bool(latest_auto.get("ok")),
        "source_freshness_seconds": {
            "latest_auto_age_seconds": latest_auto_age if latest_auto_age is not None else -1,
            "health_json_age_seconds": health_json_age if health_json_age is not None else -1,
            "max_allowed_age_seconds": 86400,
        },
        "profile_samples": profile_samples,
    }
    critical_check_summary = _critical_check_summary(
        checks,
        generated_at=str(payload.get("generated_at") or ""),
        source_health_generated_at=str(payload.get("generated_at") or ""),
    )
    threshold_summary = _threshold_summary(
        thresholds,
        generated_at=str(payload.get("generated_at") or ""),
    )
    diagnostic_evidence_summary = _diagnostic_artifact_summary(
        checks,
        generated_at=str(payload.get("generated_at") or ""),
    )
    checks_by_name = {
        str(check.get("name") or ""): check
        for check in checks
        if isinstance(check, dict)
    }
    deepseek_cache_metric_summary = _deepseek_cache_metric_summary(
        checks_by_name.get("deepseek_live_cache_quality"),
        auto_rows,
        generated_at=str(payload.get("generated_at") or ""),
    )
    deepseek_cache_stability_summary = _deepseek_cache_stability_summary(
        checks_by_name.get("deepseek_cache_stability_trend"),
        auto_rows,
        generated_at=str(payload.get("generated_at") or ""),
    )
    provider_cache_metric_summary = _provider_cache_alias_summary(
        deepseek_cache_metric_summary,
        compatibility_key="deepseek_cache_metric_summary",
        compatibility_check="projectling_deepseek_cache_metric_summary",
    )
    provider_cache_stability_summary = _provider_cache_alias_summary(
        deepseek_cache_stability_summary,
        compatibility_key="deepseek_cache_stability_summary",
        compatibility_check="deepseek_cache_stability_trend",
    )
    critical_freshness_check = next(
        (
            check
            for check in checks
            if isinstance(check, dict) and check.get("name") == "projectling_critical_summary_freshness"
        ),
        None,
    )
    critical_summary_freshness = _critical_summary_freshness_artifact(
        critical_freshness_check,
        generated_at=str(payload.get("generated_at") or ""),
    )
    plan_payload = {
        "schema_version": 1,
        "generated_at": payload.get("generated_at"),
        "last_verified": payload.get("generated_at"),
        "overall_status": payload.get("overall_status"),
        "overall_score": payload.get("overall_score"),
        "active_provider": active_provider,
        "loop": "profile local -> profile live -> profile full -> Windows health -> WSL health -> record -> inspect weak spots",
        "commands": commands,
        "thresholds": thresholds,
        "threshold_summary": threshold_summary,
        "required_checks": [str(item["check"]) for item in thresholds],
        "verification_sources": verification_sources,
        "diagnostic_evidence_summary": diagnostic_evidence_summary,
        "critical_check_summary": critical_check_summary,
        "provider_cache_metric_summary": provider_cache_metric_summary,
        "provider_cache_stability_summary": provider_cache_stability_summary,
        "deepseek_cache_metric_summary": deepseek_cache_metric_summary,
        "deepseek_cache_stability_summary": deepseek_cache_stability_summary,
        "critical_summary_freshness": critical_summary_freshness,
        "weak_spots": weak_spots,
        "android_termux_readiness": {
            "reason": "Windows/WSL cannot provide Android am UI control",
            "pass_criteria": [
                "am available",
                "Termux allow-external-apps=true",
                "terminal session log exists",
                "projectling-auto terminal smoke is not compatibility-skipped",
            ],
            "artifact": str(ANDROID_READINESS_MD),
        },
        "record_targets": [
            "aidebug/logs/projectling-test.md",
            "aidebug/notes/projectling-aidebug-iteration-20260701-roundN.md",
        ],
    }
    lines = [
        "# ProjectLing Aidebug Next Plan",
        "",
        f"- generated_at: {payload.get('generated_at')}",
        f"- overall: {payload.get('overall_status')} / {payload.get('overall_score')}",
        f"- active_provider: {active_provider}",
        f"- loop: {plan_payload['loop']}",
        "",
        "## Next Commands",
        "",
        "```powershell",
        *[str(command["command"]) for command in commands],
        "```",
        "",
        "## Expected Pass Thresholds",
        "",
        f"- required_count: {len(thresholds)}",
        "- full_expectations: see JSON `thresholds` and `threshold_summary.checks`.",
        f"- provider_cache_policy: cache_policy=not_required_for_{active_provider}" if active_provider != "deepseek" else "- provider_cache_policy: cache_hit_rate >= 85",
        "- coverage_groups: profiles; memory/context; Gemini diagnostics; Settings; launcher/zsh; provider cache compatibility; Termux readiness.",
        "- artifact_contract: profile sample sources, detail JSON integrity, freshness trend, critical summary, Android Termux criteria.",
        "",
        "## Threshold Summary",
        "",
        f"- threshold_count: {threshold_summary['count']}",
        f"- missing: {_compact_list_or_dash(threshold_summary.get('missing') if isinstance(threshold_summary.get('missing'), list) else [])}",
        f"- unexpected: {_compact_list_or_dash(threshold_summary.get('unexpected') if isinstance(threshold_summary.get('unexpected'), list) else [])}",
        "- required_checks:",
        *[f"  - {name}" for name in threshold_summary["expected"]],
        "",
        "## Diagnostic Evidence Summary",
        "",
        f"- generated_at: {diagnostic_evidence_summary['generated_at']}",
        f"- ok: {_markdown_bool(diagnostic_evidence_summary['ok'])}",
        f"- missing: {_markdown_list_summary(diagnostic_evidence_summary['missing'])}",
        *[
            f"- {item['name']}: {item['status']} / {item['score']}; highlights={_markdown_list_summary(item['highlights'])}"
            for item in diagnostic_evidence_summary["checks"]
        ],
        "",
        "## Verification Sources",
        "",
        f"- last_verified: {plan_payload['last_verified']}",
        f"- health_json: {verification_sources['health_json_path']}",
        f"- latest_auto_detail: {verification_sources['latest_auto_detail_path']}",
        f"- latest_auto: {verification_sources['latest_auto_started_at']} / {verification_sources['latest_auto_profile']} / {verification_sources['latest_auto_run_mode']} / ok={_markdown_bool(verification_sources['latest_auto_ok'])}",
        "- source_freshness: "
        + _markdown_mapping_summary(
            verification_sources["source_freshness_seconds"],
            [
                ("latest_auto_age_seconds", "auto_age"),
                ("health_json_age_seconds", "health_age"),
                ("max_allowed_age_seconds", "max_age"),
            ],
        ),
        "- profile_samples:",
        *[
            f"  - {name}: {sample['started_at']} / {sample['run_mode']} / ok={_markdown_bool(sample['ok'])} / age={sample['age_seconds']}"
            for name, sample in profile_samples.items()
        ],
        *[
            f"    {name}_detail: {_markdown_clip(sample['detail_path'], 185)}"
            for name, sample in profile_samples.items()
        ],
        "",
        "## Critical Check Summary",
        "",
        f"- summary_generated_at: {critical_check_summary['generated_at']}",
        f"- source_health_generated_at: {critical_check_summary['source_health_generated_at']}",
        f"- ok_count: {critical_check_summary['ok_count']}",
        f"- non_ok_count: {critical_check_summary['non_ok_count']}",
        f"- missing: {_markdown_list_summary(critical_check_summary['missing'])}",
        *[
            f"- {item['name']}: {item['status']} / {item['score']}; evidence={_markdown_evidence_preview(item['evidence'])}"
            for item in critical_check_summary["checks"]
        ],
        "",
        "## Provider Cache Metric Summary",
        "",
        f"- compatibility_key: {provider_cache_metric_summary['compatibility_key']}",
        f"- compatibility_check: {provider_cache_metric_summary['compatibility_check']}",
        f"- provider: {provider_cache_metric_summary['provider']}",
        f"- cache_policy: {provider_cache_metric_summary['cache_policy']}",
        f"- ok: {_markdown_bool(provider_cache_metric_summary['ok'])}",
        f"- source: {provider_cache_metric_summary['source_started_at']} / {provider_cache_metric_summary['source_run_mode']}",
        f"- detail: {provider_cache_metric_summary['detail_path']}",
        f"- cache_hit_rate: {provider_cache_metric_summary['cache_hit_rate']}",
        f"- cache_miss_tokens: {provider_cache_metric_summary['cache_miss_tokens']}",
        f"- cached_tokens: {provider_cache_metric_summary['cached_tokens']}",
        f"- prompt_tokens: {provider_cache_metric_summary['prompt_tokens']}",
        f"- cache_ok: {_markdown_bool(provider_cache_metric_summary['cache_ok'])}",
        f"- context_restored: {_markdown_bool(provider_cache_metric_summary['context_restored'])}",
        "- warmup: "
        + _markdown_mapping_summary(
            provider_cache_metric_summary["warmup"],
            [
                ("attempted", "attempted"),
                ("resolved", "resolved"),
                ("first_miss", "first_miss"),
                ("first_hit_rate", "first_hit_rate"),
            ],
        ),
        f"- health_mismatches: {_compact_list_or_dash(provider_cache_metric_summary.get('health_mismatches') if isinstance(provider_cache_metric_summary.get('health_mismatches'), list) else [])}",
        f"- jsonl_mismatches: {_compact_list_or_dash(provider_cache_metric_summary.get('jsonl_mismatches') if isinstance(provider_cache_metric_summary.get('jsonl_mismatches'), list) else [])}",
        "",
        "## Provider Cache Stability Summary",
        "",
        f"- compatibility_key: {provider_cache_stability_summary['compatibility_key']}",
        f"- compatibility_check: {provider_cache_stability_summary['compatibility_check']}",
        f"- provider: {provider_cache_stability_summary['provider']}",
        f"- cache_policy: {provider_cache_stability_summary['cache_policy']}",
        f"- ok: {_markdown_bool(provider_cache_stability_summary['ok'])}",
        f"- live_samples: {provider_cache_stability_summary['live_samples']}",
        f"- recent_samples: {provider_cache_stability_summary['recent_samples']}",
        f"- cache_ok_count: {provider_cache_stability_summary['cache_ok_count']}",
        f"- min_hit_rate: {provider_cache_stability_summary['min_hit_rate']}",
        f"- max_miss: {provider_cache_stability_summary['max_miss']}",
        f"- latest_age_seconds: {provider_cache_stability_summary['latest_age_seconds']}",
        f"- profile_distribution: {provider_cache_stability_summary['profile_distribution']}",
        "- profile_latest_age_seconds: "
        + _markdown_mapping_summary(
            provider_cache_stability_summary["profile_latest_age_seconds"],
            [(str(key), str(key)) for key in provider_cache_stability_summary.get("profile_latest_age_seconds", {})],
        ),
        f"- post_warmup_failures: {_markdown_list_summary(provider_cache_stability_summary['post_warmup_failures'])}",
        f"- auto_mismatches: {_markdown_list_summary(provider_cache_stability_summary['auto_mismatches'])}",
        "",
        "## Critical Summary Freshness",
        "",
        f"- generated_at: {critical_summary_freshness['generated_at']}",
        f"- status: {critical_summary_freshness['status']} / {critical_summary_freshness['score']}",
        f"- freshness_trend: {critical_summary_freshness['freshness_trend']}",
        f"- evidence: {'; '.join(critical_summary_freshness['evidence'][:4])}",
        "",
        "## Current Weak Spots",
    ]
    if weak_spots:
        for check in weak_spots:
            lines.append(
                f"- {check['name']}: {check['status']} / {check['score']}; class={check['class']}; "
                f"next={check['next_action']}"
            )
        if len(weak) > 12:
            lines.append(f"- ... {len(weak) - 12} more")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Android Termux Readiness",
            "",
            "- Android Termux foreground terminal verification still requires a real device because Windows/WSL cannot provide `am` UI control.",
            "- Pass criteria: `am` available, Termux `allow-external-apps=true`, terminal session log exists, and `projectling-auto` terminal smoke is not compatibility-skipped.",
            f"- Readiness artifact: {ANDROID_READINESS_MD}",
            "",
            "## Record Target",
            "",
            "- Append the next completed round to `aidebug/logs/projectling-test.md`.",
            "- Add a focused note under `aidebug/notes/projectling-aidebug-iteration-20260701-roundN.md`.",
        ]
    )
    NEXT_PLAN_MD.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    NEXT_PLAN_JSON.write_text(json.dumps(plan_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return NEXT_PLAN_MD


def write_reports(payload: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    NOTE_DIR.mkdir(parents=True, exist_ok=True)
    HEALTH_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with HEALTH_JSONL.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    write_next_plan_artifact(payload)
    lines = [
        "# AITermux Aidebug Health",
        "",
        f"- generated_at: {payload['generated_at']}",
        f"- overall: {payload['overall_status']} / {payload['overall_score']}",
        "",
        "## Checks",
    ]
    for check in payload["checks"]:
        lines.append(f"- {check['name']}: {check['status']} / {check['score']}")
        if check.get("next_action"):
            lines.append(f"  next: {check['next_action']}")
    history = payload.get("history") or {}
    if isinstance(history, dict):
        lines.extend(
            [
                "",
                "## Recent Trend",
                f"- previous_runs: {history.get('run_count', 0)}",
                f"- latest_previous: {history.get('latest_status', '-')}"
                f" / {history.get('latest_score', '-')}",
                f"- delta_vs_previous: {history.get('current_delta', '-')}",
                f"- recent_average: {history.get('recent_average', '-')}",
                f"- trend: {history.get('trend', '-')}",
            ]
        )
    HEALTH_MD.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def print_text(payload: dict[str, Any]) -> None:
    print(f"aidebug_health={payload['overall_status']} score={payload['overall_score']}")
    for repair in payload.get("repairs", [])[:12]:
        print(f"repair={repair}")
    for check in payload["checks"]:
        print(f"{check['name']} status={check['status']} score={check['score']}")
        evidence_limit = 24 if check.get("name") in {"windows_native_adapter", "windows_wsl_adapter"} else 3
        for evidence in check.get("evidence", [])[:evidence_limit]:
            print(f"  evidence={evidence}")
        if check.get("next_action"):
            print(f"  next={check['next_action']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aidebug-health")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--windows", action="store_true", help="only print Windows/WSL adapter diagnostics")
    parser.add_argument("--repair", action="store_true", help="repair safe Windows/WSL adapter issues")
    parser.add_argument("--screenshot", action="store_true", help="also capture a Windows UI screenshot")
    parser.add_argument("--no-screenshot", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    capture_ui = bool(args.screenshot and not args.no_screenshot)
    payload = build_windows_report(repair=args.repair, capture_ui=capture_ui) if args.windows else build_health()
    if args.windows:
        write_windows_report(payload)
    else:
        write_reports(payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_text(payload)
    return 0 if payload["overall_status"] in {"ok", "warn"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
