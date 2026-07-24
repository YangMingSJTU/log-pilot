from __future__ import annotations

import argparse
import platform
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BINARIES = ROOT / "ui" / "src-tauri" / "binaries"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the LogPilot Python engine for the Tauri sidecar bundle.")
    parser.add_argument("--target", default=_default_target(), help="Rust target triple used by the Tauri build.")
    args = parser.parse_args()

    try:
        import PyInstaller.__main__
    except ImportError as exc:
        raise SystemExit("Install the desktop build dependencies with: python -m pip install -e .[desktop]") from exc

    BINARIES.mkdir(parents=True, exist_ok=True)
    work = ROOT / "build" / "desktop-engine"
    dist = work / "dist"
    name = f"logpilot-engine-{args.target}"
    PyInstaller.__main__.run(
        [
            str(ROOT / "scripts" / "logpilot_engine_entry.py"),
            "--name",
            name,
            "--onefile",
            "--clean",
            "--noconfirm",
            "--collect-data",
            "logpilot",
            "--paths",
            str(ROOT / "src"),
            "--distpath",
            str(dist),
            "--workpath",
            str(work / "work"),
            "--specpath",
            str(work),
        ]
    )
    extension = ".exe" if sys.platform == "win32" else ""
    source = dist / f"{name}{extension}"
    target = BINARIES / source.name
    shutil.copy2(source, target)
    print(target)


def _default_target() -> str:
    machine = platform.machine().lower()
    architecture = "aarch64" if machine in {"arm64", "aarch64"} else "x86_64"
    if sys.platform == "win32":
        return f"{architecture}-pc-windows-msvc"
    if sys.platform == "darwin":
        return f"{architecture}-apple-darwin"
    return f"{architecture}-unknown-linux-gnu"


if __name__ == "__main__":
    main()
