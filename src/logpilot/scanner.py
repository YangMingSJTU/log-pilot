from __future__ import annotations

from pathlib import Path

from .config import ScanConfig
from .models import LogCall
from .parsers import language_for_path, parse_file


def scan_repository(repo_root: Path, config: ScanConfig) -> tuple[list[LogCall], int]:
    repo_root = repo_root.resolve()
    logs: list[LogCall] = []
    files_scanned = 0

    for path in _iter_source_files(repo_root, config):
        language = language_for_path(path)
        if not language:
            continue
        files_scanned += 1
        logs.extend(parse_file(path, repo_root, language))

    return logs, files_scanned


def _iter_source_files(repo_root: Path, config: ScanConfig):
    excluded = set(config.exclude)
    extensions = set(config.include_extensions)
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in extensions:
            continue
        parts = set(path.relative_to(repo_root).parts)
        if excluded.intersection(parts):
            continue
        yield path
