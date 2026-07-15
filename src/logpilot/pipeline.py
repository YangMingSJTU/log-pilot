from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .ai import analyze_with_ai
from .config import load_config
from .history import write_history_run
from .fixes import attach_fix_proposals
from .locking import repository_operation_lock
from .models import ScanReport
from .patching import write_patch
from .reporting import build_report, write_report
from .rules import analyze_rules
from .scanner import scan_repository_detailed
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
        logs = scan.logs
        files_scanned = scan.files_scanned
        _check_cancel(should_cancel)
        language_profile = build_language_profile(
            repo_root,
            logs,
            config.scan.exclude,
            scan.language_file_counts,
        )
        enabled_languages = set(app_settings.selected_languages) if app_settings.language_mode == "custom" else None
        _emit(progress, "rules", 0, 1, f"正在检查 {len(logs)} 条日志调用")
        rule_issues = analyze_rules(repo_root, logs, config.rules, enabled_languages)
        attach_fix_proposals(repo_root, logs, rule_issues, app_settings, language_profile)
        partial_report = build_report(repo_root, files_scanned, logs, rule_issues, [])
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
            current_report = build_report(repo_root, files_scanned, logs, combined, current_traces)
            _emit(
                progress,
                "runtime",
                completed,
                total,
                f"运行时分析完成 {completed} / {total} 批",
                current_report,
            )

        _emit(progress, "runtime", 0, 0, "正在准备运行时分析")
        ai_issues, ai_traces = analyze_with_ai(
            logs,
            config.ai,
            repo_root,
            runtime_id=runtime_id,
            registry=runtime_registry,
            executor=runtime_executor,
            progress=report_runtime_batch,
            should_cancel=should_cancel,
        )
        if not ai_traces:
            _emit(progress, "runtime", 1, 1, "当前配置已跳过运行时分析", partial_report)
        _check_cancel(should_cancel)
        issues = rule_issues + ai_issues
        _emit(progress, "fixes", 0, 1, "正在生成安全修改建议")
        attach_fix_proposals(repo_root, logs, issues, app_settings, language_profile)
        report = build_report(repo_root, files_scanned, logs, issues, ai_traces)
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
