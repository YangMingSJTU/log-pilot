from __future__ import annotations

import re
from pathlib import Path

from tree_sitter import Language, Node, Parser
import tree_sitter_c
import tree_sitter_cpp

from .models import AnalysisTarget, LogCall, relative_path


_C_PARSER = Parser(Language(tree_sitter_c.language()))
_CPP_PARSER = Parser(Language(tree_sitter_cpp.language()))
_LOG_LEVELS = {
    "debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "trace",
}


def parse_c_family_file(
    path: Path,
    repo_root: Path,
    language: str,
) -> tuple[list[LogCall], list[AnalysisTarget]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    source = text.encode("utf-8")
    parser = _C_PARSER if language == "c" else _CPP_PARSER
    tree = parser.parse(source)
    lines = text.splitlines()
    rel = relative_path(path, repo_root)
    logs: list[LogCall] = []
    targets: list[AnalysisTarget] = []

    for node in _walk_tree(tree.root_node):
        if node.type != "expression_statement":
            continue
        statement = _node_text(source, node)
        classified = _classify_c_log(statement)
        if classified:
            callee, level = classified
            start_line = node.start_point.row + 1
            end_line = node.end_point.row + 1
            source_line = "\n".join(lines[start_line - 1 : end_line])
            logs.append(
                LogCall(
                    id=f"{rel}:{start_line}:{node.start_point.column}",
                    file_path=rel,
                    line=start_line,
                    column=node.start_point.column,
                    language=language,
                    level=level,
                    callee=callee,
                    message=_c_message(statement),
                    context=_context(lines, start_line),
                    source_line=source_line,
                    end_line=end_line,
                    safe_to_delete=statement.strip() == source_line.strip(),
                )
            )
            continue

        symbol = _framework_candidate_symbol(node, source)
        if symbol:
            start_line = node.start_point.row + 1
            end_line = node.end_point.row + 1
            targets.append(
                AnalysisTarget(
                    id=f"framework:{rel}:{start_line}:{node.start_point.column}:{symbol}",
                    kind="framework_candidate",
                    file_path=rel,
                    start_line=start_line,
                    end_line=end_line,
                    language=language,
                    context=_context(lines, start_line),
                    source_line="\n".join(lines[start_line - 1 : end_line]),
                    symbol=symbol,
                    metadata={"statement": statement[:1200]},
                )
            )

    for node in _walk_tree(tree.root_node):
        if node.type != "catch_clause":
            continue
        body = next((child for child in node.named_children if child.type == "compound_statement"), None)
        if body is None:
            continue
        body_text = _node_text(source, body)
        if _classify_c_log(body_text) or _looks_like_logging_text(body_text):
            continue
        start_line = node.start_point.row + 1
        end_line = node.end_point.row + 1
        targets.append(
            AnalysisTarget(
                id=f"error_path:{rel}:{start_line}:{node.start_point.column}",
                kind="error_path",
                file_path=rel,
                start_line=start_line,
                end_line=end_line,
                language=language,
                context=_wide_context(lines, start_line, end_line),
                source_line="\n".join(lines[start_line - 1 : end_line]),
                symbol="catch",
                metadata={"construct": "catch_clause"},
            )
        )

    return logs, list({target.id: target for target in targets}.values())


def _walk_tree(node: Node):
    yield node
    for child in node.children:
        yield from _walk_tree(child)


def _node_text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _classify_c_log(statement: str) -> tuple[str, str] | None:
    qt = re.search(r"\b(qDebug|qInfo|qWarning|qCritical|qFatal)\s*\(", statement)
    if qt:
        method = qt.group(1)
        return method, {
            "qDebug": "debug",
            "qInfo": "info",
            "qWarning": "warning",
            "qCritical": "critical",
            "qFatal": "fatal",
        }[method]

    glog = re.search(r"\b(PLOG|DLOG|LOG)(?:_IF)?\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)", statement)
    if glog:
        macro, raw_level = glog.groups()
        level = _normalize_c_level(raw_level)
        if macro == "DLOG" and level == "info":
            level = "debug"
        return macro, level
    if re.search(r"\bVLOG\s*\(", statement):
        return "VLOG", "debug"

    stream = re.search(r"\bstd::(cout|cerr|clog)\b", statement)
    if stream:
        name = stream.group(1)
        return f"std::{name}", "error" if name == "cerr" else "debug" if name == "cout" else "info"

    stdio = re.search(r"\b(f?printf)\s*\((?P<args>.*)", statement, re.DOTALL)
    if stdio:
        callee = stdio.group(1)
        level = "error" if callee == "fprintf" and re.search(r"\bstderr\b", stdio.group("args")) else "debug"
        return callee, level
    return None


def _normalize_c_level(value: str) -> str:
    normalized = value.strip().lower()
    return {
        "warn": "warning",
        "warning": "warning",
        "err": "error",
        "dfatal": "fatal",
    }.get(normalized, normalized if normalized in _LOG_LEVELS else "info")


def _c_message(statement: str) -> str:
    match = re.search(r'"((?:\\.|[^"\\])*)"', statement, re.DOTALL)
    if not match:
        return "<expression>"
    return (
        match.group(1)
        .replace(r"\n", "\n")
        .replace(r"\t", "\t")
        .replace(r'\"', '"')
        .replace(r"\\", "\\")
    )


def _framework_candidate_symbol(node: Node, source: bytes) -> str:
    for child in _walk_tree(node):
        if child.type != "call_expression":
            continue
        function = child.child_by_field_name("function")
        if function is None:
            continue
        symbol = re.sub(r"\s+", "", _node_text(source, function))
        base = symbol.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
        lowered = base.lower()
        if re.search(r"(?:log|trace|debug|warn|error|fatal|report)", lowered) or re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", base):
            return symbol
    return ""


def _looks_like_logging_text(text: str) -> bool:
    return bool(re.search(r"\b(?:log|trace|debug|warn|error|fatal|critical)\w*\s*\(", text, re.IGNORECASE))


def _context(lines: list[str], line_number: int, radius: int = 2) -> str:
    start = max(1, line_number - radius)
    end = min(len(lines), line_number + radius)
    return "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1))


def _wide_context(lines: list[str], start_line: int, end_line: int, radius: int = 3) -> str:
    start = max(1, start_line - radius)
    end = min(len(lines), end_line + radius)
    return "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1))
