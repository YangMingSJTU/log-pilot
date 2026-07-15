from __future__ import annotations

import difflib
from pathlib import Path

from .fixes import apply_fix_to_text
from .models import Issue, LogCall


def write_patch(repo_root: Path, logs: list[LogCall], issues: list[Issue], output_dir: Path) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    patch = generate_patch(repo_root, logs, issues)
    (output_dir / "changes.diff").write_text(patch, encoding="utf-8")
    return patch


def generate_patch(repo_root: Path, logs: list[LogCall], issues: list[Issue]) -> str:
    fixes = {issue.fix.id: issue.fix for issue in issues if issue.fix}
    if not fixes:
        return "# No safe automatic patch generated.\n"

    fixes_by_file: dict[str, list] = {}
    for fix in fixes.values():
        fixes_by_file.setdefault(fix.file_path, []).append(fix)

    chunks: list[str] = []
    for rel_path, file_fixes in sorted(fixes_by_file.items()):
        file_path = repo_root / rel_path
        if not file_path.exists():
            continue
        original_text = file_path.read_text(encoding="utf-8", errors="ignore")
        modified_text = original_text
        try:
            for fix in sorted(file_fixes, key=lambda item: (item.start_line, item.end_line), reverse=True):
                modified_text = apply_fix_to_text(modified_text, fix)
        except ValueError:
            continue
        original = original_text.splitlines(keepends=True)
        modified = modified_text.splitlines(keepends=True)
        chunks.extend(
            difflib.unified_diff(
                original,
                modified,
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
            )
        )
    return "".join(chunks) if chunks else "# No safe automatic patch generated.\n"
