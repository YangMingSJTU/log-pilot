from __future__ import annotations

import ast
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any

from .models import FixProposal, Issue, LogCall
from .settings import RepositorySettings, resolve_template


def attach_fix_proposals(
    repo_root: Path,
    logs: list[LogCall],
    issues: list[Issue],
    settings: RepositorySettings,
    profile: dict[str, Any],
) -> list[Issue]:
    logs_by_id = {log.id: log for log in logs}
    delete_issues: dict[str, list[Issue]] = {}
    for issue in issues:
        if issue.patch_action == "delete" and issue.log_call_id in logs_by_id:
            delete_issues.setdefault(str(issue.log_call_id), []).append(issue)

    for log_id, related in delete_issues.items():
        log = logs_by_id[log_id]
        proposal = FixProposal(
            id=f"delete:{log.id}",
            action="delete",
            file_path=log.file_path,
            start_line=log.line,
            end_line=max(log.line, log.end_line or log.line),
            expected_text=log.source_line,
            replacement_text="",
            context=log.context,
            description="删除这条低价值或不合规日志",
            source="rule",
            line_delta=-(max(log.line, log.end_line or log.line) - log.line + 1),
            issue_ids=sorted(issue.id for issue in related),
            log_call_ids=[log.id],
        )
        for issue in related:
            issue.fix = proposal

    _attach_python_exception_fixes(repo_root, logs, issues, settings, profile)
    return issues


def fix_from_dict(payload: dict[str, Any]) -> FixProposal:
    return FixProposal(
        id=str(payload.get("id", "")),
        action=str(payload.get("action", "")),
        file_path=str(payload.get("file_path", "")),
        start_line=int(payload.get("start_line", 0)),
        end_line=int(payload.get("end_line", payload.get("start_line", 0))),
        expected_text=str(payload.get("expected_text", "")),
        replacement_text=str(payload.get("replacement_text", "")),
        context=str(payload.get("context", "")),
        description=str(payload.get("description", "")),
        source=str(payload.get("source", "")),
        line_delta=int(payload.get("line_delta", 0)),
        issue_ids=[str(value) for value in payload.get("issue_ids", [])],
        log_call_ids=[str(value) for value in payload.get("log_call_ids", [])],
    )


def apply_fix_to_text(text: str, fix: FixProposal, start_line: int | None = None) -> str:
    lines = text.splitlines(keepends=True)
    line_number = start_line or fix.start_line
    if line_number < 1 or line_number > len(lines):
        raise ValueError(f"修复位置超出文件范围：{fix.file_path}:{fix.start_line}")
    end_line = line_number + (fix.end_line - fix.start_line)
    if end_line > len(lines):
        raise ValueError(f"修复范围超出文件范围：{fix.file_path}:{fix.start_line}")

    actual = "\n".join(_without_eol(line) for line in lines[line_number - 1 : end_line])
    if actual != fix.expected_text:
        raise ValueError(f"源码已变化：{fix.file_path}:{fix.start_line}")

    newline = _line_ending(lines[line_number - 1]) or _preferred_newline(lines)
    replacement = _replacement_lines(fix.replacement_text, newline)
    if fix.action == "delete":
        del lines[line_number - 1 : end_line]
    elif fix.action == "replace":
        if lines[end_line - 1].endswith(("\n", "\r")):
            replacement = _ensure_last_newline(replacement, newline)
        lines[line_number - 1 : end_line] = replacement
    elif fix.action == "insert_before":
        replacement = _ensure_last_newline(replacement, newline)
        lines[line_number - 1 : line_number - 1] = replacement
    else:
        raise ValueError(f"未知修复动作：{fix.action}")
    return "".join(lines)


def shifted_fix(fix: FixProposal, start_line: int) -> FixProposal:
    offset = start_line - fix.start_line
    return replace(fix, start_line=start_line, end_line=fix.end_line + offset)


