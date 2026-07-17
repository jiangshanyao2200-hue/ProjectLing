#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aidebug.runner import aidebug_health  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def main() -> int:
    release_root = ROOT.parent if ROOT.name == "app" and (ROOT.parent / "PROJECT凌.exe").is_file() else ROOT
    launcher_source = ROOT / "windows-launcher" / "Program.cs"
    launcher_exe = release_root / "PROJECT凌.exe"
    markers = aidebug_health._windows_launcher_source_surface_markers(
        launcher_source.read_text(encoding="utf-8", errors="replace")
    )
    assert all(markers.values()), f"launcher source markers failed: {[name for name, ok in markers.items() if not ok]}"
    fresh, mode = aidebug_health._launcher_pair_freshness(launcher_exe, launcher_source)
    assert fresh, f"tracked launcher/source pair is stale: mode={mode}"

    with tempfile.TemporaryDirectory(prefix="projectling-launcher-pair-") as raw_root:
        fixture = Path(raw_root)
        fixture_source = fixture / "windows-launcher" / "Program.cs"
        fixture_exe = fixture / "PROJECT凌.exe"
        fixture_source.parent.mkdir(parents=True)
        fixture_exe.write_bytes(b"fixture-exe\n")
        fixture_source.write_text("fixture-source\n", encoding="utf-8")
        os.utime(fixture_exe, (1, 1))
        os.utime(fixture_source, (2, 2))
        stale, stale_mode = aidebug_health._launcher_pair_freshness(fixture_exe, fixture_source)
        assert not stale and stale_mode == "exe<src"
        (fixture / "SHA256SUMS.txt").write_text(
            f"{sha256(fixture_exe)}  PROJECT凌.exe\n"
            f"{sha256(fixture_source)}  windows-launcher/Program.cs\n",
            encoding="utf-8",
        )
        manifest_fresh, manifest_mode = aidebug_health._launcher_pair_freshness(fixture_exe, fixture_source)
        assert manifest_fresh and manifest_mode == "manifest"
        fixture_source.write_text("changed-source\n", encoding="utf-8")
        changed_fresh, changed_mode = aidebug_health._launcher_pair_freshness(fixture_exe, fixture_source)
        assert not changed_fresh and changed_mode == "exe<src"

    print(f"windows_launcher_source_smoke=ok markers={len(markers)} pair={mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
