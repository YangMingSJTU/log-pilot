from __future__ import annotations

import ast
import re
from pathlib import Path

from .languages import LANGUAGE_BY_SUFFIX, language_for_path
from .models import AnalysisTarget, LogCall, relative_path

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
        raise ValueError("C/C++ files must be parsed through NativeParserClient.")
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
