from __future__ import annotations

import json
from pathlib import Path

from .models import Issue, LogCall, ScanReport, ScanSummary, Severity


def build_report(repo_root: Path, files_scanned: int, logs: list[LogCall], issues: list[Issue], ai_traces) -> ScanReport:
    severity_counts = {severity.value: 0 for severity in Severity}
    for issue in issues:
        severity_counts[issue.severity.value] += 1
    score = _score(severity_counts)
    summary = ScanSummary(
        repository=str(repo_root.resolve()),
        score=score,
        files_scanned=files_scanned,
        log_count=len(logs),
        issue_count=len(issues),
        severity_counts=severity_counts,
    )
    return ScanReport(summary=summary, logs=logs, issues=issues, ai_traces=ai_traces)


def write_report(report: ScanReport, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")


def render_markdown(report: ScanReport) -> str:
    summary = report.summary
    lines = [
        "# LogPilot Report",
        "",
        "## Overview",
        "",
        f"- Repository: `{summary.repository}`",
        f"- Score: **{summary.score}/100**",
        f"- Files scanned: {summary.files_scanned}",
        f"- Logs found: {summary.log_count}",
        f"- Issues found: {summary.issue_count}",
        f"- Severity: high={summary.severity_counts.get('high', 0)}, "
        f"medium={summary.severity_counts.get('medium', 0)}, low={summary.severity_counts.get('low', 0)}",
        "",
        "## Issues",
        "",
    ]
    if not report.issues:
        lines.append("No issues found.")
    for issue in report.issues:
        lines.extend(
            [
                f"### {issue.title}",
                "",
                f"- File: `{issue.file_path}:{issue.line}`",
                f"- Severity: `{issue.severity.value}`",
                f"- Source: `{issue.source}`",
                f"- Reason: {issue.reason}",
                f"- Suggestion: {issue.suggestion}",
                "",
            ]
        )
    lines.extend(["## Logs", ""])
    if not report.logs:
        lines.append("No log calls found.")
    for log in report.logs:
        lines.extend(
            [
                f"- `{log.file_path}:{log.line}` `{log.level}` `{log.callee}` - {log.message or '<empty>'}",
            ]
        )
    return "\n".join(lines) + "\n"


def _score(severity_counts: dict[str, int]) -> int:
    penalty = (
        severity_counts.get("high", 0) * 20
        + severity_counts.get("medium", 0) * 10
        + severity_counts.get("low", 0) * 5
    )
    return max(0, 100 - penalty)
