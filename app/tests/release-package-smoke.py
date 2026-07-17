#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
NESTED_ENV = "PROJECTLING_RELEASE_SMOKE_NESTED"

REQUIRED_APP_FILES = {
    "__init__.py",
    "core.py",
    "projectling.py",
    "tooling.py",
    "run.sh",
    "projectling.zsh",
    "release/app-files.txt",
    "tests/aidebug-matrix-contract-smoke.py",
    "tests/release-package-smoke.py",
    "tests/termux-install-bridge-smoke.sh",
    "tests/termux-platform-smoke.sh",
    "tests/termux-readiness-smoke.py",
    "tests/windows-launcher-source-smoke.py",
    "aidebug/runner/relay_model_matrix.py",
    "windows-launcher/Program.cs",
    "windows-launcher/ProjectLingLauncher.csproj",
}

CRITICAL_SELFTESTS = {
    "Termux readiness properties",
    "Windows launcher source pair",
    "AIDEBUG dynamic matrix contract",
    "Release package integrity",
}

PRIVATE_CONFIG_FILES = {
    "config/env",
    "config/role.json",
    "config/focus.json",
    "config/context-budget.json",
    "config/update-plan.json",
}

SECRET_RE = re.compile(
    rb"(?i)(?:\bsk-[A-Za-z0-9_-]{12,}\b|\bAIza[A-Za-z0-9_-]{16,}\b|"
    rb"\bghp_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b|"
    rb"Authorization\s*[:=]\s*[\"']?Bearer\s+[A-Za-z0-9._-]{12,})"
)


