from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .config import ScanConfig
from .languages import language_for_path
from .storage import initialize_repository_storage


LARGE_REPOSITORY_FILES = 5_000
LARGE_REPOSITORY_BYTES = 512 * 1024 * 1024
CHUNK_MAX_FILES = 1_000
CHUNK_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024
PROJECT_MARKERS = {
    "pyproject.toml",
    "package.json",
    "CMakeLists.txt",
    "pom.xml",
    "Cargo.toml",
    "go.mod",
}


@dataclass(slots=True)
class PlannedFile:
    path: str
    size: int
    language: str
    oversized: bool = False


@dataclass(slots=True)
class ExecutionChunk:
    id: str
    index: int
    file_count: int
    total_bytes: int
    files: list[PlannedFile] = field(default_factory=list)


@dataclass(slots=True)
class ScanModule:
    id: str
    path: str
    name: str
    file_count: int
    total_bytes: int
    languages: dict[str, int]
    recommended: bool = True
    chunks: list[ExecutionChunk] = field(default_factory=list)


@dataclass(slots=True)
class ScanPlan:
    id: str
    repository: str
    created_at: str
    discovery_method: str
    source_files: int
    selected_files: int
    total_bytes: int
    large_repository: bool
    include_large_files: bool
    skipped_large_files: list[PlannedFile]
    advisory_files: list[PlannedFile]
    discovered_languages: dict[str, int]
    unrecognized_extensions: dict[str, int]
    modules: list[ScanModule]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_scan_plan(
    repo_root: Path,
    config: ScanConfig,
    *,
    include_large_files: bool = False,
) -> ScanPlan:
    repo_root = repo_root.expanduser().resolve()
    paths, discovery_method = discover_repository_files(repo_root, config.exclude)
    selected_extensions = {item.lower() for item in config.include_extensions}
    planned: list[PlannedFile] = []
    skipped: list[PlannedFile] = []
    discovered_languages: Counter[str] = Counter()
    unrecognized_extensions: Counter[str] = Counter()
    advisory_by_language: Counter[str] = Counter()
    advisory_files: list[PlannedFile] = []
    discovered_bytes = 0
    for path in paths:
        language = language_for_path(path)
        if not language:
            from .scanner import _looks_like_unknown_source

            if _looks_like_unknown_source(path):
                unrecognized_extensions[path.suffix.lower() or "<no-extension>"] += 1
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        discovered_languages[language] += 1
        discovered_bytes += size
        if path.suffix.lower() not in selected_extensions:
            if advisory_by_language[language] < 3:
                advisory_files.append(
                    PlannedFile(
                        path=path.relative_to(repo_root).as_posix(),
                        size=size,
                        language=language,
                    )
                )
                advisory_by_language[language] += 1
            continue
        item = PlannedFile(
            path=path.relative_to(repo_root).as_posix(),
            size=size,
            language=language,
            oversized=size > DEFAULT_MAX_FILE_BYTES,
        )
        if item.oversized and not include_large_files:
            skipped.append(item)
        else:
            planned.append(item)

    markers = _project_roots(paths, repo_root)
    grouped: dict[str, list[PlannedFile]] = {}
    for item in planned:
        module_path = _module_path(item.path, markers)
        grouped.setdefault(module_path, []).append(item)

    modules = [
        _build_module(module_path, files)
        for module_path, files in sorted(grouped.items(), key=lambda pair: pair[0].casefold())
    ]
    source_files = sum(discovered_languages.values()) + sum(unrecognized_extensions.values())
    return ScanPlan(
        id=uuid.uuid4().hex,
        repository=str(repo_root),
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        discovery_method=discovery_method,
        source_files=source_files,
        selected_files=len(planned) + len(skipped),
        total_bytes=discovered_bytes,
        large_repository=(
            source_files >= LARGE_REPOSITORY_FILES
            or discovered_bytes >= LARGE_REPOSITORY_BYTES
        ),
        include_large_files=include_large_files,
        skipped_large_files=sorted(skipped, key=lambda item: item.path.casefold()),
        advisory_files=sorted(advisory_files, key=lambda item: item.path.casefold()),
        discovered_languages=dict(sorted(discovered_languages.items())),
        unrecognized_extensions=dict(sorted(unrecognized_extensions.items())),
        modules=modules,
    )


