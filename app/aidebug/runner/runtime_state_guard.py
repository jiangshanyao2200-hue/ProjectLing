from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any


PROJECTLING_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = PROJECTLING_DIR / "aidebug" / "state" / "runtime-state"

WATCHED_PATHS = (
    "config/env",
    "config/role.json",
    "config/focus.json",
    "config/context-budget.json",
    "config/update-plan.json",
    "config/persona_links.json",
    "config/toolbox.json",
    "context/entries.jsonl",
    "context/shared_context.txt",
    "memory/datememory.json",
    "memory/memory.db",
)

SECRET_ENV_SUFFIXES = ("_API_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_AUTH")
SEMANTIC_ENV_KEYS = (
    "PROJECTLING_API_PROVIDER",
    "PROJECTLING_COLLAB_MODE",
    "GEMINI_BASE_URL",
    "GEMINI_PLANNER_MODEL",
    "GEMINI_EXECUTOR_MODEL",
    "GEMINI_REASONING_EFFORT",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_PLANNER_MODEL",
    "DEEPSEEK_EXECUTOR_MODEL",
    "DEEPSEEK_REASONING_EFFORT",
    "DEEPSEEK_ENABLE_SSE",
    "DEEPSEEK_TIMEOUT_SECONDS",
    "DEEPSEEK_RETRY_COUNT",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _file_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "size": 0, "sha256": ""}
    stat = path.stat()
    return {
        "exists": True,
        "size": stat.st_size,
        "sha256": _sha256(path) if path.is_file() else "",
    }


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = value.strip().strip('"').strip("'")
    return values


def _is_secret_key(name: str) -> bool:
    upper = name.upper()
    return upper.endswith(SECRET_ENV_SUFFIXES) or upper in {"API_KEY", "AUTH_TOKEN", "BEARER_TOKEN"}


def _json_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"valid": False, "keys": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {"valid": False, "keys": []}
    if not isinstance(payload, dict):
        return {"valid": True, "keys": [], "type": type(payload).__name__}
    return {
        "valid": True,
        "keys": sorted(str(key) for key in payload),
        "status": str(payload.get("status") or ""),
        "revision": payload.get("revision"),
    }


def build_snapshot(*, label: str = "") -> dict[str, Any]:
    env_path = PROJECTLING_DIR / "config" / "env"
    env = _load_env(env_path)
    files = {
        relative: _file_state(PROJECTLING_DIR / relative)
        for relative in WATCHED_PATHS
    }
    secret_presence = {
        key: {"configured": bool(value), "length": len(value)}
        for key, value in sorted(env.items())
        if _is_secret_key(key)
    }
    semantic_env = {
        key: env.get(key, "")
        for key in SEMANTIC_ENV_KEYS
    }
    json_metadata = {
        relative: _json_metadata(PROJECTLING_DIR / relative)
        for relative in WATCHED_PATHS
        if relative.endswith(".json")
    }
    return {
        "schema_version": 1,
        "label": label,
        "projectling_dir": str(PROJECTLING_DIR),
        "process": {
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "pid": os.getpid(),
        },
        "semantic_env": semantic_env,
        "secret_presence": secret_presence,
        "files": files,
        "json_metadata": json_metadata,
    }


def compare_snapshots(before: dict[str, Any], after: dict[str, Any], *, allow: set[str] | None = None) -> dict[str, Any]:
    allow = allow or set()
    before_files = before.get("files") if isinstance(before.get("files"), dict) else {}
    after_files = after.get("files") if isinstance(after.get("files"), dict) else {}
    changed_files: list[dict[str, Any]] = []
    for relative in sorted(set(before_files) | set(after_files)):
        left = before_files.get(relative) if isinstance(before_files.get(relative), dict) else {}
        right = after_files.get(relative) if isinstance(after_files.get(relative), dict) else {}
        if left == right:
            continue
        changed_files.append(
            {
                "path": relative,
                "allowed": relative in allow,
                "before": left,
                "after": right,
            }
        )

    semantic_before = before.get("semantic_env") if isinstance(before.get("semantic_env"), dict) else {}
    semantic_after = after.get("semantic_env") if isinstance(after.get("semantic_env"), dict) else {}
    semantic_changes = {
        key: {"before": semantic_before.get(key), "after": semantic_after.get(key)}
        for key in sorted(set(semantic_before) | set(semantic_after))
        if semantic_before.get(key) != semantic_after.get(key)
    }

    secrets_before = before.get("secret_presence") if isinstance(before.get("secret_presence"), dict) else {}
    secrets_after = after.get("secret_presence") if isinstance(after.get("secret_presence"), dict) else {}
    secret_presence_changed = secrets_before != secrets_after
    forbidden = [entry["path"] for entry in changed_files if not entry["allowed"]]
    return {
        "schema_version": 1,
        "ok": not forbidden and not semantic_changes and not secret_presence_changed,
        "allowed": sorted(allow),
        "changed_files": changed_files,
        "forbidden_changes": forbidden,
        "semantic_changes": semantic_changes,
        "secret_presence_changed": secret_presence_changed,
    }


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"snapshot must be a JSON object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(prog="runtime-state-guard")
    sub = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = sub.add_parser("snapshot", help="write a redacted runtime-state fingerprint")
    snapshot_parser.add_argument("--output", type=Path, required=True)
    snapshot_parser.add_argument("--label", default="")

    compare_parser = sub.add_parser("compare", help="compare two runtime-state fingerprints")
    compare_parser.add_argument("--before", type=Path, required=True)
    compare_parser.add_argument("--after", type=Path, required=True)
    compare_parser.add_argument("--output", type=Path)
    compare_parser.add_argument("--allow", action="append", default=[])

    args = parser.parse_args()
    if args.command == "snapshot":
        payload = build_snapshot(label=str(args.label or ""))
        _write_json(args.output, payload)
        print(json.dumps({"status": "ok", "output": str(args.output), "files": len(payload["files"])}, ensure_ascii=False))
        return 0

    before = _read_json(args.before)
    after = _read_json(args.after)
    result = compare_snapshots(before, after, allow={str(item) for item in args.allow})
    if args.output:
        _write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
