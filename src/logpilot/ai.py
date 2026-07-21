from __future__ import annotations

import json
import hashlib
import time
from pathlib import Path
from typing import Any, Callable

from .config import AiConfig
from .models import AiTrace, AnalysisTarget, Issue, LogCall, Severity
from .runtime import RuntimeExecution, RuntimeExecutor, RuntimeInfo, RuntimeRegistry
from .storage import repository_data_dir


AI_BATCH_MAX_LOGS = 30
AI_BATCH_MAX_CHARS = 60_000
AiBatchProgress = Callable[[int, int, list[Issue], list[AiTrace]], None]


FRAMEWORK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "apis": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "is_logging_api": {"type": "boolean"},
                    "callee": {"type": "string"},
                    "level": {"type": "string", "enum": ["trace", "debug", "info", "warning", "error", "critical", "fatal"]},
                    "framework": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["candidate_id", "is_logging_api", "callee", "level", "framework", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["apis"],
    "additionalProperties": False,
}


TARGET_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "has_issue": {"type": "boolean"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "suggestion": {"type": "string"},
                    "event_name": {"type": "string"},
                },
                "required": ["target_id", "has_issue", "severity", "title", "reason", "suggestion", "event_name"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}


UNSUPPORTED_LANGUAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "detected_language": {"type": "string"},
                    "logging_apis": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["target_id", "detected_language", "logging_apis", "notes", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["insights"],
    "additionalProperties": False,
}


ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "log_call_id": {"type": "string"},
                    "has_issue": {"type": "boolean"},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": [
                    "log_call_id",
                    "has_issue",
                    "severity",
                    "title",
                    "reason",
                    "suggestion",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}