def _attach_python_exception_fixes(
    repo_root: Path,
    logs: list[LogCall],
    issues: list[Issue],
    settings: RepositorySettings,
    profile: dict[str, Any],
) -> None:
    by_file: dict[str, list[Issue]] = {}
    for issue in issues:
        if issue.kind == "missing_exception_log" and issue.file_path.endswith(".py"):
            by_file.setdefault(issue.file_path, []).append(issue)

    logs_by_file: dict[str, list[LogCall]] = {}
    for log in logs:
        logs_by_file.setdefault(log.file_path, []).append(log)

    template, template_source = resolve_template("python", settings, profile)
    for rel_path, file_issues in by_file.items():
        path = (repo_root / rel_path).resolve()
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        handlers = {
            node.lineno: node
            for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler)
        }
        available_loggers = _available_loggers(tree, logs_by_file.get(rel_path, []))
        logger_name = available_loggers[0] if available_loggers else ""
        if "{logger}" in template and not logger_name:
            continue

        for issue in file_issues:
            handler = handlers.get(issue.line)
            if not handler or not handler.body:
                continue
            exception_name = handler.name if isinstance(handler.name, str) else ""
            if "{exception}" in template and not exception_name:
                continue
            function_name = _enclosing_function(handler, parents)
            event_name = f"{function_name}_failed" if function_name != "module" else "operation_failed"

            body_is_pass = len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass)
            anchor = handler.body[0]
            anchor_line = int(anchor.lineno)
            if anchor_line < 1 or anchor_line > len(lines):
                continue
            expected = lines[anchor_line - 1]
            indent = expected[: len(expected) - len(expected.lstrip())]
            try:
                rendered = template.format(
                    event=event_name,
                    exception=exception_name,
                    function=function_name,
                    logger=logger_name,
                    indent=indent,
                )
            except (KeyError, ValueError):
                continue
            replacement = rendered if rendered.startswith(indent) else f"{indent}{rendered}"
            target = _rendered_log_target(rendered)
            if not target or target[0] not in available_loggers:
                continue
            action = "replace" if body_is_pass else "insert_before"
            proposal = FixProposal(
                id=f"add_exception_log:{rel_path}:{issue.line}",
                action=action,
                file_path=rel_path,
                start_line=anchor_line,
                end_line=anchor_line,
                expected_text=expected,
                replacement_text=replacement,
                context=issue.context,
                description="按项目日志风格补充异常日志",
                source=template_source,
                line_delta=0 if action == "replace" else 1,
                issue_ids=[issue.id],
                log_call_ids=[],
            )
            try:
                ast.parse(apply_fix_to_text(text, proposal))
            except (SyntaxError, ValueError):
                continue
            issue.fix = proposal


def _available_loggers(tree: ast.AST, logs: list[LogCall]) -> list[str]:
    candidates: Counter[str] = Counter()
    for log in logs:
        if "." not in log.callee or log.callee == "print":
            continue
        root = log.callee.rsplit(".", 1)[0]
        weight = 3 if log.level in {"exception", "error", "critical"} else 1
        candidates[root] += weight
    discovered = [name for name, _count in candidates.most_common()]
    nodes = list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Attribute):
                continue
            if value.func.attr != "getLogger":
                continue
            for target in targets:
                if isinstance(target, ast.Name):
                    discovered.append(target.id)
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "logging":
                    discovered.append(alias.asname or "logging")
    return list(dict.fromkeys(discovered))


def _rendered_log_target(rendered: str) -> tuple[str, str] | None:
    try:
        tree = ast.parse(rendered.strip())
    except SyntaxError:
        return None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Expr):
        return None
    call = tree.body[0].value
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
        return None
    if call.func.attr not in {"exception", "error", "critical"}:
        return None
    root = _attribute_name(call.func.value)
    return (root, call.func.attr) if root else None


def _attribute_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _attribute_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    current = parents.get(node)
    while current:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parents.get(current)
    return "module"


def _without_eol(line: str) -> str:
    return line.rstrip("\r\n")


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _preferred_newline(lines: list[str]) -> str:
    for line in lines:
        ending = _line_ending(line)
        if ending:
            return ending
    return "\n"


def _replacement_lines(replacement: str, newline: str) -> list[str]:
    if not replacement:
        return []
    values = replacement.splitlines()
    return [f"{line}{newline}" if index < len(values) - 1 else line for index, line in enumerate(values)]


def _ensure_last_newline(lines: list[str], newline: str) -> list[str]:
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += newline
    return lines
