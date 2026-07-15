from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path


DATA_DIR_ENV = "LOGPILOT_DATA_DIR"


def app_data_root() -> Path:
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base).expanduser().resolve() / "LogPilot"
        return Path.home() / "AppData" / "Local" / "LogPilot"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LogPilot"

    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg_data_home).expanduser() if xdg_data_home else Path.home() / ".local" / "share"
    return base.resolve() / "logpilot"


def repository_id(repo_root: Path) -> str:
    canonical = os.path.normcase(str(repo_root.expanduser().resolve()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def repository_data_dir(repo_root: Path) -> Path:
    return app_data_root() / "repositories" / repository_id(repo_root)


def initialize_repository_storage(repo_root: Path) -> Path:
    resolved = repo_root.expanduser().resolve()
    data_dir = repository_data_dir(resolved)
    data_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "repository_id": repository_id(resolved),
        "repository": str(resolved),
        "name": resolved.name,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _write_json(data_dir / "repository.json", metadata)
    return data_dir


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