def analyze_with_ai(
    logs: list[LogCall],
    config: AiConfig,
    repo_root: Path,
    runtime_id: str | None = None,
    registry: RuntimeRegistry | None = None,
    executor: RuntimeExecutor | None = None,
    progress: AiBatchProgress | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[list[Issue], list[AiTrace]]:
    if runtime_id is None and not config.enabled:
        return [], []
    if not logs:
        return [], []

    runtime_registry = registry or RuntimeRegistry()
    runtime = runtime_registry.resolve(runtime_id or config.runtime)
    batches = _batch_logs(logs)
    all_issues: list[Issue] = []
    traces: list[AiTrace] = []
    runtime_executor = executor or RuntimeExecutor()
    use_cache = executor is None
    for index, batch in enumerate(batches, start=1):
        if should_cancel and should_cancel():
            raise InterruptedError("分析已取消。")
        prompt = build_prompt(batch)
        execution, cached = _execute_with_cache(
            runtime,
            runtime_executor,
            prompt,
            repo_root,
            ANALYSIS_SCHEMA,
            config,
            use_cache,
        )
        trace = AiTrace(
            log_call_id=f"runtime-batch-{index}",
            status="cached" if cached else execution.status,
            prompt=prompt,
            raw_response=execution.raw_response,
            error=execution.error,
            runtime_id=runtime.id,
            runtime_version=runtime.version,
            duration_ms=execution.duration_ms,
            task="log_quality",
        )
        traces.append(trace)
        if execution.status != "ok":
            trace.error = execution.error or f"{runtime.name} 运行时分析失败"
        else:
            issues, parse_error = _issues_from_response(batch, execution.raw_response, runtime.id)
            all_issues.extend(issues)
            if parse_error:
                trace.status = "parse_error"
                trace.error = parse_error
        if progress:
            progress(index, len(batches), list(all_issues), list(traces))
    return all_issues, traces


def discover_logging_apis_with_ai(
    targets: list[AnalysisTarget],
    config: AiConfig,
    repo_root: Path,
    runtime_id: str | None = None,
    registry: RuntimeRegistry | None = None,
    executor: RuntimeExecutor | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[dict[str, dict[str, object]], list[AiTrace]]:
    candidates = [target for target in targets if target.kind == "framework_candidate"]
    if (runtime_id is None and not config.enabled) or not candidates:
        return {}, []
    runtime = (registry or RuntimeRegistry()).resolve(runtime_id or config.runtime)
    runtime_executor = executor or RuntimeExecutor()
    definitions: dict[str, dict[str, object]] = {}
    traces: list[AiTrace] = []
    for index, batch in enumerate(_batch_targets(candidates, 40), start=1):
        _check_cancel(should_cancel)
        prompt = _framework_prompt(batch)
        execution, cached = _execute_with_cache(
            runtime,
            runtime_executor,
            prompt,
            repo_root,
            FRAMEWORK_SCHEMA,
            config,
            executor is None,
        )
        trace = _trace_for_execution("framework_discovery", index, execution, runtime, prompt, cached)
        traces.append(trace)
        if execution.status != "ok":
            continue
        parsed, error = _frameworks_from_response(batch, execution.raw_response)
        definitions.update(parsed)
        if error:
            trace.status = "parse_error"
            trace.error = error
    return definitions, traces


def analyze_targets_with_ai(
    targets: list[AnalysisTarget],
    config: AiConfig,
    repo_root: Path,
    runtime_id: str | None = None,
    registry: RuntimeRegistry | None = None,
    executor: RuntimeExecutor | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[list[Issue], list[AiTrace]]:
    review_targets = [target for target in targets if target.kind == "error_path"]
    if (runtime_id is None and not config.enabled) or not review_targets:
        return [], []
    runtime = (registry or RuntimeRegistry()).resolve(runtime_id or config.runtime)
    runtime_executor = executor or RuntimeExecutor()
    issues: list[Issue] = []
    traces: list[AiTrace] = []
    for index, batch in enumerate(_batch_targets(review_targets, 20), start=1):
        _check_cancel(should_cancel)
        prompt = _target_prompt(batch)
        execution, cached = _execute_with_cache(
            runtime,
            runtime_executor,
            prompt,
            repo_root,
            TARGET_ANALYSIS_SCHEMA,
            config,
            executor is None,
        )
        trace = _trace_for_execution("missing_log", index, execution, runtime, prompt, cached)
        traces.append(trace)
        if execution.status != "ok":
            continue
        parsed, error = _target_issues_from_response(batch, execution.raw_response, runtime.id)
        issues.extend(parsed)
        if error:
            trace.status = "parse_error"
            trace.error = error
    return issues, traces


def inspect_unsupported_languages_with_ai(
    targets: list[AnalysisTarget],
    config: AiConfig,
    repo_root: Path,
    runtime_id: str | None = None,
    registry: RuntimeRegistry | None = None,
    executor: RuntimeExecutor | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, object]], list[AiTrace]]:
    samples = [target for target in targets if target.kind == "unsupported_sample"]
    if (runtime_id is None and not config.enabled) or not samples:
        return [], []
    runtime = (registry or RuntimeRegistry()).resolve(runtime_id or config.runtime)
    runtime_executor = executor or RuntimeExecutor()
    insights: list[dict[str, object]] = []
    traces: list[AiTrace] = []
    for index, batch in enumerate(_batch_targets(samples, 8), start=1):
        _check_cancel(should_cancel)
        prompt = _unsupported_prompt(batch)
        execution, cached = _execute_with_cache(
            runtime,
            runtime_executor,
            prompt,
            repo_root,
            UNSUPPORTED_LANGUAGE_SCHEMA,
            config,
            executor is None,
        )
        trace = _trace_for_execution("unsupported_language", index, execution, runtime, prompt, cached)
        traces.append(trace)
        if execution.status != "ok":
            continue
        parsed, error = _unsupported_insights_from_response(batch, execution.raw_response)
        insights.extend(parsed)
        if error:
            trace.status = "parse_error"
            trace.error = error
    return insights, traces


def _batch_targets(targets: list[AnalysisTarget], max_items: int) -> list[list[AnalysisTarget]]:
    batches: list[list[AnalysisTarget]] = []
    current: list[AnalysisTarget] = []
    current_chars = 0
    for target in targets:
        weight = len(target.context[:2400]) + len(target.source_line) + 256
        if current and (len(current) >= max_items or current_chars + weight > AI_BATCH_MAX_CHARS):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(target)
        current_chars += weight
    if current:
        batches.append(current)
    return batches


def _framework_prompt(targets: list[AnalysisTarget]) -> str:
    return json.dumps(
        {
            "task": "判断候选 C/C++ 调用是否为日志接口，并识别日志级别和框架。只分类，不修改代码。",
            "rules": [
                "每个候选必须返回一项并原样保留 candidate_id 和 callee。",
                "只有证据明确时 is_logging_api=true，confidence 使用 0 到 1。",
                "普通业务函数、错误处理函数和断言不是日志接口。",
            ],
            "candidates": [
                {
                    "candidate_id": target.id,
                    "callee": target.symbol,
                    "file_path": target.file_path,
                    "line": target.start_line,
                    "language": target.language,
                    "statement": target.source_line[:1200],
                    "context": target.context[:1800],
                }
                for target in targets
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def _target_prompt(targets: list[AnalysisTarget]) -> str:
    return json.dumps(
        {
            "task": "审查异常或失败路径是否缺少必要日志。只返回结构化建议，不修改代码。",
            "rules": [
                "每个输入目标必须返回一项并原样保留 target_id。",
                "仅在日志能显著帮助定位失败时 has_issue=true。",
                "建议包含稳定事件名称、关键业务标识和异常对象。",
                "标题、原因和建议使用简洁中文。",
            ],
            "targets": [
                {
                    "target_id": target.id,
                    "kind": target.kind,
                    "file_path": target.file_path,
                    "start_line": target.start_line,
                    "end_line": target.end_line,
                    "language": target.language,
                    "context": target.context[:2400],
                }
                for target in targets
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def _unsupported_prompt(targets: list[AnalysisTarget]) -> str:
    return json.dumps(
        {
            "task": "从暂不支持的源码样本中识别语言和可能的日志接口。结果仅用于支持建议，不代表完整扫描。",
            "rules": [
                "每个样本必须返回一项并原样保留 target_id。",
                "只列出源码中真实出现的日志函数、宏或方法。",
                "无法确认时 logging_apis 返回空数组并降低 confidence。",
            ],
            "unsupported_samples": [
                {
                    "target_id": target.id,
                    "declared_language": target.language,
                    "extension": target.metadata.get("extension", target.symbol),
                    "file_path": target.file_path,
                    "source": target.context[:5000],
                }
                for target in targets
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def _frameworks_from_response(
    targets: list[AnalysisTarget],
    raw_response: str,
) -> tuple[dict[str, dict[str, object]], str]:
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        return {}, f"运行时结果不是有效 JSON：{exc}"
    values = payload.get("apis") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        return {}, "运行时结果缺少 apis 数组。"
    by_id = {target.id: target for target in targets}
    result: dict[str, dict[str, object]] = {}
    ignored = 0
    for value in values:
        if not isinstance(value, dict):
            ignored += 1
            continue
        identifier = str(value.get("candidate_id", ""))
        target = by_id.get(identifier)
        if not target or str(value.get("callee", "")) != target.symbol:
            ignored += 1
            continue
        result[identifier] = value
    error = f"忽略了 {ignored} 项无法验证的日志接口结果。" if ignored else ""
    return result, error


def _target_issues_from_response(
    targets: list[AnalysisTarget],
    raw_response: str,
    runtime_id: str,
) -> tuple[list[Issue], str]:
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        return [], f"运行时结果不是有效 JSON：{exc}"
    findings = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(findings, list):
        return [], "运行时结果缺少 findings 数组。"
    by_id = {target.id: target for target in targets}
    issues: list[Issue] = []
    ignored = 0
    for finding in findings:
        if not isinstance(finding, dict):
            ignored += 1
            continue
        target = by_id.get(str(finding.get("target_id", "")))
        if not target:
            ignored += 1
            continue
        if not finding.get("has_issue"):
            continue
        try:
            severity = Severity(str(finding.get("severity", "medium")).lower())
        except ValueError:
            severity = Severity.MEDIUM
        event_name = str(finding.get("event_name", "")).strip()
        suggestion = str(finding.get("suggestion") or "请补充包含失败上下文的错误日志。")
        if event_name:
            suggestion = f"{suggestion} 建议事件名称：`{event_name}`。"
        issues.append(
            Issue(
                id=f"ai_missing_log:{target.id}",
                file_path=target.file_path,
                line=target.start_line,
                severity=severity,
                kind="ai_missing_log",
                title=str(finding.get("title") or "失败路径缺少日志"),
                reason=str(finding.get("reason") or "该失败路径缺少可用于定位问题的日志。"),
                suggestion=suggestion,
                source=f"runtime:{runtime_id}",
                context=target.context,
                source_line=target.source_line,
            )
        )
    error = f"忽略了 {ignored} 项无法匹配的运行时结果。" if ignored else ""
    return issues, error


def _unsupported_insights_from_response(
    targets: list[AnalysisTarget],
    raw_response: str,
) -> tuple[list[dict[str, object]], str]:
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        return [], f"运行时结果不是有效 JSON：{exc}"
    values = payload.get("insights") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        return [], "运行时结果缺少 insights 数组。"
    by_id = {target.id: target for target in targets}
    result: list[dict[str, object]] = []
    ignored = 0
    for value in values:
        if not isinstance(value, dict):
            ignored += 1
            continue
        target = by_id.get(str(value.get("target_id", "")))
        if not target:
            ignored += 1
            continue
        apis = value.get("logging_apis", [])
        if not isinstance(apis, list):
            ignored += 1
            continue
        try:
            confidence = float(value.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            ignored += 1
            continue
        result.append(
            {
                "target_id": target.id,
                "file_path": target.file_path,
                "declared_language": target.language,
                "detected_language": str(value.get("detected_language", target.language)),
                "logging_apis": [str(item) for item in apis[:20]],
                "notes": str(value.get("notes", "")),
                "confidence": max(0.0, min(1.0, confidence)),
                "advisory_only": True,
            }
        )
    error = f"忽略了 {ignored} 项无法匹配的语言识别结果。" if ignored else ""
    return result, error


def _batch_logs(logs: list[LogCall]) -> list[list[LogCall]]:
    batches: list[list[LogCall]] = []
    current: list[LogCall] = []
    current_chars = 0
    for log in logs:
        weight = len(log.context[:1600]) + len(log.message) + len(log.file_path) + 256
        if current and (len(current) >= AI_BATCH_MAX_LOGS or current_chars + weight > AI_BATCH_MAX_CHARS):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(log)
        current_chars += weight
    if current:
        batches.append(current)
    return batches


def build_prompt(logs: list[LogCall]) -> str:
    payload = {
        "task": (
            "逐项审查日志调用，判断其是否低价值、字段不足、重复、泄露敏感信息或缺少业务上下文。"
            "只返回符合给定 JSON Schema 的对象，不要修改文件，不要执行任何命令。"
        ),
        "rules": [
            "每个输入日志必须返回一项 finding，并原样保留 log_call_id。",
            "没有问题时 has_issue=false，其他说明字段仍需给出简短内容。",
            "severity 只能是 low、medium 或 high。",
            "标题、原因和建议使用简洁中文。",
        ],
        "logs": [
            {
                "log_call_id": log.id,
                "file_path": log.file_path,
                "line": log.line,
                "language": log.language,
                "level": log.level,
                "callee": log.callee,
                "message": log.message,
                "context": log.context[:1600],
            }
            for log in logs
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _execute_with_cache(
    runtime: RuntimeInfo,
    executor: RuntimeExecutor,
    prompt: str,
    repo_root: Path,
    schema: dict[str, Any],
    config: AiConfig,
    use_cache: bool,
) -> tuple[RuntimeExecution, bool]:
    cache_path = _cache_path(repo_root, runtime, prompt, config.model)
    if use_cache and cache_path.is_file():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            raw_response = str(payload.get("raw_response", ""))
            if raw_response:
                return RuntimeExecution(runtime.id, "ok", raw_response, duration_ms=0), True
        except (OSError, json.JSONDecodeError):
            pass

    execution = RuntimeExecution(runtime.id, "error", error="运行时未执行")
    for _attempt in range(2):
        execution = executor.execute(
            runtime,
            prompt,
            repo_root,
            schema,
            model=config.model,
            timeout_seconds=config.timeout_seconds,
        )
        if execution.status == "ok":
            break
    if use_cache and execution.status == "ok" and execution.raw_response:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "runtime_id": runtime.id,
                        "runtime_version": runtime.version,
                        "model": config.model,
                        "raw_response": execution.raw_response,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            _prune_ai_cache(cache_path.parent)
        except OSError:
            pass
    return execution, False


def _cache_path(repo_root: Path, runtime: RuntimeInfo, prompt: str, model: str) -> Path:
    digest = hashlib.sha256(
        "\0".join([runtime.id, runtime.version, model, prompt]).encode("utf-8")
    ).hexdigest()
    return repository_data_dir(repo_root) / "ai-cache" / f"{digest}.json"


def _prune_ai_cache(cache_dir: Path, max_bytes: int = 1024**3, max_age_days: int = 30) -> None:
    cutoff = time.time() - max_age_days * 24 * 60 * 60
    files: list[tuple[Path, float, int]] = []
    total = 0
    try:
        candidates = list(cache_dir.glob("*.json"))
    except OSError:
        return
    for path in candidates:
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            path.unlink(missing_ok=True)
            continue
        files.append((path, stat.st_mtime, stat.st_size))
        total += stat.st_size
    for path, _modified, size in sorted(files, key=lambda item: item[1]):
        if total <= max_bytes:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= size


def _trace_for_execution(
    task: str,
    index: int,
    execution: RuntimeExecution,
    runtime: RuntimeInfo,
    prompt: str,
    cached: bool,
) -> AiTrace:
    return AiTrace(
        log_call_id=f"{task}-batch-{index}",
        status="cached" if cached else execution.status,
        prompt=prompt,
        raw_response=execution.raw_response,
        error=execution.error,
        runtime_id=runtime.id,
        runtime_version=runtime.version,
        duration_ms=execution.duration_ms,
        task=task,
    )


def _check_cancel(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel and should_cancel():
        raise InterruptedError("分析已取消。")


def _issues_from_response(
    logs: list[LogCall],
    raw_response: str,
    runtime_id: str,
) -> tuple[list[Issue], str]:
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        return [], f"运行时结果不是有效 JSON：{exc}"
    findings = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(findings, list):
        return [], "运行时结果缺少 findings 数组。"

    logs_by_id = {log.id: log for log in logs}
    issues: list[Issue] = []
    ignored = 0
    for finding in findings:
        if not isinstance(finding, dict):
            ignored += 1
            continue
        log = logs_by_id.get(str(finding.get("log_call_id", "")))
        if not log or not finding.get("has_issue"):
            if not log:
                ignored += 1
            continue
        try:
            severity = Severity(str(finding.get("severity", "low")).lower())
        except ValueError:
            severity = Severity.LOW
        issues.append(
            Issue(
                id=f"ai:{log.id}",
                file_path=log.file_path,
                line=log.line,
                severity=severity,
                kind="ai_log_quality",
                title=str(finding.get("title") or "运行时日志质量建议"),
                reason=str(finding.get("reason") or "运行时发现日志质量问题。"),
                suggestion=str(finding.get("suggestion") or "请审查该日志。"),
                source=f"runtime:{runtime_id}",
                log_call_id=log.id,
                patch_action=None,
                context=log.context,
                source_line=log.source_line,
            )
        )
    error = f"忽略了 {ignored} 项无法匹配的运行时结果。" if ignored else ""
    return issues, error
