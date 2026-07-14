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


def analyze_rules(repo_root: Path, logs: list[LogCall], config: RulesConfig) -> list[Issue]:
    issues: list[Issue] = []
    issues.extend(_log_call_issues(logs, config))
    issues.extend(_duplicate_issues(logs))
    issues.extend(_missing_exception_logs(repo_root))
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
                    "Forbidden log API",
                    f"`{log.callee}` is configured as a forbidden logging API.",
                    "Use a structured project logger instead.",
                    "delete" if callee in {"print", "console.log", "system.out.println"} else None,
                )
            )

        if log.level == "debug":
            issues.append(
                _issue(
                    log,
                    Severity.MEDIUM,
                    "debug_log",
                    "Debug log in source",
                    "Debug-level output can leak noisy implementation details into production code.",
                    "Remove the log or lower its scope to local debugging only.",
                    "delete" if callee in {"print", "console.log"} else None,
                )
            )

        if message.lower() in LOW_VALUE_MESSAGES or _looks_low_value(message):
            issues.append(
                _issue(
                    log,
                    Severity.LOW,
                    "low_value_log",
                    "Low-value log message",
                    "The message does not describe a business state, identifier, or useful failure context.",
                    "Replace it with a structured event name and relevant fields.",
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
                    "Potential sensitive data in log",
                    f"The log appears to include sensitive field(s): {fields}.",
                    "Mask or remove sensitive values before logging.",
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
                    "Duplicate log message",
                    "The same message appears multiple times in one file, which can reduce diagnostic clarity.",
                    "Use more specific event names or include distinguishing fields.",
                    None,
                )
            )
    return issues


def _missing_exception_logs(repo_root: Path) -> list[Issue]:
    issues: list[Issue] = []
    for path in repo_root.rglob("*.py"):
        parts = set(path.relative_to(repo_root).parts)
        if {".git", ".logpilot", ".venv", "venv", "__pycache__"}.intersection(parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
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
                    title="Exception handler without error log",
                    reason="An exception is caught without logging the failure context.",
                    suggestion="Add an error or exception log with the operation name and exception details.",
                    source="rule",
                    log_call_id=None,
                    patch_action=None,
                )
            )
    issues.extend(_missing_exception_logs_text(repo_root))
    return issues


def _missing_exception_logs_text(repo_root: Path) -> list[Issue]:
    issues: list[Issue] = []
    pattern = re.compile(r"\bcatch\s*\([^)]*\)\s*\{(?P<body>[^{}]*)\}", re.DOTALL)
    for path in list(repo_root.rglob("*.js")) + list(repo_root.rglob("*.ts")) + list(repo_root.rglob("*.java")):
        parts = set(path.relative_to(repo_root).parts)
        if {".git", ".logpilot", ".venv", "venv", "node_modules", "dist", "build"}.intersection(parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
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
                    title="Catch block without log",
                    reason="An exception is caught without logging the failure context.",
                    suggestion="Add an error log with the operation name and exception details.",
                    source="rule",
                    log_call_id=None,
                    patch_action=None,
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
    )


def _looks_low_value(message: str) -> bool:
    lowered = message.strip().lower()
    if not lowered:
        return True
    words = re.findall(r"[a-zA-Z_]+", lowered)
    return len(words) <= 2 and not any(token in lowered for token in {"fail", "error", "created", "deleted", "payment", "user"})
