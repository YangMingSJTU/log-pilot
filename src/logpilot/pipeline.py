from __future__ import annotations

from pathlib import Path

from .ai import analyze_with_ai
from .config import load_config
from .history import write_history_run
from .fixes import attach_fix_proposals
from .locking import repository_operation_lock
from .models import ScanReport
from .patching import write_patch
from .reporting import build_report, write_report
from .rules import analyze_rules
from .scanner import scan_repository
from .settings import build_language_profile, load_repository_settings, selected_extensions
from .runtime import RuntimeExecutor, RuntimeRegistry
from .storage import initialize_repository_storage


def run_scan(
    repo_root: Path,
    config_path: Path | None = None,
    runtime_id: str | None = None,
    runtime_registry: RuntimeRegistry | None = None,
    runtime_executor: RuntimeExecutor | None = None,
) -> ScanReport:
    repo_root = repo_root.resolve()
    with repository_operation_lock(repo_root):
        config = load_config(repo_root, config_path)
        app_settings = load_repository_settings(repo_root)
        extensions = selected_extensions(app_settings)
        if extensions is not None:
            config.scan.include_extensions = extensions
        logs, files_scanned = scan_repository(repo_root, config.scan)
        language_profile = build_language_profile(repo_root, logs, config.scan.exclude)
        enabled_languages = set(app_settings.selected_languages) if app_settings.language_mode == "custom" else None
        rule_issues = analyze_rules(repo_root, logs, config.rules, enabled_languages)
        ai_issues, ai_traces = analyze_with_ai(
            logs,
            config.ai,
            repo_root,
            runtime_id=runtime_id,
            registry=runtime_registry,
            executor=runtime_executor,
        )
        issues = rule_issues + ai_issues
        attach_fix_proposals(repo_root, logs, issues, app_settings, language_profile)
        report = build_report(repo_root, files_scanned, logs, issues, ai_traces)
        out = initialize_repository_storage(repo_root)
        write_report(report, out)
        patch_text = write_patch(repo_root, logs, issues, out)
        write_history_run(report, patch_text, out)
        return report
