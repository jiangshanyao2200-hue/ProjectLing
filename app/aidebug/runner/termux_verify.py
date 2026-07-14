from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_force_utf8_stdio()

SCRIPT_PATH = Path(__file__).resolve()
SCRIPT_AIDEBUG_DIR = SCRIPT_PATH.parents[1]
PROJECTLING_ROOT = SCRIPT_AIDEBUG_DIR.parent
if os.environ.get("PROJECTLING_DIR"):
    PROJECTLING_DIR = Path(os.environ["PROJECTLING_DIR"]).expanduser()
elif (PROJECTLING_ROOT / "core.py").is_file():
    PROJECTLING_DIR = PROJECTLING_ROOT
elif (PROJECTLING_ROOT / "app" / "core.py").is_file():
    PROJECTLING_DIR = PROJECTLING_ROOT / "app"
else:
    PROJECTLING_DIR = PROJECTLING_ROOT
RELEASE_ROOT = PROJECTLING_DIR.parent if PROJECTLING_DIR.name == "app" else PROJECTLING_DIR
AIDEBUG_DIR = Path(os.environ.get("AITERMUX_AIDEBUG_DIR", str(SCRIPT_AIDEBUG_DIR))).expanduser()
AIDEBUG_CODE_DIR = Path(os.environ.get("AIDEBUG_CODE_DIR", str(SCRIPT_AIDEBUG_DIR))).expanduser()
if not (AIDEBUG_CODE_DIR / "runner").is_dir():
    AIDEBUG_CODE_DIR = SCRIPT_AIDEBUG_DIR
AITERMUX_HOME = Path(os.environ.get("AITERMUX_HOME", str(RELEASE_ROOT.parent))).expanduser()
LOG_DIR = AIDEBUG_DIR / "logs"
NOTE_DIR = AIDEBUG_DIR / "notes"
REPORT_JSON = LOG_DIR / "termux-verification.json"
REPORT_JSONL = LOG_DIR / "termux-verification.jsonl"
REPORT_MD = NOTE_DIR / "termux-verification.md"
AUTO_JSONL = LOG_DIR / "projectling-auto.jsonl"

sys.path.insert(0, str(SCRIPT_PATH.parent))
from runtime_state_guard import build_snapshot, compare_snapshots  # noqa: E402


SECRET_RE = re.compile(
    r"(?i)(?:ghp_[A-Za-z0-9_]{10,}|sk-[A-Za-z0-9_-]{10,}|AIza[0-9A-Za-z_-]{10,}|Bearer\s+[A-Za-z0-9._-]{10,})"
)


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_preview(text: str, limit: int = 800) -> str:
    compact = "\n".join(line.rstrip() for line in str(text or "").splitlines()[-12:])
    return SECRET_RE.sub("<redacted>", compact)[-limit:]


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "AITERMUX_HOME": str(AITERMUX_HOME),
            "AITERMUX_AIDEBUG_DIR": str(AIDEBUG_DIR),
            "AIDEBUG_CODE_DIR": str(AIDEBUG_CODE_DIR),
            "PROJECTLING_DIR": str(PROJECTLING_DIR),
            "PROJECTLING_HOME": str(PROJECTLING_DIR),
            "PROJECTLING_RUNTIME_STATE_READ_ONLY": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


def _run(name: str, command: list[str], *, timeout: int) -> tuple[dict[str, Any], subprocess.CompletedProcess[str] | None]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(RELEASE_ROOT),
            env=_base_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return (
            {
                "name": name,
                "ok": False,
                "returncode": None,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "error": "timeout",
                "stderr_preview": _safe_preview(exc.stderr or ""),
            },
            None,
        )
    except OSError as exc:
        return (
            {
                "name": name,
                "ok": False,
                "returncode": None,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "error": f"{type(exc).__name__}: {exc}",
            },
            None,
        )
    result = {
        "name": name,
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "stdout_chars": len(completed.stdout or ""),
        "stderr_chars": len(completed.stderr or ""),
    }
    if completed.returncode != 0:
        result["stdout_preview"] = _safe_preview(completed.stdout or "")
        result["stderr_preview"] = _safe_preview(completed.stderr or "")
    return result, completed


