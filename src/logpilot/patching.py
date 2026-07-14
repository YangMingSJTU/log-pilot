from __future__ import annotations

import difflib
from pathlib import Path

from .models import Issue, LogCall


def write_patch(repo_root: Path, logs: list[LogCall], issues: list[Issue], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    patch = generate_patch(repo_root, logs, issues)
    (output_dir / "changes.diff").write_text(patch, encoding="utf-8")


def generate_patch(repo_root: Path, logs: list[LogCall], issues: list[Issue]) -> str:
    delete_ids = {issue.log_call_id for issue in issues if issue.patch_action == "delete" and issue.log_call_id}
    if not delete_ids:
        return "# No safe automatic patch generated.\n"

    logs_by_id = {log.id: log for log in logs}
    lines_to_delete_by_file: dict[str, set[int]] = {}
    for log_id in delete_ids:
        log = logs_by_id.get(log_id)
        if not log:
            continue
        lines_to_delete_by_file.setdefault(log.file_path, set()).add(log.line)

    chunks: list[str] = []
    for rel_path, line_numbers in sorted(lines_to_delete_by_file.items()):
        file_path = repo_root / rel_path
        if not file_path.exists():
            continue
        original = file_path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
        modified = [line for idx, line in enumerate(original, start=1) if idx not in line_numbers]
        chunks.extend(
            difflib.unified_diff(
                original,
                modified,
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
            )
        )
    return "".join(chunks) if chunks else "# No safe automatic patch generated.\n"
