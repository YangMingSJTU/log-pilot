from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .ai import (
    analyze_targets_with_ai,
    analyze_with_ai,
    discover_logging_apis_with_ai,
    inspect_unsupported_languages_with_ai,
)
from .config import load_config
from .fixes import attach_fix_proposals
from .languages import LANGUAGE_SPECS
from .locking import repository_operation_lock
from .models import AnalysisTarget, LanguageCoverage, ScanReport, ScanSummary
from .native_parser_client import NativeParserClient
from .parsers import promote_framework_candidates
from .patching import write_patch
from .planning import (
    ScanPlan,
    build_scan_plan,
    load_scan_plan,
    save_scan_plan,
    selected_modules,
)
from .reporting import write_report
from .result_store import RunResultStore, report_from_dict
from .rules import analyze_rules
from .runtime import RuntimeExecutor, RuntimeRegistry
from .scanner import _unsupported_sample_target, scan_repository_detailed
from .settings import (
    build_language_profile,
    load_language_profile,
    load_repository_settings,
    selected_extensions,
)
from .storage import initialize_repository_storage


ScanProgress = Callable[[dict[str, Any]], None]
_AI_PAGE_SIZE = 500


def run_scan(
    repo_root: Path,
    config_path: Path | None = None,
    runtime_id: str | None = None,
    runtime_registry: RuntimeRegistry | None = None,
    runtime_executor: RuntimeExecutor | None = None,
    progress: ScanProgress | None = None,
    should_cancel: Callable[[], bool] | None = None,
    *,
    plan: ScanPlan | None = None,
    plan_id: str | None = None,
    module_ids: list[str] | None = None,
    include_large_files: bool = False,
    run_id: str | None = None,
    resume: bool = False,
    return_report: bool = True,
) -> ScanReport | None:
    repo_root = repo_root.expanduser().resolve()
    with repository_operation_lock(repo_root):
        _emit(progress, "preparing", 0, 1, "正在读取仓库配置")
        _check_cancel(should_cancel)
        config = load_config(repo_root, config_path)
        settings = load_repository_settings(repo_root)
        extensions = selected_extensions(settings)
        if extensions is not None:
            config.scan.include_extensions = extensions

        if plan is None:
            plan = load_scan_plan(repo_root, plan_id) if plan_id else build_scan_plan(
                repo_root,
                config.scan,
                include_large_files=include_large_files,
            )
        if Path(plan.repository).resolve() != repo_root:
            raise ValueError("扫描计划不属于当前仓库。")
        if not plan_id:
            save_scan_plan(plan)
        modules = selected_modules(plan, module_ids)
        output_dir = initialize_repository_storage(repo_root)
        resolved_run_id = run_id or _new_run_id()
        store = RunResultStore.for_run(output_dir, resolved_run_id)
        if not resume or not store.database.exists():
            store.initialize_run(plan, modules, runtime_id or config.ai.runtime, settings.analysis_depth)
        else:
            store.mark_running_interrupted()
        summary_modules = selected_modules(plan, store.selected_module_ids()) if resume else modules
        store.start_run()
        _write_running_metadata(store, plan, runtime_id or config.ai.runtime, summary_modules)
        _emit(progress, "discovering", 1, 1, f"已规划 {len(modules)} 个目录模块", store=store)

        profile = load_language_profile(repo_root)
        enabled_languages = set(settings.selected_languages) if settings.language_mode == "custom" else None
        completed_chunks = store.completed_chunk_ids() if resume else set()
        native_client = NativeParserClient()
        total_chunks = sum(len(module.chunks) for module in modules)
        finished_chunks = len(completed_chunks)
        try:
            for module in modules:
                module_failed = False
                for chunk in module.chunks:
                    if chunk.id in completed_chunks:
                        continue
                    _check_cancel(should_cancel)
                    store.begin_chunk(module.id, chunk.id)
                    _emit(
                        progress,
                        "parsing",
                        finished_chunks,
                        total_chunks,
                        f"正在扫描 {module.path} · 分片 {chunk.index + 1}/{len(module.chunks)}",
                        store=store,
                    )
                    try:
                        paths = [repo_root / item.path for item in chunk.files]
                        scan = scan_repository_detailed(
                            repo_root,
                            config.scan,
                            should_cancel=should_cancel,
                            file_paths=paths,
                            native_client=native_client,
                        )
                        local_issues = analyze_rules(
                            repo_root,
                            scan.logs,
                            config.rules,
                            enabled_languages,
                            scan.analysis_targets,
                        )
                        attach_fix_proposals(repo_root, scan.logs, local_issues, settings, profile)
                        failed_paths = {failure.file_path for failure in scan.parse_failures}
                        file_rows = [
                            {
                                "path": item.path,
                                "language": item.language,
                                "size": item.size,
                                "status": "failed" if item.path in failed_paths else "analyzed",
                                "error": "解析失败" if item.path in failed_paths else "",
                            }
                            for item in chunk.files
                        ]
                        store.replace_chunk(
                            module.id,
                            chunk.id,
                            file_rows,
                            scan.logs,
                            scan.analysis_targets,
                            local_issues,
                            scan.parse_failures,
                        )
                    except InterruptedError:
                        raise
                    except Exception as exc:
                        store.record_chunk_failure(
                            module.id,
                            chunk.id,
                            [asdict(item) for item in chunk.files],
                            str(exc),
                        )
                        module_failed = True
                        break
                    finally:
                        del paths
                    finished_chunks += 1
                    _emit(
                        progress,
                        "rules",
                        finished_chunks,
                        total_chunks,
                        f"{module.path} 已提交一个扫描分片",
                        store=store,
                    )
                if module_failed:
                    continue

            _check_cancel(should_cancel)
            language_insights, advisory_traces = _analyze_advisory_files(
                repo_root,
                plan,
                config.ai,
                runtime_id,
                runtime_registry,
                runtime_executor,
                should_cancel,
            )
            if language_insights:
                store.set_run_data("language_insights", language_insights)
            if advisory_traces:
                store.append_ai_results("", [], [], advisory_traces)

            ai_requested = runtime_id is not None or config.ai.enabled
            ai_complete = True
            if ai_requested:
                ai_complete = _run_ai_round_robin(
                    store,
                    modules,
                    repo_root,
                    config.ai,
                    settings,
                    profile,
                    runtime_id,
                    runtime_registry,
                    runtime_executor,
                    progress,
                    should_cancel,
                )
            else:
                for module in modules:
                    if next((item for item in store.progress()["modules"] if item["id"] == module.id), {}).get("status") != "failed":
                        store.complete_module(module.id)

            _check_cancel(should_cancel)
            counts = store.aggregate_counts()
            ai_status = "skipped" if not ai_requested else ("complete" if ai_complete else "partial")
            summary = _build_summary(repo_root, plan, summary_modules, counts, ai_status, settings.language_mode)
            store.finish_run(asdict(summary))
            _write_run_metadata(store, summary, runtime_id or config.ai.runtime)

            if not return_report:
                _emit(progress, "complete", 1, 1, "分析完成", store=store)
                _enforce_retention(output_dir)
                return None

            report = report_from_dict(store.load_report_dict())
            build_language_profile(
                repo_root,
                report.logs,
                config.scan.exclude,
                plan.discovered_languages,
                plan.unrecognized_extensions,
            )
            write_report(report, output_dir)
            patch_text = write_patch(repo_root, report.logs, report.issues, output_dir)
            run_dir = store.database.parent
            (run_dir / "changes.diff").write_text(patch_text, encoding="utf-8")
            _emit(progress, "complete", 1, 1, "分析完成", store=store)
            _enforce_retention(output_dir)
            return report
        except InterruptedError:
            store.finish_run(store.progress().get("summary", {}), status="cancelled")
            _update_metadata_status(store, "cancelled")
            raise
        except Exception as exc:
            store.finish_run(store.progress().get("summary", {}), status="failed", error=str(exc))
            _update_metadata_status(store, "failed", str(exc))
            raise
        finally:
            native_client.close()


