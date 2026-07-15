from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .config import AiConfig
from .models import AiTrace, Issue, LogCall, Severity
from .runtime import RuntimeExecutor, RuntimeRegistry


AI_BATCH_MAX_LOGS = 30
AI_BATCH_MAX_CHARS = 60_000
AiBatchProgress = Callable[[int, int, list[Issue], list[AiTrace]], None]


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
    for index, batch in enumerate(batches, start=1):
        if should_cancel and should_cancel():
            raise InterruptedError("分析已取消。")
        prompt = build_prompt(batch)
        execution = runtime_executor.execute(
            runtime,
            prompt,
            repo_root,
            ANALYSIS_SCHEMA,
            model=config.model,
            timeout_seconds=config.timeout_seconds,
        )
        trace = AiTrace(
            log_call_id=f"runtime-batch-{index}",
            status=execution.status,
            prompt=prompt,
            raw_response=execution.raw_response,
            error=execution.error,
            runtime_id=runtime.id,
            runtime_version=runtime.version,
            duration_ms=execution.duration_ms,
        )
        traces.append(trace)
        if execution.status != "ok":
            raise RuntimeError(f"{runtime.name} 运行时分析失败：{execution.error}")

        issues, parse_error = _issues_from_response(batch, execution.raw_response, runtime.id)
        all_issues.extend(issues)
        if parse_error:
            trace.status = "parse_error"
            trace.error = parse_error
        if progress:
            progress(index, len(batches), list(all_issues), list(traces))
    return all_issues, traces


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
