from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import ScanReport
from .reporting import render_markdown
from .result_store import RunResultStore


def write_history_run(report: ScanReport, patch_text: str, output_dir: Path) -> dict[str, Any]:
    run_id = _new_run_id()
    run_dir = output_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)

    metadata = _metadata_for(report, run_id)
    (run_dir / "report.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    (run_dir / "changes.diff").write_text(patch_text, encoding="utf-8")
    (run_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return metadata


def list_history_runs(output_dir: Path) -> list[dict[str, Any]]:
    runs_dir = output_dir / "runs"
    if not runs_dir.exists():
        return []

    runs: list[dict[str, Any]] = []
    for metadata_path in runs_dir.glob("*/metadata.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(metadata, dict) and "run_id" in metadata:
            runs.append(metadata)

    return sorted(runs, key=lambda item: str(item.get("created_at", "")), reverse=True)


def load_history_run(output_dir: Path, run_id: str) -> dict[str, Any]:
    safe_id = _safe_run_id(run_id)
    run_dir = output_dir / "runs" / safe_id
    if not run_dir.exists():
        raise FileNotFoundError(f"History run not found: {safe_id}")

    database = run_dir / "results.sqlite3"
    if database.is_file():
        report = RunResultStore(database).load_report_dict()
    else:
        report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    patch_path = run_dir / "changes.diff"
    patch = patch_path.read_text(encoding="utf-8", errors="ignore") if patch_path.is_file() else ""
    metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    return {"metadata": metadata, "report": report, "patch": patch}


def _new_run_id() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f")


def _metadata_for(report: ScanReport, run_id: str) -> dict[str, Any]:
    summary = report.summary
    trace = report.ai_traces[0] if report.ai_traces else None
    return {
        "run_id": run_id,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "repository": summary.repository,
        "score": summary.score,
        "score_status": summary.score_status,
        "files_scanned": summary.files_scanned,
        "discovered_files": summary.discovered_files,
        "coverage_ratio": summary.coverage_ratio,
        "coverage_status": summary.coverage_status,
        "ai_status": summary.ai_status,
        "parse_failure_count": len(report.parse_failures),
        "excluded_mapping_count": len(report.excluded_mappings),
        "log_count": summary.log_count,
        "issue_count": summary.issue_count,
        "severity_counts": summary.severity_counts,
        "runtime_id": trace.runtime_id if trace else "",
        "runtime_version": trace.runtime_version if trace else "",
    }


def _safe_run_id(run_id: str) -> str:
    if not run_id or any(char in run_id for char in "\\/.:"):
        raise ValueError("Invalid history run id.")
    return run_id
