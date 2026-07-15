from __future__ import annotations

import ast
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .fixes import apply_fix_to_text, fix_from_dict
from .history import list_history_runs, load_history_run
from .locking import repository_operation_lock
from .models import ApplyRecord, FixProposal, PatchOperation
from .storage import initialize_repository_storage, repository_data_dir


class RemediationError(RuntimeError):
    pass


class ApplyConflictError(RemediationError):
    pass


class ApplyNotFoundError(RemediationError):
    pass


@dataclass(slots=True)
class _PreparedFile:
    operation: PatchOperation
    path: Path
    before: bytes
    after: bytes
    current_line_numbers: list[int]


def applicable_issue_groups(report: dict[str, Any]) -> list[dict[str, Any]]:
    issues = report.get("issues", []) if isinstance(report, dict) else []
    grouped: dict[str, dict[str, Any]] = {}
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get("fix"), dict):
            continue
        fix = issue["fix"]
        fix_id = str(fix.get("id") or "")
        if not fix_id:
            continue
        group = grouped.setdefault(
            fix_id,
            {
                "fix_id": fix_id,
                "action": str(fix.get("action", "")),
                "description": str(fix.get("description", "")),
                "source": str(fix.get("source", "")),
                "file_path": str(fix.get("file_path", "")),
                "line": int(fix.get("start_line", 0)),
                "source_line": str(fix.get("expected_text", "")),
                "issue_ids": [],
                "titles": [],
            },
        )
        group["issue_ids"].append(str(issue.get("id", "")))
        group["titles"].append(str(issue.get("title", "")))
    return list(grouped.values())


def apply_suggestions(repo_root: Path, run_id: str, issue_ids: list[str]) -> dict[str, Any]:
    repo_root = repo_root.expanduser().resolve()
    with repository_operation_lock(repo_root):
        data_dir = initialize_repository_storage(repo_root)
        resolved_run_id = _resolve_run_id(data_dir, run_id)
        try:
            run = load_history_run(data_dir, resolved_run_id)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            raise ApplyNotFoundError(str(exc)) from exc
        known_edits = _known_edits(data_dir, resolved_run_id)
        applied_issue_ids = _active_issue_ids(data_dir, resolved_run_id)
        prepared, affected_issue_ids = _prepare_changes(
            repo_root,
            run["report"],
            issue_ids,
            known_edits,
            applied_issue_ids,
        )
        record = _commit_apply(repo_root, data_dir, resolved_run_id, prepared, affected_issue_ids)
        return record.to_dict()


def rollback_apply(repo_root: Path, apply_id: str | None = None) -> dict[str, Any]:
    repo_root = repo_root.expanduser().resolve()
    with repository_operation_lock(repo_root):
        data_dir = repository_data_dir(repo_root)
        records = list_apply_records(data_dir)
        active = [record for record in records if record.get("status") == "applied"]
        if not active:
            raise ApplyNotFoundError("没有可撤销的采纳记录。")
        latest = active[0]
        if apply_id and latest.get("apply_id") != apply_id:
            raise ApplyConflictError("只能撤销最近一次有效采纳。")
        return _rollback_record(repo_root, data_dir, latest)


def apply_status(repo_root: Path, run_id: str | None = None) -> dict[str, Any]:
    all_records = list_apply_records(repository_data_dir(repo_root))
    active_all = [record for record in all_records if record.get("status") == "applied"]
    records = all_records
    if run_id:
        records = [record for record in records if record.get("run_id") == run_id]
    active = [record for record in records if record.get("status") == "applied"]
    applied_issue_ids = sorted(
        {
            str(issue_id)
            for record in active
            for issue_id in record.get("issue_ids", [])
        }
    )
    return {
        "records": records,
        "applied_issue_ids": applied_issue_ids,
        "latest_apply_id": active_all[0].get("apply_id", "") if active_all else "",
        "can_rollback": bool(active and active_all and active[0].get("apply_id") == active_all[0].get("apply_id")),
    }


