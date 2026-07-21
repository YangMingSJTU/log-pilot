from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Any

from .models import ScanReport


TERMINAL_SCAN_STATUSES = {"completed", "failed", "cancelled"}
_STAGE_RANGES = {
    "queued": (0, 1),
    "preparing": (1, 4),
    "discovering": (4, 8),
    "parsing": (8, 38),
    "framework": (38, 48),
    "rules": (48, 58),
    "runtime": (58, 78),
    "ai_missing": (78, 86),
    "fixes": (86, 94),
    "reporting": (94, 99),
    "complete": (100, 100),
}


class ScanJob:
    def __init__(self, repository: str, runtime_id: str) -> None:
        self.id = uuid.uuid4().hex
        self.repository = repository
        self.runtime_id = runtime_id
        self.status = "queued"
        self.stage = "queued"
        self.completed = 0
        self.total = 1
        self.message = "分析任务已创建"
        self.error = ""
        self.run_id = ""
        self.report_version = 0
        self._summary: dict[str, Any] = {}
        self._modules: list[dict[str, Any]] = []
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        self.created_at = _now()
        self.updated_at = self.created_at

    @property
    def is_terminal(self) -> bool:
        with self._lock:
            return self.status in TERMINAL_SCAN_STATUSES

    def update(self, event: dict[str, Any]) -> None:
        job_progress = event.get("progress")
        with self._lock:
            if self.status in TERMINAL_SCAN_STATUSES:
                return
            self.status = "cancelling" if self._cancel_event.is_set() else "running"
            self.stage = str(event.get("stage", self.stage))
            self.completed = max(0, int(event.get("completed", self.completed)))
            self.total = max(0, int(event.get("total", self.total)))
            self.message = str(event.get("message", self.message))
            if isinstance(job_progress, dict):
                self.run_id = str(job_progress.get("run_id", self.run_id))
                raw_modules = job_progress.get("modules", [])
                self._modules = list(raw_modules) if isinstance(raw_modules, list) else self._modules
                raw_summary = event.get("summary", job_progress.get("summary", {}))
                if isinstance(raw_summary, dict):
                    self._summary = dict(raw_summary)
                self.report_version += 1
            self.updated_at = _now()

    def request_cancel(self) -> bool:
        with self._lock:
            if self.status in TERMINAL_SCAN_STATUSES:
                return False
            self._cancel_event.set()
            self.status = "cancelling"
            self.message = "正在停止，当前步骤完成后退出"
            self.updated_at = _now()
            return True

    def should_cancel(self) -> bool:
        return self._cancel_event.is_set()

    def complete(self, report: ScanReport | None, run_id: str) -> None:
        with self._lock:
            self.status = "completed"
            self.stage = "complete"
            self.completed = 1
            self.total = 1
            self.message = "分析完成"
            self.error = ""
            self.run_id = run_id
            if report is not None:
                self._summary = report.to_dict().get("summary", {})
            self.report_version += 1
            self.updated_at = _now()

    def fail(self, error: str) -> None:
        with self._lock:
            self.status = "failed"
            self.error = error
            self.message = "分析失败"
            self.updated_at = _now()

    def cancel(self) -> None:
        with self._lock:
            self.status = "cancelled"
            self.error = ""
            self.message = "分析已取消，部分结果未保存"
            self.updated_at = _now()

    def snapshot(self, known_report_version: int = -1) -> dict[str, Any]:
        with self._lock:
            payload: dict[str, Any] = {
                "job_id": self.id,
                "repository": self.repository,
                "runtime_id": self.runtime_id,
                "status": self.status,
                "stage": self.stage,
                "completed": self.completed,
                "total": self.total,
                "percent": _overall_percent(self.stage, self.completed, self.total),
                "message": self.message,
                "error": self.error,
                "run_id": self.run_id,
                "report_version": self.report_version,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "modules": list(self._modules),
            }
            if self._summary and known_report_version < self.report_version:
                # Kept as a compact compatibility envelope; full findings stay in SQLite.
                summary = dict(self._summary)
                if "issue_count" not in summary:
                    summary["issue_count"] = int(summary.get("issue_count", 0))
                payload["partial_report"] = {
                    "summary": summary,
                    "logs": [],
                    "issues": [],
                    "ai_traces": [],
                    "language_insights": [],
                    "parse_failures": [],
                }
            return payload


def _overall_percent(stage: str, completed: int, total: int) -> int:
    start, end = _STAGE_RANGES.get(stage, (0, 99))
    if start == end:
        return start
    ratio = min(1.0, max(0.0, completed / total)) if total > 0 else 0.0
    return round(start + (end - start) * ratio)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