def save_scan_plan(plan: ScanPlan) -> Path:
    data_dir = initialize_repository_storage(Path(plan.repository))
    plans_dir = data_dir / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / f"{plan.id}.json"
    path.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_scan_plan(repo_root: Path, plan_id: str) -> ScanPlan:
    if not plan_id or any(char in plan_id for char in "\\/.: "):
        raise ValueError("无效的扫描计划 ID。")
    path = initialize_repository_storage(repo_root) / "plans" / f"{plan_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    modules = []
    for raw_module in payload.get("modules", []):
        chunks = []
        for raw_chunk in raw_module.get("chunks", []):
            chunks.append(
                ExecutionChunk(
                    id=str(raw_chunk["id"]),
                    index=int(raw_chunk["index"]),
                    file_count=int(raw_chunk["file_count"]),
                    total_bytes=int(raw_chunk["total_bytes"]),
                    files=[PlannedFile(**item) for item in raw_chunk.get("files", [])],
                )
            )
        module_data = {key: value for key, value in raw_module.items() if key != "chunks"}
        modules.append(ScanModule(**module_data, chunks=chunks))
    return ScanPlan(
        id=str(payload["id"]),
        repository=str(payload["repository"]),
        created_at=str(payload["created_at"]),
        discovery_method=str(payload.get("discovery_method", "walk")),
        source_files=int(payload.get("source_files", 0)),
        selected_files=int(payload.get("selected_files", payload.get("source_files", 0))),
        total_bytes=int(payload.get("total_bytes", 0)),
        large_repository=bool(payload.get("large_repository", False)),
        include_large_files=bool(payload.get("include_large_files", False)),
        skipped_large_files=[PlannedFile(**item) for item in payload.get("skipped_large_files", [])],
        advisory_files=[PlannedFile(**item) for item in payload.get("advisory_files", [])],
        discovered_languages={str(key): int(value) for key, value in payload.get("discovered_languages", {}).items()},
        unrecognized_extensions={str(key): int(value) for key, value in payload.get("unrecognized_extensions", {}).items()},
        modules=modules,
    )


def discover_repository_files(repo_root: Path, excludes: Iterable[str]) -> tuple[list[Path], str]:
    git_paths = _git_files(repo_root)
    if git_paths is not None:
        return git_paths, "git"
    excluded = set(excludes)
    paths: list[Path] = []
    for current_root, directories, filenames in os.walk(repo_root):
        current = Path(current_root)
        directories[:] = sorted(item for item in directories if item not in excluded)
        for filename in sorted(filenames):
            path = current / filename
            if not excluded.intersection(path.relative_to(repo_root).parts):
                paths.append(path)
    return paths, "walk"


def selected_modules(plan: ScanPlan, module_ids: Iterable[str] | None) -> list[ScanModule]:
    requested = set(module_ids or [])
    if not requested:
        return list(plan.modules)
    known = {module.id for module in plan.modules}
    unknown = requested - known
    if unknown:
        raise ValueError(f"扫描计划中不存在模块：{', '.join(sorted(unknown))}")
    return [module for module in plan.modules if module.id in requested]


def resolve_module_selectors(plan: ScanPlan, selectors: Iterable[str]) -> list[str]:
    values = [value.strip().replace("\\", "/").rstrip("/") or "." for value in selectors if value.strip()]
    if not values:
        return []
    by_id = {module.id: module.id for module in plan.modules}
    by_path = {module.path.casefold(): module.id for module in plan.modules}
    resolved: list[str] = []
    for value in values:
        module_id = by_id.get(value) or by_path.get(value.casefold())
        if not module_id:
            raise ValueError(f"未找到目录模块：{value}")
        if module_id not in resolved:
            resolved.append(module_id)
    return resolved


def _git_files(repo_root: Path) -> list[Path] | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-co", "--exclude-standard", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    paths: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", errors="surrogateescape")
        candidate = repo_root / relative
        if candidate.is_file():
            paths.append(candidate)
    return sorted(paths, key=lambda path: path.relative_to(repo_root).as_posix().casefold())


def _project_roots(paths: list[Path], repo_root: Path) -> set[str]:
    roots: set[str] = set()
    for absolute in paths:
        path = absolute.relative_to(repo_root)
        if path.name in PROJECT_MARKERS or path.suffix.lower() in {".sln", ".csproj"}:
            roots.add(path.parent.as_posix() if path.parent != Path(".") else ".")
    return roots


def _module_path(file_path: str, markers: set[str]) -> str:
    parent = Path(file_path).parent
    candidates: list[str] = []
    for marker in markers:
        marker_path = Path(marker)
        try:
            parent.relative_to(marker_path)
        except ValueError:
            continue
        candidates.append(marker)
    if candidates:
        return max(candidates, key=lambda value: len(Path(value).parts))
    parts = Path(file_path).parts
    return parts[0] if len(parts) > 1 else "."


def _build_module(module_path: str, files: list[PlannedFile]) -> ScanModule:
    ordered = sorted(files, key=lambda item: item.path.casefold())
    module_id = hashlib.sha256(module_path.encode("utf-8")).hexdigest()[:16]
    chunks: list[ExecutionChunk] = []
    current: list[PlannedFile] = []
    current_bytes = 0
    for item in ordered:
        if current and (
            len(current) >= CHUNK_MAX_FILES
            or current_bytes + item.size > CHUNK_MAX_BYTES
        ):
            chunks.append(_chunk(module_id, len(chunks), current, current_bytes))
            current = []
            current_bytes = 0
        current.append(item)
        current_bytes += item.size
    if current:
        chunks.append(_chunk(module_id, len(chunks), current, current_bytes))
    counts = Counter(item.language for item in ordered)
    return ScanModule(
        id=module_id,
        path=module_path,
        name=Path(module_path).name if module_path != "." else "仓库根目录",
        file_count=len(ordered),
        total_bytes=sum(item.size for item in ordered),
        languages=dict(sorted(counts.items())),
        chunks=chunks,
    )


def _chunk(module_id: str, index: int, files: list[PlannedFile], total_bytes: int) -> ExecutionChunk:
    return ExecutionChunk(
        id=f"{module_id}-{index:05d}",
        index=index,
        file_count=len(files),
        total_bytes=total_bytes,
        files=list(files),
    )
