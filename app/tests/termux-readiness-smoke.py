#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aidebug.runner import aidebug_health  # noqa: E402
import tooling  # noqa: E402


def write_properties(home: Path, text: str) -> Path:
    path = home / ".termux" / "termux.properties"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    cases = (
        ("allow-external-apps=true\n", True),
        ("allow-external-apps = true\n", True),
        ("# allow-external-apps=true\n", False),
        ("allow-external-apps=false\n", False),
        ("allow-external-apps=true\nallow-external-apps = false\n", False),
        ("allow-external-apps=false\nallow-external-apps = true\n", True),
    )
    with tempfile.TemporaryDirectory(prefix="projectling-termux-readiness-") as raw_home:
        home = Path(raw_home)
        for index, (content, expected) in enumerate(cases, start=1):
            properties = write_properties(home, content)
            actual_health = aidebug_health._termux_boolean_property_enabled(
                properties,
                "allow-external-apps",
            )
            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(
                tooling.Path,
                "home",
                return_value=home,
            ):
                actual_runtime = tooling._termux_allow_external_apps_enabled()
            assert actual_health is expected, f"health parser case {index}: {actual_health} != {expected}"
            assert actual_runtime is expected, f"runtime parser case {index}: {actual_runtime} != {expected}"

    assert aidebug_health._android_termux_readiness_score(
        is_android_termux=True,
        missing=[],
    ) == 100
    assert aidebug_health._android_termux_readiness_score(
        is_android_termux=True,
        missing=["allow_external_apps"],
    ) < 100
    assert aidebug_health._android_termux_readiness_score(
        is_android_termux=False,
        missing=["am", "allow_external_apps"],
    ) == 85

    zsh_source = (ROOT / "projectling.zsh").read_text(encoding="utf-8", errors="replace")
    for marker in (
        "main|main_api|main-api|planner",
        "executor|executor_api|executor-api|support",
        "gpt|codex|openai",
        "gemini|grok|xai",
        "} always {",
    ):
        assert marker in zsh_source, f"missing zsh settings alias group: {marker}"

    zsh_status = "skip"
    if shutil.which("zsh"):
        aliases = (
            "main",
            "planner",
            "executor",
            "support",
            "gpt",
            "codex",
            "openai",
            "gemini",
            "grok",
            "xai",
            "deepseek",
            "web-search",
        )
        special_commands = (
            ("gpt", "/gpt", "settings:gpt"),
            ("codex", "/codex", "settings:gpt"),
            ("openai", "/openai", "settings:gpt"),
            ("gemini", "/gemini", "settings:gemini"),
            ("grok", "/grok", "settings:grok"),
            ("xai", "/xai", "settings:grok"),
            ("deepseek", "/deepseek", "settings:deepseek"),
        )
        script = "\n".join(
            [
                f"source {shlex.quote(str(ROOT / 'projectling.zsh'))}",
                "projectling_run_on_tty() { print -r -- \"$@\"; }",
                *[
                    f"print -r -- {shlex.quote(alias)}=$(projectling_run_local_command settings {shlex.quote(alias)})"
                    for alias in aliases
                ],
                *[
                    f"print -r -- special_{label}=$(projectling_special_command_kind {shlex.quote(command)})"
                    for label, command, _expected in special_commands
                ],
            ]
        )
        completed = subprocess.run(
            ["zsh", "-fc", script],
            cwd=str(ROOT),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=20,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr or completed.stdout
        rows = dict(
            line.split("=", 1)
            for line in completed.stdout.splitlines()
            if "=" in line
        )
        for alias in aliases:
            expected_tab = "websearch" if alias == "web-search" else alias
            assert rows.get(alias) == f"shell-settings --tab {expected_tab}", (
                alias,
                rows.get(alias),
                completed.stdout,
            )
        for label, _command, expected in special_commands:
            assert rows.get(f"special_{label}") == expected, (
                label,
                rows.get(f"special_{label}"),
                completed.stdout,
            )
        zsh_status = "ok"

    print(f"termux_readiness_smoke=ok cases=6 scoring=ok zsh_settings={zsh_status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