def list_apply_records(data_dir: Path) -> list[dict[str, Any]]:
    applies_dir = data_dir / "applies"
    if not applies_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in applies_dir.glob("*/record.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("apply_id"):
            records.append(value)
    return sorted(records, key=lambda item: str(item.get("created_at", "")), reverse=True)


def _resolve_run_id(data_dir: Path, run_id: str) -> str:
    requested = run_id.strip() or "latest"
    if requested != "latest":
        return requested
    runs = list_history_runs(data_dir)
    if not runs:
        raise ApplyNotFoundError("没有可采纳的分析记录，请先执行分析。")
    return str(runs[0]["run_id"])


def _prepare_changes(
    repo_root: Path,
    report: dict[str, Any],
    requested_issue_ids: list[str],
    known_edits: dict[str, list[dict[str, Any]]] | None = None,
    applied_issue_ids: set[str] | None = None,
) -> tuple[list[_PreparedFile], list[str]]:
    requested = {issue_id for issue_id in requested_issue_ids if issue_id}
    if not requested:
        raise RemediationError("请至少选择一项可采纳建议。")

    issues = [issue for issue in report.get("issues", []) if isinstance(issue, dict)]
    issues_by_id = {str(issue.get("id")): issue for issue in issues}
    missing = sorted(requested.difference(issues_by_id))
    if missing:
        raise ApplyNotFoundError(f"分析结果中不存在问题：{', '.join(missing)}")
    already_applied = sorted(requested.intersection(applied_issue_ids or set()))
    if already_applied:
        raise ApplyConflictError(f"问题已经采纳：{', '.join(already_applied)}")

    selected_fixes: dict[str, FixProposal] = {}
    for issue_id in requested:
        issue = issues_by_id[issue_id]
        raw_fix = issue.get("fix")
        if not isinstance(raw_fix, dict):
            raise RemediationError(f"问题不支持自动采纳：{issue_id}")
        fix = fix_from_dict(raw_fix)
        if not fix.id or not fix.file_path or fix.start_line < 1:
            raise RemediationError(f"问题的修复信息无效：{issue_id}")
        selected_fixes[fix.id] = fix

    selected_fix_ids = set(selected_fixes)
    affected_issue_ids = sorted(
        str(issue.get("id"))
        for issue in issues
        if isinstance(issue.get("fix"), dict)
        and str(issue["fix"].get("id", "")) in selected_fix_ids
    )
    operations_by_file: dict[str, PatchOperation] = {}
    for fix in selected_fixes.values():
        operation = operations_by_file.setdefault(fix.file_path, PatchOperation(fix.file_path, [], [], []))
        operation.edits.append(fix)
        operation.line_numbers.append(fix.start_line)
        operation.log_call_ids.extend(fix.log_call_ids)
        operation.issue_ids.extend(fix.issue_ids)
    for operation in operations_by_file.values():
        operation.line_numbers = sorted(set(operation.line_numbers))
        operation.log_call_ids = sorted(set(operation.log_call_ids))
        operation.issue_ids = sorted(set(operation.issue_ids))

    prepared: list[_PreparedFile] = []
    for operation in sorted(operations_by_file.values(), key=lambda item: item.file_path):
        path = _safe_source_path(repo_root, operation.file_path)
        if not path.is_file():
            raise ApplyConflictError(f"目标文件不存在：{operation.file_path}")
        before = path.read_bytes()
        try:
            text = before.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ApplyConflictError(f"目标文件不是 UTF-8 编码：{operation.file_path}") from exc
        lines = text.splitlines(keepends=True)
        previous_edits = (known_edits or {}).get(operation.file_path, [])
        current_line_numbers: list[int] = []
        current_positions: dict[str, int] = {}
        for fix in operation.edits:
            current_line = _validate_snapshot(operation.file_path, lines, fix, previous_edits)
            current_line_numbers.append(current_line)
            current_positions[fix.id] = current_line
        after_text = text
        try:
            for fix in sorted(operation.edits, key=lambda item: current_positions[item.id], reverse=True):
                after_text = apply_fix_to_text(after_text, fix, current_positions[fix.id])
        except ValueError as exc:
            raise ApplyConflictError(f"源码已变化，请重新分析：{operation.file_path}") from exc
        if operation.file_path.endswith(".py") and any(fix.action != "delete" for fix in operation.edits):
            try:
                ast.parse(after_text)
            except SyntaxError as exc:
                raise RemediationError(f"采纳后 Python 语法无效：{operation.file_path}:{exc.lineno}") from exc
        after = after_text.encode("utf-8")
        operation.line_numbers.sort()
        operation.before_sha256 = _sha256(before)
        operation.after_sha256 = _sha256(after)
        prepared.append(_PreparedFile(operation, path, before, after, current_line_numbers))
    return prepared, affected_issue_ids


def _validate_snapshot(
    file_path: str,
    lines: list[str],
    fix: FixProposal,
    known_edits: list[dict[str, Any]],
) -> int:
    line_number = fix.start_line
    current_line_number = _current_line_number(line_number, known_edits)
    current_end = current_line_number + (fix.end_line - fix.start_line)
    if current_line_number < 1 or current_end > len(lines):
        raise ApplyConflictError(f"源码已变化，请重新分析：{file_path}:{line_number}")
    current_text = "\n".join(line.rstrip("\r\n") for line in lines[current_line_number - 1 : current_end])
    if current_text != fix.expected_text:
        raise ApplyConflictError(f"源码已变化，请重新分析：{file_path}:{line_number}")

    for context_line, expected in _parse_context(fix.context).items():
        if _context_was_replaced(context_line, known_edits):
            continue
        current_context_line = _current_line_number(context_line, known_edits)
        if current_context_line < 1 or current_context_line > len(lines):
            raise ApplyConflictError(f"源码上下文已变化，请重新分析：{file_path}:{line_number}")
        if lines[current_context_line - 1].rstrip("\r\n") != expected:
            raise ApplyConflictError(f"源码上下文已变化，请重新分析：{file_path}:{line_number}")
    return current_line_number


def _current_line_number(original_line: int, known_edits: list[dict[str, Any]]) -> int:
    return original_line + sum(
        int(edit.get("line_delta", 0))
        for edit in known_edits
        if int(edit.get("start_line", 0)) < original_line
    )


def _known_edits(data_dir: Path, run_id: str) -> dict[str, list[dict[str, Any]]]:
    known: dict[str, list[dict[str, Any]]] = {}
    for record in list_apply_records(data_dir):
        if record.get("run_id") != run_id or record.get("status") != "applied":
            continue
        for operation in record.get("operations", []):
            if not isinstance(operation, dict):
                continue
            rel_path = str(operation.get("file_path", ""))
            edits = operation.get("edits", [])
            if isinstance(edits, list) and edits:
                known.setdefault(rel_path, []).extend(edit for edit in edits if isinstance(edit, dict))
            else:
                known.setdefault(rel_path, []).extend(
                    {
                        "id": f"legacy-delete:{rel_path}:{line}",
                        "action": "delete",
                        "start_line": int(line),
                        "end_line": int(line),
                        "line_delta": -1,
                    }
                    for line in operation.get("line_numbers", [])
                )
    return known


def _active_issue_ids(data_dir: Path, run_id: str) -> set[str]:
    return {
        str(issue_id)
        for record in list_apply_records(data_dir)
        if record.get("run_id") == run_id and record.get("status") == "applied"
        for issue_id in record.get("issue_ids", [])
    }


def _context_was_replaced(line_number: int, known_edits: list[dict[str, Any]]) -> bool:
    return any(
        str(edit.get("action", "")) in {"delete", "replace"}
        and int(edit.get("start_line", 0)) <= line_number <= int(edit.get("end_line", 0))
        for edit in known_edits
    )


def _parse_context(context: str) -> dict[int, str]:
    parsed: dict[int, str] = {}
    for item in context.splitlines():
        number, separator, source = item.partition(": ")
        if separator and number.isdigit():
            parsed[int(number)] = source
    return parsed


def _safe_source_path(repo_root: Path, rel_path: str) -> Path:
    relative = Path(rel_path)
    if relative.is_absolute():
        raise RemediationError(f"不允许修改仓库外文件：{rel_path}")
    target = (repo_root / relative).resolve()
    try:
        target.relative_to(repo_root)
    except ValueError as exc:
        raise RemediationError(f"不允许修改仓库外文件：{rel_path}") from exc
    return target


def _commit_apply(
    repo_root: Path,
    data_dir: Path,
    run_id: str,
    prepared: list[_PreparedFile],
    issue_ids: list[str],
) -> ApplyRecord:
    apply_id = _new_apply_id()
    apply_dir = data_dir / "applies" / apply_id
    backup_dir = apply_dir / "files"
    backup_dir.mkdir(parents=True, exist_ok=False)
    for index, item in enumerate(prepared, start=1):
        backup_name = f"{index:04d}.bin"
        (backup_dir / backup_name).write_bytes(item.before)
        item.operation.backup_file = f"files/{backup_name}"

    record = ApplyRecord(
        apply_id=apply_id,
        run_id=run_id,
        repository=str(repo_root),
        created_at=datetime.now().astimezone().isoformat(timespec="microseconds"),
        status="preparing",
        issue_ids=issue_ids,
        operations=[item.operation for item in prepared],
    )
    _write_record(apply_dir, record.to_dict())

    written: list[_PreparedFile] = []
    try:
        for item in prepared:
            _atomic_write(item.path, item.after)
            written.append(item)
    except Exception as exc:
        restore_errors: list[str] = []
        for item in reversed(written):
            try:
                _atomic_write(item.path, item.before)
            except Exception as restore_exc:
                restore_errors.append(f"{item.operation.file_path}: {restore_exc}")
        failed = record.to_dict()
        failed["status"] = "failed"
        failed["error"] = str(exc)
        failed["restore_errors"] = restore_errors
        _write_record(apply_dir, failed)
        if restore_errors:
            raise RemediationError(
                f"采纳失败且部分源码未能自动恢复，请使用事务备份：{apply_dir}"
            ) from exc
        raise RemediationError(f"采纳失败，源码已恢复：{exc}") from exc

    record.status = "applied"
    _write_record(apply_dir, record.to_dict())
    return record


def _rollback_record(repo_root: Path, data_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    apply_id = str(record["apply_id"])
    apply_dir = data_dir / "applies" / apply_id
    prepared: list[tuple[Path, bytes, bytes]] = []
    for operation in record.get("operations", []):
        if not isinstance(operation, dict):
            continue
        path = _safe_source_path(repo_root, str(operation.get("file_path", "")))
        current = path.read_bytes() if path.exists() else b""
        if _sha256(current) != operation.get("after_sha256"):
            raise ApplyConflictError(f"源码在采纳后已变化，无法自动撤销：{operation.get('file_path', '')}")
        backup_path = apply_dir / str(operation.get("backup_file", ""))
        if not backup_path.is_file():
            raise ApplyNotFoundError(f"采纳备份不存在：{operation.get('file_path', '')}")
        prepared.append((path, current, backup_path.read_bytes()))

    restored: list[tuple[Path, bytes, bytes]] = []
    try:
        for item in prepared:
            _atomic_write(item[0], item[2])
            restored.append(item)
    except Exception as exc:
        recovery_errors: list[str] = []
        for path, current, _backup in reversed(restored):
            try:
                _atomic_write(path, current)
            except Exception as recovery_exc:
                recovery_errors.append(f"{path}: {recovery_exc}")
        if recovery_errors:
            raise RemediationError(
                f"撤销失败且部分源码未能恢复到撤销前状态，请检查事务备份：{apply_dir}"
            ) from exc
        raise RemediationError(f"撤销失败，源码已恢复到撤销前状态：{exc}") from exc

    record["status"] = "rolled_back"
    record["rolled_back_at"] = datetime.now().astimezone().isoformat(timespec="microseconds")
    _write_record(apply_dir, record)
    return record


def _atomic_write(path: Path, content: bytes) -> None:
    original_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.logpilot-", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if original_mode is not None:
            os.chmod(temp_path, original_mode)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _write_record(apply_dir: Path, payload: dict[str, Any]) -> None:
    (apply_dir / "record.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _new_apply_id() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f")