_PERSONAL_USER_COMPONENT = rb"[^\\/\x00\r\n\"']+"
PERSONAL_PATH_RE = re.compile(
    rb"(?i)(?:"
    + rb"\b[A-Z]:\\+"
    + b"Users"
    + rb"\\+"
    + _PERSONAL_USER_COMPONENT
    + rb"\\+"
    + b"|/mnt/"
    + rb"[a-z]/"
    + b"Users/"
    + _PERSONAL_USER_COMPONENT
    + b"/|/"
    + b"Users/"
    + _PERSONAL_USER_COMPONENT
    + b"/|/"
    + b"home/"
    + _PERSONAL_USER_COMPONENT
    + rb"/)"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def assert_relative_file(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    assert relative == pure.as_posix(), f"non-canonical manifest path: {relative!r}"
    assert not pure.is_absolute(), f"absolute manifest path: {relative!r}"
    assert pure.parts and all(part not in {"", ".", ".."} for part in pure.parts), (
        f"unsafe manifest path: {relative!r}"
    )
    path = root.joinpath(*pure.parts)
    assert path.is_file() and not path.is_symlink(), f"missing manifest file: {relative}"
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise AssertionError(f"manifest path escapes app root: {relative}") from exc
    return path


def is_private_or_runtime_path(relative: str) -> bool:
    pure = PurePosixPath(relative)
    parts = pure.parts
    folded = tuple(part.casefold() for part in parts)
    normalized = pure.as_posix().casefold()
    if normalized in {path.casefold() for path in PRIVATE_CONFIG_FILES}:
        return True
    if folded and folded[0] == "memory":
        return True
    if folded and folded[0] == "context" and normalized != "context/prompts.json":
        return True
    if len(folded) >= 2 and folded[0] == "aidebug" and folded[1] in {
        "logs",
        "notes",
        "state",
        "tmp",
        "backup",
        "legacy",
    }:
        return True
    if len(folded) >= 2 and folded[0] == "windows-launcher" and folded[1] in {"bin", "obj"}:
        return True
    if ".git" in folded or "__pycache__" in folded or "terminal output" in folded:
        return True
    return pure.suffix.casefold() == ".pyc"


def read_app_manifest(root: Path = ROOT) -> list[str]:
    manifest = root / "release" / "app-files.txt"
    assert manifest.is_file(), f"missing canonical app manifest: {manifest}"
    entries = [
        line.strip()
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert entries, "canonical app manifest is empty"
    folded = [entry.casefold() for entry in entries]
    assert len(folded) == len(set(folded)), "canonical app manifest contains duplicates"
    assert entries == sorted(entries, key=str.casefold), "canonical app manifest must remain sorted"
    for relative in entries:
        assert_relative_file(root, relative)
        assert not is_private_or_runtime_path(relative), f"private/runtime path in app manifest: {relative}"
    missing_required = sorted(REQUIRED_APP_FILES.difference(entries))
    assert not missing_required, f"canonical app manifest misses required files: {missing_required}"
    return entries


def assert_build_script_contracts(source_root: Path) -> None:
    contracts = {
        "release/build.ps1": (
            "Read-AppFileManifest",
            "release\\app-files.txt",
            "OutputRoot must be outside the ProjectLing source tree",
            "Private or runtime path in app manifest",
            "tests/release-package-smoke.py",
            "Assert-CleanRelease",
            "$CanonicalLauncher",
            "@('PROJECT凌.exe', 'PROJECT LING.exe')",
            "source_layout: canonical-app-manifest",
        ),
        "release/github/build.ps1": (
            "Read-AppFileManifest",
            "release\\app-files.txt",
            "OutputRoot must be outside the ProjectLing source tree",
            "Private or runtime path in app manifest",
            "tests/release-package-smoke.py",
            "Assert-PublicClean",
            "@('PROJECT凌.exe', 'PROJECT LING.exe')",
            "Join-Path $OutputRoot 'ProjectLing'",
            '"ProjectLing-Public-$Version.zip"',
        ),
    }
    for relative, markers in contracts.items():
        text = assert_relative_file(source_root, relative).read_text(encoding="utf-8", errors="replace")
        missing = [marker for marker in markers if marker not in text]
        assert not missing, f"release builder contract missing in {relative}: {missing}"
        assert "source_root:" not in text, f"release builder leaks local source path in build metadata: {relative}"
        assert "C:\\Users\\" not in text, f"release builder leaks a local Windows user path: {relative}"

    publish_text = assert_relative_file(source_root, "release/github/publish.ps1").read_text(
        encoding="utf-8",
        errors="replace",
    )
    for marker in (
        "Name = 'ProjectLing'",
        "Directory = Join-Path $BuildRoot 'ProjectLing'",
        '"ProjectLing-Public-$Version.zip"',
        "Name = 'ProjectLing-Private'",
    ):
        assert marker in publish_text, f"GitHub publish target contract missing: {marker}"
    assert "PROJECTling" not in publish_text, "GitHub publish script still targets the legacy public repository name"
    assert "C:\\Users\\" not in publish_text, "GitHub publish script leaks a local Windows user path"


def copy_relative(source_root: Path, destination_root: Path, relative: str) -> None:
    source = assert_relative_file(source_root, relative)
    target = destination_root.joinpath(*PurePosixPath(relative).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def copy_tree(source: Path, destination: Path) -> None:
    assert source.is_dir(), f"missing release tree: {source}"
    shutil.copytree(source, destination, copy_function=shutil.copy2)


def package_files(root: Path) -> list[Path]:
    return sorted(
        (path for path in root.rglob("*") if path.is_file() and path.name != "SHA256SUMS.txt"),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def write_checksums(root: Path) -> Path:
    manifest = root / "SHA256SUMS.txt"
    lines = [f"{sha256(path)}  {path.relative_to(root).as_posix()}" for path in package_files(root)]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return manifest


def verify_checksums(root: Path, *, exact: bool) -> int:
    manifest = root / "SHA256SUMS.txt"
    assert manifest.is_file(), f"missing package checksums: {manifest}"
    expected: dict[str, str] = {}
    for number, raw_line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), start=1):
        parts = raw_line.strip().split(None, 1)
        assert len(parts) == 2 and re.fullmatch(r"[0-9A-Fa-f]{64}", parts[0]), (
            f"invalid checksum line {number}: {raw_line!r}"
        )
        relative = parts[1].lstrip("*").replace("\\", "/")
        assert relative != "SHA256SUMS.txt", "checksum manifest must not hash itself"
        assert relative not in expected, f"duplicate checksum entry: {relative}"
        path = assert_relative_file(root, relative)
        actual = sha256(path)
        assert actual == parts[0].upper(), f"checksum mismatch: {relative}"
        expected[relative] = actual
    if exact:
        actual_files = {path.relative_to(root).as_posix() for path in package_files(root)}
        assert set(expected) == actual_files, (
            f"checksum coverage mismatch: missing={sorted(actual_files - set(expected))} "
            f"extra={sorted(set(expected) - actual_files)}"
        )
    return len(expected)


def secret_hits(paths: Iterable[Path]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        overlap = b""
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                sample = overlap + chunk
                if SECRET_RE.search(sample):
                    hits.append(str(path))
                    break
                overlap = sample[-256:]
    return hits


def personal_path_hits(paths: Iterable[Path]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        overlap = b""
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                sample = overlap + chunk
                if PERSONAL_PATH_RE.search(sample):
                    hits.append(str(path))
                    break
                overlap = sample[-256:]
    return hits


def assert_public_clean(package_root: Path, *, allow_generated_runtime: bool = False) -> None:
    forbidden = []
    for path in package_root.rglob("*"):
        if path == package_root / "SHA256SUMS.txt":
            continue
        relative = path.relative_to(package_root).as_posix()
        app_relative = relative[4:] if relative.casefold().startswith("app/") else relative
        if (
            not allow_generated_runtime
            and path.is_file()
            and is_private_or_runtime_path(app_relative)
        ):
            forbidden.append(relative)
        if not allow_generated_runtime and (path.name == "__pycache__" or path.suffix.casefold() == ".pyc"):
            forbidden.append(relative)
    assert not forbidden, f"forbidden public package paths: {sorted(set(forbidden))}"
    hits = secret_hits(path for path in package_root.rglob("*") if path.is_file())
    assert not hits, f"secret-like content in public package: {hits}"
    personal_path_files = []
    for path in package_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(package_root).as_posix()
        app_relative = relative[4:] if relative.casefold().startswith("app/") else relative
        if allow_generated_runtime and is_private_or_runtime_path(app_relative):
            continue
        personal_path_files.append(path)
    path_hits = personal_path_hits(personal_path_files)
    assert not path_hits, f"personal absolute path in public package: {path_hits}"


def build_combined_public_package(source_root: Path, package_root: Path, entries: list[str]) -> None:
    app_root = package_root / "app"
    for relative in entries:
        copy_relative(source_root, app_root, relative)

    launcher_name = next(
        (name for name in ("PROJECT凌.exe", "PROJECT LING.exe") if (source_root / name).is_file()),
        None,
    )
    assert launcher_name is not None, "missing tracked Windows launcher"
    launcher = assert_relative_file(source_root, launcher_name)
    shutil.copy2(launcher, package_root / "PROJECT凌.exe")

    copy_relative(source_root, package_root, "release/combined/README.md")
    (package_root / "release" / "combined" / "README.md").replace(package_root / "README.md")
    copy_relative(source_root, package_root, "release/combined/run.sh")
    (package_root / "release" / "combined" / "run.sh").replace(package_root / "run.sh")
    copy_relative(source_root, package_root, "release/combined/projectling.zsh")
    (package_root / "release" / "combined" / "projectling.zsh").replace(package_root / "projectling.zsh")
    shutil.rmtree(package_root / "release")

    copy_tree(source_root / "release" / "termux", package_root / "Termux")
    copy_tree(source_root / "release" / "windows", package_root / "Windows")
    copy_relative(source_root, package_root, "windows-launcher/assets/projectling-icon.png")
    icon_source = package_root / "windows-launcher" / "assets" / "projectling-icon.png"
    icon_target = package_root / "assets" / "projectling-icon.png"
    icon_target.parent.mkdir(parents=True, exist_ok=True)
    icon_source.replace(icon_target)
    shutil.rmtree(package_root / "windows-launcher")

    docs = {
        "release/RELEASE-STRUCTURE.md": "RELEASE-STRUCTURE.md",
        "release/FINAL-RELEASE.md": "RELEASE-NOTES.md",
        "release/FINAL-INVENTORY.md": "RELEASE-FILE-INVENTORY.md",
        "aidebug/notes/projectling-relay-model-compatibility.md": "MODEL-COMPATIBILITY.md",
        "aidebug/notes/projectling-relay-model-compatibility.json": "MODEL-COMPATIBILITY.json",
        "aidebug/notes/projectling-gemini-parameter-support.md": "GEMINI-PARAMETER-SUPPORT.md",
        "aidebug/notes/projectling-gemini-parameter-support.json": "GEMINI-PARAMETER-SUPPORT.json",
    }
    for source_relative, target_name in docs.items():
        source = assert_relative_file(source_root, source_relative)
        target = package_root / "docs" / target_name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def sanitized_environment(home: Path) -> dict[str, str]:
    secret_name = re.compile(
        r"(?i)(?:key|token|secret|password|authorization|openai|deepseek|gemini|grok|xai|github|"
        r"projectling_|aitermux|pythonpath|pythonhome|proxy)"
    )
    env = {name: value for name, value in os.environ.items() if not secret_name.search(name)}
    env.update(
        {
            "HOME": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            NESTED_ENV: "1",
        }
    )
    return env


def run_checked(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float = 240.0,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(f"command timeout after {timeout:g}s: {command!r}") from exc
    if completed.returncode != 0:
        stdout = completed.stdout.strip()[-3000:]
        stderr = completed.stderr.strip()[-3000:]
        failed_summary = ""
        try:
            payload = json.loads(completed.stdout)
            failures = [
                f"{item.get('name')}: {item.get('detail')}"
                for item in payload.get("results", [])
                if isinstance(item, dict) and item.get("status") == "fail"
            ]
            if failures:
                failed_summary = "\nfailed=" + " | ".join(failures)
        except (TypeError, ValueError):
            pass
        raise AssertionError(
            f"command failed rc={completed.returncode}: {command!r}{failed_summary}"
            f"\nstdout={stdout}\nstderr={stderr}"
        )
    return completed


def nested_package_check() -> int:
    assert ROOT.name == "app", f"nested release guard outside packaged app: {ROOT}"
    entries = read_app_manifest(ROOT)
    package_root = ROOT.parent
    required_root = (
        package_root / "PROJECT凌.exe",
        package_root / "run.sh",
        package_root / "projectling.zsh",
        package_root / "Termux" / "install.sh",
        package_root / "Termux" / "run.sh",
        package_root / "Windows" / "aidebug.cmd",
    )
    missing = [str(path.relative_to(package_root)) for path in required_root if not path.is_file()]
    assert not missing, f"packaged combined layout is incomplete: {missing}"
    # Importing the packaged runtime may create migration markers before this
    # nested check runs.  Every shipped checksum must still match; the outer
    # builder check already proved exact initial coverage before execution.
    checksum_count = verify_checksums(package_root, exact=False)
    print(f"release_package_smoke=nested-ok app_files={len(entries)} checksums={checksum_count}")
    return 0


def main() -> int:
    if os.environ.get(NESTED_ENV) == "1":
        return nested_package_check()

    entries = read_app_manifest(ROOT)
    assert_build_script_contracts(ROOT)
    # On Termux, tempfile.gettempdir() is under $PREFIX/tmp and ProjectLing
    # intentionally blocks model-authored writes anywhere below $PREFIX.
    # Keep the disposable release under the user workspace so its own tool
    # execution selftest exercises the authorized boundary.
    with tempfile.TemporaryDirectory(prefix="projectling-release-package-", dir=str(ROOT.parent)) as raw_temp:
        temp_root = Path(raw_temp)
        package_root = temp_root / "PROJECTLing-Combined-Public"
        package_root.mkdir()
        build_combined_public_package(ROOT, package_root, entries)
        write_checksums(package_root)
        checksum_count = verify_checksums(package_root, exact=True)
        assert_public_clean(package_root)

        home = temp_root / "home"
        home.mkdir()
        env = sanitized_environment(home)
        selftest = run_checked(
            [sys.executable, "core.py", "selftest", "--json"],
            cwd=package_root / "app",
            env=env,
        )
        payload = json.loads(selftest.stdout)
        assert payload.get("status") == "ok" and payload.get("failed") == 0 and payload.get("score") == 100, (
            f"packaged selftest failed: {payload}"
        )
        results = {
            str(item.get("name")): str(item.get("status"))
            for item in payload.get("results", [])
            if isinstance(item, dict)
        }
        missing_critical = sorted(CRITICAL_SELFTESTS.difference(results))
        skipped_or_failed = {name: results.get(name) for name in CRITICAL_SELFTESTS if results.get(name) != "ok"}
        assert not missing_critical, f"packaged selftest misses critical checks: {missing_critical}"
        assert not skipped_or_failed, f"packaged critical checks did not execute: {skipped_or_failed}"

        run_checked(
            ["bash", "Termux/install.sh", "--check", "--no-zshrc"],
            cwd=package_root,
            env=env,
        )
        relay = run_checked(
            [sys.executable, "aidebug/runner/relay_model_matrix.py", "--local-contracts"],
            cwd=package_root / "app",
            env=env,
        )
        relay_payload = json.loads(relay.stdout)
        assert relay_payload.get("ok") is True, f"packaged relay contracts failed: {relay_payload}"
        assert set((relay_payload.get("providers") or {}).keys()) == {"gpt", "gemini", "grok", "deepseek"}
        assert (relay_payload.get("dual_star_isolation") or {}).get("ok") is True

        verify_checksums(package_root, exact=False)
        assert_public_clean(package_root, allow_generated_runtime=True)
        print(
            "release_package_smoke=ok "
            f"app_files={len(entries)} checksums={checksum_count} "
            f"selftest={payload.get('passed')}/{payload.get('total')} providers=4"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"release_package_smoke=fail reason={exc}", file=sys.stderr)
        raise
