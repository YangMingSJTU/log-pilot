from __future__ import annotations

import ast
import re
from collections import Counter, defaultdict
from pathlib import Path

from .config import RulesConfig
from .models import Issue, LogCall, Severity, relative_path

LOW_VALUE_MESSAGES = {
    "",
    "start",
    "end",
    "test",
    "debug",
    "enter",
    "enter function",
    "start payment",
    "hello",
}


def analyze_rules(
    repo_root: Path,
    logs: list[LogCall],
    config: RulesConfig,
    enabled_languages: set[str] | None = None,
) -> list[Issue]:
    issues: list[Issue] = []
    issues.extend(_log_call_issues(logs, config))
    issues.extend(_duplicate_issues(logs))
    issues.extend(_missing_exception_logs(repo_root, enabled_languages))
    return issues


def _log_call_issues(logs: list[LogCall], config: RulesConfig) -> list[Issue]:
    issues: list[Issue] = []
    forbidden = {item.lower() for item in config.forbidden_logs}
    sensitive = {item.lower() for item in config.sensitive_fields}

    for log in logs:
        callee = log.callee.lower().replace(" ", "")
        message = log.message.strip()
        haystack = f"{log.message}\n{log.source_line}".lower()

        if callee in forbidden or any(callee.endswith(f".{item}") for item in forbidden):
            issues.append(
                _issue(
                    log,
                    Severity.MEDIUM,
                    "forbidden_log",
                    "使用了禁用的日志接口",
                    f"`{log.callee}` 已被配置为禁用的日志接口。",
                    "请改用项目统一的结构化日志组件。",
                    "delete" if callee in {"print", "console.log", "system.out.println"} else None,
                )
            )

        if log.level == "debug":
            issues.append(
                _issue(
                    log,
                    Severity.MEDIUM,
                    "debug_log",
                    "源码中保留了调试日志",
                    "调试级输出可能将实现细节和无效噪声带入生产环境。",
                    "请删除该日志，或将其限制在本地调试范围内。",
                    "delete" if callee in {"print", "console.log"} else None,
                )
            )

        if message.lower() in LOW_VALUE_MESSAGES or _looks_low_value(message):
            issues.append(
                _issue(
                    log,
                    Severity.LOW,
                    "low_value_log",
                    "日志信息价值过低",
                    "该信息未描述业务状态、关键标识或有效的失败上下文。",
                    "请使用结构化事件名称，并补充与问题定位相关的字段。",
                    "delete" if log.source_line else None,
                )
            )

        matched_sensitive = [field for field in sensitive if field and field in haystack]
        if matched_sensitive:
            fields = ", ".join(sorted(set(matched_sensitive)))
            issues.append(
                _issue(
                    log,
                    Severity.HIGH,
                    "sensitive_log",
                    "日志可能包含敏感数据",
                    f"日志中可能包含敏感字段：{fields}。",
                    "记录前应对敏感值进行脱敏，或直接移除这些字段。",
                    None,
                )
            )

    return issues


def _duplicate_issues(logs: list[LogCall]) -> list[Issue]:
    by_file: dict[str, Counter[str]] = defaultdict(Counter)
    for log in logs:
        key = log.message.strip().lower()
        if key:
            by_file[log.file_path][key] += 1

    duplicated = {
        (file_path, message)
        for file_path, counter in by_file.items()
        for message, count in counter.items()
        if count > 1
    }
    issues: list[Issue] = []
    emitted: set[tuple[str, str]] = set()
    for log in logs:
        key = (log.file_path, log.message.strip().lower())
        if key in duplicated and key not in emitted:
            emitted.add(key)
            issues.append(
                _issue(
                    log,
                    Severity.LOW,
                    "duplicate_log",
                    "同一文件中存在重复日志",
                    "相同信息在一个文件中多次出现，会降低问题定位的清晰度。",
                    "请使用更具体的事件名称，或加入能够区分场景的字段。",
                    None,
                )
            )
    return issues


