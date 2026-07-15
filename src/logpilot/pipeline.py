from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .ai import (
    analyze_targets_with_ai,
    analyze_with_ai,
    discover_logging_apis_with_ai,
    inspect_unsupported_languages_with_ai,
)
from .config import load_config
from .history import write_history_run
from .fixes import attach_fix_proposals
from .locking import repository_operation_lock
from .models import ScanReport
from .patching import write_patch
from .reporting import build_report, write_report
from .rules import analyze_rules
from .scanner import scan_repository_detailed
from .parsers import promote_framework_candidates
from .settings import build_language_profile, load_repository_settings, selected_extensions
from .runtime import RuntimeExecutor, RuntimeRegistry
from .storage import initialize_repository_storage


ScanProgress = Callable[[dict[str, Any]], None]


def run_scan(
    repo_root: Path,
    config_path: Path | None = None,
    runtime_id: str | None = None,
    runtime_registry: RuntimeRegistry | None = None,
    runtime_executor: RuntimeExecutor | None = None,
    progress: ScanProgress | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ScanReport:
    repo_root = repo_root.resolve()
    with repository_operation_lock(repo_root):
        _emit(progress, "preparing", 0, 1, "正在读取仓库配置")
        _check_cancel(should_cancel)
        config = load_config(repo_root, config_path)
        app_settings = load_repository_settings(repo_root)
        extensions = selected_extensions(app_settings)
        if extensions is not None:
            config.scan.include_extensions = extensions
        _emit(progress, "discovering", 0, 0, "正在发现源码文件")
        last_reported = 0

        def report_file(completed: int, total: int, path: str) -> None:
            nonlocal last_reported
            interval = max(1, total // 200)
            if completed == 1 or completed == total or completed - last_reported >= interval:
                last_reported = completed
                _emit(progress, "parsing", completed, total, f"正在解析 {path}")

        scan = scan_repository_detailed(repo_root, config.scan, report_file, should_cancel)
        logs = list(scan.logs)
        files_scanned = scan.files_scanned
        _check_cancel(should_cancel)
        ai_requested = runtime_id is not None or config.ai.enabled
        analysis_targets = _targets_for_depth(scan.analysis_targets, app_settings.analysis_depth)
        _emit(progress, "framework", 0, 1, "正在识别自定义日志框架")
        framework_definitions, framework_traces = discover_logging_apis_with_ai(
            analysis_targets,
            config.ai,
            repo_root,
            runtime_id=runtime_id,
            registry=runtime_registry,
            executor=runtime_executor,
            should_cancel=should_cancel,
        )
        promoted_logs = promote_framework_candidates(analysis_targets, framework_definitions)
        language_insights, language_traces = inspect_unsupported_languages_with_ai(
            analysis_targets,
            config.ai,
            repo_root,
            runtime_id=runtime_id,
            registry=runtime_registry,
            executor=runtime_executor,
            should_cancel=should_cancel,
        )
        framework_traces = language_traces + framework_traces
        known_ids = {log.id for log in logs}
        logs.extend(log for log in promoted_logs if log.id not in known_ids)
        _emit(
            progress,
            "framework",
            1,
            1,
            f"日志框架识别完成，确认 {len(promoted_logs)} 条自定义日志",
        )
        _check_cancel(should_cancel)
        language_profile = build_language_profile(
            repo_root,
            logs,
            config.scan.exclude,
            scan.discovered_language_counts,
            scan.unrecognized_extension_counts,
        )
        enabled_languages = set(app_settings.selected_languages) if app_settings.language_mode == "custom" else None
        _emit(progress, "rules", 0, 1, f"正在检查 {len(logs)} 条日志调用")
        rule_issues = analyze_rules(repo_root, logs, config.rules, enabled_languages)
        attach_fix_proposals(repo_root, logs, rule_issues, app_settings, language_profile)
        report_kwargs = {
            "discovered_language_counts": scan.discovered_language_counts,
            "analyzed_language_counts": scan.language_file_counts,
            "failed_language_counts": scan.failed_language_counts,
            "unrecognized_extension_counts": scan.unrecognized_extension_counts,
            "analysis_scope": "custom" if app_settings.language_mode == "custom" else "repository",
        }
        partial_report = build_report(
            repo_root,
            files_scanned,
            logs,
            rule_issues,
            framework_traces,
            ai_status="running" if ai_requested else "skipped",
            language_insights=language_insights,
            **report_kwargs,
        )
        _emit(
            progress,
            "rules",
            1,
            1,
            f"本地规则发现 {len(rule_issues)} 项问题",
            partial_report,
        )
        _check_cancel(should_cancel)

        def report_runtime_batch(
            completed: int,
            total: int,
            current_issues,
            current_traces,
        ) -> None:
            combined = rule_issues + current_issues
            attach_fix_proposals(repo_root, logs, combined, app_settings, language_profile)
            current_report = build_report(
                repo_root,
                files_scanned,
                logs,
                combined,
                framework_traces + current_traces,
                ai_status="running",
                language_insights=language_insights,
                **report_kwargs,
            )
            _emit(
                progress,
                "runtime",
                completed,
                total,
                f"运行时分析完成 {completed} / {total} 批",
                current_report,
            )

        _emit(progress, "runtime", 0, 0, "正在准备运行时分析")
        ai_issues, quality_traces = analyze_with_ai(
            _logs_for_depth(logs, app_settings.analysis_depth),
            config.ai,
            repo_root,
            runtime_id=runtime_id,
            registry=runtime_registry,
            executor=runtime_executor,
            progress=report_runtime_batch,
            should_cancel=should_cancel,
        )
        if not quality_traces and not framework_traces:
            _emit(progress, "runtime", 1, 1, "当前配置已跳过运行时分析", partial_report)
        _check_cancel(should_cancel)
        _emit(progress, "ai_missing", 0, 1, "正在检查异常路径的日志缺口")
        missing_issues, missing_traces = analyze_targets_with_ai(
            analysis_targets,
            config.ai,
            repo_root,
            runtime_id=runtime_id,
            registry=runtime_registry,
            executor=runtime_executor,
            should_cancel=should_cancel,
        )
        ai_traces = framework_traces + quality_traces + missing_traces
        ai_status = _ai_status(ai_requested, ai_traces)
        issues = rule_issues + ai_issues + missing_issues
        missing_report = build_report(
            repo_root,
            files_scanned,
            logs,
            issues,
            ai_traces,
            ai_status=ai_status,
            language_insights=language_insights,
            **report_kwargs,
        )
        _emit(
            progress,
            "ai_missing",
            1,
            1,
            f"异常路径分析完成，发现 {len(missing_issues)} 项建议",
            missing_report,
        )
        _check_cancel(should_cancel)
        _emit(progress, "fixes", 0, 1, "正在生成安全修改建议")
        attach_fix_proposals(repo_root, logs, issues, app_settings, language_profile)
        report = build_report(
            repo_root,
            files_scanned,
            logs,
            issues,
            ai_traces,
            ai_status=ai_status,
            language_insights=language_insights,
            **report_kwargs,
        )
        _emit(progress, "fixes", 1, 1, "安全修改建议已生成", report)
        _check_cancel(should_cancel)
        _emit(progress, "reporting", 0, 1, "正在保存报告和历史记录")
        out = initialize_repository_storage(repo_root)
        write_report(report, out)
        patch_text = write_patch(repo_root, logs, issues, out)
        write_history_run(report, patch_text, out)
        _emit(progress, "reporting", 1, 1, "报告与历史记录已保存", report)
        return report


def _emit(
    progress: ScanProgress | None,
    stage: str,
    completed: int,
    total: int,
    message: str,
    report: ScanReport | None = None,
) -> None:
    if progress:
        progress(
            {
                "stage": stage,
                "completed": completed,
                "total": total,
                "message": message,
                "report": report,
            }
        )


def _check_cancel(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel and should_cancel():
        raise InterruptedError("分析已取消。")


def _ai_status(requested: bool, traces) -> str:
    if not requested:
        return "skipped"
    if not traces:
        return "complete"
    return "complete" if all(trace.status in {"ok", "cached"} for trace in traces) else "partial"


def _logs_for_depth(logs, depth: str):
    limit = {"quick": 100, "standard": 1000}.get(depth)
    if limit is None:
        return logs
    prioritized = sorted(
        logs,
        key=lambda log: (
            log.level not in {"error", "critical", "fatal", "warning", "debug"},
            log.file_path,
            log.line,
        ),
    )
    return prioritized[:limit]


def _targets_for_depth(targets, depth: str):
    limits_by_depth = {
        "quick": {"framework_candidate": 40, "error_path": 50, "unsupported_sample": 8},
        "standard": {"framework_candidate": 200, "error_path": 300, "unsupported_sample": 12},
    }
    limits = limits_by_depth.get(depth)
    if limits is None:
        return targets
    counts = {kind: 0 for kind in limits}
    selected = []
    for target in targets:
        limit = limits.get(target.kind)
        if limit is None or counts[target.kind] >= limit:
            continue
        counts[target.kind] += 1
        selected.append(target)
    return selected
