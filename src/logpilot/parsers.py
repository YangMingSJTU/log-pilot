from __future__ import annotations

import ast
import re
from pathlib import Path

from tree_sitter import Language, Node, Parser
import tree_sitter_c
import tree_sitter_cpp

from .languages import LANGUAGE_BY_SUFFIX, language_for_path
from .models import AnalysisTarget, LogCall, relative_path

_C_PARSER = Parser(Language(tree_sitter_c.language()))
_CPP_PARSER = Parser(Language(tree_sitter_cpp.language()))

LOG_METHODS = {
    "debug",
    "info",
    "warning",
    "warn",
    "error",
    "exception",
    "critical",
    "fatal",
    "trace",
}

CALL_PATTERN = re.compile(
    r"(?P<callee>\b(?:console|logger|log|logging|System\.out)\s*\.\s*"
    r"(?P<method>log|debug|info|warn|warning|error|exception|critical|fatal|println)\s*)"
    r"\((?P<args>.*)\)"
)


def parse_file(path: Path, repo_root: Path, language: str) -> list[LogCall]:
    logs, _targets = parse_file_with_targets(path, repo_root, language)
    return logs


def parse_file_with_targets(
    path: Path,
    repo_root: Path,
    language: str,
) -> tuple[list[LogCall], list[AnalysisTarget]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if language == "python":
        return _parse_python(path, repo_root, text), []
    if language in {"c", "cpp"}:
        return _parse_c_family(path, repo_root, language, text)
    return _parse_text_language(path, repo_root, language, text), []


def _parse_python(path: Path, repo_root: Path, text: str) -> list[LogCall]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _parse_text_language(path, repo_root, "python", text)

    lines = text.splitlines()
    calls: list[LogCall] = []
    rel = relative_path(path, repo_root)
    standalone_calls = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
    }

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee, level = _python_call_name(node.func)
        if not callee:
            continue
        message = _python_message(node)
        source_line = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""
        end_line = int(getattr(node, "end_lineno", node.lineno) or node.lineno)
        source_segment = ast.get_source_segment(text, node) or ""
        safe_to_delete = (
            id(node) in standalone_calls
            and end_line == node.lineno
            and source_line.strip() == source_segment.strip()
        )
        calls.append(
            LogCall(
                id=f"{rel}:{node.lineno}:{node.col_offset}",
                file_path=rel,
                line=node.lineno,
                column=node.col_offset,
                language="python",
                level=level,
                callee=callee,
                message=message,
                context=_context(lines, node.lineno),
                source_line=source_line,
                end_line=end_line,
                safe_to_delete=safe_to_delete,
            )
        )
    return calls


def _python_call_name(func: ast.expr) -> tuple[str, str]:
    if isinstance(func, ast.Name) and func.id == "print":
        return "print", "debug"
    if isinstance(func, ast.Attribute) and func.attr in LOG_METHODS:
        root = _attribute_root(func.value)
        if root in {"logger", "log", "logging"} or root.endswith(".logger") or root.endswith(".log"):
            return f"{root}.{func.attr}" if root else func.attr, _normalize_level(func.attr)
    return "", ""


def _attribute_root(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _attribute_root(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _python_message(node: ast.Call) -> str:
    if not node.args:
        return ""
    first = node.args[0]
    if isinstance(first, ast.Constant):
        return str(first.value)
    if isinstance(first, ast.JoinedStr):
        return "<f-string>"
    if isinstance(first, ast.Name):
        return f"<{first.id}>"
    if isinstance(first, ast.Attribute):
        return f"<{_attribute_root(first)}>"
    return ast.unparse(first) if hasattr(ast, "unparse") else "<expression>"


def _parse_text_language(path: Path, repo_root: Path, language: str, text: str) -> list[LogCall]:
    lines = text.splitlines()
    rel = relative_path(path, repo_root)
    calls: list[LogCall] = []
    for index, line in enumerate(lines, start=1):
        match = CALL_PATTERN.search(line)
        if not match:
            continue
        callee = re.sub(r"\s+", "", match.group("callee"))
        method = match.group("method")
        level = _normalize_level(method)
        calls.append(
            LogCall(
                id=f"{rel}:{index}:{match.start()}",
                file_path=rel,
                line=index,
                column=match.start(),
                language=language,
                level=level,
                callee=callee,
                message=_first_argument(match.group("args")),
                context=_context(lines, index),
                source_line=line,
                end_line=index,
                safe_to_delete=bool(CALL_PATTERN.fullmatch(line.strip().removesuffix(";").strip())),
            )
        )
    return calls


def _parse_c_family(
    path: Path,
    repo_root: Path,
    language: str,
    text: str,
) -> tuple[list[LogCall], list[AnalysisTarget]]:
    source = text.encode("utf-8")
    parser = _C_PARSER if language == "c" else _CPP_PARSER
    tree = parser.parse(source)
    lines = text.splitlines()
    rel = relative_path(path, repo_root)
    logs: list[LogCall] = []
    targets: list[AnalysisTarget] = []
    logged_ranges: set[tuple[int, int]] = set()

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
            safe_to_delete = statement.strip() == source_line.strip()
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
                    safe_to_delete=safe_to_delete,
                )
            )
            logged_ranges.add((node.start_byte, node.end_byte))
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

    return logs, _deduplicate_targets(targets)


def promote_framework_candidates(
    targets: list[AnalysisTarget],
    definitions: dict[str, dict[str, object]],
) -> list[LogCall]:
    promoted: list[LogCall] = []
    for target in targets:
        if target.kind != "framework_candidate":
            continue
        definition = definitions.get(target.id)
        if not definition or not definition.get("is_logging_api"):
            continue
        if str(definition.get("callee", "")) != target.symbol:
            continue
        try:
            confidence = float(definition.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0
        if confidence < 0.8:
            continue
        level = _normalize_level(str(definition.get("level", "info")))
        promoted.append(
            LogCall(
                id=f"{target.file_path}:{target.start_line}:ai:{target.symbol}",
                file_path=target.file_path,
                line=target.start_line,
                column=0,
                language=target.language,
                level=level if level in LOG_METHODS else "info",
                callee=target.symbol,
                message=_c_message(target.source_line),
                context=target.context,
                source_line=target.source_line,
                end_line=target.end_line,
                safe_to_delete=False,
            )
        )
    return promoted


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
    }.get(normalized, normalized if normalized in LOG_METHODS else "info")


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


def _wide_context(lines: list[str], start_line: int, end_line: int, radius: int = 3) -> str:
    start = max(1, start_line - radius)
    end = min(len(lines), end_line + radius)
    return "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1))


def _deduplicate_targets(targets: list[AnalysisTarget]) -> list[AnalysisTarget]:
    return list({target.id: target for target in targets}.values())


def _first_argument(args: str) -> str:
    if not args.strip():
        return ""
    result: list[str] = []
    quote: str | None = None
    depth = 0
    for char in args:
        if quote:
            result.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
            result.append(char)
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            break
        result.append(char)
    return "".join(result).strip().strip('"').strip("'").strip("`")


def _normalize_level(level: str) -> str:
    normalized = level.lower()
    if normalized in {"warn", "warning"}:
        return "warning"
    if normalized in {"println", "log", "print"}:
        return "debug"
    return normalized


def _context(lines: list[str], line_number: int, radius: int = 2) -> str:
    start = max(1, line_number - radius)
    end = min(len(lines), line_number + radius)
    return "\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1))