def _missing_exception_logs(repo_root: Path, enabled_languages: set[str] | None = None) -> list[Issue]:
    issues: list[Issue] = []
    if enabled_languages is not None and "python" not in enabled_languages:
        return _missing_exception_logs_text(repo_root, enabled_languages)
    for path in repo_root.rglob("*.py"):
        parts = set(path.relative_to(repo_root).parts)
        if {".git", ".venv", "venv", "__pycache__"}.intersection(parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if _body_has_log(node.body):
                continue
            rel = relative_path(path, repo_root)
            issues.append(
                Issue(
                    id=f"missing_exception_log:{rel}:{node.lineno}",
                    file_path=rel,
                    line=node.lineno,
                    severity=Severity.HIGH,
                    kind="missing_exception_log",
                    title="异常处理缺少错误日志",
                    reason="代码捕获了异常，但没有记录失败上下文。",
                    suggestion="请添加错误日志，记录操作名称和异常详情。",
                    source="rule",
                    log_call_id=None,
                    patch_action=None,
                    context=_source_context(lines, node.lineno),
                    source_line=lines[node.lineno - 1] if node.lineno <= len(lines) else "",
                )
            )
    issues.extend(_missing_exception_logs_text(repo_root, enabled_languages))
    return issues


def _missing_exception_logs_text(repo_root: Path, enabled_languages: set[str] | None = None) -> list[Issue]:
    issues: list[Issue] = []
    pattern = re.compile(r"\bcatch\s*\([^)]*\)\s*\{(?P<body>[^{}]*)\}", re.DOTALL)
    for path in list(repo_root.rglob("*.js")) + list(repo_root.rglob("*.ts")) + list(repo_root.rglob("*.java")):
        language = {".js": "javascript", ".ts": "typescript", ".java": "java"}.get(path.suffix.lower())
        if enabled_languages is not None and language not in enabled_languages:
            continue
        parts = set(path.relative_to(repo_root).parts)
        if {".git", ".venv", "venv", "node_modules", "dist", "build"}.intersection(parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        for match in pattern.finditer(text):
            body = match.group("body")
            if re.search(r"\b(logger|log|console)\s*\.\s*(error|exception|warn|warning|log)\s*\(", body):
                continue
            line = text[: match.start()].count("\n") + 1
            rel = relative_path(path, repo_root)
            issues.append(
                Issue(
                    id=f"missing_exception_log:{rel}:{line}",
                    file_path=rel,
                    line=line,
                    severity=Severity.HIGH,
                    kind="missing_exception_log",
                    title="异常捕获缺少错误日志",
                    reason="代码捕获了异常，但没有记录失败上下文。",
                    suggestion="请添加错误日志，记录操作名称和异常详情。",
                    source="rule",
                    log_call_id=None,
                    patch_action=None,
                    context=_source_context(lines, line),
                    source_line=lines[line - 1] if line <= len(lines) else "",
                )
            )
    return issues


def _body_has_log(nodes: list[ast.stmt]) -> bool:
    for node in ast.walk(ast.Module(body=nodes, type_ignores=[])):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "print":
                return True
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "error",
                "exception",
                "warning",
                "warn",
                "critical",
            }:
                return True
    return False


def _issue(
    log: LogCall,
    severity: Severity,
    kind: str,
    title: str,
    reason: str,
    suggestion: str,
    patch_action: str | None,
) -> Issue:
    if patch_action and not log.safe_to_delete:
        patch_action = None
    return Issue(
        id=f"{kind}:{log.id}",
        file_path=log.file_path,
        line=log.line,
        severity=severity,
        kind=kind,
        title=title,
        reason=reason,
        suggestion=suggestion,
        source="rule",
        log_call_id=log.id,
        patch_action=patch_action,
        context=log.context,
        source_line=log.source_line,
    )


def _source_context(lines: list[str], line_number: int, radius: int = 2) -> str:
    start = max(1, line_number - radius)
    end = min(len(lines), line_number + radius)
    return "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1))


def _looks_low_value(message: str) -> bool:
    lowered = message.strip().lower()
    if not lowered:
        return True
    words = re.findall(r"[a-zA-Z_]+", lowered)
    return len(words) <= 2 and not any(token in lowered for token in {"fail", "error", "created", "deleted", "payment", "user"})