def retry_module(
    repo_root: Path,
    run_id: str,
    module_id: str,
    *,
    runtime_id: str | None = None,
    progress: ScanProgress | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ScanReport | None:
    data_dir = initialize_repository_storage(repo_root)
    store = RunResultStore.for_run(data_dir, run_id)
    with store.connection() as connection:
        row = connection.execute("SELECT plan_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise FileNotFoundError(f"分析记录不存在：{run_id}")
    plan = load_scan_plan(repo_root, str(row[0]))
    store.close_module_results(module_id)
    return run_scan(
        repo_root,
        runtime_id=runtime_id,
        progress=progress,
        should_cancel=should_cancel,
        plan=plan,
        module_ids=[module_id],
        run_id=run_id,
        resume=True,
        return_report=False,
    )


def _run_ai_round_robin(
    store: RunResultStore,
    modules,
    repo_root: Path,
    ai_config,
    settings,
    profile,
    runtime_id,
    registry,
    executor,
    progress,
    should_cancel,
) -> bool:
    log_limit = {"quick": 100, "standard": 1_000}.get(settings.analysis_depth)
    target_limits = {
        "quick": {"framework_candidate": 40, "error_path": 50},
        "standard": {"framework_candidate": 200, "error_path": 300},
    }.get(settings.analysis_depth)
    remaining_logs = log_limit
    target_counts: Counter[str] = Counter()
    cursors = {module.id: {"logs": 0, "targets": 0, "done": False} for module in modules}
    active = list(modules)
    all_ok = True
    completed_rounds = 0
    while active:
        next_active = []
        made_progress = False
        log_share = (
            _AI_PAGE_SIZE
            if remaining_logs is None
            else max(0, min(_AI_PAGE_SIZE, (remaining_logs + len(active) - 1) // len(active)))
        )
        target_shares = {
            kind: max(0, (limit - target_counts[kind] + len(active) - 1) // len(active))
            for kind, limit in (target_limits or {}).items()
        }
        for module in active:
            _check_cancel(should_cancel)
            cursor = cursors[module.id]
            store.begin_module_ai(module.id)
            page_limit = log_share if remaining_logs is None else min(log_share, max(0, remaining_logs))
            logs = store.module_logs(module.id, page_limit, cursor["logs"]) if page_limit else []
            targets = store.module_targets(module.id, _AI_PAGE_SIZE, cursor["targets"])
            raw_target_count = len(targets)
            if target_limits is not None:
                selected_target_counts: Counter[str] = Counter()
                selected_targets = []
                for target in targets:
                    if target.kind not in target_limits:
                        continue
                    if target_counts[target.kind] >= target_limits[target.kind]:
                        continue
                    if selected_target_counts[target.kind] >= target_shares[target.kind]:
                        continue
                    selected_target_counts[target.kind] += 1
                    selected_targets.append(target)
                targets = selected_targets
            if not logs and not targets:
                store.complete_module(module.id)
                cursor["done"] = True
                continue
            made_progress = True
            cursor["logs"] += len(logs)
            cursor["targets"] += _AI_PAGE_SIZE
            if remaining_logs is not None:
                remaining_logs -= len(logs)
            for target in targets:
                target_counts[target.kind] += 1

            definitions, framework_traces = discover_logging_apis_with_ai(
                targets, ai_config, repo_root, runtime_id, registry, executor, should_cancel
            )
            promoted = promote_framework_candidates(targets, definitions)
            _emit(
                progress,
                "runtime",
                completed_rounds,
                max(completed_rounds + len(active), len(modules)),
                f"正在分析 {module.path}",
                store=store,
            )
            quality_issues, quality_traces = analyze_with_ai(
                logs + promoted, ai_config, repo_root, runtime_id, registry, executor,
                should_cancel=should_cancel,
            )
            missing_issues, missing_traces = analyze_targets_with_ai(
                targets, ai_config, repo_root, runtime_id, registry, executor, should_cancel
            )
            issues = quality_issues + missing_issues
            attach_fix_proposals(repo_root, logs + promoted, issues, settings, profile)
            traces = framework_traces + quality_traces + missing_traces
            all_ok = all_ok and all(trace.status in {"ok", "cached"} for trace in traces)
            store.append_ai_results(module.id, promoted, issues, traces)
            completed_rounds += 1
            _emit(
                progress,
                "runtime",
                completed_rounds,
                max(completed_rounds + len(next_active), len(modules)),
                f"{module.path} 已完成一轮 AI 分析",
                store=store,
            )
            can_continue_logs = remaining_logs is None or remaining_logs > 0
            target_budget_left = target_limits is None or any(
                target_counts[kind] < limit for kind, limit in target_limits.items()
            )
            if (page_limit and len(logs) == page_limit and can_continue_logs) or (
                raw_target_count == _AI_PAGE_SIZE and target_budget_left
            ):
                next_active.append(module)
            else:
                store.complete_module(module.id)
                cursor["done"] = True
        if not made_progress:
            break
        active = next_active
    for module in modules:
        if not cursors[module.id]["done"]:
            store.complete_module(module.id)
    return all_ok


def _analyze_advisory_files(
    repo_root: Path,
    plan: ScanPlan,
    ai_config,
    runtime_id,
    registry,
    executor,
    should_cancel,
):
    targets: list[AnalysisTarget] = []
    for item in plan.advisory_files:
        targets.append(
            _unsupported_sample_target(
                repo_root / item.path,
                repo_root,
                item.language,
                Path(item.path).suffix.lower(),
            )
        )
    return inspect_unsupported_languages_with_ai(
        targets, ai_config, repo_root, runtime_id, registry, executor, should_cancel
    )


def _build_summary(
    repo_root: Path,
    plan: ScanPlan,
    modules,
    counts: dict[str, Any],
    ai_status: str,
    language_mode: str,
) -> ScanSummary:
    files_scanned = int(counts["files_scanned"])
    discovered_files = int(plan.source_files)
    failed_files = sum(counts["failed_languages"].values())
    coverage_ratio = round(files_scanned / discovered_files, 4) if discovered_files else 0.0
    selected_all = len(modules) == len(plan.modules)
    analysis_scope = "custom" if language_mode == "custom" else (
        "repository" if selected_all else "selected_modules"
    )
    if discovered_files == 0:
        coverage_status = "no_source"
    elif files_scanned == 0:
        coverage_status = "unsupported"
    elif coverage_ratio >= 0.95 and failed_files == 0 and selected_all and not plan.skipped_large_files:
        coverage_status = "complete"
    else:
        coverage_status = "partial"
    severity = {key: int(counts["severity_counts"].get(key, 0)) for key in ("high", "medium", "low")}
    if counts["log_count"] == 0:
        score_status = "no_log_samples"
    elif not selected_all:
        score_status = "scoped"
    elif coverage_status != "complete" and language_mode != "custom":
        score_status = "insufficient_coverage"
    elif ai_status == "partial":
        score_status = "ai_incomplete"
    elif ai_status == "skipped":
        score_status = "local_only"
    else:
        score_status = "scored"
    score = None
    if score_status in {"scored", "scoped", "local_only"}:
        score = max(0, 100 - severity["high"] * 20 - severity["medium"] * 10 - severity["low"] * 5)
    language_coverage = []
    for spec in LANGUAGE_SPECS:
        discovered = plan.discovered_languages.get(spec.id, 0)
        if not discovered:
            continue
        language_coverage.append(
            LanguageCoverage(
                language=spec.id,
                label=spec.label,
                support_level=spec.support_level,
                discovered_files=discovered,
                analyzed_files=counts["analyzed_languages"].get(spec.id, 0),
                failed_files=counts["failed_languages"].get(spec.id, 0),
                log_count=counts["log_languages"].get(spec.id, 0),
            )
        )
    unsupported = sum(
        plan.discovered_languages.get(spec.id, 0)
        for spec in LANGUAGE_SPECS
        if not spec.analyzable
    ) + sum(plan.unrecognized_extensions.values())
    return ScanSummary(
        repository=str(repo_root),
        score=score,
        files_scanned=files_scanned,
        log_count=int(counts["log_count"]),
        issue_count=int(counts["issue_count"]),
        severity_counts=severity,
        discovered_files=discovered_files,
        unsupported_files=unsupported,
        unrecognized_files=sum(plan.unrecognized_extensions.values()),
        failed_files=failed_files + len(plan.skipped_large_files),
        coverage_ratio=coverage_ratio,
        coverage_status=coverage_status,
        analysis_scope=analysis_scope,
        ai_status=ai_status,
        score_status=score_status,
        language_coverage=language_coverage,
        unrecognized_extensions=plan.unrecognized_extensions,
    )


def _write_run_metadata(store: RunResultStore, summary: ScanSummary, runtime_id: str) -> None:
    metadata = {
        "run_id": store.run_id,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "repository": summary.repository,
        "score": summary.score,
        "score_status": summary.score_status,
        "files_scanned": summary.files_scanned,
        "discovered_files": summary.discovered_files,
        "coverage_ratio": summary.coverage_ratio,
        "coverage_status": summary.coverage_status,
        "analysis_scope": summary.analysis_scope,
        "ai_status": summary.ai_status,
        "parse_failure_count": summary.failed_files,
        "log_count": summary.log_count,
        "issue_count": summary.issue_count,
        "severity_counts": summary.severity_counts,
        "runtime_id": runtime_id,
        "storage": "sqlite",
        "status": "completed",
    }
    (store.database.parent / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _write_running_metadata(store: RunResultStore, plan: ScanPlan, runtime_id: str, modules) -> None:
    payload = {
        "run_id": store.run_id,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "repository": plan.repository,
        "score": None,
        "score_status": "running",
        "files_scanned": 0,
        "discovered_files": plan.source_files,
        "coverage_ratio": 0,
        "coverage_status": "running",
        "analysis_scope": "repository" if len(modules) == len(plan.modules) else "selected_modules",
        "ai_status": "running",
        "parse_failure_count": 0,
        "log_count": 0,
        "issue_count": 0,
        "severity_counts": {"high": 0, "medium": 0, "low": 0},
        "runtime_id": runtime_id,
        "storage": "sqlite",
        "status": "running",
    }
    (store.database.parent / "metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _update_metadata_status(store: RunResultStore, status: str, error: str = "") -> None:
    path = store.database.parent / "metadata.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {"run_id": store.run_id}
    payload["status"] = status
    payload["error"] = error
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _enforce_retention(output_dir: Path, max_runs: int = 20, max_bytes: int = 5 * 1024**3) -> None:
    runs_dir = output_dir / "runs"
    if not runs_dir.is_dir():
        return
    runs = sorted(
        (path for path in runs_dir.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )
    sizes = {path: sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) for path in runs}
    retained_bytes = 0
    for index, path in enumerate(runs):
        retained_bytes += sizes[path]
        if index < max_runs and retained_bytes <= max_bytes:
            continue
        # Apply backups live outside runs and are never removed here.
        import shutil

        shutil.rmtree(path, ignore_errors=True)


def _emit(
    progress: ScanProgress | None,
    stage: str,
    completed: int,
    total: int,
    message: str,
    *,
    store: RunResultStore | None = None,
) -> None:
    if progress:
        payload = {
            "stage": stage,
            "completed": completed,
            "total": total,
            "message": message,
        }
        if store is not None:
            store_progress = store.progress()
            payload["progress"] = store_progress
            payload["summary"] = store.aggregate_counts()
        progress(payload)


def _check_cancel(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel and should_cancel():
        raise InterruptedError("分析已取消。")


def _new_run_id() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f")
