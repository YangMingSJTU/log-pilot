from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(slots=True)
class LogCall:
    id: str
    file_path: str
    line: int
    column: int
    language: str
    level: str
    callee: str
    message: str
    context: str
    source_line: str
    end_line: int = 0
    safe_to_delete: bool = False


@dataclass(slots=True)
class FixProposal:
    id: str
    action: str
    file_path: str
    start_line: int
    end_line: int
    expected_text: str
    replacement_text: str
    context: str
    description: str
    source: str
    line_delta: int
    issue_ids: list[str] = field(default_factory=list)
    log_call_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Issue:
    id: str
    file_path: str
    line: int
    severity: Severity
    kind: str
    title: str
    reason: str
    suggestion: str
    source: str
    log_call_id: str | None = None
    patch_action: str | None = None
    context: str = ""
    source_line: str = ""
    fix: FixProposal | None = None


@dataclass(slots=True)
class AiTrace:
    log_call_id: str
    status: str
    prompt: str
    raw_response: str = ""
    error: str = ""
    runtime_id: str = ""
    runtime_version: str = ""
    duration_ms: int = 0


@dataclass(slots=True)
class ScanSummary:
    repository: str
    score: int
    files_scanned: int
    log_count: int
    issue_count: int
    severity_counts: dict[str, int]


@dataclass(slots=True)
class ScanReport:
    summary: ScanSummary
    logs: list[LogCall] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    ai_traces: list[AiTrace] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PatchOperation:
    file_path: str
    line_numbers: list[int]
    issue_ids: list[str]
    log_call_ids: list[str]
    before_sha256: str = ""
    after_sha256: str = ""
    backup_file: str = ""
    edits: list[FixProposal] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ApplyRecord:
    apply_id: str
    run_id: str
    repository: str
    created_at: str
    status: str
    issue_ids: list[str]
    operations: list[PatchOperation]
    rolled_back_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()
