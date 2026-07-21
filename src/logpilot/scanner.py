from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .config import ScanConfig
from .languages import known_extensions, language_for_path, language_spec
from .models import AnalysisTarget, LogCall, ParseFailure, relative_path
from .native_parser_client import NativeParserClient
from .parsers import parse_file_with_targets


ScanFileProgress = Callable[[int, int, str], None]


@dataclass(slots=True)
class RepositoryScan:
    logs: list[LogCall]
    files_scanned: int
    language_file_counts: dict[str, int]
    analysis_targets: list[AnalysisTarget]
    discovered_language_counts: dict[str, int]
    failed_language_counts: dict[str, int]
    selected_language_counts: dict[str, int]
    parse_failures: list[ParseFailure]
    unrecognized_extension_counts: dict[str, int]

    @property
    def discovered_files(self) -> int:
        return sum(self.discovered_language_counts.values()) + sum(self.unrecognized_extension_counts.values())

    @property
    def unsupported_files(self) -> int:
        return sum(
            count
            for language, count in self.discovered_language_counts.items()
            if not (language_spec(language) and language_spec(language).analyzable)
        )

    @property
    def failed_files(self) -> int:
        return sum(self.failed_language_counts.values())


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
    native_client_factory: Callable[[], NativeParserClient] | None = None,
    file_paths: Iterable[Path] | None = None,
    native_client: NativeParserClient | None = None,
) -> RepositoryScan:
    repo_root = repo_root.resolve()
    logs: list[LogCall] = []
    targets: list[AnalysisTarget] = []
    file_counts: Counter[str] = Counter()
    discovered_counts: Counter[str] = Counter()
    failed_counts: Counter[str] = Counter()
    selected_counts: Counter[str] = Counter()
    parse_failures: list[ParseFailure] = []
    unrecognized_counts: Counter[str] = Counter()
    inventory_files = (
        sorted((path.resolve() for path in file_paths), key=lambda path: relative_path(path, repo_root).casefold())
        if file_paths is not None
        else list(_iter_repository_files(repo_root, config))
    )
    selected_extensions = set(config.include_extensions)
    source_files: list[tuple[Path, str]] = []
    for path in inventory_files:
        language = language_for_path(path)
        if not language:
            if _looks_like_unknown_source(path):
                extension = path.suffix.lower() or "<no-extension>"
                unrecognized_counts[extension] += 1
                if unrecognized_counts[extension] <= 3:
                    targets.append(_unsupported_sample_target(path, repo_root, "unknown", extension))
            continue
        discovered_counts[language] += 1
        spec = language_spec(language)
        if spec and not spec.analyzable and discovered_counts[language] <= 3:
            targets.append(_unsupported_sample_target(path, repo_root, language, path.suffix.lower()))
        if path.suffix.lower() not in selected_extensions:
            continue
        selected_counts[language] += 1
        if spec and spec.analyzable:
            source_files.append((path, language))
    total = len(source_files)

    owned_native_client = native_client is None
    try:
        for index, (path, language) in enumerate(source_files, start=1):
            rel = path.relative_to(repo_root).as_posix()
            if should_cancel and should_cancel():
                raise InterruptedError("分析已取消。")
            try:
                if language in {"c", "cpp"}:
                    if native_client is None:
                        native_client = (native_client_factory or NativeParserClient)()
                    result = native_client.parse_file(path, repo_root, language, should_cancel)
                    if result.failure:
                        failed_counts[language] += 1
                        parse_failures.append(result.failure)
                        continue
                    parsed_logs, parsed_targets = result.logs, result.targets
                else:
                    parsed_logs, parsed_targets = parse_file_with_targets(path, repo_root, language)
            except InterruptedError:
                raise
            except Exception as exc:
                failed_counts[language] += 1
                parse_failures.append(
                    ParseFailure(
                        file_path=rel,
                        language=language,
                        error_kind="parse_error",
                        message=f"{type(exc).__name__}: {exc}",
                    )
                )
            else:
                parsed_logs, parsed_targets = _bounded_context(parsed_logs, parsed_targets)
                file_counts[language] += 1
                logs.extend(parsed_logs)
                targets.extend(parsed_targets)
            finally:
                if progress:
                    progress(index, total, rel)
    finally:
        if owned_native_client and native_client is not None:
            native_client.close()

    return RepositoryScan(
        logs=logs,
        files_scanned=sum(file_counts.values()),
        language_file_counts=dict(file_counts),
        analysis_targets=targets,
        discovered_language_counts=dict(discovered_counts),
        failed_language_counts=dict(failed_counts),
        selected_language_counts=dict(selected_counts),
        parse_failures=parse_failures,
        unrecognized_extension_counts=dict(unrecognized_counts),
    )


def _bounded_context(
    logs: list[LogCall],
    targets: list[AnalysisTarget],
    limit: int = 8 * 1024,
) -> tuple[list[LogCall], list[AnalysisTarget]]:
    for log in logs:
        log.context = log.context[:limit]
        log.source_line = log.source_line[:limit]
    for target in targets:
        target.context = target.context[:limit]
        target.source_line = target.source_line[:limit]
    return logs, targets


def _iter_repository_files(repo_root: Path, config: ScanConfig):
    excluded = set(config.exclude)
    for current_root, directories, filenames in os.walk(repo_root):
        current = Path(current_root)
        directories[:] = sorted(directory for directory in directories if directory not in excluded)
        for filename in sorted(filenames):
            path = current / filename
            parts = set(path.relative_to(repo_root).parts)
            if not excluded.intersection(parts):
                yield path


_NON_SOURCE_EXTENSIONS = {
    ".bmp", ".csv", ".gif", ".ico", ".ini", ".jpeg", ".jpg", ".json", ".lock",
    ".md", ".pdf", ".png", ".qrc", ".rst", ".svg", ".toml", ".tsv", ".txt",
    ".ui", ".webp", ".xml", ".yaml", ".yml",
}
_CODE_HINT = re.compile(
    r"(?:^|\n)\s*(?:class|def|fn|func|function|import|package|pub|struct|using)\b|"
    r"#include\s*[<\"]|(?:\{|\}|;)\s*(?:\n|$)",
    re.MULTILINE,
)


def _looks_like_unknown_source(path: Path) -> bool:
    suffix = path.suffix.lower()
    if not suffix or suffix in _NON_SOURCE_EXTENSIONS or suffix in known_extensions():
        return False
    try:
        sample = path.read_bytes()[:16_384]
    except OSError:
        return False
    if not sample or b"\0" in sample:
        return False
    text = sample.decode("utf-8", errors="ignore")
    return bool(_CODE_HINT.search(text))


def _unsupported_sample_target(
    path: Path,
    repo_root: Path,
    language: str,
    extension: str,
) -> AnalysisTarget:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")[:6000]
    except OSError:
        text = ""
    rel = path.relative_to(repo_root).as_posix()
    return AnalysisTarget(
        id=f"unsupported:{language}:{rel}",
        kind="unsupported_sample",
        file_path=rel,
        start_line=1,
        end_line=min(80, max(1, text.count("\n") + 1)),
        language=language,
        context=text,
        source_line=text,
        symbol=extension,
        metadata={"extension": extension},
    )
