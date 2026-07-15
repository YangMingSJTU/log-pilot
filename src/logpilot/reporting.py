from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .languages import LANGUAGE_SPECS
from .models import AiTrace, Issue, LanguageCoverage, LogCall, ScanReport, ScanSummary, Severity


def build_report(
    repo_root: Path,
    files_scanned: int,
    logs: list[LogCall],
    issues: list[Issue],
    ai_traces: list[AiTrace],
    *,
    discovered_language_counts: Mapping[str, int] | None = None,
    analyzed_language_counts: Mapping[str, int] | None = None,
    failed_language_counts: Mapping[str, int] | None = None,
    unrecognized_extension_counts: Mapping[str, int] | None = None,
    analysis_scope: str = "repository",
    ai_status: str | None = None,
    language_insights: list[dict[str, object]] | None = None,
) -> ScanReport:
    severity_counts = {severity.value: 0 for severity in Severity}
    for issue in issues:
        severity_counts[issue.severity.value] += 1
    discovered_counts = dict(discovered_language_counts or {})
    analyzed_counts = dict(analyzed_language_counts or {})
    failed_counts = dict(failed_language_counts or {})
    unrecognized_counts = dict(unrecognized_extension_counts or {})
    unrecognized_files = sum(unrecognized_counts.values())
    discovered_files = sum(discovered_counts.values()) + unrecognized_files or files_scanned
    failed_files = sum(failed_counts.values())
    coverage_ratio = round(files_scanned / discovered_files, 4) if discovered_files else 0.0
    unsupported_files = unrecognized_files + sum(
        discovered_counts.get(spec.id, 0)
        for spec in LANGUAGE_SPECS
        if not spec.analyzable
    )
    coverage_status = _coverage_status(discovered_files, files_scanned, failed_files, coverage_ratio)
    resolved_ai_status = ai_status or _ai_status(ai_traces)
    score_status = _score_status(logs, coverage_status, analysis_scope, resolved_ai_status)
    score = _score(severity_counts) if score_status in {"scored", "scoped", "local_only"} else None
    log_counts: dict[str, int] = {}
    for log in logs:
        log_counts[log.language] = log_counts.get(log.language, 0) + 1
    language_coverage = [
        LanguageCoverage(
            language=spec.id,
            label=spec.label,
            support_level=spec.support_level,
            discovered_files=discovered_counts.get(spec.id, 0),
            analyzed_files=analyzed_counts.get(spec.id, 0),
            failed_files=failed_counts.get(spec.id, 0),
            log_count=log_counts.get(spec.id, 0),
        )
        for spec in LANGUAGE_SPECS
        if discovered_counts.get(spec.id, 0) > 0
    ]
    summary = ScanSummary(
        repository=str(repo_root.resolve()),
        score=score,
        files_scanned=files_scanned,
        log_count=len(logs),
        issue_count=len(issues),
        severity_counts=severity_counts,
        discovered_files=discovered_files,
        unsupported_files=unsupported_files,
        unrecognized_files=unrecognized_files,
        failed_files=failed_files,
        coverage_ratio=coverage_ratio,
        coverage_status=coverage_status,
        analysis_scope=analysis_scope,
        ai_status=resolved_ai_status,
        score_status=score_status,
        language_coverage=language_coverage,
        unrecognized_extensions=unrecognized_counts,
    )
    return ScanReport(
        summary=summary,
        logs=logs,
        issues=issues,
        ai_traces=ai_traces,
        language_insights=list(language_insights or []),
    )


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
        f"- Score: **{summary.score}/100**" if summary.score is not None else f"- Score: **N/A** ({summary.score_status})",
        f"- Files scanned: {summary.files_scanned}/{summary.discovered_files}",
        f"- Coverage: {summary.coverage_ratio:.1%} ({summary.coverage_status})",
        f"- AI status: {summary.ai_status}",
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


def _coverage_status(discovered: int, analyzed: int, failed: int, ratio: float) -> str:
    if discovered == 0:
        return "no_source"
    if analyzed == 0:
        return "unsupported"
    if ratio >= 0.95 and failed == 0:
        return "complete"
    return "partial"


def _ai_status(traces: list[AiTrace]) -> str:
    if not traces:
        return "skipped"
    return "complete" if all(trace.status in {"ok", "cached"} for trace in traces) else "partial"


def _score_status(
    logs: list[LogCall],
    coverage_status: str,
    analysis_scope: str,
    ai_status: str,
) -> str:
    if not logs:
        return "no_log_samples"
    if coverage_status != "complete" and analysis_scope != "custom":
        return "insufficient_coverage"
    if ai_status in {"running", "partial", "failed"}:
        return "ai_incomplete"
    if analysis_scope == "custom":
        return "scoped"
    if ai_status == "skipped":
        return "local_only"
    return "scored"
