from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import ScanConfig
from .models import LogCall
from .parsers import language_for_path, parse_file


ScanFileProgress = Callable[[int, int, str], None]


@dataclass(slots=True)
class RepositoryScan:
    logs: list[LogCall]
    files_scanned: int
    language_file_counts: dict[str, int]


def scan_repository(
    repo_root: Path,
    config: ScanConfig,
    progress: ScanFileProgress | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[list[LogCall], int]:
    result = scan_repository_detailed(repo_root, config, progress, should_cancel)
    return result.logs, result.files_scanned


def scan_repository_detailed(
    repo_root: Path,
    config: ScanConfig,
    progress: ScanFileProgress | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> RepositoryScan:
    repo_root = repo_root.resolve()
    logs: list[LogCall] = []
    file_counts: Counter[str] = Counter()
    source_files = [path for path in _iter_source_files(repo_root, config) if language_for_path(path)]
    total = len(source_files)

    for index, path in enumerate(source_files, start=1):
        if should_cancel and should_cancel():
            raise InterruptedError("分析已取消。")
        language = language_for_path(path)
        if not language:
            continue
        file_counts[language] += 1
        logs.extend(parse_file(path, repo_root, language))
        if progress:
            progress(index, total, path.relative_to(repo_root).as_posix())

    return RepositoryScan(logs, total, dict(file_counts))


def _iter_source_files(repo_root: Path, config: ScanConfig):
    excluded = set(config.exclude)
    extensions = set(config.include_extensions)
    for current_root, directories, filenames in os.walk(repo_root):
        current = Path(current_root)
        directories[:] = sorted(directory for directory in directories if directory not in excluded)
        for filename in sorted(filenames):
            path = current / filename
            if path.suffix.lower() not in extensions:
                continue
            parts = set(path.relative_to(repo_root).parts)
            if not excluded.intersection(parts):
                yield path
