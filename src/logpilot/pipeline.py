from __future__ import annotations

from pathlib import Path

from .ai import analyze_with_ai
from .config import load_config
from .history import write_history_run
from .models import ScanReport
from .patching import write_patch
from .reporting import build_report, write_report
from .rules import analyze_rules
from .scanner import scan_repository
from .runtime import RuntimeExecutor, RuntimeRegistry


def run_scan(
    repo_root: Path,
    output_dir: Path | None = None,
    config_path: Path | None = None,
    runtime_id: str | None = None,
    runtime_registry: RuntimeRegistry | None = None,
    runtime_executor: RuntimeExecutor | None = None,
) -> ScanReport:
    repo_root = repo_root.resolve()
    config = load_config(repo_root, config_path)
    logs, files_scanned = scan_repository(repo_root, config.scan)
    rule_issues = analyze_rules(repo_root, logs, config.rules)
    ai_issues, ai_traces = analyze_with_ai(
        logs,
        config.ai,
        repo_root,
        runtime_id=runtime_id,
        registry=runtime_registry,
        executor=runtime_executor,
    )
    issues = rule_issues + ai_issues
    report = build_report(repo_root, files_scanned, logs, issues, ai_traces)
    out = output_dir or repo_root / ".logpilot"
    write_report(report, out)
    patch_text = write_patch(repo_root, logs, issues, out)
    write_history_run(report, patch_text, out)
    return report