def _json_payload(completed: subprocess.CompletedProcess[str] | None) -> dict[str, Any]:
    if completed is None:
        return {}
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _latest_auto_row() -> dict[str, Any]:
    try:
        lines = AUTO_JSONL.read_text(encoding="utf-8", errors="replace").splitlines()
        payload = json.loads(lines[-1]) if lines else {}
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_report(payload: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    NOTE_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with REPORT_JSONL.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    lines = [
        "# ProjectLing Termux Verification",
        "",
        f"- generated_at: {payload['generated_at']}",
        f"- profile: {payload['profile']}",
        f"- status: {'ok' if payload['ok'] else 'fail'}",
        f"- elapsed_seconds: {payload['elapsed_seconds']}",
        f"- runtime_state_guard: {payload['runtime_state_guard']['ok']}",
        "",
        "## Steps",
    ]
    for step in payload["steps"]:
        lines.append(
            f"- {step['name']}: {'ok' if step.get('ok') else 'fail'} "
            f"(rc={step.get('returncode')}, {step.get('elapsed_seconds')}s)"
        )
        if step.get("summary"):
            lines.append(f"  summary: {step['summary']}")
    REPORT_MD.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def verify(profile: str, *, include_motd: bool = True) -> dict[str, Any]:
    started = time.monotonic()
    before = build_snapshot(label=f"termux-verify-{profile}-before")
    steps: list[dict[str, Any]] = []

    result, completed = _run(
        "termux_install_check",
        ["bash", str(RELEASE_ROOT / "Termux" / "install.sh"), "--check", "--no-zshrc"],
        timeout=120,
    )
    if completed:
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        result["summary"] = lines[-1][:220] if lines else "-"
    steps.append(result)

    result, completed = _run("doctor", ["bash", str(RELEASE_ROOT / "run.sh"), "doctor"], timeout=90)
    doctor = _json_payload(completed)
    if doctor:
        result["summary"] = (
            f"provider={doctor.get('api_provider')} key={bool(doctor.get('api_key_configured'))} "
            f"tools={bool(doctor.get('allow_tools'))}"
        )
    steps.append(result)

    result, completed = _run("selftest", ["bash", str(RELEASE_ROOT / "run.sh"), "selftest"], timeout=240)
    if completed:
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        result["summary"] = next(
            (line[:240] for line in lines if line.startswith("ProjectLing selftest:")),
            lines[0][:240] if lines else "-",
        )
    steps.append(result)

    result, _completed = _run(
        f"projectling_auto_{profile}",
        ["bash", str(AIDEBUG_CODE_DIR / "bin" / "aidebug"), "projectling-auto", "--rounds", "1", "--profile", profile],
        timeout=600 if profile == "full" else 420,
    )
    auto = _latest_auto_row()
    result["summary"] = (
        f"profile={auto.get('profile')} mode={auto.get('run_mode')} ok={auto.get('ok')} "
        f"terminal={bool((auto.get('terminal') or {}).get('ok'))} "
        f"runtime_state={bool((auto.get('runtime_state') or {}).get('ok'))}"
    )
    result["ok"] = bool(result.get("ok") and auto.get("ok") is True and auto.get("profile") == profile)
    steps.append(result)

    if include_motd:
        result, completed = _run(
            "motd_zshrc_smoke",
            ["bash", str(AIDEBUG_CODE_DIR / "bin" / "aidebug"), "motd-zshrc-smoke", "--json"],
            timeout=180,
        )
        motd = _json_payload(completed)
        result["summary"] = (
            f"ok={motd.get('ok')} non_tty={bool((motd.get('non_tty_motd') or {}).get('ok'))} "
            f"zshrc={bool((motd.get('zshrc') or {}).get('ok'))} tty={bool((motd.get('tty_motd') or {}).get('ok'))}"
        )
        result["ok"] = bool(result.get("ok") and motd.get("ok") is True)
        steps.append(result)

    result, completed = _run(
        "aidebug_termux_health",
        ["bash", str(AIDEBUG_CODE_DIR / "bin" / "aidebug"), "termux", "--json"],
        timeout=360,
    )
    health = _json_payload(completed)
    result["summary"] = (
        f"status={health.get('overall_status')} score={health.get('overall_score')} "
        f"checks={len(health.get('checks') or [])}"
    )
    result["ok"] = bool(result.get("ok") and health.get("overall_status") == "ok")
    steps.append(result)

    after = build_snapshot(label=f"termux-verify-{profile}-after")
    state_guard = compare_snapshots(before, after)
    payload = {
        "generated_at": timestamp(),
        "profile": profile,
        "runtime": "android-termux",
        "projectling_dir": str(PROJECTLING_DIR),
        "aidebug_dir": str(AIDEBUG_DIR),
        "ok": bool(all(step.get("ok") for step in steps) and state_guard.get("ok")),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "steps": steps,
        "runtime_state_guard": {
            "ok": state_guard.get("ok"),
            "forbidden_changes": state_guard.get("forbidden_changes") or [],
            "semantic_changes": sorted((state_guard.get("semantic_changes") or {}).keys()),
            "secret_presence_changed": bool(state_guard.get("secret_presence_changed")),
        },
    }
    _write_report(payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aidebug verify-termux")
    parser.add_argument("--profile", choices=("local", "live", "full"), default="local")
    parser.add_argument("--skip-motd", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    payload = verify(args.profile, include_motd=not args.skip_motd)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"termux_verification={'ok' if payload['ok'] else 'fail'} "
            f"profile={payload['profile']} elapsed={payload['elapsed_seconds']}s"
        )
        for step in payload["steps"]:
            print(
                f"{step['name']}={'ok' if step.get('ok') else 'fail'} "
                f"rc={step.get('returncode')} summary={step.get('summary', '-')}"
            )
        print(f"runtime_state_guard={'ok' if payload['runtime_state_guard']['ok'] else 'fail'}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
